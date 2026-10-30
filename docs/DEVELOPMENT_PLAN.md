# 开发进度

## Phase 0

已实现 Python 包、独立环境、配置与 JSON 轮转日志、统一模型客户端、pytest 框架和 API 验证入口。真实 API 请求及模型 Tool Calling 验收因用户主动留空配置而待执行。

## Phase 1

已实现 AgentState、LangGraph 循环、时间工具、注册器、执行器、CLI 多轮会话、执行限制、异常处理、消息去重和串行更新。离线自动化验收见 TEST_REPORT.md；真实模型自主选择工具仍需填好配置后验证。

## 下一阶段（未启动）

Phase 2：先确认消息保留/删除策略与记忆共享规则，再开发 SQLite Schema、权限过滤、记忆 CRUD 与持久化 Checkpointer。QQ 方案和真实数据源继续留待 Phase 3 / 4 决定。
