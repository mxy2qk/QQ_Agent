# Phase 0 / Phase 1 验收报告

日期：2026-10-30（Asia/Shanghai）。

## 环境

- Windows / PowerShell。
- 本机 Conda：`C:\code\miniconda`（base Python 3.13.12，未修改）。
- 项目独立环境：`.venv`，CPython 3.12.15，由 uv 创建。
- 可编辑安装成功；`uv.lock` 已生成。
- 安装版本：LangGraph 1.2.18、OpenAI SDK 2.54.0、Pydantic 2.14.2、pytest 9.2.0。

## 已执行

| 检查 | 结果 |
| --- | --- |
| `uv sync --extra dev --python 3.12` | 成功，安装项目与开发依赖 |
| `.venv\Scripts\python.exe -m pytest -q` | **36 passed in 10.30s** |
| `.venv\Scripts\ruff.exe check .` | All checks passed |
| `.venv\Scripts\ruff.exe format --check .` | 18 files already formatted |
| CLI 离线单条时间查询 | 成功，真实时间工具返回 Asia/Shanghai 的 ISO 时间 |
| CLI 中文多轮管道输入 | 成功，问候 → 时间工具 → /reset → /exit，退出码 0 |
| 缺失 API 配置启动 | 清楚列出三个缺失配置字段，退出码 2，无 traceback |

## 自动化覆盖

- 普通问候不调用工具，多轮上下文传递。
- LangGraph 条件路由、真实时间工具执行、匹配 tool_call_id、结果返回模型。
- 未注册工具、无效 JSON、多余参数、错误类型、无效时区。
- 工具异常信息不外泄；工具超时取消 handler 并回传错误。
- 工具失败后的执行拒绝、批量工具预算、非正常模型反复调用时的模型步数终止。
- 整体请求超时、失败历史清理、后续回合恢复。
- 并发重复事件只执行一次，同一 thread 串行，不同用户/群/平台隔离。
- 历史保留上限、会话清空、消息文本校验、thread 分隔符碰撞防护。
- 工具重复注册、同步函数、有外部副作用工具拒绝。
- SDK JSON 请求和响应映射、usage、401/402/429/500 脱敏错误。
- 模型空回答、截断、拒绝和网络超时。
- CLI 子进程：离线查询、空配置、中文多轮输入与退出；请求关联日志。

## 未验收项与结论

用户要求先留空 DeepSeek 配置，因此 **没有执行真实 API 请求**。SDK 测试使用 MockTransport；Agent 行为测试使用 ScriptedClient；CLI demo 使用确定性 DemoClient。它们验证程序执行逻辑，不证明某个真实 DeepSeek 模型的能力。

Phase 0 初始化与 Phase 1 核心实现已完成并通过离线验收。Phase 0 的真实 API 连通性、指定模型 Tool Calling，以及 Phase 1 的真实模型自主决策仍待填写 `.env` 后验收。

填写配置后运行：

```powershell
.\.venv\Scripts\qq-agent.exe --verify-api
.\.venv\Scripts\qq-agent.exe --message "你好"
.\.venv\Scripts\qq-agent.exe --message "现在上海几点？"
.\.venv\Scripts\qq-agent.exe
```

`--verify-api` 验证普通文本、强制 Tool Calling、参数校验、真实工具执行和结果回传。接着用普通消息模式观察模型是否自主选用时间工具。暂未进行 QQ、长期记忆或真实内容数据源验收；这些不属于当前实现范围。
