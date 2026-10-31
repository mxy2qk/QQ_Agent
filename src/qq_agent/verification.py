"""Explicit source acceptance, independent of model credit and chat databases."""

from datetime import UTC, datetime

from qq_agent.tools import ToolCall, ToolExecutor


async def verify_sources(registry, settings):
    executor = ToolExecutor(registry, settings.tool_timeout_seconds)
    results = {}
    for name, arguments in [
        ("get_bilibili_hot_videos", '{"limit": 3}'),
        ("search_bilibili_videos", '{"keyword": "Python", "limit": 3}'),
        ("search_tieba_posts", '{"keyword": "神贴", "limit": 3}'),
    ]:
        results[name] = await executor.execute(ToolCall("verify-" + name, name, arguments))
    return {
        "verified_at": datetime.now(UTC).isoformat(),
        "scope": "popular_and_search_adapters_only_not_full_phase_acceptance",
        "complete": all(item["ok"] for item in results.values()),
        "results": results,
    }
