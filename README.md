# QQ Personal AI Agent

按根目录需求文档实现的 Phase 0 / Phase 1：DeepSeek 模型封装、LangGraph 单 Agent 工具循环、CLI 多轮对话。实际 API 验收待填写本地配置。

## 安装

要求 Python 3.11+。当前项目已在 `.venv` 安装独立环境（Python 3.12），不会改变本机 Conda 环境。

```powershell
# 推荐：uv 根据锁文件安装开发依赖
uv sync --extra dev --locked

# 或使用自己选定的 Conda 环境
conda create -n qq-personal-agent python=3.12 -y
conda activate qq-personal-agent
python -m pip install -e '.[dev]'
```

项目内已创建空白 `.env`。如果从新副本安装，先 `Copy-Item .env.example .env`，不要覆盖已填好的配置。

## 立即运行离线演示

```powershell
.\.venv\Scripts\qq-agent.exe --demo
.\.venv\Scripts\qq-agent.exe --demo --message "现在几点？"
```

离线演示使用确定性模拟客户端和真实时间工具，不请求 DeepSeek，不能证明模型的自主工具决策能力。

## 使用真实 DeepSeek

在本地 `.env` 填写以下三个字段，或设置同名环境变量（环境变量优先）：

```dotenv
DEEPSEEK_API_KEY=
DEEPSEEK_BASE_URL=
DEEPSEEK_MODEL=
```

官方兼容地址为 `https://api.deepseek.com`。2026-10-30 核对官方文档列出的模型包括 `deepseek-flash` 与 `deepseek-v4-pro`，请按账号实际权限填写。项目显式禁用 thinking，使用 Chat Completions + Function Calling，本地 Pydantic 严格校验参数；未开启 beta strict schema。

官方参考：[首次调用](https://api-docs.deepseek.com/)、[工具调用](https://api-docs.deepseek.com/guides/tool_calls)。模型 ID 没有默认值。

```powershell
# 发出真实请求：普通文本 + 强制工具调用 + 本地执行 + 工具结果回传
.\.venv\Scripts\qq-agent.exe --verify-api

# 模型自主决定是否调用工具；可查看 stderr 或 logs/agent.log
.\.venv\Scripts\qq-agent.exe --message "你好"
.\.venv\Scripts\qq-agent.exe --message "现在上海几点？"

# 连续聊天
.\.venv\Scripts\qq-agent.exe
```

在已激活 Conda 环境并安装项目的情况下，可直接用 `qq-agent` 或 `python main.py`。`/exit` 退出，`/reset` 清空当前会话；`--session` 与 `--user` 指定本地身份。历史仅在当前进程保留，不支持重启恢复；单条命令每次启动新进程。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\ruff.exe check .
```

测试覆盖对话、时间工具闭环、参数拒绝、异常、取消、调用预算、消息去重、并发串行及会话隔离。SDK 传输测试使用本地 MockTransport，不消耗 API 额度。结果见 [测试报告](docs/TEST_REPORT.md)。

## 边界与配置

默认最多 5 次模型请求、5 次工具尝试；单工具 30 秒、单模型请求 60 秒、整体 120 秒。SDK 自动重试关闭。配置参见 `.env.example`；超时返回清楚错误，工具失败后禁止继续实际执行工具。

日志为 JSON，默认只记录执行元数据，不记录完整消息、Prompt、工具参数、返回值或 API Key。日志轮转保存到 `logs/`。`.env`、日志与环境目录已加入 `.gitignore`。请勿开启第三方 SDK 的网络调试日志。

Phase 1 的去重缓存每个会话最多 256 条，历史最多 20 个完整回合，均为进程内数据。会话总数没有自动淘汰，适用于本地 CLI；QQ 接入前需加入全局容量控制、持久化去重和平台授权策略。

本阶段不提供 QQ 连接、长期记忆、SQLite Checkpointer、Skill 加载或贴吧/B 站数据。QQ 身份、共享策略和保留时间仍待后续阶段确认。

架构和决策见 [ARCHITECTURE](docs/ARCHITECTURE.md)、[DECISIONS](docs/DECISIONS.md)、[开发进度](docs/DEVELOPMENT_PLAN.md)。原始需求文档保持原样。
