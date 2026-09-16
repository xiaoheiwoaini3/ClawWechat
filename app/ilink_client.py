"""iLink Bot 官方 API HTTP 客户端。

基于 httpx.AsyncClient，封装 iLink Bot 的鉴权、长轮询、发消息。

统一返回约定（不向调用方抛裸异常）：
- 成功：{"ok": True, ...字段}
- 失败：{"ok": False, "error": <错误码>, "message": <人类可读说明>, ...原始数据}

错误码（字符串常量，供调用方分支判断）：
- timeout           请求超时
- network           网络异常（连接失败、DNS、TLS 等）
- http_error        HTTP 状态非 2xx
- parse_error       响应非 JSON
- rate_limit        ret == -2，发送过快
- session_expired   errcode == -14，需重新扫码
- api_error         其他业务错误或字段缺失
"""
from __future__ import annotations

import base64
import logging
import secrets
import struct
from typing import Any, Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# ===== 错误码常量 =====
ERR_TIMEOUT = "timeout"
ERR_NETWORK = "network"
ERR_HTTP = "http_error"
ERR_PARSE = "parse_error"
ERR_RATE_LIMIT = "rate_limit"            # ret == -2
ERR_SESSION_EXPIRED = "session_expired"  # errcode == -14
ERR_API = "api_error"


def _gen_x_wechat_uin() -> str:
    """生成 X-WECHAT-UIN 请求头：base64(随机 uint32)。

    每次请求都重新生成一个随机值，符合协议要求。
    """
    # secrets.randbelow 返回 [0, n)，n=0x100000000 即 [0, 2^32)
    val = secrets.randbelow(0x100000000)
    # struct.pack(">I", ...) 大端 uint32（4 字节），再 base64 编码
    return base64.b64encode(struct.pack(">I", val)).decode("ascii")


def _ok(**data: Any) -> dict:
    """构造成功响应。"""
    return {"ok": True, **data}


def _err(error: str, message: str, **extra: Any) -> dict:
    """构造失败响应。extra 用于附带原始数据，方便上层排查。"""
    out: dict = {"ok": False, "error": error, "message": message}
    out.update(extra)
    return out


