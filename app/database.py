"""SQLAlchemy 引擎、会话工厂与数据访问函数。

数据访问函数：
- 会话/消息：get_or_create_conversation / save_message / update_context_token / get_recent_messages
- 用户：create_user / get_user_by_username / list_users / delete_user / init_admin_if_absent
- 安全：hash_password / verify_password / make_token / parse_token

返回的对象为 detached（session 已关闭），可读已加载的列属性，
但不要访问关系属性（会触发懒加载报错）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from app.config import settings

if TYPE_CHECKING:
    from app.models import Conversation, Message

# SQLite 需要 check_same_thread=False，FastAPI 多线程下才能共享连接
_connect_args = (
    {"check_same_thread": False}
    if settings.DATABASE_URL.startswith("sqlite")
    else {}
)

engine = create_engine(
    settings.DATABASE_URL,
    connect_args=_connect_args,
    pool_pre_ping=True,
    future=True,
)

SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    future=True,
)

# 所有 ORM 模型的基类
Base = declarative_base()


def get_db():
    """FastAPI 依赖：每个请求获取独立 DB session，请求结束自动关闭。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """启动时建表（开发用，生产应使用 Alembic 迁移）。"""
    # 延迟 import 触发模型注册，避免循环引用
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)


# ============================================================
# 数据访问函数
# ============================================================
def get_or_create_conversation(bot_id: int, user_wxid: str) -> Conversation:
    """找或建会话。返回 detached Conversation 对象（已 commit）。

    依赖 conversations 表的 UNIQUE(bot_id, user_wxid) 约束：
    同一 Bot 对同一 user_wxid 只有一条会话记录。
    """
    from app.models import Conversation

    with SessionLocal() as db:
        conv = (
            db.query(Conversation)
            .filter(
                Conversation.bot_id == bot_id,
                Conversation.user_wxid == user_wxid,
            )
            .first()
        )
        if conv is None:
            conv = Conversation(bot_id=bot_id, user_wxid=user_wxid)
            db.add(conv)
            db.commit()
            db.refresh(conv)
        return conv


def save_message(conversation_id: int, role: str, content: str) -> Message:
    """保存一条消息。返回 detached Message 对象。

    role: "user"（对方发的） / "assistant"（Bot 发的），
    与 ai_service.history 格式对齐。
    """
    from app.models import Message

    with SessionLocal() as db:
        msg = Message(
            conversation_id=conversation_id,
            role=role,
            content=content,
            created_at=datetime.utcnow(),
        )
        db.add(msg)
        db.commit()
        db.refresh(msg)
        return msg


def update_context_token(conversation_id: int, token: str) -> None:
    """更新会话最近 context_token 与 updated_at。

    iLink 要求发消息时回传收到消息时的 context_token（24h 过期），
    每次收到入站消息后应调用本函数刷新。
    """
    from app.models import Conversation

    with SessionLocal() as db:
        conv = db.get(Conversation, conversation_id)
        if conv is not None:
            conv.context_token = token
            conv.updated_at = datetime.utcnow()
            db.commit()


def get_recent_messages(conversation_id: int, limit: int = 20) -> list[Message]:
    """取最近 N 条消息，按时间升序返回（便于直接拼成 history 喂给 AI）。"""
    from app.models import Message

    with SessionLocal() as db:
        rows = (
            db.query(Message)
            .filter(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.desc())
            .limit(limit)
            .all()
        )
        # 数据库按 desc 取最近 N 条，再反转成升序
        return list(reversed(rows))


# ============================================================
# 安全：密码哈希 + Token 签名（标准库实现，无第三方依赖）
# ============================================================
# pbkdf2 迭代次数（OWASP 推荐 ≥ 100k）
_PBKDF2_ITERATIONS = 100_000
# token 签名密钥（从 .env 读，务必在生产修改）
_TOKEN_SECRET = os.getenv("JWT_SECRET", "change-me-in-production")
# token 有效期（秒）
_TOKEN_TTL = 86400


