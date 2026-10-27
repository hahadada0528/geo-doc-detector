# GEO 文档检测工具

本地小工具：录入品牌与问题表 → 连接豆包依次提问入库引用 → 投放站分析 → 本地 Agent 月度加权训练。

## 快速开始

1. 安装依赖

```bash
pip install -r requirements.txt
```

2. 配置密钥

复制 `.env.example` 为 `.env`，填入：

- `ARK_API_KEY`：火山方舟 API Key
- `ARK_MODEL`：模型/接入点 ID（如 `doubao-seed-1-6-251015` 或控制台 `ep-xxxx`）

3. 启动

```bash
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

浏览器打开 http://127.0.0.1:8000

## 使用流程

1. 添加品牌 → 录入问题 → 开始依次提问  
2. 「投放网站分析」看某站加权写法规律，并在结果下方用 Hermes **生成文章**  
3. 「本月手动训练」生成本地 Agent 包

### Hermes 生成文章

在「投放分析」页生成资料型文章并导出 Word/PDF；可在「知识库」页上传文件。

功能分页：首页 `/` · 采集检测 `/collect` · 投放分析 `/analyze` · 本地 Agent `/agent` · 知识库 `/knowledge`

## 本地 Agent（月度训练）

每月手动点一次「本月手动训练」：

1. 自动导入 `citations` 一年内样本  
2. 按权重训练：1–30 天 `1.0`，31–60 `0.8`，61–90 `0.65`，91–180 `0.45`，181–365 `0.2`  
3. 输出到 `data/agent_packs/YYYY-MM/`，含分站点**资料分级**（用户反馈级、产品信息级、评测对比级等）加权配比  

手动改单条引用分级：`POST /api/citations/{id}/level` JSON `{"content_level":"用户反馈级"}`

API：

- `POST /api/agent/train`

## 数据存放

SQLite：`data/geo.db`  
表：`brands`、`questions`、`citations`、`detection_runs`、`ask_logs`、`agent_trainings`

## hahadada 知识库 API Server + Hermes Proxy

`start_geo.bat` 启动时会同时启动三个服务：

| 服务 | 端口 | 用途 |
|------|------|------|
| Hermes Proxy | 8080 | OpenAI 兼容 API (`/v1/chat/completions`)，供 GEO 工具生成文章 |
| Wiki API | 8686 | 文件上传 (`/v1/files`)，自动入库到 `~/wiki/raw/brand/` |
| GEO Tool | 8765 | 文档检测工具主服务 |

### Hermes Proxy (端口 8080)
- `POST /v1/chat/completions` — OpenAI 兼容，model 用 `z-ai/glm-5.2` 或其他 Nous Portal 模型
- `GET /v1/models` — 列出可用模型
- 任意 bearer token 即可

### Wiki API (端口 8686)
- 网页上传：http://127.0.0.1:8686
- `POST /v1/files` — multipart: file + purpose
- `GET /v1/files` — 列出已入库文件
- `GET /health` — 健康检查
- 支持 .xlsx / .csv / .md / .txt，上传后自动转 markdown 存入 `~/wiki/raw/brand/`
- 上传后写 pending 标记，cron 每 10 分钟自动按 llm-wiki + obsidian 完整流程入库

代码位置：`%USERPROFILE%\wiki\api_server.py`
