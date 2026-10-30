# 决策记录

## 2026-10-30：Phase 0 / Phase 1

- 用户授权连续开展 Phase 0 与 Phase 1；后续阶段未启动。
- 工作区原先仅有需求文档，不是 Git 仓库。保留原始文件；不自动创建提交。
- 用户要求 DeepSeek 三字段留空，稍后自行填写；因此不执行真实 API 请求，不宣称 API 已验证。
- 本机 Conda 位于 `C:\code\miniconda`，base Python 3.13.12。项目使用 uv 创建的 `.venv` / Python 3.12.15 做隔离，Conda 环境不变；README 同时提供 Conda 安装路径。
- 已核对 DeepSeek 官方首次调用与 Tool Calls 文档（2026-10-30）：使用 OpenAI 兼容 SDK、可配置 model/base_url、关闭 thinking、非 beta Function Calling、本地严格校验参数。真实能力取决于用户填写的模型和账号。
- 已核对 LangGraph Graph API；使用 StateGraph / TypedDict reducer / 异步节点。
- 模型最多 5 次、工具最多 5 次、工具 30 秒、模型 60 秒、整体 120 秒；SDK 重试为 0，避免隐式请求增加耗时。均可配置。
- 工具失败后只允许模型解释；即使模型忽略 tool_choice=none，程序仍拒绝进一步实际执行工具，并以步数上限终止。
- Phase 1 仅注册无外部副作用的异步工具。同步工具不通过线程超时伪装取消；未来需要同步或外部写工具时单独设计执行与授权。
- CLI 历史只驻留内存，最多 20 完整回合；去重最多 256 结果/会话。同一会话串行，调用失败不保留半完成的工具调用。
- 临时会话键采用编码后的 platform/chat_type/chat_id/user_id，不在 Phase 1 建立群共享 thread。群级公开上下文在 Phase 2 与 QQ 阶段确定。
- 不保存原始聊天到磁盘，不自动提取记忆，不添加数据库空表。SQLite 与 Checkpointer 属于 Phase 2。
- Q1–Q10 仍未定案：QQ 模式 unselected；未来临时 mention_only、个人记忆 private、仅响应策略来自需求文档的临时默认值，不是用户确认。消息保留时间不设有效默认。
- 工具默认时区 Asia/Shanghai，可配置 TIMEZONE 并在调用中指定 IANA 时区；不依赖执行主机时区。

## 2026-10-30：Git 初始化

- 用户要求为项目配置 Git，初始化本地仓库，默认分支为 `main`。
- 提交沿用现有 Git 身份；不改动全局配置。
- `.gitattributes` 统一源代码及文档为 LF，本仓库 `core.autocrlf=false`；`core.quotepath=false` 便于阅读中文路径。
- `.env`、虚拟环境、日志、缓存、构建产物与覆盖率文件均忽略；保留无密钥的 `.env.example` 和依赖锁文件。
- 建立 Phase 0/1 初始提交；未配置远程仓库或推送。

## 参考

- https://api-docs.deepseek.com/
- https://api-docs.deepseek.com/guides/tool_calls
- https://docs.langchain.com/oss/python/langgraph/graph-api
