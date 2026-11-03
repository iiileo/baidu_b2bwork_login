# B2BWork Login

单 worker + FastAPI：异步登录队列、人工验证码/扫码、签名 webhook，以及带 **VNC 内嵌** 的 Web 控制台。

## 本地启动

```bash
uv sync
uv run playwright install chromium
cp .env.example .env

# 终端 1
uv run python login_worker.py

# 终端 2
uv run python main.py
```

- 控制台：<http://127.0.0.1:8056/ui/>
- 接口文档：<http://127.0.0.1:8056/docs.html>
- 默认 Basic：`admin` / `admin`（`.env` 的 `BASIC_AUTH_*`）
- 程序调用仍可用 `Authorization: Bearer <API_TOKEN>`

## 鉴权说明

| 场景 | 方式 |
|------|------|
| 控制台 `/ui`、VNC 代理 `/vnc` | HTTP Basic（`BASIC_AUTH_USER` / `BASIC_AUTH_PASSWORD`） |
| `/v1/*` API | Bearer **或** Basic |
| `/health`、`/docs.html` | 公开 |

启用 Basic 后，VNC 经同源 `/vnc` 代理访问，x11vnc 无需密码；不要对外映射 7900。

## 控制台能力

- 提交测试登录（创建 `/v1/sessions`）
- 轮询会话状态；验证码提交；安全扫码展示 `qr_content`
- 右侧 iframe 嵌入 VNC（默认 `/vnc/...`）
- 实时日志 WebSocket
- 静默模式：遇人工挑战直接失败并继续下一个

## API 摘要

完整说明见 [`/docs.html`](http://127.0.0.1:8056/docs.html)。

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/v1/sessions` | 创建登录会话 `202`；可选 `silent=true` |
| GET | `/v1/sessions/{id}` | 查询状态 / challenge / result |
| POST | `/v1/sessions/{id}/captcha` | 人工验证码 |
| DELETE | `/v1/sessions/{id}` | 取消 |
| GET | `/v1/queue` | 队列统计 |
| GET/PUT | `/v1/console/settings` | GUI 设置 |
| WS | `/v1/logs` | 日志 |

## Docker（API + Worker + noVNC）

基础镜像：`mcr.azure.cn/playwright/python:v1.64.1-noble`。

```bash
docker compose up --build
```

- API / GUI / VNC 代理：`http://localhost:8056/`（仅暴露 8056）
- Basic 默认 `admin` / `admin`，请在环境变量中修改
- 数据卷：`/data`
- `shm_size: 2gb`