def hash_password(password: str) -> str:
    """对密码做 pbkdf2_hmac 哈希。返回 salt_b64$iterations$hash_b64。"""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return "%s$%d$%s" % (
        base64.b64encode(salt).decode(),
        _PBKDF2_ITERATIONS,
        base64.b64encode(dk).decode(),
    )


def verify_password(password: str, stored: str) -> bool:
    """校验密码与存储的哈希是否匹配。"""
    try:
        salt_b64, iter_str, hash_b64 = stored.split("$")
        salt = base64.b64decode(salt_b64)
        iterations = int(iter_str)
        expected = base64.b64decode(hash_b64)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        # 常量时间比较，防时序攻击
        return secrets.compare_digest(dk, expected)
    except (ValueError, AttributeError, TypeError):
        return False


def _sign(payload: str) -> str:
    """HMAC-SHA256 签名。"""
    return hmac.new(_TOKEN_SECRET.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def make_token(username: str) -> str:
    """生成登录 token：base64(payload).hmac_sig。"""
    payload = base64.b64encode(
        json.dumps({"u": username, "t": int(time.time())}).encode("utf-8")
    ).decode()
    return payload + "." + _sign(payload)


def parse_token(token: str) -> str | None:
    """解析并验签 token。返回 username，失败/过期返回 None。"""
    try:
        payload_b64, sig = token.split(".")
        # 验签
        if not hmac.compare_digest(_sign(payload_b64), sig):
            return None
        data = json.loads(base64.b64decode(payload_b64))
        # 过期检查
        if time.time() - data["t"] > _TOKEN_TTL:
            return None
        return data["u"]
    except (ValueError, KeyError, json.JSONDecodeError):
        return None


# ============================================================
# 用户 CRUD
# ============================================================
def create_user(username: str, password: str, role: str = "user") -> User:
    """创建用户。username 重复抛 ValueError。返回 detached User 对象。"""
    from app.models import User

    with SessionLocal() as db:
        existing = db.query(User).filter(User.username == username).first()
        if existing:
            raise ValueError("用户名 %s 已存在" % username)
        user = User(
            username=username,
            password_hash=hash_password(password),
            role=role,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return user


def get_user_by_username(username: str) -> User | None:
    """按 username 查询用户。返回 detached User 或 None。"""
    from app.models import User

    with SessionLocal() as db:
        return (
            db.query(User)
            .filter(User.username == username)
            .first()
        )


def list_users() -> list[User]:
    """列出所有用户（按创建时间升序）。"""
    from app.models import User

    with SessionLocal() as db:
        return db.query(User).order_by(User.created_at.asc()).all()


def delete_user(user_id: int) -> bool:
    """按 id 删除用户。成功返回 True，不存在返回 False。"""
    from app.models import User

    with SessionLocal() as db:
        user = db.get(User, user_id)
        if not user:
            return False
        db.delete(user)
        db.commit()
        return True


def init_admin_if_absent() -> dict:
    """若无 admin 用户，用 .env 配置创建一个。

    读 ADMIN_USERNAME（默认 admin）/ ADMIN_PASSWORD（默认 admin）。
    已存在 admin 则跳过。返回操作信息。
    """
    admin_username = os.getenv("ADMIN_USERNAME", "admin")
    admin_password = os.getenv("ADMIN_PASSWORD", "admin")
    with SessionLocal() as db:
        from app.models import User

        existing = db.query(User).filter(User.role == "admin").first()
        if existing:
            return {
                "ok": True,
                "created": False,
                "message": "管理员已存在: %s" % existing.username,
            }
        user = User(
            username=admin_username,
            password_hash=hash_password(admin_password),
            role="admin",
        )
        db.add(user)
        db.commit()
        return {
            "ok": True,
            "created": True,
            "message": "已创建管理员: %s" % admin_username,
        }
