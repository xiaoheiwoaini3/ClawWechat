"""AI 调用层：OpenAI 兼容格式。

使用 openai SDK 的 AsyncOpenAI 客户端，兼容 OpenAI / DeepSeek / 智谱 / 通义等。
chat() 签名：chat(system_prompt, history, user_text, model, temperature) -> str

异常处理：超时 / 限流 / 无效 Key / 连接失败 / 其他 API 错误均返回友好文本，不向调用方抛异常。
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx
import openai
from openai import AsyncOpenAI

from app.config import settings

logger = logging.getLogger(__name__)

# 默认参数
DEFAULT_TEMPERATURE = 0.7
DEFAULT_MAX_TOKENS = 1024
REQUEST_TIMEOUT = 60.0


class AIService:
    """OpenAI 兼容 AI 服务。"""

    def __init__(self) -> None:
        self._client: Optional[AsyncOpenAI] = None

    def _get_client(self) -> AsyncOpenAI:
        """懒加载 AsyncOpenAI 客户端。"""
        if self._client is None:
            self._client = AsyncOpenAI(
                api_key=settings.AI_API_KEY or "missing",
                base_url=settings.AI_BASE_URL,
                timeout=REQUEST_TIMEOUT,
            )
        return self._client

    def _build_messages(
        self,
        system_prompt: str,
        history: list[dict],
        user_text: str,
    ) -> list[dict]:
        """组装 OpenAI messages：system + history + 当前 user_text。

        - system_prompt 为空则不插 system 消息
        - history 只保留 role∈{user, assistant} 的项
        - user_text 非空时追加为最后一条 user 消息
        """
        messages: list[dict] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})

        for m in history or []:
            role = m.get("role")
            content = m.get("content")
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": str(content)})

        if user_text:
            messages.append({"role": "user", "content": str(user_text)})
        return messages

    async def chat(
        self,
        system_prompt: str,
        history: list[dict],
        user_text: str,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
    ) -> str:
        """多轮对话，返回 assistant 文本。失败返回友好错误文本。

        参数:
            system_prompt: 角色 system prompt
            history: 历史 [{"role": "user"/"assistant", "content": "..."}]（不含当前消息）
            user_text: 当前用户消息（与 history 分开传入）
            model: 模型名，回退到 settings.AI_MODEL
            temperature: 采样温度，回退到 0.7
        """
        # 未配置 Key：直接返回提示，不发请求
        if not settings.AI_API_KEY:
            return "AI 服务未配置 API Key，请联系管理员。"

        model = model or settings.AI_MODEL
        temperature = temperature if temperature is not None else DEFAULT_TEMPERATURE

        messages = self._build_messages(system_prompt, history, user_text)

        # 无任何 user 消息则不调用（避免空请求报错）
        if not any(m["role"] == "user" for m in messages):
            return ""

        client = self._get_client()
        try:
            resp = await client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=DEFAULT_MAX_TOKENS,
            )
            # OpenAI 标准响应：choices[0].message.content
            return (resp.choices[0].message.content or "").strip()
        except openai.APITimeoutError:
            logger.error("AI 请求超时（%ss）", REQUEST_TIMEOUT)
            return "AI 响应超时，请稍后重试。"
        except openai.RateLimitError:
            logger.error("AI 触发限流")
            return "AI 服务繁忙，请稍后重试。"
        except openai.AuthenticationError:
            logger.error("AI API Key 无效")
            return "AI 服务认证失败，请联系管理员检查 API Key。"
        except openai.APIConnectionError as e:
            logger.error("AI 连接失败: %s", e)
            return "AI 服务连接失败，请稍后重试。"
        except openai.APIError as e:
            logger.exception("AI API 错误: %s", e)
            return "AI 服务异常，请稍后重试。"
        except Exception as e:
            logger.exception("AI 未知异常: %s", e)
            return "AI 处理失败，请稍后重试。"


ai_service = AIService()