class ILinkBotClient:
    """iLink Bot HTTP 客户端。

    设计要点：
    - 一个实例复用一个 httpx.AsyncClient（连接池），bot_token 作为方法参数传入，
      这样同一个 client 实例可服务多个 Bot，且单 Bot 切换 token 时无需重建。
    - 长轮询超时单独配置，比 iLink 服务端 hold 时间（~35s）多留余量。
    - 所有网络/解析异常在此层捕获，对外只返回 dict。
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> None:
        self.base_url = (base_url or settings.ILINK_BASE_URL).rstrip("/")
        # 普通请求超时（鉴权、发消息）
        self._default_timeout: float = float(timeout or 10.0)
        # 长轮询超时：iLink 默认 hold ~35s，客户端 +5s 余量
        self._long_poll_timeout: float = float(settings.LONG_POLL_TIMEOUT) + 5.0
        self._client: Optional[httpx.AsyncClient] = None

    # ===== client 生命周期 =====
    async def _get_client(self) -> httpx.AsyncClient:
        """懒加载 AsyncClient，关闭后自动重建。"""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient()
        return self._client

    async def aclose(self) -> None:
        """关闭底层连接池。应用关闭时调用。"""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    # ===== 请求头 =====
    def _auth_headers(self, bot_token: str) -> dict:
        """构造鉴权请求头（扫码确认后的接口使用）。"""
        return {
            "AuthorizationType": "ilink_bot_token",
            "Authorization": f"Bearer {bot_token}",
            "X-WECHAT-UIN": _gen_x_wechat_uin(),
            "Content-Type": "application/json",
        }

    # ===== 业务错误码检查 =====
    def _check_biz_error(self, data: dict) -> Optional[dict]:
        """检查 iLink 业务错误码（ret / errcode）。

        返回错误 dict 或 None（无业务错误）。
        iLink 字段类型可能是 int 或 str，统一兼容。
        """
        ret = data.get("ret")
        errcode = data.get("errcode")
        errmsg = data.get("errmsg") or data.get("errmsg_zh") or ""

        # errcode -14：会话过期，需重新扫码
        if errcode in (-14, "-14"):
            return _err(ERR_SESSION_EXPIRED, "会话过期（errcode=-14），需重新扫码", raw=data)

        # ret -2：可能是限流，也可能是参数错误
        # 实测 errmsg="invalid arguments" 表示参数格式错误（非限流，重试无用）
        if ret in (-2, "-2"):
            if "invalid" in errmsg.lower():
                return _err(
                    ERR_API,
                    f"参数错误 ret=-2 errmsg={errmsg}（请求体格式不对，不重试）",
                    raw=data,
                )
            return _err(ERR_RATE_LIMIT, f"触发限流 ret=-2 errmsg={errmsg}", raw=data)

        # ret 非 0：其他业务错误
        if ret is not None and ret not in (0, "0"):
            return _err(
                ERR_API,
                f"iLink 业务错误 ret={ret} errcode={errcode} errmsg={errmsg}",
                raw=data,
            )
        return None

    # ===== 统一请求入口 =====
    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        json_body: Optional[dict] = None,
        bot_token: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> dict:
        """统一请求：处理超时/网络/HTTP/JSON/业务错误，返回统一 dict。"""
        client = await self._get_client()

        # 构造请求头：鉴权接口带四件套，非鉴权接口只带 Content-Type
        if bot_token:
            headers = self._auth_headers(bot_token)
        else:
            headers = {"Content-Type": "application/json"}

        url = f"{self.base_url}{path}"

        # --- 发请求：捕获所有网络/超时异常 ---
        try:
            resp = await client.request(
                method,
                url,
                params=params,
                json=json_body,
                headers=headers,
                timeout=timeout if timeout is not None else self._default_timeout,
            )
        except httpx.TimeoutException as e:
            return _err(ERR_TIMEOUT, f"请求超时: {e}")
        except httpx.HTTPError as e:
            # 覆盖连接失败、DNS、TLS、读错误等
            return _err(ERR_NETWORK, f"网络错误: {e}")
        except Exception as e:  # 兜底：未知异常
            logger.exception("未知请求异常 path=%s", path)
            return _err(ERR_NETWORK, f"未知异常: {e}")

        # --- HTTP 状态码 ---
        if resp.status_code < 200 or resp.status_code >= 300:
            return _err(
                ERR_HTTP,
                f"HTTP {resp.status_code}: {resp.text[:200]}",
                status_code=resp.status_code,
                body=resp.text[:500],
            )

        # --- 解析 JSON ---
        try:
            data = resp.json()
        except Exception as e:
            return _err(ERR_PARSE, f"响应非 JSON: {e}", body=resp.text[:500])

        # --- 业务错误码 ---
        if not isinstance(data, dict):
            return _err(ERR_PARSE, f"响应不是 JSON 对象: {type(data).__name__}", raw=data)

        biz_err = self._check_biz_error(data)
        if biz_err is not None:
            return biz_err

        return _ok(raw=data)

    # ============================================================
    # 鉴权
    # ============================================================
    async def get_bot_qrcode(self) -> dict:
        """获取绑定二维码（无需 token）。

        GET /ilink/bot/get_bot_qrcode?bot_type=3

        返回:
            成功: {"ok": True, "qrcode": "qrc_xxx", "qrcode_img_content": "https://...", "raw": {...}}
            失败: {"ok": False, "error": ..., "message": ...}
        """
        r = await self._request(
            "GET",
            "/ilink/bot/get_bot_qrcode",
            params={"bot_type": 3},
        )
        if not r["ok"]:
            return r

        data: dict = r["raw"]
        qrcode = data.get("qrcode")
        img = data.get("qrcode_img_content")
        if not qrcode:
            return _err(ERR_API, "响应缺少 qrcode 字段", raw=data)
        return _ok(qrcode=qrcode, qrcode_img_content=img, raw=data)

    async def get_qrcode_status(self, qrcode: str) -> dict:
        """轮询扫码状态（无需 token）。

        GET /ilink/bot/get_qrcode_status?qrcode=xxx

        ⚠️ 该接口是长轮询：服务端会 hold 请求直到状态变化或约 30s 超时，
        客户端必须用 >= 35s 的超时配置，否则会误判为超时失败。
        本方法复用 self._long_poll_timeout（默认 45s）。

        状态流转: wait -> scaned -> confirmed / expired

        返回:
            成功: {"ok": True, "status": "wait|scaned|confirmed|expired", "raw": {...}}
            confirmed 时额外附带: bot_token / bot_wxid / nickname（若返回）
            失败: {"ok": False, "error": ..., "message": ...}
        """
        if not qrcode:
            return _err(ERR_API, "qrcode 不能为空")

        r = await self._request(
            "GET",
            "/ilink/bot/get_qrcode_status",
            params={"qrcode": qrcode},
            timeout=self._long_poll_timeout,  # 长轮询必须用长超时（>= 35s）
        )
        if not r["ok"]:
            return r

        data: dict = r["raw"]
        # 状态字段兼容 status / qrcode_status
        status = data.get("status") or data.get("qrcode_status") or "unknown"

        out: dict = {"status": status}

        if status == "confirmed":
            # confirmed 时必须返回 bot_token，否则视为协议异常
            bot_token = data.get("bot_token")
            if not bot_token:
                return _err(ERR_API, "status=confirmed 但缺少 bot_token", raw=data)
            out["bot_token"] = bot_token

            # 扫码微信 wxid：iLink 真实字段名是 ilink_user_id（不是 bot_wxid）
            # 兼容字段优先级：bot_wxid > ilink_user_id > from_user_id
            bot_wxid = (
                data.get("bot_wxid")
                or data.get("ilink_user_id")
                or data.get("from_user_id")
            )
            if bot_wxid:
                out["bot_wxid"] = bot_wxid

            # Bot 自身 id：iLink 真实字段名是 ilink_bot_id
            if data.get("ilink_bot_id"):
                out["ilink_bot_id"] = data["ilink_bot_id"]

            # 昵称（iLink 实测不返回，但保留兼容）
            if data.get("nickname"):
                out["nickname"] = data["nickname"]

        out["raw"] = data
        return _ok(**out)

    # ============================================================
    # 长轮询收消息
    # ============================================================
    async def get_updates(
        self,
        bot_token: str,
        get_updates_buf: Optional[str],
    ) -> dict:
        """长轮询拉取新消息（需 token）。

        POST /ilink/bot/getupdates
        Body: {"get_updates_buf": "上次游标", "base_info": {}}

        服务端 hold ~35s，返回新消息 + 新 get_updates_buf。
        新游标必须由调用方持久化，下次回传。

        返回:
            成功: {"ok": True, "messages": [...], "get_updates_buf": "新游标", "raw": {...}}
            失败: {"ok": False, "error": ..., "message": ...}
        """
        if not bot_token:
            return _err(ERR_API, "bot_token 不能为空")

        body = {
            "get_updates_buf": get_updates_buf or "",
            "base_info": {},
        }
        r = await self._request(
            "POST",
            "/ilink/bot/getupdates",
            json_body=body,
            bot_token=bot_token,
            timeout=self._long_poll_timeout,
        )
        if not r["ok"]:
            return r

        data: dict = r["raw"]
        new_buf = data.get("get_updates_buf")

        # 消息列表字段名兼容：iLink 真实字段名是 "msgs"，
        # 同时兜底 updates / messages 以防接口变更
        msgs = data.get("msgs")
        if msgs is None:
            msgs = data.get("updates")
        if msgs is None:
            msgs = data.get("messages")
        if not isinstance(msgs, list):
            msgs = msgs or []

        return _ok(
            messages=msgs,
            get_updates_buf=new_buf,
            raw=data,
        )

    # ============================================================
    # 发送消息
    # ============================================================
    async def send_message(
        self,
        bot_token: str,
        to_user: str,
        text: str,
        context_token: str,
    ) -> dict:
        """被动回复文本消息（需 token）。

        POST /ilink/bot/sendmessage

        ⚠️ iLink 真实出站 body 结构与入站消息对称（实测）：
        {
          "msg": {
            "to_user_id": "<用户 wxid>",          # 注意是 to_user_id，不是 to_user
            "item_list": [                       # 用 item_list 包消息项，不是顶层 text
              {"type": 1, "text_item": {"text": "<内容>"}}
            ],
            "context_token": "<回传入站时的 token>"
          }
        }

        必须回传收到消息时的 context_token，24 小时过期。
        发送过快会触发 ret=-2 限流，调用方需自行加间隔。

        返回:
            成功: {"ok": True, "raw": {...}}
            失败: {"ok": False, "error": ..., "message": ...}
        """
        if not (bot_token and to_user and text and context_token):
            return _err(
                ERR_API,
                "bot_token / to_user / text / context_token 均不能为空",
            )

        body = {
            "msg": {
                "to_user_id": to_user,                       # 真实字段名 to_user_id
                "item_list": [                                # 与入站消息结构对称
                    {
                        "type": 1,
                        "text_item": {"text": text},
                    }
                ],
                "context_token": context_token,
            }
        }
        # 打印请求体便于排查参数错误
        logger.info(
            "send_message body: to_user_id=%s, text=%s, context_token=%s...",
            to_user,
            text[:50],
            context_token[:30] if context_token else "",
        )
        r = await self._request(
            "POST",
            "/ilink/bot/sendmessage",
            json_body=body,
            bot_token=bot_token,
        )
        # 打印完整 raw 响应（不只是 ret）辅助排查"ret=0 但微信没收到"
        logger.info(
            "send_message response: ok=%s, raw=%s, raw_type=%s",
            r.get("ok"),
            r.get("raw"),
            type(r.get("raw")).__name__,
        )
        return r


# 默认单例：共享连接池，应用全局复用
ilink_client = ILinkBotClient()
