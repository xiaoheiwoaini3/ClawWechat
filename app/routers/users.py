"""用户认证与自助接口。

路由前缀：/api/users
- POST /setup-admin    首次初始化管理员（无 admin 时可用，引导页用）
- GET  /setup-status   是否已初始化管理员（前端决定显示引导页还是登录页）
- POST /register       用户自助注册（pending，待审批）
- POST /login          登录（校验密码 + 状态）
- GET  /me             当前登录用户信息（含 role/status）
- POST /change-password  修改自己的密码（可选）

管理员操作见 app/routers/admin.py（/api/admin/*）。
"""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict

from app import database as db
from app.database import (
    get_user_by_username,
    has_any_admin,
    make_token,
    parse_token,
    register_user,
    setup_first_admin,
    verify_password,
)
from app.deps import get_current_user

router = APIRouter()


# ============ 请求/响应模型 ============
class LoginIn(BaseModel):
    username: str
    password: str


class LoginOut(BaseModel):
    ok: bool
    token: Optional[str] = None
    role: Optional[str] = None
    username: Optional[str] = None
    status: Optional[str] = None
    message: Optional[str] = None


class RegisterIn(BaseModel):
    username: str
    password: str
    display_name: str = ""


class SetupAdminIn(BaseModel):
    username: str
    password: str


class MeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    username: str
    role: str
    status: str
    display_name: Optional[str] = None
    ai_config: Optional[str] = None
    created_at: datetime


class ChangePasswordIn(BaseModel):
    old_password: str
    new_password: str


# ============ 接口 ============
@router.get("/setup-status")
def setup_status():
    """前端决定显示引导页还是登录页：是否已有管理员。"""
    return {"has_admin": has_any_admin()}


@router.post("/setup-admin")
def setup_admin(payload: SetupAdminIn):
    """首次初始化管理员（仅无 admin 时可用）。"""
    if has_any_admin():
        raise HTTPException(400, "管理员已初始化，不能重复设置")
    if len(payload.username) < 2 or len(payload.password) < 6:
        raise HTTPException(400, "用户名至少 2 位，密码至少 6 位")
    try:
        u = setup_first_admin(payload.username, payload.password)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "username": u.username, "token": make_token(u.username)}


@router.post("/register")
def register(payload: RegisterIn):
    """用户自助注册：创建 pending 账号，等待管理员审批。"""
    if len(payload.username) < 2 or len(payload.password) < 6:
        raise HTTPException(400, "用户名至少 2 位，密码至少 6 位")
    try:
        u = register_user(payload.username, payload.password, payload.display_name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "message": "注册成功，请等待管理员审批", "username": u.username}


@router.post("/login", response_model=LoginOut)
def login(payload: LoginIn):
    """用户名密码登录，校验通过返回 token。未批准/被拒/被禁用都拒绝登录。"""
    user = get_user_by_username(payload.username)
    if not user or not verify_password(payload.password, user.password_hash):
        return LoginOut(ok=False, message="用户名或密码错误")
    # 状态拦截
    if user.status == "pending":
        return LoginOut(ok=False, status=user.status, message="账号待管理员审批")
    if user.status == "rejected":
        return LoginOut(ok=False, status=user.status, message="账号申请已被拒绝")
    if user.status == "disabled":
        return LoginOut(ok=False, status=user.status, message="账号已被禁用")
    # approved
    return LoginOut(
        ok=True,
        token=make_token(user.username),
        role=user.role,
        username=user.username,
        status=user.status,
    )


@router.get("/me", response_model=MeOut)
def me(user=Depends(get_current_user)):
    """当前登录用户信息。"""
    return user


@router.post("/change-password")
def change_password(payload: ChangePasswordIn, user=Depends(get_current_user)):
    """修改自己的密码。"""
    if not verify_password(payload.old_password, user.password_hash):
        raise HTTPException(400, "原密码错误")
    if len(payload.new_password) < 6:
        raise HTTPException(400, "新密码至少 6 位")
    db.set_user_password(user.id, payload.new_password)
    return {"ok": True}
