"""Agent / 微信账号管理接口（基于 OpenClaw）。

新架构：iLink 协议由 OpenClaw 网关接管，本接口只做：
- 列出所有 Agent + 绑定的微信账号（读 openclaw.json + accounts.json）
- 扫码登录新微信（subprocess 调 `openclaw channels login`）
- 创建 Agent + 绑定 + 重启网关（subprocess 调 `openclaw agents add/bind` + `gateway restart`）

不再操作数据库 Bot 表（OpenClaw accounts.json 就是 source of truth）。
不再有 bot_manager（OpenClaw 自己管长轮询）。
"""
import asyncio
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from app import openclaw_accessor, openclaw_cli

logger = logging.getLogger(__name__)

router = APIRouter()


# ============ 响应模型 ============
class WeixinAccountOut(BaseModel):
    """微信账号信息（脱敏，不含 token）。"""
    account_id: str
    user_id: str  # 微信号本身（xxx@im.wechat）
    saved_at: str
    note: str = ""  # 用户备注


class AgentOut(BaseModel):
    """OpenClaw Agent + 绑定的微信账号。"""
    agent_id: str
    name: str
    workspace: str
    model: Optional[str] = None
    bound_accounts: list[WeixinAccountOut] = []
    soul_md: Optional[str] = None  # 完整 SOUL.md 内容（按需加载）


class QrcodeOut(BaseModel):
    """扫码响应：二维码 data URL + 流程说明。"""
    qr_data: str  # data:image/png;base64,... 或 https://...
    message: str = "请用微信扫码"


class BindIn(BaseModel):
    """扫码成功后绑定请求。"""
    agent_id: str  # 创建/绑定的 Agent ID
    workspace: Optional[str] = None  # Agent workspace 路径，不传用默认 ~/agent_id
    name: Optional[str] = None  # Agent 显示名
    account_id: Optional[str] = None  # 微信 accountId（扫码后拿到）


# ============ 列表 ============
@router.get("/", response_model=list[AgentOut])
def list_agents():
    """列出所有 Agent 及绑定的微信账号。

    数据源：
    - openclaw_accessor.list_agents() 读 openclaw.json
    - openclaw_accessor.list_weixin_accounts() 读 accounts.json
    不查 SOUL.md（前端按需单独请求 /{agent_id}/soul）。
    """
    agents = openclaw_accessor.list_agents()
    accounts = {a.account_id: a for a in openclaw_accessor.list_weixin_accounts()}

    result: list[AgentOut] = []
    for a in agents:
        bound: list[WeixinAccountOut] = []
        for acc_id in a.bound_account_ids:
            acc = accounts.get(acc_id)
            if acc:
                bound.append(
                    WeixinAccountOut(
                        account_id=acc.account_id,
                        user_id=acc.user_id,
                        saved_at=acc.saved_at,
                        note=acc.note,
                    )
                )
        result.append(
            AgentOut(
                agent_id=a.agent_id,
                name=a.name,
                workspace=a.workspace,
                model=a.model,
                bound_accounts=bound,
            )
        )
    return result


class AccountOut(BaseModel):
    """微信账号（含未绑定），bound_agent_id 为 null 表示未绑定到任何 Agent。"""
    account_id: str
    user_id: str
    saved_at: str
    note: str = ""
    bound_agent_id: Optional[str] = None


@router.get("/accounts", response_model=list[AccountOut])
def list_accounts():
    """列出所有微信账号（含未绑定）。

    说明：GET /api/bots/ 只返回「已绑定」账号（读 bindings 配置，
    accountId='*' 通配被排除），而新扫码账号只写入 accounts.json、
    尚未产生 binding，前端无法用它检测新增。前端扫码后应轮询本接口。

    路由顺序注意：本接口必须定义在 /{agent_id} 之前，
    否则 "accounts" 会被当作 agent_id 匹配（与 qrcode-status 同理）。
    """
    result: list[AccountOut] = []
    for acc in openclaw_accessor.list_weixin_accounts():
        agent = openclaw_accessor.find_agent_by_account(acc.account_id)
        result.append(
            AccountOut(
                account_id=acc.account_id,
                user_id=acc.user_id,
                saved_at=acc.saved_at,
                note=acc.note,
                bound_agent_id=agent.agent_id if agent else None,
            )
        )
    return result


@router.put("/accounts/{account_id}/note")
def set_account_note(account_id: str, payload: dict):
    """给微信账号设置备注（如「我的主号」），方便在界面上识别。"""
    acc = openclaw_accessor.get_weixin_account(account_id)
    if not acc:
        raise HTTPException(404, f"账号不存在: {account_id}")
    note = (payload or {}).get("note", "")
    if not isinstance(note, str):
        raise HTTPException(400, "note 必须是字符串")
    openclaw_accessor.write_account_note(account_id, note)
    return {"account_id": account_id, "note": note.strip()}


