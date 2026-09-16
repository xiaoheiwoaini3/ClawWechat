"""FastAPI 入口：OpenClaw 控制台。

新架构：iLink 协议由 OpenClaw 网关接管，本服务只做配置/读取面板。
- 启动时建表（仅用户登录表）
- 路由：users / bots / roles / conversations
"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.database import init_db
from app.routers import bots, conversations, roles, users

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：建表。OpenClaw 网关自行管长轮询，无需本服务启动。"""
    logger.info("OpenClaw 控制台启动中")
    init_db()
    yield
    logger.info("OpenClaw 控制台已关闭")


app = FastAPI(title="OpenClaw 控制台", version="0.4.0", lifespan=lifespan)

# 静态文件目录：项目根/static，前端 SPA 位于 static/index.html
static_dir = Path(__file__).resolve().parent.parent / "static"
app.mount(
    "/static",
    StaticFiles(directory=static_dir),
    name="static",
)


@app.get("/", include_in_schema=False)
async def serve_spa():
    """根路径返回前端单页应用。

    强制 no-cache：前端 JS 频繁迭代（扫码二维码刷新跟随等），
    浏览器若缓存旧版 index.html 会导致用户扫到过期二维码（微信报网络错误）。
    """
    resp = FileResponse(static_dir / "index.html")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


# CORS（开发全开，生产收紧）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(users.router, prefix="/api/users", tags=["users"])
app.include_router(bots.router, prefix="/api/bots", tags=["bots"])
app.include_router(roles.router, prefix="/api/roles", tags=["roles"])
app.include_router(conversations.router, prefix="/api/conversations", tags=["conversations"])


@app.get("/health", tags=["meta"])
def health():
    """健康检查。"""
    return {"status": "ok"}
