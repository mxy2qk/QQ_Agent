# Phase 0–4 架构

```text
CLI Adapter → IncomingMessage → AgentService → LangGraph
                                                │
         prepare_context → agent → tools → agent → finalize
                              │       │
                         LLMClient   ToolExecutor → ToolRegistry
                              │
                         DeepSeek SDK
```

- `config.py`：环境变量与 .env、密钥包装、范围校验。
- `model.py`：LLMClient 协议、ModelResponse/ToolCall、DeepSeek SDK 边界与显式离线 DemoClient。
- `graph.py`：AgentState 与 StateGraph、条件路由、模型/工具预算和最终回复。
- `tools.py`：只读异步工具注册、Pydantic JSON 参数校验、可取消超时、执行结果与元数据日志。
- `service.py`：会话键、每会话锁、去重缓存、多轮历史、整体超时和原子提交。
- `messages.py`：稳定用户 ID 与平台命名空间、校验统一消息输入。
- `cli.py`：终端输入输出、单条消息与真实 API 验证入口。
- `observability.py`：ContextVar 请求关联、JSON 日志、文件轮转。
- `storage.py`：SQLite v1 Schema、身份/消息/授权记录、权限化记忆 CRUD、FTS5 和清理队列。
- `memory.py`：明确指令提取、授权记忆检索、记忆管理命令与请求级注入上下文。
- `persistence.py`：AsyncSqliteSaver、OS 文件锁、过期清理和跨库失效队列。
- `skills.py` / `skill_specs/`：元数据发现校验、意图匹配、按需读取受信任说明与工具白名单。
- `public_sources.py`：固定平台 HTTPS 只读适配、Schema、来源解析、限速缓存与失败边界；客户端随 CLI 退出关闭。
- `channels/base.py` / `channels/onebot.py`：OneBot 正向 WS、可信事件规范化、白名单/@过滤、受控回复路由、动作ACK、去重与重连。渠道不拼装Prompt，也不直接调用模型SDK。

QQ群回复的 IncomingMessage.public_response=true，SQL只检索group记忆，防止成员自己的private/global记忆随群回复公开。私聊和CLI模拟仍按原身份隔离。qq_deliveries仅保存事件键和投递状态，采用最多一次发送尝试；不能把平台ACK超时当作未送达并自动重发。模型不持有发送接口，收件人始终由已校验事件决定。

运行图 prepare_context 根据当前用户消息匹配技能，模型只接收简短目录与命中技能全文。模型提出的工具调用还需执行器复核技能白名单，不能因为外部网页提及某个工具就提升权限。执行状态包含 skill_names，不包含数据源凭据；JSON 日志记录 skill_started/skill_completed、耗时、状态和工具次数，不记录搜索关键词或正文。

State 消息列表采用 LangGraph 官方支持的 TypedDict + reducer，保存 SDK 无关的协议字典。工具调用和对应结果按完整回合保存；失败或超时不提交不完整协议。上下文窗口暂以 20 回合限制，Token 预算管理留待后续。

所有 Tool 必须明确注册，且 Phase 1 拒绝有副作用的工具。没有任意 eval、Shell、Markdown 执行或外发消息工具。工具结果作为 tool 角色数据回传，系统提示说明外部内容不可信；这不代表已验证未来网页抓取的提示注入防护。

默认持久化入口将请求串行执行，避免记忆撤回与其他成员生成旧答复同时发生；`--ephemeral` 仍按 thread 串行。整体期限覆盖锁等待，取消时不提交半完成会话。业务 SQLite 存放已完成快照和去重结果，AsyncSqliteSaver 保存图 State，回合结束时压缩为一个完整 Checkpoint。重启从已提交的完成回合恢复，不自动重放中断工具。

记忆在服务层按稳定身份、当前会话与 scope 过滤，经 ContextVar 注入单次请求，不把记忆 Prompt 保存到 State。近期 thread 仍按成员隔离；群共享通过明确授权记忆提供。删除或撤回使相关快照、缓存答复和 Checkpoint 同时失效，并通过持久队列保证跨库清理可在重启后完成。

用户确认原始消息和会话保留 7 天；启动、请求前及 CLI 空闲每分钟清理。长期明确记忆独立存活，来源原文过期后保留来源 ID 与授权事件；用户删除时一并清理。详细权限和删除策略见 DATA_MODEL.md。

依据：[LangGraph Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api)、[DeepSeek Tool Calls](https://api-docs.deepseek.com/guides/tool_calls)。
Checkpoint 依据：[LangGraph Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)。