@router.get("/qrcode-status")
async def poll_qr_status():
    """占位接口：新架构下扫码状态由 OpenClaw 自己管，前端无需轮询。

    扫码成功后 OpenClaw 自动写 accounts.json，前端用 GET /api/bots/accounts
    拉最新账号列表即可。保留此接口避免前端报 404。
    注意：必须定义在 /{agent_id} 之前，否则会被当作 agent_id 匹配。
    """
    return {"status": "managed_by_openclaw", "message": "扫码状态由 OpenClaw 处理，请直接刷新列表"}


@router.get("/qrcode-current")
async def get_qrcode_current():
    """获取当前最新二维码。

    OpenClaw 的微信二维码约 1-2 分钟过期并自动刷新（刷新 3 次后放弃）。
    前端轮询本接口拿到最新二维码，避免用户扫到已失效的旧码（微信会报网络错误）。
    必须定义在 /{agent_id} 之前。
    """
    qr = openclaw_cli._qr_login_proc.get("current_qr_data")
    return {"qr_data": qr or None}


@router.get("/{agent_id}", response_model=AgentOut)
def get_agent_detail(agent_id: str):
    """获取单个 Agent 详情（含 SOUL.md 内容）。"""
    a = openclaw_accessor.get_agent(agent_id)
    if not a:
        raise HTTPException(404, f"Agent not found: {agent_id}")

    accounts = {acc.account_id: acc for acc in openclaw_accessor.list_weixin_accounts()}
    bound: list[WeixinAccountOut] = []
    for acc_id in a.bound_account_ids:
        acc = accounts.get(acc_id)
        if acc:
            bound.append(
                WeixinAccountOut(
                    account_id=acc.account_id,
                    user_id=acc.user_id,
                    saved_at=acc.saved_at,
                    note=acc.note,
                )
            )

    return AgentOut(
        agent_id=a.agent_id,
        name=a.name,
        workspace=a.workspace,
        model=a.model,
        bound_accounts=bound,
        soul_md=openclaw_accessor.read_soul_md(agent_id) or "",
    )


# ============ 扫码登录 ============
@router.post("/qrcode", response_model=QrcodeOut)
async def start_qr_login():
    """启动扫码登录流程。

    流程：
    1. 先停 gateway（释放 state sqlite 锁，否则 CLI 报 disk I/O error）
    2. 启动 `openclaw channels login` 子进程，等二维码出现在 stdout
    3. 返回二维码，子进程继续在后台跑等扫码
    4. 扫码成功后 OpenClaw CLI 自己写 accounts.json
    5. 前端轮询 GET / 检测新 accountId，然后 POST /bind
    6. /bind 完成后重启 gateway 恢复服务

    若 60s 内未出二维码，返回 504。
    并发保护：同时只允许一个扫码流程。
    """
    # 并发锁：防止用户多次快速点击
    if openclaw_cli._qr_login_proc.get("in_progress"):
        # 幂等：已有活跃扫码子进程且二维码有效 → 直接复用（打开弹窗预热后秒回）
        proc = openclaw_cli._qr_login_proc.get("proc")
        cur = openclaw_cli._qr_login_proc.get("current_qr_data")
        if proc is not None and proc.returncode is None and cur:
            return QrcodeOut(qr_data=cur, message="二维码已就绪，请用微信扫码")
        raise HTTPException(409, "扫码流程正在进行中，请等待完成或先取消")
    openclaw_cli._qr_login_proc["in_progress"] = True

    gateway_running = False
    # 先杀掉之前可能残留的扫码进程
    openclaw_cli.kill_login_proc()

    # 检查 gateway 状态，跑着就先停（超时 5s 快速失败）
    try:
        status_r = await openclaw_cli._run_openclaw(["gateway", "status"], timeout=5.0)
        gateway_running = status_r[0] == 0 and "running" in status_r[1].lower()
    except Exception:
        gateway_running = False
    openclaw_cli._qr_login_proc["gateway_was_running"] = gateway_running

    if gateway_running:
        logger.info("Stopping gateway before QR login (release sqlite lock)")
        r = await openclaw_cli.stop_gateway(timeout=15.0)
        if not r["ok"]:
            logger.warning("gateway stop rc=%s: %s", r["rc"], r.get("stderr", ""))
        await asyncio.sleep(2)

    try:
        result = await openclaw_cli.start_qr_login_bg(qr_timeout=60.0)
    except Exception as e:
        logger.exception("qr_login failed")
        openclaw_cli._qr_login_proc["in_progress"] = False
        if gateway_running:
            await openclaw_cli.start_gateway(timeout=30.0)
        raise HTTPException(500, f"扫码启动异常：{e}")

    if result.error or not result.qr_data:
        # 失败：清 flag + 恢复 gateway
        openclaw_cli._qr_login_proc["in_progress"] = False
        if gateway_running:
            logger.info("Restoring gateway after QR login error")
            await openclaw_cli.start_gateway(timeout=30.0)
        if result.error:
            raise HTTPException(502, result.error)
        raise HTTPException(504, "60s 内未捕获到二维码输出")

    # 成功：保存子进程 + 保持 in_progress=True（扫码中），返回 QR
    # in_progress 会在 /bind 或 /qrcode-cancel 时清除
    openclaw_cli._qr_login_proc["proc"] = result.proc
    return QrcodeOut(qr_data=result.qr_data, message=result.message or "请用微信扫码")


