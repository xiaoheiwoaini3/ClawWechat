"""用户管理：数据库持久化 + HMAC token 鉴权。

接口：
- POST /init          首次初始化管理员（无鉴权，仅首次可用）
- POST /login         用户名密码登录，返回签名 token
- POST /users         创建用户（需 admin）
- GET  /users         列出用户（需 admin）
- DELETE /users/{id}  删除用户（需 admin）

鉴权：Authorization: Bearer <token>，token 由 make_token 生成（HMAC 签名 + 24h 过期）。
密码：pbkdf2_hmac 哈希存储，verify_password 校验。
"""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict

from app.database import (
    create_user as db_create_user,
    delete_user as db_delete_user,
    get_user_by_username,
    init_admin_if_absent,
    list_users as db_list_users,
    make_token,
    parse_token,
    verify_password,
)

router = APIRouter()


# ============ 请求/响应模型 ============
class LoginIn(BaseModel):
    """登录请求。"""
    username: str
    password: str


class LoginOut(BaseModel):
    """登录结果。"""
    ok: bool
    token: Optional[str] = None
    role: Optional[str] = None
    username: Optional[str] = None


class UserCreateIn(BaseModel):
    """创建用户请求。"""
    username: str
    password: str
    role: str = "user"  # admin / user


class UserOut(BaseModel):
    """用户信息（不含密码哈希）。"""
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    role: str
    created_at: datetime


class InitOut(BaseModel):
    """初始化结果。"""
    ok: bool
    created: bool
    message: str


# ============ 鉴权依赖 ============
def require_admin(authorization: Optional[str] = Header(None)) -> str:
    """校验 Bearer token，要求是 admin 用户。返回 username。"""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "缺少 Authorization: Bearer <token>")
    token = authorization.removeprefix("Bearer ").strip()
    username = parse_token(token)
    if not username:
        raise HTTPException(401, "token 无效或已过期")
    user = get_user_by_username(username)
    if not user:
        raise HTTPException(401, "用户不存在")
    if user.role != "admin":
        raise HTTPException(403, "需要管理员权限")
    return username


# ============ 接口 ============
@router.post("/init", response_model=InitOut)
def init_admin():
    """初始化管理员账号（读 .env 的 ADMIN_USERNAME/ADMIN_PASSWORD）。

    无鉴权：仅用于首次部署创建管理员。已存在 admin 时跳过。
    生产环境初始化后建议限制该接口（如限内网或下线）。
    """
    try:
        result = init_admin_if_absent()
        return InitOut(**result)
    except Exception as e:
        raise HTTPException(500, "初始化管理员失败: %s" % e)


@router.post("/login", response_model=LoginOut)
def login(payload: LoginIn):
    """用户名密码登录，校验通过返回签名 token。"""
    user = get_user_by_username(payload.username)
    if not user or not verify_password(payload.password, user.password_hash):
        # 统一返回 ok=False，不暴露是用户名还是密码错
        return LoginOut(ok=False)
    return LoginOut(
        ok=True,
        token=make_token(user.username),
        role=user.role,
        username=user.username,
    )


@router.post("/users", response_model=UserOut, status_code=201)
def create_user(
    payload: UserCreateIn,
    _admin: str = Depends(require_admin),
):
    """创建用户（需 admin）。"""
    # role 校验：只允许 admin/user
    if payload.role not in ("admin", "user"):
        raise HTTPException(400, "role 必须是 admin 或 user")
    try:
        user = db_create_user(payload.username, payload.password, payload.role)
    except ValueError as e:
        # username 重复
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, "创建用户失败: %s" % e)
    return user


@router.get("/users", response_model=list[UserOut], dependencies=[Depends(require_admin)])
def list_users():
    """列出所有用户（需 admin）。"""
    return db_list_users()


@router.delete("/users/{user_id}", status_code=204)
def delete_user(user_id: int, admin: str = Depends(require_admin)):
    """删除用户（需 admin）。禁止删除自己。"""
    # 防止管理员删除自己
    admin_user = get_user_by_username(admin)
    if admin_user and admin_user.id == user_id:
        raise HTTPException(400, "不能删除当前登录的管理员账号")
    ok = db_delete_user(user_id)
    if not ok:
        raise HTTPException(404, "用户不存在")
    return None
