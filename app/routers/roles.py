"""角色管理：列出所有 Agent 的 SOUL.md（兼容旧 /api/roles/ 路由）。

新架构：每个 OpenClaw Agent 有自己的 SOUL.md，不再用统一 roles.json。
- GET / → 列出所有 Agent 的 soul.md 内容（前端编辑器用）
- PUT /{agent_id} → 更新某 Agent 的 SOUL.md（转发到 openclaw_accessor）
"""
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app import openclaw_accessor

logger = logging.getLogger(__name__)

router = APIRouter()


# ============ 响应模型 ============
class AgentSoulOut(BaseModel):
    """Agent + 其 SOUL.md 内容。"""
    agent_id: str
    name: str
    workspace: str
    content: str  # SOUL.md 完整内容


class SoulUpdateIn(BaseModel):
    """更新 SOUL.md 请求。"""
    content: str


# ============ 接口 ============
@router.get("/", response_model=list[AgentSoulOut])
def list_souls():
    """列出所有 Agent 的 SOUL.md 内容。

    前端可以拿这个列表渲染多 tab 编辑器，每个 tab 一个 Agent 的 soul。
    """
    result: list[AgentSoulOut] = []
    for a in openclaw_accessor.list_agents():
        content = openclaw_accessor.read_soul_md(a.agent_id) or ""
        result.append(
            AgentSoulOut(
                agent_id=a.agent_id,
                name=a.name,
                workspace=a.workspace,
                content=content,
            )
        )
    return result


@router.put("/{agent_id}")
def update_soul(agent_id: str, payload: SoulUpdateIn):
    """更新指定 Agent 的 SOUL.md。"""
    try:
        openclaw_accessor.write_soul_md(agent_id, payload.content)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"agent_id": agent_id, "saved": True, "length": len(payload.content)}


@router.get("/{agent_id}")
def get_soul(agent_id: str):
    """获取指定 Agent 的 SOUL.md。"""
    content = openclaw_accessor.read_soul_md(agent_id)
    if content is None:
        raise HTTPException(404, f"Agent not found: {agent_id}")
    return {"agent_id": agent_id, "content": content}
