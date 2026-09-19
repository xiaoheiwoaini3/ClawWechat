"""FastAPI 鉴权依赖：当前登录用户 / 管理员。

用法：
    from app.deps import get_current_user, require_admin
    @router.get("/me")
    def me(user: User = Depends(get_current_user)): ...

token 由 Authorization: Bearer <token> 传入，
token 载荷只有 username，这里再查库补全 User 对象（含 id/role/status）。
"""
from typing import Optional

from fastapi import Depends, Header, HTTPException

from app.database import get_user_by_username, parse_token


def get_current_user(authorization: Optional[str] = Header(None)):
    """解析 Bearer token，返回已批准的 User 对象。

    - 缺 token / 签名无效 / 过期 → 401
    - 用户不存在 → 401
    - 用户未批准（pending/rejected/disabled）→ 403 提示原因
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "未登录")
    token = authorization.removeprefix("Bearer ").strip()
    username = parse_token(token)
    if not username:
        raise HTTPException(401, "登录已过期，请重新登录")
    user = get_user_by_username(username)
    if not user:
        raise HTTPException(401, "用户不存在")
    if user.status == "pending":
        raise HTTPException(403, "账号待管理员审批，请耐心等待")
    if user.status == "rejected":
        raise HTTPException(403, "账号申请已被拒绝")
    if user.status == "disabled":
        raise HTTPException(403, "账号已被禁用，请联系管理员")
    # approved
    return user


def require_admin(user=Depends(get_current_user)):
    """要求当前用户是管理员。返回 User 对象。"""
    if user.role != "admin":
        raise HTTPException(403, "需要管理员权限")
    return user
