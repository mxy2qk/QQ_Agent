"""Local command reference. Help never invokes the model or exposes user data."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Command:
    name: str
    syntax: str
    summary: str
    detail: str
    scope: str = "both"


COMMANDS = (
    Command(
        "style",
        "/style [show|set 内容|reset]",
        "查看、修改或恢复全局回复风格",
        "例如 /style set 甜软俏皮，少说一点。群聊和私聊共用一个风格，立即生效并保存。授权群内任何成员都可@机器人修改；只改变表达风格。/style reset恢复默认嘉然风格，模型回复总计最多70字符。",
    ),
    Command(
        "help",
        "/help [命令名|memory|skills]",
        "查看帮助",
        "如 /help forget 查看删除说明；/memory-help 等同 /help memory。",
    ),
    Command(
        "remember",
        "/remember 名称=内容",
        "保存当前会话私有记忆",
        "例如 /remember preference=喜欢简短回答；也可说“记住：我喜欢Python”。同名键更新，自由文本按内容去重。QQ中请私聊使用。",
        "private",
    ),
    Command(
        "remember-global",
        "/remember-global 名称=内容",
        "保存本人跨会话记忆",
        "global仍仅同平台本人可见，不是公开信息，也不会在群公开回复中注入。QQ中请私聊使用。",
        "private",
    ),
    Command(
        "remember-group",
        "/remember-group 名称=内容",
        "明确保存当前群共享记忆",
        "例如 /remember-group meeting=周五读书会。同群成员可读取，其他群不可用；创建者可纠正或删除。仅在群会话使用。",
        "group",
    ),
    Command(
        "memories",
        "/memories [关键词]",
        "查看或搜索有权访问的记忆",
        "私聊查看本人当前可用记忆；QQ群只显示该群共享记忆。序号从1开始，修改/删除用固定ID，不用序号。",
    ),
    Command(
        "correct",
        "/correct ID 新内容",
        "纠正自己的记忆",
        "例如 /correct 12 喜欢详细回答。群里只能纠正自己创建的共享记忆；纠正会清理旧来源原文和受影响近期会话。",
    ),
    Command(
        "forget",
        "/forget ID",
        "删除自己的记忆",
        "先 /memories 找ID，例如 /forget 12。不是列表序号。删除记忆、索引、来源原文及受影响近期会话；不能删除他人记忆。ID不会复用。",
    ),
    Command(
        "sources",
        "/sources ID",
        "查看自己记忆的来源",
        "例如 /sources 12，返回来源消息ID、操作事件及时间。QQ中请私聊使用。",
        "private",
    ),
    Command(
        "history",
        "/history 关键词",
        "搜索本人当前会话原始消息",
        "例如 /history Python，最多20条；不能搜索其他成员或其他会话。QQ中请私聊使用。",
        "private",
    ),
    Command(
        "reset",
        "/reset",
        "清空当前近期会话",
        "删除当前会话的近期消息和Checkpoint，明确保存的长期记忆仍保留。启用群上下文时，还清除本人在该群的摘录，并使同群答复快照失效；不删除其他成员原始摘录。",
    ),
    Command(
        "skill",
        "/skill 技能名 任务",
        "指定现有技能",
        "例如 /skill bilibili-hot 推荐3个热门视频；也可直接问“B站热门”。来源受限时明确失败，视频仅可访问简介与元数据。",
    ),
    Command(
        "share",
        "/share ID",
        "授权原群个人记忆共享（高级）",
        "仅适用于原群会话中本人拥有的个人记忆，不能把私聊记忆凭ID跨群发布。QQ群创建共享信息请用 /remember-group。",
        "advanced",
    ),
    Command(
        "unshare",
        "/unshare ID",
        "撤回原群个人记忆共享（高级）",
        "仅用于原群的个人记忆；QQ群不开放此个人管理命令。group类型记忆不能改为私有，应由创建者 /forget ID 删除。",
        "advanced",
    ),
)


def command_help(text, message, settings, skill_manager=None, persistent=True):
    parts = text.split()
    if not parts or parts[0].lower() not in {"/help", "/memory-help"}:
        return None
    topic = "memory" if parts[0].lower() == "/memory-help" else ""
    if len(parts) > 1:
        topic = parts[1].lower().lstrip("/")
    topic = {"记忆": "memory", "技能": "skills", "全部": "all"}.get(topic, topic)
    by_name = {item.name: item for item in COMMANDS}
    if topic in by_name:
        item = by_name[topic]
        return f"{item.syntax}\n{item.summary}\n{item.detail}"
    if topic == "skills":
        catalog = skill_manager.catalog() if skill_manager else "当前服务未加载技能目录。"
        state = "已启用（仍可能受来源限制）" if settings.public_sources_enabled else "已关闭"
        return (
            f"技能说明\n{catalog}\n用法：/skill 技能名 任务\n"
            f"例如：/skill bilibili-hot 推荐3个热门视频\n公开来源：{state}。"
            "\nB站搜索/贴吧可能受限；不绕过限制，不编造结果。没有时间工具、通用搜索或MCP。"
        )
    if topic not in {"", "memory", "all"}:
        return "未找到该帮助主题。发送 /help 查看总览，或 /help 命令名（如 /help forget）。"
    group = message.chat_type == "group"
    scope = "group" if group else "private"
    rows = [
        item
        for item in COMMANDS
        if item.scope in {"both", scope}
        and (topic != "memory" or item.name not in {"help", "skill", "style"})
    ]
    title = "群聊操作帮助" if group else "私聊/CLI操作帮助"
    intro = (
        "群内需先@机器人；只公开使用本群共享记忆。个人记忆管理请私聊。"
        if group
        else "QQ私聊需已加好友且在白名单内。直接发普通文字即可聊天。"
    )
    if group and settings.qq_group_context_enabled:
        intro += (
            f" 已启用授权成员普通文字上下文，保留{settings.message_retention_days}天；仍仅@时回复。"
        )
    lines = [title, intro, *[f"{item.syntax}：{item.summary}" for item in rows]]
    if topic == "memory":
        lines.append("高级范围管理：/help share、/help unshare；私聊记忆不能凭ID跨群共享。")
    lines.extend(
        [
            "例：/help forget 查看详细说明；/help skills 查看已加载技能。",
            "列表序号≠固定ID，修改/删除请用ID；同名键更新，群记忆由创建者管理。",
            f"消息/会话保留{settings.message_retention_days}天，明确记忆独立保留，/reset不删长期记忆。",
            "当前无时间工具、通用搜索或MCP；没有工具支持的实时信息无法核实。",
        ]
    )
    if not persistent:
        lines.append("当前为临时模式，未启用持久化记忆命令。")
    elif not settings.memory_extraction_enabled:
        lines.append("当前已关闭明确记忆写入，已有记忆仍可管理。")
    return "\n".join(lines)
