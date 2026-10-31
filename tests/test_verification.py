import pytest

from qq_agent.config import Settings
from qq_agent.public_sources import PublicSources
from qq_agent.tools import Tool, ToolFailure, default_registry
from qq_agent.verification import verify_sources


@pytest.mark.parametrize("failure", [False, True])
async def test_source_verifier_reports_partial_failure_and_keeps_other_results(failure):
    from qq_agent.public_sources import HotArguments, SearchArguments, TiebaSearchArguments

    registry = default_registry()
    calls = []

    async def hot(**kwargs):
        calls.append("hot")
        return {"items": [], "fetched_at": "snapshot"}

    async def search(**kwargs):
        calls.append("search")
        if failure:
            raise ToolFailure("source_blocked", "来源访问受限。")
        return {"items": []}

    registry.register(Tool("get_bilibili_hot_videos", "hot", HotArguments, hot))
    registry.register(Tool("search_bilibili_videos", "search", SearchArguments, search))
    registry.register(Tool("search_tieba_posts", "search", TiebaSearchArguments, search))
    result = await verify_sources(registry, Settings(_env_file=None))
    assert result["complete"] is (not failure)
    assert result["results"]["get_bilibili_hot_videos"]["ok"]
    assert result["verified_at"]
    assert calls == ["hot", "search", "search"]


async def test_queued_requests_do_not_continue_after_first_is_blocked():
    import asyncio

    import httpx

    requests = []

    async def handler(request):
        requests.append(request)
        await asyncio.sleep(0.01)
        return httpx.Response(403)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sources = PublicSources(Settings(_env_file=None, public_sources_enabled=True), client)
    sources.interval = 0
    try:
        result = await asyncio.gather(
            sources.hot(), sources.search_video("Python"), return_exceptions=True
        )
        assert all(isinstance(r, ToolFailure) for r in result)
        assert len(requests) == 1
    finally:
        await sources.close()
