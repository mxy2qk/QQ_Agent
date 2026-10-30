# Phase 0 / 1 架构

```text
CLI Adapter → IncomingMessage → AgentService → LangGraph
                                                │
         prepare_context → agent → tools → agent → finalize
                              │       │
                         LLMClient   ToolExecutor → ToolRegistry
                              │
                         DeepSeek SDK
```

- `config.py`：环境变量与 .env、密钥包装、范围与时区校验。
- `model.py`：LLMClient 协议、ModelResponse/ToolCall、DeepSeek SDK 边界与显式离线 DemoClient。
- `graph.py`：AgentState 与 StateGraph、条件路由、模型/工具预算和最终回复。
- `tools.py`：只读异步工具注册、Pydantic JSON 参数校验、可取消超时、执行结果与元数据日志。
- `service.py`：会话键、每会话锁、去重缓存、多轮历史、整体超时和原子提交。
- `messages.py`：稳定用户 ID 与平台命名空间、校验统一消息输入。
- `cli.py`：终端输入输出、单条消息与真实 API 验证入口。
- `observability.py`：ContextVar 请求关联、JSON 日志、文件轮转。

State 消息列表采用 LangGraph 官方支持的 TypedDict + reducer，保存 SDK 无关的协议字典。工具调用和对应结果按完整回合保存；失败或超时不提交不完整协议。上下文窗口暂以 20 回合限制，Token 预算管理留待后续。

所有 Tool 必须明确注册，且 Phase 1 拒绝有副作用的工具。没有任意 eval、Shell、Markdown 执行或外发消息工具。工具结果作为 tool 角色数据回传，系统提示说明外部内容不可信；这不代表已验证未来网页抓取的提示注入防护。

同一个 thread 串行执行，不同 thread 可并发。整体期限覆盖锁等待，取消时不会写入半完成会话。当前锁、去重、历史仅驻留内存。未来记忆系统需独立做权限过滤，不能把群级 thread 当作个人记忆的授权依据。

依据：[LangGraph Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api)、[DeepSeek Tool Calls](https://api-docs.deepseek.com/guides/tool_calls)。
