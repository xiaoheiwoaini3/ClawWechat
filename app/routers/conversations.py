"""会话与消息查询（基于 OpenClaw sqlite）。

GET  /?agent_id=xxx[&account_id=yyy]       某 Agent（+ 微信号过滤）的会话列表
GET  /{conversation_id}                    会话详情
GET  /{conversation_id}/messages?agent_id=xxx   某会话消息历史
"""
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app import openclaw_accessor

router = APIRouter()


# ============ 响应模型 ============
class ConversationOut(BaseModel):
    conversation_id: str
    account_id: str
    peer_id: str
    kind: str
    label: Optional[str] = None
    created_at: int  # ms
    updated_at: int  # ms
    session_id: Optional[str] = None


class MessageOut(BaseModel):
    message_id: str
    role: str
    content: str
    timestamp: float  # ms


# ============ 接口 ============
@router.get("/", response_model=list[ConversationOut])
def list_conversations(
    agent_id: str = Query(..., description="OpenClaw Agent ID, 如 main / friend1"),
    account_id: Optional[str] = Query(
        None, description="可选：按微信 accountId 过滤只看某微信号收到的会话"
    ),
):
    """列出某 Agent 下的所有会话。

    数据源：~/.openclaw/agents/<agent_id>/agent/openclaw-agent.sqlite 的 conversations 表。
    按 updated_at 倒序返回（最近活跃的会话在最前）。
    """
    try:
        convs = openclaw_accessor.list_conversations(agent_id, account_id)
    except FileNotFoundError:
        # Agent 刚创建、sqlite 尚未建立（gateway 首次收到/发出消息才建库）。
        # 此时不是错误，返回空列表，前端显示「暂无会话」。
        return []
    except Exception as e:
        raise HTTPException(500, f"读会话失败：{e}")

    return [ConversationOut(**c.to_dict()) for c in convs]


@router.get("/{conversation_id}", response_model=ConversationOut)
def get_conversation(
    conversation_id: str,
    agent_id: str = Query(..., description="Agent ID"),
):
    """会话详情。"""
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
    agent_id: str = Query(..., description="Agent ID, 用于定位 sqlite 文件"),
    limit: int = Query(100, le=500),
):
    """某会话的消息历史（按时间升序）。

    数据源：session_transcript_fts_content 表，按 session_id 查。
    若 conversation_id 对应不到 session_id，回退到 transcript_events 表。
    """
    try:
        msgs = openclaw_accessor.list_messages(agent_id, conversation_id, limit=limit)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(500, f"读消息失败：{e}")

    return [MessageOut(**m.to_dict()) for m in msgs]