@router.post("/qrcode-cancel")
async def cancel_qr_login():
    """取消扫码：杀掉登录子进程 + 恢复 gateway。

    前端关闭扫码弹窗时调本接口，避免 gateway 一直处于停止状态。
    """
    openclaw_cli.kill_login_proc()
    openclaw_cli._qr_login_proc["in_progress"] = False
    was_running = openclaw_cli._qr_login_proc.get("gateway_was_running", False)
    if was_running:
        logger.info("Restoring gateway after QR cancel")
        r = await openclaw_cli.start_gateway(timeout=30.0)
        if not r["ok"]:
            logger.warning("gateway restore failed: %s", r.get("stderr"))
    return {"cancelled": True, "gateway_restored": was_running}


# ============ 绑定（创建 Agent + 绑定 + 重启网关）============
@router.post("/bind", response_model=AgentOut)
async def bind_agent(payload: BindIn):
    """扫码成功后调用：创建 Agent（如不存在）+ 绑定 accountId + 重启网关。

    前端流程：
    1. POST /qrcode 拿二维码 → 用户扫码
    2. 扫码成功后，OpenClaw 自动写入 accounts.json
    3. 前端轮询 GET / 直到出现新 accountId
    4. 拿到新 accountId 后，POST /bind 创建 Agent + 绑定

    本接口做了 3 件事：
    - openclaw agents add <agent_id>（如不存在）
    - openclaw agents bind --agent <agent_id> --bind openclaw-weixin:<account_id>
    - openclaw gateway restart
    """
    if not payload.account_id:
        raise HTTPException(400, "account_id is required (扫码后从账号列表拿到)")

    # workspace 默认 ~/agent_id
    import os
    workspace = payload.workspace or str(
        Path(os.path.expanduser("~")) / payload.agent_id
    )

    # 1) 检查 Agent 是否已存在，不存在则创建
    existing = openclaw_accessor.get_agent(payload.agent_id)
    if not existing:
        logger.info("Creating agent %s workspace=%s", payload.agent_id, workspace)
        try:
            r = await openclaw_cli.add_agent(
                agent_id=payload.agent_id,
                workspace=workspace,
                name=payload.name,
            )
            if not r["ok"]:
                # CLI 报错但可能已写入配置 → 兜底检查
                if not openclaw_accessor.get_agent(payload.agent_id):
                    raise HTTPException(
                        500,
                        f"创建 Agent 失败：{r.get('stderr') or r.get('stdout')}",
                    )
                logger.warning("add_agent 报错但配置已写入，继续绑定: %s", r.get("stderr"))
        except TimeoutError:
            # 超时后 add 可能已写入配置（本次故障的根因）→ 兜底检查
            if not openclaw_accessor.get_agent(payload.agent_id):
                raise HTTPException(500, "创建 Agent 超时，请重试")
            logger.warning("add_agent 超时但配置已写入，继续绑定")

    # 2) 绑定 Agent ↔ accountId
    logger.info(
        "Binding agent %s <-> account %s", payload.agent_id, payload.account_id
    )
    r = await openclaw_cli.bind_agent(payload.agent_id, payload.account_id)
    if not r["ok"]:
        # 即使 CLI 报错，可能配置文件已经被改了。先读 accessor 看 binding 是否已存在
        a = openclaw_accessor.get_agent(payload.agent_id)
        if not a or payload.account_id not in a.bound_account_ids:
            raise HTTPException(
                500,
                f"绑定失败：{r.get('stderr') or r.get('stdout')}",
            )

    # 3) 杀掉扫码子进程 + 清并发锁 + 启动网关恢复服务
    logger.info("Killing QR login subprocess + starting gateway")
    openclaw_cli.kill_login_proc()
    openclaw_cli._qr_login_proc["in_progress"] = False
    # 网关状态判断后决定：运行中 → 防抖调度重启（不阻塞、合并多次变更）；
    # 停止中（扫码流程停的）→ 立即启动恢复服务。
    try:
        status_r = await openclaw_cli._run_openclaw(["gateway", "status"], timeout=5.0)
        gw_running = status_r[0] == 0 and "running" in status_r[1].lower()
    except Exception:
        gw_running = False
    if gw_running:
        openclaw_cli.schedule_gateway_restart()
        logger.info("Gateway running, restart scheduled after bind (debounced)")
    else:
        r = await openclaw_cli.start_gateway(timeout=120.0)
        if not r["ok"]:
            # gateway start 失败不致命，binding 已写入配置文件，下次手动启动会生效
            logger.warning("gateway start failed: %s", r.get("stderr"))

    # 返回最新 Agent 状态
    a = openclaw_accessor.get_agent(payload.agent_id)
    if not a:
        raise HTTPException(500, "Agent 创建后查询失败")

    accounts = {acc.account_id: acc for acc in openclaw_accessor.list_weixin_accounts()}
    bound = [
        WeixinAccountOut(
            account_id=acc.account_id,
            user_id=acc.user_id,
            saved_at=acc.saved_at,
            note=acc.note,
        )
        for acc_id in a.bound_account_ids
        if (acc := accounts.get(acc_id))
    ]
    return AgentOut(
        agent_id=a.agent_id,
        name=a.name,
        workspace=a.workspace,
        model=a.model,
        bound_accounts=bound,
        soul_md=openclaw_accessor.read_soul_md(a.agent_id) or "",
    )


