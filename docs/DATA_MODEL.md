# Phase 2 数据与权限

记忆ID是稳定的全库主键，不因删除而重置或复用，不表示当前用户的列表位置。界面单独显示从1开始的连续序号；修改/删除等命令仍使用明确标记的ID。筛选后的结果也重新从1编号。历史命令引用已删除ID时应拒绝，不能改指向新记忆。

业务库 `DATABASE_PATH` 与 LangGraph 库 `CHECKPOINT_DB_PATH` 分开存储，禁止指向同一文件。SQLite WAL + 外键 + secure_delete；启动按 `PRAGMA user_version` 应用幂等 v3 Schema，兼容从v1/v2升级，遇到高版本拒绝打开。FTS5 trigram 用于中英文子串全文检索，少于三字符使用参数化 `instr`。

| 表 | 用途 |
| --- | --- |
| users | `(platform,user_id)` 稳定身份与展示昵称 |
| groups / group_members | 平台群及观察到的成员；observed 不代表 QQ 平台知情授权已完成 |
| messages / messages_fts | 来源消息、原始消息去重、当前本人会话历史检索 |
| message_receipts | 持久化事件结果；失效后无文本 tombstone 防止旧事件重新写入 |
| memories / memories_fts | 统一存放 personal / group 类型、private / group / global 范围、分类、可信度、明确来源和可选过期时间 |
| memory_sources | 来源消息引用、稳定外部消息 ID、来源用户、明确操作与时间 |
| consent_records | remember/share/unshare/correct 等明确授权事件；删除记忆时级联删除 |
| sessions | 每成员会话已完成回合快照；作为重启恢复的事务提交边界 |
| purge_queue | 跨业务库/Checkpoint 库失效队列，重启先完成清理 |
| group_context_messages | 明确启用后的授权群文字摘录、发送者QQ号与群显示名；按群与成员过滤、默认7天，清除后保留无正文去重标记 |
| reply_style_settings | 当前单条全局风格微调，所有群聊与私聊共用，作为配置持续保留；基础规则和默认人格来自代码，恢复默认时删除微调记录 |

权限 SQL 必须同时匹配平台、所有者、当前 chat_type/chat_id 和 scope。private 只对所属用户在原会话可见；group 只对同平台该群的观察成员可见；global 只对同平台同一稳定用户可见。跨平台身份关联尚未实现，不能按昵称或同名 ID 合并。群组记忆有创建者所有权，其他成员可读取，不能修改或删除。

长期记忆写入只从明确指令进入，不暴露给模型作工具。`/share` 和 `/remember-group` 为明确群共享授权；群里说过某事不会自动转为长期记忆。没有不经确认的模型自动提取。命名键支持确定性更新；自由文本按内容哈希去重，语义纠正使用 `/correct`。经额外授权开启的普通文字群上下文独立保存和限期清理，详见 [GROUP_CONTEXT.md](GROUP_CONTEXT.md)。

删除会移除记忆、FTS 索引、授权记录、来源引用及旧来源消息。纠正与同键更新会移除旧来源文本，并记录新的来源。来源原文按 7 天到期后可为空，但外部来源消息 ID 与授权事件作为仍存活记忆的元数据保留。用户消息时间不决定到期，以本地入库时间为准，避免无效或恶意外部时间戳延长保留。

私有记忆变化清理该用户本平台的近期会话；群共享记忆变化清理该群所有近期会话。清理包含缓存答复、会话快照和 LangGraph 全部 checkpoints/writes。删除原文后保留 7 天内无文本事件标记，防止重复保存。启动先处理持久化 purge_queue，再接收请求；正常完成回合会压缩为一个完整图 Checkpoint，避免旧上下文在历史 checkpoints 中无限积累。

会话恢复使用已提交的 sessions 快照重新初始化图，图仍使用 AsyncSqliteSaver 在执行过程中保存 State。崩溃中断的半轮不自动重放外部工具，下一次消息从最近完成回合继续。模型注入记忆通过请求级 ContextVar 提供，不把记忆 Prompt 保存进 State；模型答复可能复述记忆，因此撤回/删除必须清理受影响会话。

单进程内持久化请求串行，数据库与 Checkpoint 路径各持有 OS 文件锁，阻止两个进程绕过撤回锁。此为本地小群设计，不是多进程服务方案。SQLite 逻辑删除不会删除用户自行复制的数据库备份，也不保证物理磁盘不可恢复。

删除来源原文会通过触发器同步清除群摘录副本。本人群摘录被reset或摘录到期时，同群近期答复与Checkpoint失效，防止间接引用旧文本；重置保留其他成员原始摘录。群上下文不注入私聊，临时注入数据不写入图State。
