"""管理员后台接口（全部需 admin 权限）。

路由前缀：/api/admin
- GET  /users                 所有用户（含状态/角色/创建时间）
- POST /users/{id}/approve    批准
- POST /users/{id}/reject     拒绝
- POST /users/{id}/disable    禁用
- POST /users/{id}/reset-password  重置密码
- GET  /agents/ownership      所有 Agent 归属
- POST /agents/{agent_id}/owner    把 Agent 分配给某用户
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app import database as db
from app.database import (
    get_user_by_id,
    list_all_ownerships,
    list_users,
    set_agent_owner,
    set_user_status,
)
from app.deps import require_admin

router = APIRouter(dependencies=[Depends(require_admin)])


class ResetPasswordIn(BaseModel):
    new_password: str


class AssignOwnerIn(BaseModel):
    user_id: int


@router.get("/users")
def admin_list_users():
    """所有用户（管理员视图）。"""
    users = list_users()
    return [
        {
            "id": u.id,
            "username": u.username,
            "role": u.role,
            "status": getattr(u, "status", "approved"),
            "display_name": getattr(u, "display_name", None),
            "created_at": u.created_at.isoformat(),
        }
        for u in users
    ]


@router.post("/users/{user_id}/approve")
def approve_user(user_id: int):
    u = get_user_by_id(user_id)
    if not u:
        raise HTTPException(404, "用户不存在")
    if u.role == "admin":
        raise HTTPException(400, "管理员账号无需审批")
    set_user_status(user_id, "approved")
    return {"ok": True, "user_id": user_id, "status": "approved"}


@router.post("/users/{user_id}/reject")
def reject_user(user_id: int, admin=Depends(require_admin)):
    u = get_user_by_id(user_id)
    if not u:
        raise HTTPException(404, "用户不存在")
    if u.role == "admin":
        raise HTTPException(400, "不能操作管理员账号")
    set_user_status(user_id, "rejected")
    return {"ok": True, "user_id": user_id, "status": "rejected"}


@router.post("/users/{user_id}/disable")
def disable_user(user_id: int, admin=Depends(require_admin)):
    u = get_user_by_id(user_id)
    if not u:
        raise HTTPException(404, "用户不存在")
    if u.id == admin.id:
        raise HTTPException(400, "不能禁用当前登录的管理员账号")
    if u.role == "admin":
        raise HTTPException(400, "不能禁用其他管理员")
    set_user_status(user_id, "disabled")
    return {"ok": True, "user_id": user_id, "status": "disabled"}


@router.post("/users/{user_id}/reset-password")
def reset_password(user_id: int, payload: ResetPasswordIn, admin=Depends(require_admin)):
    if len(payload.new_password) < 6:
        raise HTTPException(400, "新密码至少 6 位")
    u = get_user_by_id(user_id)
    if not u:
        raise HTTPException(404, "用户不存在")
    db.set_user_password(user_id, payload.new_password)
    return {"ok": True}


@router.get("/agents/ownership")
def admin_list_ownership():
    """所有 Agent 归属（管理员视图）。"""
    return list_all_ownerships()


@router.post("/agents/{agent_id}/owner")
def admin_assign_owner(agent_id: str, payload: AssignOwnerIn):
    """把 Agent 分配给指定用户。"""
    u = get_user_by_id(payload.user_id)
    if not u:
        raise HTTPException(404, "目标用户不存在")
    if u.status != "approved":
        raise HTTPException(400, "目标用户未批准，不能分配")
    set_agent_owner(agent_id, payload.user_id)
    return {"ok": True, "agent_id": agent_id, "owner_user_id": payload.user_id}
