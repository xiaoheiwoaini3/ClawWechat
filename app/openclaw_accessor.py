"""OpenClaw 数据访问层（只读 + 角色 md 读写）。

不依赖 OpenClaw CLI 命令，直接读 ~/.openclaw 下的文件：
- openclaw.json：agents / bindings / providers 配置
- openclaw-weixin/accounts.json：账号列表
- openclaw-weixin/accounts/<id>.json：账号详情（token / userId / savedAt）
- workspace/SOUL.md 等角色文件
- agents/<id>/agent/openclaw-agent.sqlite：会话 + 消息历史

所有读 sqlite 的地方都用 URI 只读模式（mode=ro&immutable=1），
绕过 OpenClaw gateway 持有的 WAL 锁。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# OpenClaw 根目录：默认 ~/.openclaw，可被 OPENCLAW_STATE_DIR 覆盖
import os

_OPENCLAW_HOME = Path(
    os.environ.get("OPENCLAW_STATE_DIR")
    or Path.home() / ".openclaw"
)


# ============================================================
# 数据类
# ============================================================
@dataclass
class AgentInfo:
    """OpenClaw Agent 信息（来自 openclaw.json）。"""

    agent_id: str
    name: str
    workspace: str
    agent_dir: str | None = None
    model: str | None = None
    bound_account_ids: list[str] = field(default_factory=list)
    soul_md: str | None = None  # SOUL.md 内容（懒加载）

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "name": self.name,
            "workspace": self.workspace,
            "agent_dir": self.agent_dir,
            "model": self.model,
            "bound_account_ids": self.bound_account_ids,
        }


@dataclass
class WeixinAccount:
    """微信账号信息（来自 accounts/<id>.json + 备注 meta）。"""

    account_id: str
    token: str
    user_id: str  # 微信号本身（xxx@im.wechat）
    saved_at: str
    base_url: str
    note: str = ""  # 用户备注（如「我的主号」），存 accounts/<id>.meta.json

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "user_id": self.user_id,
            "saved_at": self.saved_at,
            "base_url": self.base_url,
            # 不返回 token（敏感）
        }


@dataclass
class ConversationInfo:
    """会话信息（来自 conversations 表）。"""

    conversation_id: str
    account_id: str
    peer_id: str  # 对方微信号
    kind: str  # direct / group
    label: str | None
    created_at: int  # ms
    updated_at: int  # ms
    session_id: str | None = None  # 关联 session_windows.session_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "account_id": self.account_id,
            "peer_id": self.peer_id,
            "kind": self.kind,
            "label": self.label,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "session_id": self.session_id,
        }


@dataclass
class MessageInfo:
    """单条消息（来自 session_transcript_fts_content 表）。"""

    message_id: str
    role: str  # user / assistant
    content: str
    timestamp: float  # ms epoch

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "role": self.role,
            "content": self.content,
            "timestamp": self.timestamp,
        }


# ============================================================
# 路径辅助
# ============================================================
def _config_path() -> Path:
    return _OPENCLAW_HOME / "openclaw.json"


def _accounts_index_path() -> Path:
    return _OPENCLAW_HOME / "openclaw-weixin" / "accounts.json"


def _account_detail_path(account_id: str) -> Path:
    return _OPENCLAW_HOME / "openclaw-weixin" / "accounts" / f"{account_id}.json"


def _account_meta_path(account_id: str) -> Path:
    """账号备注 meta 文件（用户自定义，OpenClaw 不会覆盖）。"""
    return _OPENCLAW_HOME / "openclaw-weixin" / "accounts" / f"{account_id}.meta.json"


def read_account_note(account_id: str) -> str:
    p = _account_meta_path(account_id)
    if not p.exists():
        return ""
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("note", "")
    except Exception:
        return ""


def write_account_note(account_id: str, note: str) -> None:
    p = _account_meta_path(account_id)
    if note.strip():
        p.write_text(
            json.dumps({"note": note.strip()}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    elif p.exists():
        p.unlink()  # 清空备注则删 meta


def _agent_db_path(agent_id: str) -> Path:
    return _OPENCLAW_HOME / "agents" / agent_id / "agent" / "openclaw-agent.sqlite"


def _workspace_soul_path(workspace: str) -> Path:
    return Path(workspace) / "SOUL.md"


# ============================================================
# 读 openclaw.json
# ============================================================
def read_config() -> dict[str, Any]:
    """读 ~/.openclaw/openclaw.json。文件不存在返回空 dict。"""
    p = _config_path()
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def write_config(cfg: dict[str, Any]) -> None:
    """写回 openclaw.json（备份原文件）。

    用 shutil.copy2 备份 + 直接覆盖写，避免 Windows 上 rename 失败
    （gateway 进程持有文件句柄时 PermissionError）。
    """
    import shutil

    p = _config_path()
    if p.exists():
        backup = p.with_suffix(".json.bak.before-our-edit")
        try:
            shutil.copy2(p, backup)  # 复制备份，不移动原文件
        except Exception:
            # 备份失败不阻断主流程
            pass
    # 直接覆盖写入（OpenClaw gateway 会 watch 文件变化重新加载）
    p.write_text(
        json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8"
    )


# ============================================================
# Agent 列表 + 绑定关系
# ============================================================
def list_agents() -> list[AgentInfo]:
    """列出所有 Agent 及其绑定的微信号 accountId。

    数据源：
    - agents.entries：Agent 元数据（workspace / agentDir / name / model）
    - bindings：accountId → agentId 路由规则
    - agents.defaults.model：默认模型
    """
    cfg = read_config()
    agents_entries = cfg.get("agents", {}).get("entries", {})
    bindings = cfg.get("bindings", [])
    default_model = cfg.get("agents", {}).get("defaults", {}).get("model")

    # 把 bindings 按 agentId 分组
    bound: dict[str, list[str]] = {}
    for b in bindings:
        agent_id = b.get("agentId")
        match = b.get("match", {})
        account_id = match.get("accountId")
        # match.accountId == "*" 表示通配（main agent 通常这样），不算具体绑定
        if agent_id and account_id and account_id != "*":
            bound.setdefault(agent_id, []).append(account_id)

    result: list[AgentInfo] = []
    for agent_id, entry in agents_entries.items():
        result.append(
            AgentInfo(
                agent_id=agent_id,
                name=entry.get("name") or agent_id,
                workspace=entry.get("workspace", ""),
                agent_dir=entry.get("agentDir"),
                model=entry.get("model") or default_model,
                bound_account_ids=bound.get(agent_id, []),
            )
        )
    return result


def get_agent(agent_id: str) -> AgentInfo | None:
    for a in list_agents():
        if a.agent_id == agent_id:
            return a
    return None


def find_agent_by_account(account_id: str) -> AgentInfo | None:
    """根据微信 accountId 反查绑定的 Agent。"""
    for a in list_agents():
        if account_id in a.bound_account_ids:
            return a
    return None


# ============================================================
# 微信账号
# ============================================================
def list_weixin_accounts() -> list[WeixinAccount]:
    """列出所有已扫码绑定的微信账号。"""
    idx_path = _accounts_index_path()
    if not idx_path.exists():
        return []
    account_ids = json.loads(idx_path.read_text(encoding="utf-8"))
    result: list[WeixinAccount] = []
    for aid in account_ids:
        detail_path = _account_detail_path(aid)
        if not detail_path.exists():
            continue
        try:
            d = json.loads(detail_path.read_text(encoding="utf-8"))
            result.append(
                WeixinAccount(
                    account_id=aid,
                    token=d.get("token", ""),
                    user_id=d.get("userId", ""),
                    saved_at=d.get("savedAt", ""),
                    base_url=d.get("baseUrl", ""),
                    note=read_account_note(aid),
                )
            )
        except Exception:
            continue
    return result


def get_weixin_account(account_id: str) -> WeixinAccount | None:
    for a in list_weixin_accounts():
        if a.account_id == account_id:
            return a
    return None


# ============================================================
# Agent 角色（SOUL.md / AGENTS.md）
# ============================================================
def read_soul_md(agent_id: str) -> str | None:
    """读 Agent 的 SOUL.md 内容。"""
    agent = get_agent(agent_id)
    if not agent:
        return None
    p = _workspace_soul_path(agent.workspace)
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8")


def write_soul_md(agent_id: str, content: str) -> None:
    """写 Agent 的 SOUL.md。workspace 目录不存在则创建。"""
    agent = get_agent(agent_id)
    if not agent:
        raise ValueError(f"Agent not found: {agent_id}")
    p = _workspace_soul_path(agent.workspace)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def read_workspace_file(agent_id: str, filename: str) -> str | None:
    """读 Agent workspace 下任意文件。"""
    agent = get_agent(agent_id)
    if not agent:
        return None
    p = Path(agent.workspace) / filename
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8")


def write_workspace_file(agent_id: str, filename: str, content: str) -> None:
    """写 Agent workspace 下任意文件。"""
    agent = get_agent(agent_id)
    if not agent:
        raise ValueError(f"Agent not found: {agent_id}")
    p = Path(agent.workspace) / filename
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


# ============================================================
# 会话 + 消息历史（读 sqlite）
# ============================================================
def _open_agent_db_ro(agent_id: str) -> sqlite3.Connection:
    """用只读模式打开 Agent sqlite（不阻塞 gateway 写）。

    注意：不能用 immutable=1——网关运行时会持续把新消息写入 -wal，
    immutable 假设主文件不变、不读 WAL，导致消息只有网关重启后才可见。
    mode=ro 只读连接可正常读取 WAL 中的最新数据（SQLite WAL 支持并发读）。
    """
    p = _agent_db_path(agent_id)
    if not p.exists():
        raise FileNotFoundError(f"Agent sqlite not found: {p}")
    uri = f"file:{p.as_posix()}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def list_conversations(agent_id: str, account_id: str | None = None) -> list[ConversationInfo]:
    """列出 Agent 下的所有会话（按 update 时间倒序）。

    可选 account_id 过滤：只显示某微信号收到的会话。
    """
    conn = _open_agent_db_ro(agent_id)
    try:
        cur = conn.cursor()
        if account_id:
            cur.execute(
                """
                SELECT conversation_id, account_id, peer_id, kind, label,
                       created_at, updated_at
                FROM conversations
                WHERE account_id = ?
                ORDER BY updated_at DESC
                """,
                (account_id,),
            )
        else:
            cur.execute(
                """
                SELECT conversation_id, account_id, peer_id, kind, label,
                       created_at, updated_at
                FROM conversations
                ORDER BY updated_at DESC
                """,
            )
        rows = cur.fetchall()

        # 关联 session_id（从 session_conversations + session_windows 反查）
        # session_windows.session_key 格式如 "agent:main:main"，但 conversations 表没有 session_key
        # 这里简单处理：通过 session_conversations.conversation_id 找 session_id
        result: list[ConversationInfo] = []
        for r in rows:
            conv_id, acc_id, peer_id, kind, label, created, updated = r
            session_id = _find_session_id_for_conversation(conn, conv_id)
            result.append(
                ConversationInfo(
                    conversation_id=conv_id,
                    account_id=acc_id,
                    peer_id=peer_id,
                    kind=kind,
                    label=label,
                    created_at=created,
                    updated_at=updated,
                    session_id=session_id,
                )
            )
        return result
    finally:
        conn.close()


def _find_session_id_for_conversation(conn: sqlite3.Connection, conversation_id: str) -> str | None:
    """从 session_conversations 反查 session_id。"""
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT session_id FROM session_conversations WHERE conversation_id = ? LIMIT 1",
            (conversation_id,),
        )
        r = cur.fetchone()
        return r[0] if r else None
    except Exception:
        return None


def list_messages(agent_id: str, conversation_id: str, limit: int = 100) -> list[MessageInfo]:
    """列出某会话的消息历史。

    用 session_transcript_fts_content 表（已建好的全文索引），
    按 timestamp 升序返回。需要先经 conversation_id → session_id 反查。

    若 conversation_id 查不到 session_id，回退查 transcript_events 表。
    """
    conn = _open_agent_db_ro(agent_id)
    try:
        cur = conn.cursor()

        # 1) 查 conversation_id 对应的 session_id
        cur.execute(
            "SELECT session_id FROM session_conversations WHERE conversation_id = ? LIMIT 1",
            (conversation_id,),
        )
        r = cur.fetchone()
        session_id = r[0] if r else None

        if session_id:
            # 2) 从 session_transcript_fts_content 表查消息
            # FTS5 表列：text, session_id, message_id, role, timestamp
            # content 表自动映射：c0=text, c1=session_id, c2=message_id, c3=role, c4=timestamp
            cur.execute(
                """
                SELECT c2, c3, c0, c4
                FROM session_transcript_fts_content
                WHERE c1 = ?
                ORDER BY c4
                LIMIT ?
                """,
                (session_id, limit),
            )
            rows = cur.fetchall()
            return [
                MessageInfo(
                    message_id=row[0] or "",
                    role=row[1] or "user",
                    content=row[2] or "",
                    timestamp=float(row[3] or 0),
                )
                for row in rows
                if row[2]  # 过滤掉空内容
            ]

        # 3) 回退：从 transcript_events 查（解析 event_json）
        return _list_messages_from_transcript_events(conn, conversation_id, limit)
    finally:
        conn.close()


def _list_messages_from_transcript_events(
    conn: sqlite3.Connection, conversation_id: str, limit: int
) -> list[MessageInfo]:
    """回退方案：从 transcript_events 表解析 event_json 拿消息。

    conversation_id 不是 transcript_events 的直接关联键，可能要靠 session_id。
    这里简单地按 session_id 查，找不到就返回空。
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT session_id FROM session_conversations WHERE conversation_id = ? LIMIT 1",
        (conversation_id,),
    )
    r = cur.fetchone()
    if not r:
        return []
    session_id = r[0]

    cur.execute(
        """
        SELECT event_json
        FROM transcript_events
        WHERE session_id = ?
        ORDER BY seq
        """,
        (session_id,),
    )
    result: list[MessageInfo] = []
    for (event_json,) in cur.fetchall():
        try:
            ev = json.loads(event_json)
        except Exception:
            continue
        if ev.get("type") != "message":
            continue
        msg = ev.get("message", {})
        role = msg.get("role", "user")
        content = msg.get("content", "")
        # content 可能是 string 或 list[dict]（多模态）
        if isinstance(content, list):
            content = " ".join(
                c.get("text", "") for c in content if isinstance(c, dict)
            )
        ts = ev.get("timestamp") or msg.get("timestamp") or 0
        try:
            ts_float = float(ts)
        except (TypeError, ValueError):
            ts_float = 0.0
        result.append(
            MessageInfo(
                message_id=ev.get("id", ""),
                role=role,
                content=str(content),
                timestamp=ts_float,
            )
        )
        if len(result) >= limit:
            break
    return result


# ============================================================
# 验证用（python -m app.openclaw_accessor）
# ============================================================
if __name__ == "__main__":
    print("=== Agents ===")
    for a in list_agents():
        print(a)
    print("\n=== Weixin accounts ===")
    for acc in list_weixin_accounts():
        print(acc)
    print("\n=== main SOUL.md ===")
    print(read_soul_md("main"))
    print("\n=== main conversations ===")
    for c in list_conversations("main"):
        print(c)
    print("\n=== messages of first conversation ===")
    convs = list_conversations("main")
    if convs:
        for m in list_messages("main", convs[0].conversation_id, limit=10):
            print(f"[{m.role}] {m.content[:80]}")
