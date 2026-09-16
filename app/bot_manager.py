"""Bot 生命周期与消息处理管理器（适配新 models）。

每个 active Bot 拥有：
- 一个长轮询 asyncio task（_poll_loop）：拉消息、持久化游标、分发处理
- 一个发送 asyncio task（_sender_loop）：消费 send_queue，串行发送并加随机延迟防限流

数据访问统一用 database.py 的四个函数，字段适配新 models：
- Bot.status / Bot.get_updates_buf
- Conversation.role_name / Conversation.context_token
- Message.role（user/assistant）/ Message.content

错误处理：
- errcode == -14（session_expired）：标记 Bot status=expired，停止轮询（需重新扫码）
- ret == -2（rate_limit）：发送侧加倍延迟重试一次
- timeout/network：轮询侧退避重试
"""
from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from app.ai_service import ai_service
from app.database import (
    SessionLocal,
    get_or_create_conversation,
    get_recent_messages,
    save_message,
    update_context_token,
)
from app.ilink_client import (
    ERR_NETWORK,
    ERR_RATE_LIMIT,
    ERR_SESSION_EXPIRED,
    ERR_TIMEOUT,
    ilink_client,
)
from app.models import Bot, Conversation
from app.role_manager import role_manager

logger = logging.getLogger(__name__)

# 发送防限流延迟区间（秒）
# iLink 限流较严，ret=0 但发送过快也会被静默丢弃，提升到 10-15s
SEND_DELAY_MIN = 10.0
SEND_DELAY_MAX = 15.0
# 限流时指数退避重试：5s → 15s → 30s
RATE_LIMIT_RETRY_DELAYS = [5.0, 15.0, 30.0]
# 历史消息条数（喂给 AI 的上下文）
HISTORY_LIMIT = 20


@dataclass
class BotRuntime:
    """单个 Bot 的运行时状态。

    context_tokens: user_wxid -> context_token 的内存缓存，
    与 conversations.context_token 双写（DB 持久化 + 内存加速）。
    """

    bot_id: int
    bot_token: str
    poll_running: bool = False
    # 对应需求中的 poll_thread；asyncio 实现下存 Task 而非 OS 线程
    poll_task: Optional[asyncio.Task] = None
    send_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    context_tokens: dict = field(default_factory=dict)
    sender_task: Optional[asyncio.Task] = None


def _parse_incoming(msg: dict) -> dict:
    """从 iLink 消息体提取字段。

    iLink 真实消息结构（实测）：
    {
      "seq": 5,
      "message_id": 7505631358879709960,
      "from_user_id": "o9cq801JUyzIEoInD_PkZmLsrJeI@im.wechat",  # 发送者
      "to_user_id": "1df5fda5af35@im.bot",
      "message_type": 1,
      "item_list": [                                                # 消息项列表
        {
          "type": 1,
          "text_item": {"text": "你好"},                            # 文本内容
          "msg_id": "v1:3399221760381267929",
          ...
        }
      ],
      "context_token": "AARzJWAFAAAB...",  # 回消息时回传
      "create_time_ms": 1789481963761,
      ...
    }

    兼容多种字段命名以防协议变更。
    """
    # 发送者 wxid：iLink 真实字段名是 from_user_id
    user_wxid = (
        msg.get("from_user_id")
        or msg.get("from_user")
        or msg.get("from_user_wxid")
        or msg.get("user")
        or msg.get("wxid")
        or msg.get("sender")
    )

    # 文本内容：iLink 真实结构是 item_list[0].text_item.text
    text = ""
    # 1. 优先从 item_list 取（iLink 真实结构）
    item_list = msg.get("item_list") or []
    if isinstance(item_list, list) and item_list:
        first_item = item_list[0] if isinstance(item_list[0], dict) else {}
        text_item = first_item.get("text_item") or {}
        if isinstance(text_item, dict):
            text = text_item.get("text") or ""
    # 2. 兜底从顶层取（兼容其他字段命名）
    if not text:
        text = msg.get("text") or msg.get("content") or msg.get("msg_content") or ""
    text = str(text or "")

    # context_token：实测字段名就是 context_token
    context_token = msg.get("context_token") or msg.get("contextToken") or ""
    return {
        "user_wxid": user_wxid,
        "text": text,
        "context_token": context_token,
    }


