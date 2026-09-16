"""Pydantic 请求/响应模型。

字段命名与 ORM 对齐，from_attributes=True 支持直接从 ORM 实例序列化。
"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


# ===== 通用 =====
class BaseOut(BaseModel):
    """统一开启 ORM 实例 -> Pydantic 的转换。"""
    model_config = ConfigDict(from_attributes=True)


# ===== Bot =====
class BotCreate(BaseModel):
    """创建 Bot（仅记录 owner，扫码前 status=pending）。"""
    owner_user_id: str


class BotOut(BaseOut):
    id: int
    owner_user_id: str
    bot_wxid: Optional[str] = None
    nickname: Optional[str] = None
    status: str
    created_at: datetime
    updated_at: datetime


# ===== Conversation =====
class ConversationOut(BaseOut):
    id: int
    bot_id: int
    user_wxid: str
    user_nickname: Optional[str] = None
    current_role_id: Optional[str] = None
    last_message_at: Optional[datetime] = None
    created_at: datetime


# ===== Message =====
class MessageOut(BaseOut):
    id: int
    conversation_id: int
    direction: str
    text: str
    context_token: Optional[str] = None
    created_at: datetime


# ===== Role =====
class Role(BaseModel):
    """角色配置（与 roles.json 对齐）。"""
    id: str
    name: str
    system_prompt: str
    model: Optional[str] = None


class RoleSwitch(BaseModel):
    """切换会话当前角色。"""
    role_id: str
