"""ORM 模型：bots / conversations / messages / users。

多用户隔离：bots.owner_wxid 标识 Bot 的拥有者（约定为登录用户的 username），
所有列表/操作查询按 owner_wxid 过滤即可隔离不同用户的 Bot，互不可见。

会话约束：bot_id + user_wxid 联合唯一（同一 Bot 对同一对方 wxid 只有一条会话）。
users：控制台账号，username 唯一，role=admin/user，密码用 pbkdf2_hmac 哈希存储。
"""
from datetime import datetime

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from app.database import Base


class Bot(Base):
    """Bot 实例：1 个微信小号 ↔ 1 个 owner_wxid。

    ilink_user_id / ilink_bot_id 为 iLink 平台返回的标识，扫码确认后填入。
    bot_token 是后续请求头的 Bearer token。
    """

    __tablename__ = "bots"

    id = Column(Integer, primary_key=True, index=True)
    bot_token = Column(String(256), nullable=True, comment="扫码确认后获得")
    ilink_user_id = Column(String(64), nullable=True, comment="iLink 平台用户 ID")
    ilink_bot_id = Column(String(64), nullable=True, comment="iLink 平台 Bot ID")
    owner_wxid = Column(
        String(64),
        nullable=False,
        index=True,
        comment="Bot 拥有者 wxid（多用户隔离键）",
    )
    status = Column(
        String(16),
        default="active",
        nullable=False,
        index=True,
        comment="active / expired / disabled",
    )
    get_updates_buf = Column(
        Text,
        nullable=True,
        comment="长轮询游标，崩溃恢复后续拉未消费消息",
    )
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    conversations = relationship(
        "Conversation",
        back_populates="bot",
        cascade="all, delete-orphan",
    )


class Conversation(Base):
    """1v1 私聊会话。bot_id + user_wxid 联合唯一。"""

    __tablename__ = "conversations"
    __table_args__ = (
        UniqueConstraint("bot_id", "user_wxid", name="uq_bot_user"),
    )

    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(
        Integer,
        ForeignKey("bots.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_wxid = Column(String(64), nullable=False)
    role_name = Column(String(64), nullable=True, comment="当前 AI 角色名")
    context_token = Column(
        String(128),
        nullable=True,
        comment="最近入站消息的 context_token，发消息时回传（24h 过期）",
    )
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    bot = relationship("Bot", back_populates="conversations")
    messages = relationship(
        "Message",
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="Message.created_at",
    )


class Message(Base):
    """单条消息。

    role 取值与 OpenAI history 对齐：user（对方发的）/ assistant（Bot 发的），
    这样 get_recent_messages 的结果可直接喂给 ai_service.chat 的 history。
    """

    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, index=True)
    conversation_id = Column(
        Integer,
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role = Column(String(16), nullable=False, comment="user / assistant")
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    conversation = relationship("Conversation", back_populates="messages")


class User(Base):
    """控制台账号。

    username 唯一；password_hash 用 pbkdf2_hmac 哈希（格式: salt_b64$iterations$hash_b64）；
    role: admin（可管理用户/Bot）/ user（普通用户，绑定自己的 Bot）。
    约定 Bot.owner_wxid == User.username，实现多用户隔离。
    """

    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(64), nullable=False, unique=True, index=True)
    password_hash = Column(
        String(256),
        nullable=False,
        comment="pbkdf2_hmac 哈希，格式 salt_b64$iterations$hash_b64",
    )
    role = Column(String(16), nullable=False, default="user", comment="admin / user")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
