"""Bounded, read-only public adapters; no cookies, signatures, proxies or auth bypass."""

import asyncio
import copy
import json
import re
import time
from datetime import UTC, datetime
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup
from pydantic import Field

from qq_agent.pixiv_images import PixivImages
from qq_agent.tools import Tool, ToolArguments, ToolFailure


class SearchArguments(ToolArguments):
    keyword: str = Field(min_length=1, max_length=100)
    limit: int = Field(default=5, ge=1, le=20)


class TiebaSearchArguments(SearchArguments):
    forum_name: str | None = Field(default=None, min_length=1, max_length=80)


class HotArguments(ToolArguments):
    area: str | None = Field(default=None, max_length=80)
    limit: int = Field(default=5, ge=1, le=20)


class VideoArguments(ToolArguments):
    video_id: str = Field(pattern=r"^BV[a-zA-Z0-9]{10}$")


class PostArguments(ToolArguments):
    post_url: str = Field(pattern=r"^https://tieba\.baidu\.com/p/[0-9]{1,20}$")


def text(value, limit=2000):
    return BeautifulSoup(str(value or ""), "html.parser").get_text(" ", strip=True)[:limit]


def video(row):
    bvid = row.get("bvid", "")
    if not re.fullmatch(r"BV[a-zA-Z0-9]{10}", bvid):
        raise ToolFailure("incomplete_data", "数据源缺少有效视频 ID，不能生成来源链接。")
    owner, stat = row.get("owner") or {}, row.get("stat") or {}

    def metric(value):
        return (
            value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
        )

    return {
        "video_id": bvid,
        "title": text(row.get("title"), 300),
        "url": "https://www.bilibili.com/video/" + bvid,
        "author": text(owner.get("name") or row.get("author"), 100),
        "description": text(row.get("desc") or row.get("description")),
        "area": text(row.get("tname") or row.get("typename"), 80),
        "published_at": row.get("pubdate"),
        "views": metric(stat.get("view", row.get("play"))),
        "likes": metric(stat.get("like")),
        "content_scope": "metadata_and_description_only",
    }


