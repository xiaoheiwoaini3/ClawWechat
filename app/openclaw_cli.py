"""OpenClaw CLI 包装：subprocess 调 openclaw 命令。

只包装那些必须用 CLI 的操作：
- channels login：扫码登录（交互式，需要捕获二维码输出）
- agents add：创建新 Agent
- agents bind：绑定 Agent ↔ accountId
- gateway restart：重启网关让配置生效

其他数据读取都用 openclaw_accessor（直接读写文件）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Optional

logger = logging.getLogger(__name__)

# Windows 上 openclaw 是 .ps1 / .cmd 包装，需要通过 shell 调
# 在 PATH 里找 openclaw
_OPENCLAW_BIN = shutil.which("openclaw") or "openclaw"


# ============================================================
# 通用 subprocess 工具
# ============================================================
async def _run_openclaw(
    args: list[str],
    *,
    timeout: float = 30.0,
    check: bool = False,
) -> tuple[int, str, str]:
    """跑 openclaw 命令，返回 (returncode, stdout, stderr)。

    Windows 上 openclaw 是 .ps1 包装，需要走 shell。
    """
    cmd = [_OPENCLAW_BIN] + args
    logger.info("openclaw CLI: %s", " ".join(cmd))

    # Windows: 用 shell=True 让 PowerShell 处理 .ps1
    proc = await asyncio.create_subprocess_shell(
        " ".join(cmd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError as e:
        proc.kill()
        await proc.wait()
        raise TimeoutError(f"openclaw {' '.join(args)} timed out after {timeout}s") from e

    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")
    rc = proc.returncode if proc.returncode is not None else -1

    if check and rc != 0:
        raise RuntimeError(
            f"openclaw {' '.join(args)} failed (rc={rc}):\nstdout: {stdout}\nstderr: {stderr}"
        )
    return rc, stdout, stderr


# ============================================================
# 扫码登录：后台子进程模式
# ============================================================
@dataclass
class QrLoginResult:
    """扫码启动结果。"""

    qr_data: Optional[str] = None  # 二维码数据（base64 或 URL）
    message: str = ""
    proc: Optional[asyncio.subprocess.Process] = None  # 后台活着的子进程
    error: Optional[str] = None


async def start_qr_login_bg(
    channel: str = "openclaw-weixin",
    qr_timeout: float = 60.0,
) -> QrLoginResult:
    """启动 `openclaw channels login`，等二维码出现就返回。

    与旧的 stream_qr_login 不同：
    - 不用 async generator（generator close 会 kill 进程）
    - 找到二维码后直接返回，子进程继续在后台跑等扫码
    - 扫码成功后 OpenClaw CLI 自己写 accounts.json
    - 返回的 proc 由调用方管理生命周期

    调用前必须先 stop gateway（否则 sqlite 锁冲突）。
    """
    cmd = f"{_OPENCLAW_BIN} channels login --channel {channel}"
    logger.info("openclaw CLI: %s", cmd)

    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    assert proc.stdout is not None
    deadline = asyncio.get_event_loop().time() + qr_timeout
    buf = ""

    while asyncio.get_event_loop().time() < deadline:
        try:
            chunk = await asyncio.wait_for(proc.stdout.read(4096), timeout=10.0)
        except asyncio.TimeoutError:
            if proc.returncode is not None:
                break
            continue

        if not chunk:
            if proc.returncode is None:
                await proc.wait()
            break

        text = chunk.decode("utf-8", errors="replace")
        buf += text
        logger.debug("openclaw login stdout: %s", text[:200])

        # 检查错误
        if "disk I/O error" in buf or "ERR_SQLITE_ERROR" in buf:
            # 杀掉进程，返回错误
            proc.kill()
            return QrLoginResult(
                error="OpenClaw state sqlite 被锁，请先停 gateway 再扫码",
                proc=None,
            )

        # 找二维码
        qr = _extract_qr(buf)
        if qr:
            # 记录当前二维码，并启动后台任务持续读 stdout 跟随刷新
            # （OpenClaw 的微信二维码约 1-2 分钟过期，会自动刷新新码）
            _qr_login_proc["current_qr_data"] = qr
            _qr_login_proc["reader_task"] = asyncio.create_task(
                _read_qr_loop(proc, buf)
            )
            return QrLoginResult(
                qr_data=qr,
                message="二维码已生成，请用微信扫码",
                proc=proc,
            )

        # 进程提前退出
        if proc.returncode is not None:
            return QrLoginResult(
                error=f"openclaw login 进程退出(rc={proc.returncode})：{buf[-300:]}",
                proc=None,
            )

        # 截断 buffer 防无限增长
        if len(buf) > 8192:
            buf = buf[-1024:]

    # 超时：杀进程
    if proc.returncode is None:
        proc.kill()
        await proc.wait()
    return QrLoginResult(error=f"{qr_timeout}s 内未捕获到二维码输出", proc=None)


def _strip_ansi(s: str) -> str:
    """去掉 ANSI 转义序列。"""
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", s)


async def _read_qr_loop(
    proc: asyncio.subprocess.Process, initial_buf: str
) -> None:
    """后台持续读扫码进程 stdout，二维码刷新时更新 current_qr_data。

    OpenClaw 的微信二维码有效期约 1-2 分钟，过期后自动刷新新二维码
    （日志: "refreshing QR code (N/3)"）。前端轮询 GET /api/bots/qrcode-current
    拿到最新二维码，保证用户扫的永远是有效码。
    """
    buf = initial_buf
    while proc.returncode is None:
        try:
            chunk = await asyncio.wait_for(proc.stdout.read(4096), timeout=10.0)
        except asyncio.TimeoutError:
            continue
        if not chunk:
            break
        buf += chunk.decode("utf-8", errors="replace")
        if len(buf) > 8192:
            buf = buf[-2048:]
        qr = _extract_qr(buf)
        if qr and qr != _qr_login_proc.get("current_qr_data"):
            _qr_login_proc["current_qr_data"] = qr
            logger.info("扫码二维码已刷新（前端将跟随更新）")
            buf = ""  # 已提取，清空累积，避免重复识别旧码
    # 进程结束：清理任务引用（保留 current_qr_data 便于排查）
    if _qr_login_proc.get("reader_task") is asyncio.current_task():
        _qr_login_proc["reader_task"] = None


# ASCII QR 字符 → 两位像素（上,下），1=黑 0=白
_QR_CHAR_MAP = {
    "█": (1, 1),  # 全块
    "▄": (0, 1),  # 下半
    "▀": (1, 0),  # 上半
    " ": (0, 0),  # 空
    "▌": (1, 0),  # 左半（QR 通常不用，兼容）
    "▐": (0, 1),  # 右半
}


def _is_qr_line(line: str) -> bool:
    """判断一行是否是 QR 字符画的一部分。"""
    if len(line) < 10:
        return False
    qr_chars = set(_QR_CHAR_MAP.keys())
    count = sum(1 for c in line if c in qr_chars)
    return count >= len(line) * 0.5


def _ascii_qr_to_svg_data_url(text: str) -> Optional[str]:
    """把 ASCII 字符画二维码转成 SVG data URL。

    OpenClaw 在终端输出用 █▄▀ 字符渲染的二维码，
    每个字符代表 2 个垂直像素。本函数解析成 SVG，
    返回 data:image/svg+xml;base64,... 格式的 data URL。
    """
    lines = text.splitlines()
    # 找到连续的 QR 行
    qr_lines: list[str] = []
    in_qr = False
    for line in lines:
        stripped = line.rstrip()
        if _is_qr_line(stripped):
            in_qr = True
            qr_lines.append(stripped)
        elif in_qr and not stripped:
            # 空行可能是 QR 结束
            if len(qr_lines) >= 10:
                break
            qr_lines.append(stripped)
        elif in_qr:
            if len(qr_lines) >= 10:
                break
            in_qr = False

    if len(qr_lines) < 10:
        return None

    # 去掉尾部空行
    while qr_lines and not qr_lines[-1].strip():
        qr_lines.pop()
    if len(qr_lines) < 10:
        return None

    # 解析成像素矩阵
    width = max(len(line) for line in qr_lines)
    height = len(qr_lines) * 2  # 每行 = 2 像素高
    pixels: list[list[int]] = [[0] * width for _ in range(height)]

    for row_idx, line in enumerate(qr_lines):
        for col_idx in range(width):
            ch = line[col_idx] if col_idx < len(line) else " "
            top, bottom = _QR_CHAR_MAP.get(ch, (0, 0))
            pixels[row_idx * 2][col_idx] = top
            pixels[row_idx * 2 + 1][col_idx] = bottom

    # 生成 SVG：每个像素 = scale x scale 的矩形
    scale = 8  # 每像素 8px，50x50 QR → 400x400 SVG
    svg_w = width * scale
    svg_h = height * scale
    rects: list[str] = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{svg_w}" height="{svg_h}" shape-rendering="crispEdges">']
    rects.append(f'<rect width="{svg_w}" height="{svg_h}" fill="white"/>')
    for y in range(height):
        for x in range(width):
            if pixels[y][x]:
                rects.append(
                    f'<rect x="{x*scale}" y="{y*scale}" width="{scale}" height="{scale}" fill="black"/>'
                )
    rects.append("</svg>")
    svg = "".join(rects)

    # 编码成 data URL
    import base64
    b64 = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{b64}"


def _url_to_qr_png_data_url(url: str) -> Optional[str]:
    """用 qrcode 库把 URL 编码成 PNG data URL。

    浏览器直接把 iLink URL 当 <img src> 会显示破损图标
    （因为该 URL 返回的是 HTML 页面，不是图片）。
    所以在后端把 URL 转成可扫描的 QR 码 PNG 图片。
    """
    try:
        import io
        import qrcode

        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=10,
            border=2,
        )
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        import base64
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return f"data:image/png;base64,{b64}"
    except Exception as e:
        logger.warning("qrcode 生成失败: %s", e)
        return None


def _extract_qr(text: str) -> Optional[str]:
    """从累积输出里提取二维码数据。

    优先级：
    1. data:image/png;base64,.... （PNG data URL）
    2. iLink URL：含 qrcode=qrc_xxx → 用 qrcode 库转成 PNG data URL
    3. 超长 base64 字符串（>200 字符的 base64）
    4. ASCII 字符画二维码（█▄▀）→ 转 SVG data URL
    """
    # 1) PNG data URL
    m = re.search(r"data:image/png;base64,[A-Za-z0-9+/=]{100,}", text)
    if m:
        return m.group(0)

    # 2) iLink URL → 转 PNG
    # ⚠️ 必须捕获完整 URL（含 &bot_type=3）。之前只匹配到 qrcode=xxx 就停，
    #    生成的二维码缺 bot_type 参数，微信扫码打开 liteapp 链接会被服务端拒绝，
    #    表现为「网络错误」。与 OpenClaw 终端显示的完整链接保持一致。
    m = re.search(r"https?://[^\s\"'<>]+?qrcode=[A-Za-z0-9_\-]+(?:&[^\s\"'<>]*)?", text)
    if m:
        url = m.group(0)
        png = _url_to_qr_png_data_url(url)
        if png:
            return png
        return url  # 退回到原始 URL

    # 3) 纯 base64（>200 字符）
    m = re.search(r"[A-Za-z0-9+/]{200,}={0,2}", text)
    if m:
        candidate = m.group(0)
        return "data:image/png;base64," + candidate

    # 4) ASCII QR 字符画 → SVG data URL
    svg = _ascii_qr_to_svg_data_url(text)
    if svg:
        return svg

    return None


# ============================================================
# Agent 管理
# ============================================================
async def add_agent(agent_id: str, workspace: str, name: str | None = None) -> dict:
    """创建新 Agent。

    openclaw agents add <id> --workspace <path> --non-interactive

    ⚠️ openclaw CLI 的 name 是位置参数（即 <id> 本身），不存在 --name 选项，
    传了会报 'does not recognize option "--name"'。name 参数保留仅为兼容调用方，不使用。
    """
    args = ["agents", "add", agent_id, "--workspace", workspace, "--non-interactive"]
    # CLI 冷启动（node + 插件注册）约 12s，负载高时更慢；20s 太紧会被误杀。
    # 即使超时，add 也可能已写入配置——调用方应兜底检查 entries。
    rc, stdout, stderr = await _run_openclaw(args, timeout=90.0, check=False)
    return {
        "ok": rc == 0,
        "rc": rc,
        "stdout": stdout,
        "stderr": stderr,
    }


async def bind_agent(agent_id: str, account_id: str) -> dict:
    """绑定 Agent ↔ 微信账号（本地写配置，不依赖 CLI，避免超时）。

    直接改 openclaw.json 的 bindings（与删除 Agent 的本地操作对称）。
    通配绑定（accountId='*'）保留；重复绑定去重。
    """
    from app import openclaw_accessor  # noqa: PLC0415

    cfg = openclaw_accessor.read_config()
    bindings = cfg.get("bindings", [])
    # 去重：移除同一 agent+account 的旧绑定（保留其他）
    bindings = [
        b
        for b in bindings
        if not (
            b.get("agentId") == agent_id
            and b.get("match", {}).get("accountId") == account_id
        )
    ]
    bindings.append(
        {
            "agentId": agent_id,
            "match": {"channel": "openclaw-weixin", "accountId": account_id},
        }
    )
    cfg["bindings"] = bindings
    openclaw_accessor.write_config(cfg)
    return {"ok": True, "rc": 0, "stdout": f"bound {agent_id} <-> {account_id}", "stderr": ""}


async def restart_gateway(timeout: float = 30.0) -> dict:
    """重启 OpenClaw 网关。"""
    rc, stdout, stderr = await _run_openclaw(["gateway", "restart"], timeout=timeout, check=False)
    return {
        "ok": rc == 0,
        "rc": rc,
        "stdout": stdout,
        "stderr": stderr,
    }


async def stop_gateway(timeout: float = 15.0) -> dict:
    """停止 OpenClaw 网关（释放 state sqlite 锁，扫码前必须调）。

    必须加 --force，否则 openclaw 拒绝停止运行中的 gateway 服务。
    """
    rc, stdout, stderr = await _run_openclaw(
        ["gateway", "stop", "--force"], timeout=timeout, check=False
    )
    return {
        "ok": rc == 0,
        "rc": rc,
        "stdout": stdout,
        "stderr": stderr,
    }


async def start_gateway(timeout: float = 30.0) -> dict:
    """启动 OpenClaw 网关（扫码绑定后调）。"""
    rc, stdout, stderr = await _run_openclaw(["gateway", "start"], timeout=timeout, check=False)
    return {
        "ok": rc == 0,
        "rc": rc,
        "stdout": stdout,
        "stderr": stderr,
    }


def kill_login_proc() -> None:
    """杀掉当前扫码子进程（如有），并清理后台读流任务与二维码缓存。"""
    proc = _qr_login_proc.get("proc")
    if proc and proc.returncode is None:
        proc.kill()
    task = _qr_login_proc.get("reader_task")
    if task and not task.done():
        task.cancel()
    _qr_login_proc["proc"] = None
    _qr_login_proc["reader_task"] = None
    _qr_login_proc["current_qr_data"] = None


# 全局存储扫码子进程 + 扫码前 gateway 是否在跑（扫码后恢复）
# current_qr_data: 最新二维码（OpenClaw 会自动刷新，前端轮询跟随）
# reader_task: 后台持续读 stdout 的任务
_qr_login_proc: dict = {
    "proc": None,
    "gateway_was_running": False,
    "reader_task": None,
    "current_qr_data": None,
}


# ============================================================
# 网关重启调度（异步 + 防抖合并）
# ============================================================
# 目的：配置变更（绑定/删除 Agent）后避免每次都立即重启网关——
# OpenClaw 不支持配置热重载，必须重启才生效；但频繁重启导致服务不稳定。
# 方案：变更操作只「登记」，后台防抖合并，10s 窗口内的多次变更只重启一次；
# 接口不阻塞（立即返回），前端提示「配置已保存，网关自动重启中」。
_gateway_restart_state: dict = {
    "task": None,          # asyncio.Task：正在等待防抖窗口/正在重启
    "pending": False,      # 窗口内又来了新变更
    "restarting": False,   # 正在执行重启
}


async def _run_scheduled_gateway_restart() -> None:
    """防抖循环：窗口内持续有新变更则延后，直到静默 10s 后真正重启。"""
    while True:
        await asyncio.sleep(10.0)
        if _gateway_restart_state["pending"]:
            _gateway_restart_state["pending"] = False
            continue  # 窗口内又来了新变更，继续等待
        break

    _gateway_restart_state["restarting"] = True
    _gateway_restart_state["task"] = None
    try:
        logger.info("Scheduled gateway restart: applying config changes")
        r = await restart_gateway(timeout=180.0)
        if not r["ok"]:
            logger.warning(
                "Scheduled gateway restart failed: %s",
                r.get("stderr") or r.get("stdout"),
            )
    except Exception:
        logger.exception("Scheduled gateway restart crashed")
    finally:
        _gateway_restart_state["restarting"] = False


def schedule_gateway_restart() -> None:
    """登记一次网关重启（防抖合并，不阻塞调用方）。

    若已有计划任务在跑/在等窗口，只标记 pending（窗口内合并）；
    否则新起一个后台任务，静默 10s 后执行重启。
    """
    cur = _gateway_restart_state
    if cur["task"] is not None and not cur["task"].done():
        cur["pending"] = True
        return
    if cur["restarting"]:
        cur["pending"] = True
        return
    cur["pending"] = False
    cur["task"] = asyncio.create_task(_run_scheduled_gateway_restart())


# ============================================================
# 验证
# ============================================================
if __name__ == "__main__":
    # 测试 restart
    import asyncio as _asyncio

    async def _test():
        print("=== gateway restart ===")
        r = await restart_gateway()
        print(r)

    _asyncio.run(_test())
