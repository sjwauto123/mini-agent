# Mini Agent

一个从零实现的工具型 Agent。核心 Runtime 不依赖 LangGraph、OpenHands、OpenClaw、PI 等 Agent 框架，模型根据注册工具的 JSON Schema 自主决定直接回复或调用工具。

## 功能

- 自研 Agent Loop：模型判断、工具执行、结果回灌、继续或完成。
- 六个工具：安全计算器、会话待办、模拟搜索、模拟天气、资源读取和资源查找。
- 多会话隔离、历史持久化、纯对话追问和工具结果追问。
- 软硬阈值上下文压缩，大结果及长文本外置读取。
- 运行上限、超时、有限重试、协作式停止和结构化 trace。
- FastAPI API 与 React Web 界面。

搜索和天气工具使用固定模拟数据，页面和工具结果会标明 `mock`，不代表实时联网结果。

## 环境

- Python 3.11+
- Node.js 20+

PowerShell 安装：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
Set-Location frontend
npm install
npx playwright install chromium
Set-Location ..
```

## 模型配置

复制 `models.example.toml` 为 `models.toml`，填写一个兼容 Chat Completions 的实际端点、模型名称、上下文窗口和输出预留。复制 `.env.example` 为 `.env` 并填写密钥；服务启动时会自动读取项目根目录的 `.env`，系统环境变量优先：

```powershell
Copy-Item .env.example .env
# 编辑 .env，填入新的 DEEPSEEK_API_KEY
```

如需使用其他环境文件，可设置 `MINI_AGENT_ENV_FILE`。

`mode = "native"` 使用端点原生工具调用；`mode = "json"` 使用统一 JSON 输出协议。首版通过 `httpx` 接入一个选定端点，不承诺所有兼容服务的私有扩展字段。

## 初始化与启动

```powershell
.\.venv\Scripts\alembic.exe -c backend/alembic.ini upgrade head
.\.venv\Scripts\python.exe -m uvicorn --app-dir backend mini_agent.api:app --host 127.0.0.1 --port 8000
```

后端监听 [http://127.0.0.1:8000](http://127.0.0.1:8000)。开发前端另开终端：

```powershell
Set-Location frontend
npm run dev
```

后端若运行在 8001 等其他端口，先设置前端开发代理：

```powershell
$env:VITE_API_TARGET = "http://127.0.0.1:8001"
npm run dev
```

打开 [http://127.0.0.1:5173](http://127.0.0.1:5173)。生产式本地运行可先执行 `npm run build`，FastAPI 会从 `frontend/dist` 提供页面。

两个浏览器窗口可以分别创建会话；当前会话 ID 保存在各自 URL 查询参数中，历史、摘要、待办和外置资源不会跨会话共享。

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest
Set-Location frontend
npm run build
```

默认测试使用脚本化 Fake LLM，但让响应经过真实解析器、注册表、工具、Runtime 和数据库。真实模型测试需要有效配置，单独运行并记录结果，不能用 Fake LLM 的通过结果代替。

真实模型测试会产生 API 费用，只有显式设置开关后才执行：

```powershell
$env:RUN_REAL_MODEL_TESTS = "1"
.\.venv\Scripts\python.exe -m pytest tests/test_real_model.py -q
```

该文件同时覆盖 Runtime 工具循环，以及浏览器页面经 HTTP/SSE 调用真实模型的完整问答链路。普通浏览器测试使用 Fake LLM，只验证页面交互、失败提示、Session 隔离和恢复。

## 主要 API

- `POST /api/sessions`、`GET /api/sessions`
- `GET /api/sessions/{id}/messages`
- `POST /api/sessions/{id}/runs`
- `GET /api/runs/{id}`、`POST /api/runs/{id}/cancel`
- `GET /api/runs/{id}/events`、`GET /api/runs/{id}/trace`
- `POST /api/sessions/{id}/resources`

详细行为见 [设计文档](docs/design.md)，确认记录见 [架构决策](docs/decisions.md)。
