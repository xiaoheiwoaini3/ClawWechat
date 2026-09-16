"""全局配置：从 .env 读取，集中管理可调参数。

设计原则：
- 所有运行期配置统一从 Settings 取，避免散落各处硬编码。
- 任何环境差异通过 .env 切换，代码不动。
"""
import os
from dataclasses import dataclass

from dotenv import load_dotenv

# 加载 .env（找不到则跳过，使用系统环境变量或默认值）
load_dotenv()


def _get_int(key: str, default: int) -> int:
    """读取整型环境变量，非法值回退默认值。"""
    try:
        return int(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Settings:
    # ===== 数据库 =====
    # 开发：sqlite:///./ilink_bot.db；生产：postgresql+psycopg://user:pass@host/db
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///./ilink_bot.db")

    # ===== AI（OpenAI 兼容）=====
    AI_API_KEY: str = os.getenv("AI_API_KEY", "")
    AI_BASE_URL: str = os.getenv("AI_BASE_URL", "https://api.openai.com/v1")
    AI_MODEL: str = os.getenv("AI_MODEL", "gpt-4o-mini")

    # ===== iLink Bot 官方 API =====
    ILINK_BASE_URL: str = os.getenv("ILINK_BASE_URL", "https://ilinkai.weixin.qq.com")

    # ===== 长轮询 / 限流 =====
    LONG_POLL_TIMEOUT: int = _get_int("LONG_POLL_TIMEOUT", 40)
    SEND_INTERVAL_MS: int = _get_int("SEND_INTERVAL_MS", 500)


settings = Settings()