class PublicSources:
    ALLOWED = frozenset({"api.bilibili.com", "tieba.baidu.com"})

    def __init__(self, settings, client=None):
        self.pixiv = PixivImages(settings)
        self.enabled = settings.public_sources_enabled
        self.interval = settings.source_interval_seconds
        self.client = client or httpx.AsyncClient(
            timeout=settings.source_timeout_seconds,
            follow_redirects=False,
            headers={"User-Agent": "QQPersonalAgent/0.1"},
        )
        self.blocked, self.cache, self.last, self.robots = set(), {}, {}, {}
        self.lock = asyncio.Lock()

    async def close(self):
        await self.client.aclose()
        await self.pixiv.close()

    async def _fetch(self, url, params=None):
        u = urlsplit(url)
        if (
            u.scheme != "https"
            or u.hostname not in self.ALLOWED
            or u.port not in (None, 443)
            or u.username
        ):
            raise ToolFailure("url_not_allowed", "仅允许已配置的平台 HTTPS 来源。")
        if not self.enabled:
            raise ToolFailure("source_disabled", "公开数据源未启用，无法获取实时内容。")
        host = u.hostname
        if host in self.blocked:
            raise ToolFailure(
                "source_blocked", "该来源已返回访问限制，本进程停止访问；请稍后手动重试。"
            )
        async with self.lock:
            if host in self.blocked:
                raise ToolFailure("source_blocked", "该来源已停止访问，不执行排队中的请求。")
            delay = self.interval - (time.monotonic() - self.last.get(host, 0))
            if delay > 0:
                await asyncio.sleep(delay)
            self.last[host] = time.monotonic()
            try:
                async with self.client.stream("GET", url, params=params) as response:
                    if response.status_code in {401, 403, 412, 429} or response.is_redirect:
                        self.blocked.add(host)
                        raise ToolFailure(
                            "source_blocked", "来源要求登录、限流或重定向，已停止访问；未绕过限制。"
                        )
                    if response.status_code != 200:
                        raise ToolFailure("source_http_error", "公开来源暂不可用，未取得真实数据。")
                    chunks, size = [], 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > 1_000_000:
                            raise ToolFailure("source_too_large", "来源响应超出 1 MB 读取上限。")
                        chunks.append(chunk)
                    kind = response.headers.get("content-type", "")
                    raw = b"".join(chunks)
                    body = (
                        raw.decode("utf-8", errors="replace")
                        if "json" in kind
                        else str(BeautifulSoup(raw, "html.parser"))
                    )
            except httpx.TimeoutException:
                raise ToolFailure("source_timeout", "公开来源请求超时，未取得真实数据。") from None
            except httpx.HTTPError:
                raise ToolFailure("source_network_error", "公开来源网络连接失败。") from None
        if "json" not in kind:
            title = BeautifulSoup(body, "html.parser").title
            if title and any(
                word in title.get_text()
                for word in ("安全验证", "百度验证", "验证码", "登录", "访问验证")
            ):
                self.blocked.add(host)
                raise ToolFailure(
                    "source_blocked", "来源返回登录或安全验证页面，已停止访问；未绕过限制。"
                )
        return body

    async def _get(self, url, params=None):
        key = (url, json.dumps(params, sort_keys=True))
        cached = self.cache.get(key)
        if cached and time.monotonic() - cached[0] < 60:
            return copy.deepcopy(cached[1])
        if urlsplit(url).hostname == "tieba.baidu.com":
            if "tieba" not in self.robots:
                rules = RobotFileParser()
                rules.parse((await self._fetch("https://tieba.baidu.com/robots.txt")).splitlines())
                self.robots["tieba"] = rules
            if not self.robots["tieba"].can_fetch("QQPersonalAgent", url):
                raise ToolFailure("robots_disallowed", "来源 robots 规则不允许访问此路径。")
        value = await self._fetch(url, params)
        envelope = {
            "source_url": str(httpx.URL(url, params=params)),
            "fetched_at": datetime.now(UTC).isoformat(),
            "untrusted_data": True,
            "provider": "public_web_unofficial",
            "body": value,
        }
        # Bound process cache size; it holds only fetched public content.
        if len(self.cache) >= 32:
            self.cache.pop(next(iter(self.cache)))
        self.cache[key] = (time.monotonic(), envelope)
        return copy.deepcopy(envelope)

    async def _bili(self, path, params):
        result = await self._get("https://api.bilibili.com" + path, params)
        try:
            body = json.loads(result.pop("body"))
        except (ValueError, TypeError):
            raise ToolFailure(
                "invalid_source_data", "B 站返回非预期数据，不能作为视频事实。"
            ) from None
        if not isinstance(body, dict):
            raise ToolFailure("invalid_source_data", "来源返回结构无效。")
        if body.get("code") != 0:
            self.blocked.add("api.bilibili.com")
            raise ToolFailure(
                "source_blocked", "B 站接口拒绝当前公开访问，已停止访问；未尝试签名或登录绕过。"
            )
        data = body.get("data")
        if not isinstance(data, dict):
            raise ToolFailure("incomplete_data", "B 站返回数据缺少必要字段。")
        return result, data

    async def hot(self, area=None, limit=5):
        result, data = await self._bili("/x/web-interface/popular", {"pn": 1, "ps": 20})
        if not isinstance(data.get("list"), list):
            raise ToolFailure("incomplete_data", "热门来源缺少视频列表。")
        rows = [video(row) for row in data["list"]]
        if area:
            rows = [r for r in rows if area.casefold() in r["area"].casefold()]
        return {
            **result,
            "items": rows[:limit],
            "coverage": "first_page_of_popular_only",
            "requested_area": area,
            "complete": False,
            "note": "仅已获取候选页；不代表全站排名。空筛选结果不代表该分区没有视频。",
        }

    async def search_video(self, keyword, limit=5):
        result, data = await self._bili(
            "/x/web-interface/search/type", {"search_type": "video", "keyword": keyword, "page": 1}
        )
        return {
            **result,
            "items": [video(r) for r in (data.get("result") or [])[:limit]],
            "complete": False,
            "coverage": "first_search_page",
        }

    async def video_detail(self, video_id):
        result, data = await self._bili("/x/web-interface/view", {"bvid": video_id})
        return {
            **result,
            "item": video(data),
            "complete": False,
            "note": "仅标题、简介和元数据，未观看视频或读取字幕。",
        }

    async def search_post(self, keyword, limit=5, forum_name=None):
        params = {"ie": "utf-8", "qw": keyword}
        if forum_name:
            params["kw"] = forum_name
        result = await self._get("https://tieba.baidu.com/f/search/res", params)
        soup = BeautifulSoup(result.pop("body"), "html.parser")
        items = []
        for block in soup.select(".s_post"):
            anchor = block.select_one(".p_title a, a.bluelink")
            href = anchor.get("href", "") if anchor else ""
            match = re.fullmatch(r"(?:https://tieba.baidu.com)?/p/(\d+)(?:\?.*)?", href)
            if not match:
                continue
            items.append(
                {
                    "title": anchor.get_text(" ", strip=True)[:300],
                    "url": "https://tieba.baidu.com/p/" + match[1],
                    "snippet": text(block.get_text(" ", strip=True)),
                    "content_scope": "search_snippet_only",
                    "reply_count": None,
                }
            )
        if not items and not soup.select_one(".s_post, .s_post_list, .s_noresult"):
            raise ToolFailure("source_parse_error", "贴吧搜索页面不可识别，不能编造帖子或热度。")
        return {
            **result,
            "items": items[:limit],
            "complete": False,
            "coverage": "first_search_page_not_hot_ranking",
        }

    async def post_detail(self, post_url):
        result = await self._get(post_url)
        soup = BeautifulSoup(result.pop("body"), "html.parser")
        parts = [p.get_text(" ", strip=True) for p in soup.select(".d_post_content")]
        if not parts:
            raise ToolFailure("source_parse_error", "帖子正文不可读取，不能仅根据标题编造正文。")
        heading = soup.select_one("h3.core_title_txt, h1")
        return {
            **result,
            "url": post_url,
            "title": text(heading.get_text() if heading else "", 300),
            "content": "\n".join(parts)[:12000],
            "complete": False,
            "coverage": "first_page_visible_posts_only",
            "reply_count": None,
        }

    def register(self, registry):
        self.pixiv.register(registry)
        for name, description, arguments, handler in [
            (
                "get_bilibili_hot_videos",
                "读取 B站热门第一页，可按分区名称筛选候选。",
                HotArguments,
                self.hot,
            ),
            (
                "search_bilibili_videos",
                "搜索 B站公开视频，失败时明确数据不可用。",
                SearchArguments,
                self.search_video,
            ),
            (
                "get_bilibili_video_detail",
                "读取 BV 视频标题、简介和指标，不是视频完整内容。",
                VideoArguments,
                self.video_detail,
            ),
            (
                "search_tieba_posts",
                "搜索公开贴吧帖子，非实时热度榜；可能受安全验证限制。",
                TiebaSearchArguments,
                self.search_post,
            ),
            (
                "get_tieba_post",
                "读取公开帖子可见第一页正文，不能绕过访问限制。",
                PostArguments,
                self.post_detail,
            ),
        ]:
            registry.register(Tool(name, description, arguments, handler))