# ============ 启停（OpenClaw 自动管，保留接口避免前端报错）============
@router.post("/{agent_id}/start")
async def start_agent(agent_id: str):
    """OpenClaw 网关自动管长轮询，无需手动启动。保留接口兼容前端。"""
    return {"agent_id": agent_id, "running": True, "message": "OpenClaw 自动管理，无需启动"}


@router.post("/{agent_id}/stop")
async def stop_agent(agent_id: str):
    """OpenClaw 网关自动管长轮询。保留接口兼容前端。"""
    return {"agent_id": agent_id, "running": False, "message": "OpenClaw 自动管理，无需停止"}


@router.delete("/{agent_id}", status_code=204)
async def delete_agent(agent_id: str):
    """删除 Agent：摘除配置 + 清理会话 sqlite + 重启网关。

    不调 `openclaw agents remove` CLI（避免 OpenClaw 沙箱限制）。
    关键：必须清理该 Agent 的会话 sqlite 并重启网关——
    否则网关内存/会话存储仍把消息路由到已删除的 Agent，
    导致「Something went wrong」错误。
    禁止删除最后一个 Agent（OpenClaw 要求至少一个 configured agent）。
    """
    cfg = openclaw_accessor.read_config()
    entries = cfg.get("agents", {}).get("entries", {})
    if agent_id not in entries:
        raise HTTPException(404, f"Agent not found: {agent_id}")

    if len(entries) <= 1:
        raise HTTPException(400, "禁止删除最后一个 Agent，OpenClaw 要求至少保留一个")

    # 1) 从 agents.entries 删
    del entries[agent_id]

    # 2) 从 bindings 删该 agent 的所有绑定
    cfg["bindings"] = [
        b for b in cfg.get("bindings", []) if b.get("agentId") != agent_id
    ]

    openclaw_accessor.write_config(cfg)

    # 3) 删除该 Agent 的会话 sqlite（清残留会话，防止消息路由到已删 Agent）
    agent_dir = Path.home() / ".openclaw" / "agents" / agent_id / "agent"
    for suffix in ("", "-wal", "-shm"):
        f = agent_dir / f"openclaw-agent.sqlite{suffix}"
        if f.exists():
            try:
                f.unlink()
                logger.info("已删除会话库 %s", f)
            except PermissionError:
                logger.warning("会话库 %s 被占用（网关运行中），稍后自动清除", f)

    # 4) 网关防抖调度重启：新配置生效 + 清内存会话（不阻塞接口）
    openclaw_cli.schedule_gateway_restart()
    logger.info("Agent %s deleted, gateway restart scheduled (debounced)", agent_id)
    return None


# ============ SOUL.md 读写 ============
@router.get("/{agent_id}/soul")
def get_soul_md(agent_id: str):
    """获取 Agent 的 SOUL.md 内容。"""
    content = openclaw_accessor.read_soul_md(agent_id)
    if content is None:
        raise HTTPException(404, f"Agent not found: {agent_id}")
    return {"agent_id": agent_id, "content": content}


@router.put("/{agent_id}/soul")
def update_soul_md(agent_id: str, body: dict):
    """更新 Agent 的 SOUL.md。body: {"content": "..."}"""
    content = body.get("content", "")
    try:
        openclaw_accessor.write_soul_md(agent_id, content)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"agent_id": agent_id, "saved": True}


# ============ 辅助：workspace 默认路径 ============
from pathlib import Path  # noqa: E402
