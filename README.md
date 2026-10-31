# QQ Personal AI Agent

基于 DeepSeek、LangGraph、SQLite 和 NapCat/OneBot 11 的 QQ 个人助手。支持 CLI、群内 @ 回复、好友私聊、明确授权的长期记忆及可扩展 Skill。

## 当前功能

- 多轮对话，模型/工具执行预算、超时及异常处理。
- 个人记忆、群共享记忆、权限过滤、来源追溯和全文检索。
- 会话重启恢复、7 天消息清理、发送去重、限流与断线重连。
- QQ发送者身份与群昵称识别；可明确授权开启指定成员普通文字上下文，仍仅@时回复，私聊与各群隔离。
- 默认采用简短口语的群友风格；`/style`查看、`/style set 内容`替换、`/style reset`恢复默认。全局共用一份风格，授权群内任何成员可@修改。
- B站热门与视频元数据；B站/贴吧搜索可能受平台访问限制，不保证可用。
- 时间查询工具已移除；未接入通用搜索或 MCP。无工具支持的实时信息应明确说明无法核实。

## 安装

需要 Python 3.11+。推荐使用 [uv](https://docs.astral.sh/uv/)：

```powershell
uv sync --extra dev --locked
Copy-Item .env.example .env
```

已有 `.env` 时不要覆盖。也可在独立 Conda 环境执行 `python -m pip install -e '.[dev]'`。

在本地 `.env` 填写 `DEEPSEEK_API_KEY`、`DEEPSEEK_BASE_URL` 和 `DEEPSEEK_MODEL`。支持 OpenAI 兼容 Chat Completions 接口，模型需支持工具调用。密钥不要提交到 Git。

## 运行

```powershell
# 无模型请求的离线演示
.\.venv\Scripts\qq-agent.exe --demo

# 真实模型 CLI；/exit 退出，/reset 清空当前近期会话
.\.venv\Scripts\qq-agent.exe

# 单条消息不保存到磁盘
.\.venv\Scripts\qq-agent.exe --ephemeral --message "你好"

# 仅验证模型文本请求
.\.venv\Scripts\qq-agent.exe --verify-api

# 查看技能 / 检查公开来源（失败会返回非零状态）
.\.venv\Scripts\qq-agent.exe --list-skills
.\.venv\Scripts\qq-agent.exe --verify-sources

# 完成 NapCat、白名单及成员告知配置后启用 QQ
.\.venv\Scripts\qq-agent.exe --qq
```

QQ 配置见 [QQ_SETUP](docs/QQ_SETUP.md)。本机已安装 NapCat 时，可用根目录 `start-napcat.cmd` 启动；新的仓库副本不包含 NapCat 二进制或登录数据，需自行安装到相应目录。

Windows 日常使用可直接双击 `启动机器人.cmd`、`停止机器人.cmd`、`重启机器人.cmd`、`查看状态.cmd`。启动后由后台守护程序负责 Agent 异常退出重试，连续失败会暂停；停止/重启不关闭 NapCat。使用说明见 [运行管理](docs/RUNTIME.md)。不包含开机自启。

`QQ_ALLOWED_GROUPS` 控制允许的群；可用 `QQ_ALLOWED_GROUP_USERS` 进一步限制群内成员，留空时允许这些群的所有成员。好友私聊由 `QQ_ALLOWED_USERS` 和 `QQ_PRIVATE_ENABLED` 独立控制。真实白名单只填写本地 `.env`，修改后重启 Agent。

经明确授权，可设置 `QQ_GROUP_CONTEXT_ENABLED=true` 读取该群授权成员未@的普通文字，作为7天内同群问答摘录；默认关闭。成员昵称、群名片和QQ号由渠道提供，细节见 [群成员与上下文](docs/GROUP_CONTEXT.md)。不自动补历史或转成长期记忆。

同一数据库只允许一个 Agent 进程。`--data-dir PATH` 可隔离测试数据；demo 默认使用 `data/demo`。不要与正在运行的 QQ Agent 同时使用默认数据库启动另一 CLI。

## 记忆命令

群里 **@机器人 /help**（或只@不带文字）或好友私聊 **/help** 获取操作说明；`/help 命令名` 查看详细用法，`/help memory`、`/help skills` 按主题查看。完整中文文档见 [操作手册](docs/OPERATIONS.md)。帮助由程序直接返回，不调用模型。

回复风格修改立即生效并在重启后保留。群内任何成员可@使用 `/style`，其余聊天和数据访问仍受原白名单控制。详细说明见 [全局回复风格](docs/REPLY_STYLE.md)。

| 命令 | 用途 |
| --- | --- |
| `/help [命令名或主题]` | 按当前群聊/私聊显示操作说明 |
| `/remember key=value` 或 `记住：内容` | 保存当前会话私有记忆 |
| `/remember-global key=value` | 保存同平台本人跨会话记忆，仍仅本人可见 |
| `/remember-group key=value` | 明确授权保存当前群共享记忆 |
| `/memories [关键词]` | 查看有权访问的记忆 |
| `/correct ID 新内容` | 纠正自己的记忆 |
| `/forget ID` | 删除自己的记忆、来源原文和受影响的近期上下文 |
| `/share ID`、`/unshare ID` | 切换当前群个人记忆的共享授权 |
| `/sources ID` | 查看来源事件 |
| `/history 关键词` | 检索当前本人会话原始消息 |
| `/reset` | 清空近期会话，保留明确长期记忆 |
| `/memory-help` | 查看完整帮助 |

列表序号从 1 连续显示；固定 ID 不重用，修改和删除使用 **ID**。群公开回复只使用共享记忆，个人记忆管理请私聊。群组记忆由创建者纠正或删除。

## 数据与边界

原始消息、缓存答复及会话保留 7 天，明确保存的长期记忆单独存活；删除/撤回会清理来源和相关历史。模型仅能调用注册且与技能绑定的工具。外部内容不可信，无验证码/登录绕过，无群管理或主动推送工具。

QQ 发送采用最多一次尝试：结果不确定时不自动重发，过载或断线可能丢消息。日志默认不保存完整消息或密钥。SQLite 文件为明文，请保护本地目录和备份。

## 项目结构

```text
src/qq_agent/      应用、模型、记忆、存储、渠道和 Skill
tests/           自动化测试（含测试专用模拟工具）
docs/             配置、架构、数据模型、需求与测试说明
.env.example      无密钥配置模板
pyproject.toml    项目配置
uv.lock           依赖锁文件
```

本地运行所需的 `.env`、`.venv/`、`data/`、`logs/`、`napcat/` 不上传。管理脚本的当前运行信息保存在 `data/run/runtime.json` 和 `runtime-worker.json`；不要删除正在使用的控制文件、数据库或 NapCat。

## 开发验证

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\ruff.exe check src tests
.\.venv\Scripts\ruff.exe format --check src tests
```

更多：[架构](docs/ARCHITECTURE.md) · [数据与权限](docs/DATA_MODEL.md) · [决策](docs/DECISIONS.md) · [测试](docs/TESTING.md) · [原始需求基线](docs/REQUIREMENTS.md)。原始需求是历史基线，当前实现及用户后续决策以本文和决策记录为准。
