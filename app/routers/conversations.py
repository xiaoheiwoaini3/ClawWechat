"""会话与消息查询（基于 OpenClaw sqlite，多用户隔离）。

GET  /?agent_id=xxx[&account_id=yyy]       某 Agent（+ 微信号过滤）的会话列表
GET  /{conversation_id}                    会话详情
GET  /{conversation_id}/messages?agent_id=xxx   某会话消息历史

隔离：所有接口需登录，普通用户只能查自己拥有的 Agent 的会话。
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app import database as db
from app import openclaw_accessor
from app.deps import get_current_user

router = APIRouter()


def _assert_can_access(agent_id: str, user) -> None:
    """普通用户只能访问自己 Agent 的会话；管理员放行。"""
    if user.role == "admin":
        return
    owner = db.get_agent_owner_id(agent_id)
    if owner != user.id:
        raise HTTPException(403, "无权访问该 Agent 的会话")


# ============ 响应模型 ============
class ConversationOut(BaseModel):
    conversation_id: str
    account_id: str
    peer_id: str
    kind: str
    label: Optional[str] = None
    created_at: int
    updated_at: int
    session_id: Optional[str] = None


class MessageOut(BaseModel):
    message_id: str
    role: str
    content: str
    timestamp: float


# ============ 接口 ============
@router.get("/", response_model=list[ConversationOut])
def list_conversations(
    agent_id: str = Query(..., description="OpenClaw Agent ID"),
    account_id: Optional[str] = Query(None),
    user=Depends(get_current_user),
):
    """列出某 Agent 下的所有会话（普通用户仅自己的 Agent）。"""
    _assert_can_access(agent_id, user)
    try:
        convs = openclaw_accessor.list_conversations(agent_id, account_id)
    except FileNotFoundError:
        return []
    except Exception as e:
        raise HTTPException(500, f"读会话失败：{e}")
    return [ConversationOut(**c.to_dict()) for c in convs]


@router.get("/{conversation_id}", response_model=ConversationOut)
def get_conversation(
    conversation_id: str,
    agent_id: str = Query(...),
    user=Depends(get_current_user),
):
    """会话详情。"""
    _assert_can_access(agent_id, user)
    try:
        convs = openclaw_accessor.list_conversations(agent_id)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    for c in convs:
        if c.conversation_id == conversation_id:
            return ConversationOut(**c.to_dict())
    raise HTTPException(404, f"会话不存在: {conversation_id}")


@router.get("/{conversation_id}/messages", response_model=list[MessageOut])
def list_messages(
    conversation_id: str,
    agent_id: str = Query(...),
    limit: int = Query(100, le=500),
    user=Depends(get_current_user),
):
    """某会话的消息历史（按时间升序）。"""
    _assert_can_access(agent_id, user)
    try:
        msgs = openclaw_accessor.list_messages(agent_id, conversation_id, limit=limit)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(500, f"读消息失败：{e}")
    return [MessageOut(**m.to_dict()) for m in msgs]
