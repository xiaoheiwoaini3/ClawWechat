# OpenClaw 微信 Bot 控制台

基于 **OpenClaw** 的微信 AI Bot 管理面板：在一个 Web 界面里创建 Agent、扫码绑定微信账号、查看/管理消息会话。后端 FastAPI + SQLite，前端单页（原生 HTML/JS）。

## 功能

- **Agent 管理**：创建、删除 Agent（配置自动写入 OpenClaw `openclaw.json`）
- **微信扫码绑定**：获取二维码 → 微信扫码 → 自动检测新账号并绑定到 Agent
- **绑定已有账号**：已登录的微信账号可直接绑定到新 Agent，无需重复扫码
- **会话查看**：按 Agent 查看微信会话与消息（SQLite WAL 实时读取）
- **消息轮询**：前端 3 秒自动刷新消息列表
- **账号备注**：给微信号设置可读备注（如"我的主号"），避免只见 openid
- **网关防抖重启**：绑定/删除操作异步合并重启 OpenClaw 网关，不阻塞界面、减少服务中断

## 架构

```
浏览器 (static/index.html)
    │  HTTP (REST)
    ▼
FastAPI (app/main.py + app/routers/)
    ├── app/openclaw_accessor.py   ← 读写 OpenClaw 配置 / 会话 SQLite
    ├── app/openclaw_cli.py       ← 调用 openclaw CLI（扫码、Agent、网关）
    └── app/ilink_client.py       ← iLink 微信 Bot API 客户端（可选直连）
    ▼
OpenClaw 网关 (端口 18789)  ←→ 微信 iLink 服务
```

## 目录结构

```
├── app/
│   ├── main.py               # FastAPI 入口（根路由 no-cache）
│   ├── config.py             # 环境变量配置
│   ├── database.py           # SQLite 会话/用户数据
│   ├── models.py / schemas.py
│   ├── openclaw_accessor.py  # OpenClaw 配置与会话库访问
│   ├── openclaw_cli.py       # OpenClaw CLI 封装（扫码/绑定/网关/防抖重启）
│   └── routers/
│       ├── bots.py           # Agent / 绑定 / 扫码接口
│       ├── conversations.py  # 会话消息接口
│       ├── users.py          # 登录认证
│       └── roles.py          # 角色管理
├── static/
│   └── index.html            # 单页控制台（v2.x）
├── .env.example              # 环境变量模板
├── requirements.txt
└── ilink_bot.db              # 本地 SQLite（运行生成，不提交）
```

## 快速开始

```bash
# 1. 创建虚拟环境并安装依赖
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt

# 2. 配置环境变量
copy .env.example .env          # Windows
# 编辑 .env，填入 AI_API_KEY 等

# 3. 启动控制台
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# 4. 浏览器打开
# http://127.0.0.1:8000/
```

## 环境变量

见 [`.env.example`](.env.example)：

| 变量 | 说明 | 默认 |
|---|---|---|
| `DATABASE_URL` | 数据库连接（默认 SQLite） | `sqlite:///./ilink_bot.db` |
| `AI_API_KEY` | AI 服务密钥（OpenAI 兼容） | 空 |
| `AI_BASE_URL` | AI 服务地址 | `https://api.openai.com/v1` |
| `AI_MODEL` | 默认模型 | `gpt-4o-mini` |
| `ILINK_BASE_URL` | iLink 微信 Bot API 地址 | `https://ilinkai.weixin.qq.com` |
| `LONG_POLL_TIMEOUT` | 长轮询超时（秒） | `40` |
| `SEND_INTERVAL_MS` | 消息发送间隔（防限流） | `500` |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | 控制台管理员账号 | `admin` / `admin` |

> ⚠️ 首次部署请修改管理员默认密码；`.env` 已被 `.gitignore` 排除，不会提交。

## OpenClaw 依赖

本项目通过 CLI 驱动 OpenClaw（`openclaw` 命令需在 PATH 中）：

- 扫码登录：`openclaw channels login --channel openclaw-weixin`
- 网关：端口 18789（Windows 可注册计划任务 `OpenClaw Gateway` 自启）
- 配置根：`~/.openclaw/`（`openclaw.json`、`openclaw-weixin/accounts.json`）
- 会话库：`~/.openclaw/agents/<id>/agent/openclaw-agent.sqlite`

绑定/删除 Agent 后需网关重启才生效（本项目已实现异步防抖自动重启，通常无需手动干预）。

## 常见问题

- **扫码显示"网络错误"**：二维码约 30 秒过期，请用最新二维码；确保链接包含 `&bot_type=3` 参数。
- **消息不更新**：确认网关运行中（`http://127.0.0.1:18789/health`），页面会自动轮询。
- **删除 Agent 后仍收到旧回复**：删除会清理会话库并重启网关，等待约 1-2 分钟生效。

## 许可

MIT License（如需商用请自行评估依赖组件许可）。