class BotManager:
    """多 Bot 实例管理器。"""

    def __init__(self) -> None:
        self._runtimes: dict[int, BotRuntime] = {}

    # ============================================================
    # 生命周期
    # ============================================================
    async def start_bot(self, bot_id: int) -> bool:
        """启动指定 Bot 的长轮询与发送任务。"""
        existing = self._runtimes.get(bot_id)
        if existing and existing.poll_running:
            return True

        bot = await asyncio.to_thread(self._get_bot, bot_id)
        if not bot:
            logger.error("启动失败：Bot %s 不存在", bot_id)
            return False
        if bot.status != "active" or not bot.bot_token:
            logger.error(
                "启动失败：Bot %s status=%s，需 active 且有 bot_token",
                bot_id, bot.status,
            )
            return False

        runtime = BotRuntime(
            bot_id=bot_id,
            bot_token=bot.bot_token,
            poll_running=True,
        )
        runtime.poll_task = asyncio.create_task(
            self._poll_loop(runtime), name=f"poll-{bot_id}"
        )
        runtime.sender_task = asyncio.create_task(
            self._sender_loop(runtime), name=f"sender-{bot_id}"
        )
        self._runtimes[bot_id] = runtime
        logger.info("Bot %s 已启动", bot_id)
        return True

    async def stop_bot(self, bot_id: int) -> bool:
        """停止指定 Bot 的所有任务。"""
        runtime = self._runtimes.get(bot_id)
        if not runtime:
            return False
        runtime.poll_running = False
        await self._cancel_task(runtime.poll_task)
        await self._cancel_task(runtime.sender_task)
        self._runtimes.pop(bot_id, None)
        logger.info("Bot %s 已停止", bot_id)
        return True

    async def _cancel_task(self, task: Optional[asyncio.Task]) -> None:
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    def is_running(self, bot_id: int) -> bool:
        runtime = self._runtimes.get(bot_id)
        return bool(runtime and runtime.poll_running)

    # ============================================================
    # 长轮询循环
    # ============================================================
    async def _poll_loop(self, runtime: BotRuntime) -> None:
        bot_id = runtime.bot_id
        logger.info("Bot %s 轮询任务启动", bot_id)

        while runtime.poll_running:
            # 每轮重新读游标（崩溃恢复后从 DB 取最新）
            bot = await asyncio.to_thread(self._get_bot, bot_id)
            if not bot:
                logger.error("Bot %s 不存在，轮询退出", bot_id)
                break
            if bot.status != "active":
                logger.warning("Bot %s status=%s，轮询退出", bot_id, bot.status)
                break
            # token 可能被重新扫码更新，运行时同步
            runtime.bot_token = bot.bot_token or runtime.bot_token

            r = await ilink_client.get_updates(
                runtime.bot_token, bot.get_updates_buf
            )

            if not r["ok"]:
                err = r["error"]
                if err == ERR_SESSION_EXPIRED:
                    logger.warning("Bot %s 会话过期（-14），标记需重新扫码", bot_id)
                    await asyncio.to_thread(self._mark_bot_expired, bot_id)
                    runtime.poll_running = False
                    break
                if err == ERR_TIMEOUT:
                    # 长轮询超时是正常现象，立即重试
                    continue
                if err == ERR_NETWORK:
                    logger.warning("Bot %s 网络错误，5s 后重试", bot_id)
                    await asyncio.sleep(5)
                    continue
                # 其他业务错误：退避
                logger.warning("Bot %s 轮询错误 %s: %s，2s 后重试",
                               bot_id, err, r.get("message"))
                await asyncio.sleep(2)
                continue

            # 成功：持久化新游标（即使无消息也要更新游标）
            new_buf = r.get("get_updates_buf")
            if new_buf is not None:
                await asyncio.to_thread(self._persist_cursor, bot_id, new_buf)

            messages = r.get("messages") or []
            for msg in messages:
                try:
                    await self._process_message(runtime, msg)
                except Exception as e:
                    logger.exception("Bot %s 消息处理异常: %s", bot_id, e)

        runtime.poll_running = False
        logger.info("Bot %s 轮询任务退出", bot_id)

    # ============================================================
    # 发送循环（消费 send_queue）
    # ============================================================
    async def _sender_loop(self, runtime: BotRuntime) -> None:
        bot_id = runtime.bot_id
        logger.info("Bot %s 发送任务启动", bot_id)

        while runtime.poll_running or not runtime.send_queue.empty():
            try:
                # 队列空时短超时轮询，便于及时响应 stop
                item = await asyncio.wait_for(
                    runtime.send_queue.get(), timeout=1.0
                )
            except asyncio.TimeoutError:
                continue

            try:
                await self._send_with_retry(runtime, item)
            finally:
                runtime.send_queue.task_done()

        logger.info("Bot %s 发送任务退出", bot_id)

    async def _send_with_retry(self, runtime: BotRuntime, item: dict) -> None:
        """带随机延迟与限流指数退避重试的发送。

        iLink 限流（ret=-2）窗口较长，5s 单次重试仍会失败，改用指数退避：
        5s → 15s → 30s，最多重试 3 次。每次重试都打印 raw 响应辅助排查。
        """
        to_user = item["to_user"]
        text = item["text"]
        context_token = item["context_token"]

        if not context_token:
            logger.warning(
                "Bot %s 发送给 %s 缺 context_token，跳过（24h 内必须有）",
                runtime.bot_id, to_user,
            )
            return

        # 随机延迟防限流（3-8s）
        await asyncio.sleep(random.uniform(SEND_DELAY_MIN, SEND_DELAY_MAX))

        # 首次发送 + 最多 3 次限流重试
        for attempt in range(len(RATE_LIMIT_RETRY_DELAYS) + 1):
            r = await ilink_client.send_message(
                runtime.bot_token, to_user, text, context_token
            )

            if r["ok"]:
                if attempt > 0:
                    logger.info("Bot %s 发送重试第 %d 次成功",
                                runtime.bot_id, attempt)
                return

            err = r["error"]

            # 会话过期：标记 Bot expired，停止轮询
            if err == ERR_SESSION_EXPIRED:
                logger.warning("Bot %s 发送时会话过期（-14），标记需重新扫码",
                               runtime.bot_id)
                await asyncio.to_thread(self._mark_bot_expired, runtime.bot_id)
                runtime.poll_running = False
                return

            # 限流：指数退避重试
            if err == ERR_RATE_LIMIT:
                if attempt < len(RATE_LIMIT_RETRY_DELAYS):
                    delay = RATE_LIMIT_RETRY_DELAYS[attempt]
                    logger.warning(
                        "Bot %s 触发限流（第 %d 次），%ss 后重试。raw=%s",
                        runtime.bot_id, attempt + 1, delay,
                        str(r.get("raw"))[:200],
                    )
                    await asyncio.sleep(delay)
                    continue
                # 重试次数用尽
                logger.error(
                    "Bot %s 发送重试 %d 次仍限流，放弃。msg=%s raw=%s",
                    runtime.bot_id, attempt, text[:50],
                    str(r.get("raw"))[:300],
                )
                return

            # 其他错误：不重试，直接报错
            logger.error(
                "Bot %s 发送失败 %s: %s。raw=%s",
                runtime.bot_id, err, r.get("message"),
                str(r.get("raw"))[:200],
            )
            return

    # ============================================================
    # 消息处理
    # ============================================================
    async def _process_message(self, runtime: BotRuntime, msg: dict) -> None:
        parsed = _parse_incoming(msg)
        user_wxid = parsed["user_wxid"]
        text = parsed["text"]
        context_token = parsed["context_token"]

        if not user_wxid:
            logger.warning("消息缺少 user_wxid，跳过: %s", msg)
            return

        # 找/建会话（用 database.get_or_create_conversation）
        conv = await asyncio.to_thread(
            get_or_create_conversation, runtime.bot_id, user_wxid
        )
        conv_id = conv.id
        role_name = conv.role_name

        # 刷新 context_token（DB + 内存缓存）
        if context_token:
            await asyncio.to_thread(update_context_token, conv_id, context_token)
            runtime.context_tokens[user_wxid] = context_token

        # 保存入站消息（role=user，与 ai_service.history 格式对齐）
        await asyncio.to_thread(save_message, conv_id, "user", text)

        # /role 命令分支
        if text.startswith("/role"):
            reply = await asyncio.to_thread(
                self._handle_role_command, conv_id, role_name, text
            )
            await asyncio.to_thread(save_message, conv_id, "assistant", reply)
            await runtime.send_queue.put({
                "to_user": user_wxid,
                "text": reply,
                "context_token": context_token,
            })
            return

        # 普通 AI 回复
        reply = await self._generate_ai_reply(conv_id, role_name)
        await asyncio.to_thread(save_message, conv_id, "assistant", reply)
        await runtime.send_queue.put({
            "to_user": user_wxid,
            "text": reply,
            "context_token": context_token,
        })

    async def _generate_ai_reply(
        self, conversation_id: int, role_name: Optional[str]
    ) -> str:
        """组装历史 + 角色配置，调用 AI。

        history 的最后一条（当前用户消息，role=user）拆出作为 user_text 单独传入，
        其余作为 history 喂给 ai_service.chat（5 参数签名）。
        """
        # role_name 兼容 None / "default" → 默认角色
        role = role_manager.get_role(role_name or "default") or role_manager.get_default_role()
        system_prompt = role.get("system_prompt", "") if role else ""
        model = role.get("model") if role else None
        temperature = role.get("temperature") if role else None

        # 取最近 N 条（升序，Message 对象有 .role / .content）
        history_rows = await asyncio.to_thread(
            get_recent_messages, conversation_id, HISTORY_LIMIT
        )

        # 拆出最后一条 user 作为 user_text，其余作为 history
        user_text = ""
        if history_rows and history_rows[-1].role == "user":
            user_text = history_rows[-1].content
            history_rows = history_rows[:-1]

        history_payload = [
            {"role": m.role, "content": m.content} for m in history_rows
        ]

        # ai_service.chat 内部已捕获所有异常并返回友好文本；
        # 这里再加一层兜底，防止极端情况导致整条消息处理中断
        try:
            return await ai_service.chat(
                system_prompt, history_payload, user_text, model, temperature
            )
        except Exception as e:
            logger.exception("AI 调用失败: %s", e)
            return "AI 处理失败，请稍后重试。"

    def _handle_role_command(
        self, conversation_id: int, current_role: Optional[str], text: str
    ) -> str:
        """处理 /role 命令：列出或切换角色。返回回复文本。"""
        parts = text.split(maxsplit=2)
        if len(parts) < 2:
            roles = role_manager.list_roles()
            lines = [f"- {r['id']}: {r['name']}" for r in roles]
            cur = current_role or "default"
            return "可用角色（当前: %s）：\n%s" % (cur, "\n".join(lines))

        role_id = parts[1]
        role = role_manager.get_role(role_id)
        if not role:
            return "角色不存在: %s" % role_id

        with SessionLocal() as db:
            conv = db.get(Conversation, conversation_id)
            if not conv:
                return "会话不存在"
            conv.role_name = role_id
            conv.updated_at = datetime.utcnow()
            db.commit()
        return "已切换到角色: %s" % role["name"]

    # ============================================================
    # DB helper（database.py 未覆盖的 Bot 级操作）
    # ============================================================
    def _get_bot(self, bot_id: int) -> Optional[Bot]:
        with SessionLocal() as db:
            return db.get(Bot, bot_id)

    def _persist_cursor(self, bot_id: int, buf: str) -> None:
        """持久化长轮询游标，崩溃恢复后可续拉。"""
        with SessionLocal() as db:
            bot = db.get(Bot, bot_id)
            if bot:
                bot.get_updates_buf = buf
                db.commit()

    def _mark_bot_expired(self, bot_id: int) -> None:
        """errcode -14 时标记 Bot 需重新扫码。"""
        with SessionLocal() as db:
            bot = db.get(Bot, bot_id)
            if bot:
                bot.status = "expired"
                db.commit()
                logger.warning("Bot %s 已标记为 expired", bot_id)


bot_manager = BotManager()
