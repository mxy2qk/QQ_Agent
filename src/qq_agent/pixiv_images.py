"""Bounded public Pixiv adapter. No login, proxies, access-control workarounds or adult mode."""

import asyncio
import hashlib
import io
import json
import random
import re
import time
import warnings
from pathlib import Path
from urllib.parse import quote, urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from PIL import Image, ImageOps
from pydantic import Field

from qq_agent.tools import Tool, ToolArguments, ToolFailure

AGENT = "QQPersonalAgent/0.1"
DENIED = re.compile(
    r"r[-_ ]?18|r[-_ ]?16|nsfw|porn|hentai|sex|性交|性器|乳首|全裸|露骨|色情|裸露|ヌード|エロ|ロリ|萝莉|幼女|ショタ|shota|loli",
    re.IGNORECASE,
)
ALIASES = {
    "风景": "風景",
    "夜景": "夜景",
    "初音未来": "初音ミク",
    "白发": "白髪",
    "蓝眼睛": "青い目",
}


class TagArguments(ToolArguments):
    tags: str = Field(default="风景", min_length=1, max_length=120)


def query_tags(value):
    parts = value.split()
    if not parts or len(parts) > 5 or any(len(p) > 32 for p in parts):
        raise ToolFailure("invalid_tags", "请输入1～5个标签，每个最多32字。")
    if DENIED.search(value):
        raise ToolFailure(
            "unsupported_content", "不提供露骨或年龄不明确的性内容，请换一个非露骨主题标签。"
        )
    return " ".join(ALIASES.get(p, p) for p in parts)


def safe_work(row, detail=False):
    if not isinstance(row, dict):
        return False
    if type(row.get("xRestrict")) is not int or row["xRestrict"] != 0:
        return False
    if (
        type(row.get("illustType")) is not int
        or row["illustType"] != 0
        or type(row.get("aiType")) is not int
        or row["aiType"] != 1
    ):
        return False
    if row.get("restrict", 0) != 0 or row.get("isMasked") or row.get("isUnlisted"):
        return False
    tags = row.get("tags")
    if detail:
        tags = tags.get("tags") if isinstance(tags, dict) else None
        if not isinstance(tags, list) or any(
            not isinstance(t, dict) or not isinstance(t.get("tag"), str) for t in tags
        ):
            return False
        tags = [t["tag"] for t in tags]
    if not isinstance(tags, list) or any(not isinstance(t, str) for t in tags):
        return False
    return not DENIED.search(" ".join(tags) + " " + str(row.get("title", "")))


def checked_url(url):
    u = urlsplit(url)
    if (
        u.scheme != "https"
        or u.hostname not in {"www.pixiv.net", "i.pximg.net"}
        or u.username
        or u.password
        or u.port not in {None, 443}
        or u.fragment
    ):
        raise ToolFailure("invalid_source_url", "图片来源地址无效。")
    return u


def prepare_image(data):
    if len(data) > 8 * 1024 * 1024:
        raise ToolFailure("image_too_large", "图片过大，请换一个标签。")
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(data)) as im:
            if im.format not in {"JPEG", "PNG", "WEBP"} or getattr(im, "n_frames", 1) != 1:
                raise ToolFailure("unsupported_image", "只支持静态插画。")
            if im.width * im.height > 20_000_000 or min(im.size) < 64:
                raise ToolFailure("invalid_dimensions", "图片尺寸不合适，请换一张。")
            im.load()
            im = ImageOps.exif_transpose(im).convert("RGB")
            im.thumbnail((1600, 1600))
            output = io.BytesIO()
            im.save(output, format="JPEG", quality=85)
            result = output.getvalue()
            if len(result) > 2 * 1024 * 1024:
                raise ToolFailure("image_too_large", "图片压缩后仍过大。")
            return result


class PixivImages:
    def __init__(self, settings, client=None):
        self.enabled = settings.pixiv_images_enabled
        self.root = Path(settings.pixiv_cache_dir).resolve()
        self.client = client or httpx.AsyncClient(
            timeout=10, follow_redirects=False, headers={"User-Agent": AGENT}
        )
        self.robots, self.lock, self.last = {}, asyncio.Lock(), 0
        self.blocked_until = 0
        self.interval = 1

    async def close(self):
        await self.client.aclose()

    async def raw(self, url, limit):
        checked_url(url)
        if time.monotonic() < self.blocked_until:
            raise ToolFailure("source_restricted", "Pixiv访问受限，请稍后再试。")
        async with self.lock:
            await asyncio.sleep(max(0, self.interval - (time.monotonic() - self.last)))
            self.last = time.monotonic()
            async with self.client.stream("GET", url) as response:
                if response.status_code in {401, 403, 429}:
                    self.blocked_until = time.monotonic() + 300
                    raise ToolFailure("source_restricted", "Pixiv访问受限，已暂停请求。")
                if response.status_code == 404:
                    return None
                if response.status_code != 200:
                    raise ToolFailure("source_unavailable", "Pixiv暂时不可访问，请稍后重试。")
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > limit:
                        raise ToolFailure("response_too_large", "来源返回内容过大。")
                return bytes(data)

    async def fetch(self, url, image=False):
        u = checked_url(url)
        if u.hostname not in self.robots:
            raw = await self.raw("https://" + u.hostname + "/robots.txt", 128_000)
            parser = RobotFileParser()
            parser.parse(raw.decode("utf-8", errors="replace").splitlines() if raw else [])
            self.robots[u.hostname] = parser
        if not self.robots[u.hostname].can_fetch(AGENT, url):
            raise ToolFailure("robots_disallowed", "来源规则不允许读取该路径。")
        data = await self.raw(url, 8 * 1024 * 1024 if image else 2 * 1024 * 1024)
        if data is None:
            raise ToolFailure("not_found", "作品已不可访问。")
        return data

    async def envelope(self, url):
        result = json.loads(await self.fetch(url))
        if result.get("error") is not False or not isinstance(result.get("body"), dict):
            raise ToolFailure("invalid_source_data", "Pixiv未返回可用作品。")
        return result["body"]

    async def find(self, tags="风景", excluded=()):
        if not self.enabled:
            raise ToolFailure("disabled", "Pixiv图片功能尚未启用。")
        query = query_tags(tags)
        url = "https://www.pixiv.net/ajax/search/illustrations/" + quote(query, safe="")
        url = str(
            httpx.URL(
                url,
                params={
                    "word": query,
                    "mode": "safe",
                    "order": "date_d",
                    "p": "1",
                    "s_mode": "s_tag",
                    "type": "illust",
                    "lang": "zh",
                },
            )
        )
        body = await self.envelope(url)
        rows = body.get("illust", {}).get("data", [])
        if not isinstance(rows, list):
            raise ToolFailure("invalid_source_data", "作品列表格式异常。")
        candidates = [
            r
            for r in rows[:100]
            if safe_work(r)
            and all(
                any(part.casefold() in tag.casefold() for tag in r["tags"])
                for part in query.split()
            )
            and re.fullmatch(r"[1-9][0-9]{0,19}", str(r.get("id", "")))
            and str(r["id"]) not in excluded
        ]
        random.SystemRandom().shuffle(candidates)
        for row in candidates[:3]:
            pid = str(row["id"])
            detail = await self.envelope("https://www.pixiv.net/ajax/illust/" + pid)
            if str(detail.get("id")) != pid or not safe_work(detail, True):
                continue
            if not all(
                any(part.casefold() in tag["tag"].casefold() for tag in detail["tags"]["tags"])
                for part in query.split()
            ):
                continue
            image_url = detail.get("urls", {}).get("regular", "")
            parsed = checked_url(image_url)
            if parsed.hostname != "i.pximg.net" or not re.search(
                r"/" + re.escape(pid) + r"_p0(?:_|\.)", parsed.path
            ):
                continue
            return {
                "pid": pid,
                "author": str(detail.get("userName", ""))[:80],
                "source_url": "https://www.pixiv.net/artworks/" + pid,
                "image_url": image_url,
                "content_scope": "public_metadata_filtered_unofficial",
            }
        raise ToolFailure("no_match", "没有符合条件且近期未发过的插画，请换个标签。")

    def cleanup(self):
        self.root.mkdir(parents=True, exist_ok=True)
        files = [
            p
            for p in self.root.glob("*.jpg")
            if re.fullmatch(r"[a-f0-9]{64}\.jpg", p.name) and p.is_file() and not p.is_symlink()
        ]
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        total = 0
        for p in files:
            total += p.stat().st_size
            if time.time() - p.stat().st_mtime > 86400 or total > 200 * 1024 * 1024:
                p.unlink()

    async def pick(self, tags="风景", excluded=()):
        async with asyncio.timeout(25):
            work = await self.find(tags, excluded)
            raw = await self.fetch(work["image_url"], image=True)
            data = await asyncio.to_thread(prepare_image, raw)
            self.cleanup()
            asset = hashlib.sha256(data).hexdigest()
            target = self.root / (asset + ".jpg")
            if target.is_symlink():
                raise ToolFailure("invalid_cache", "图片缓存异常。")
            target.write_bytes(data)
            self.cleanup()
            return {**work, "image_id": asset}

    def load(self, asset):
        if not re.fullmatch(r"[a-f0-9]{64}", asset):
            raise ToolFailure("invalid_asset", "图片编号无效。")
        target = self.root / (asset + ".jpg")
        if (
            target.is_symlink()
            or not target.is_file()
            or time.time() - target.stat().st_mtime > 86400
            or target.stat().st_size > 2 * 1024 * 1024
        ):
            raise ToolFailure("expired_asset", "图片缓存已过期，请重新/tag。")
        data = target.read_bytes()
        if hashlib.sha256(data).hexdigest() != asset:
            raise ToolFailure("invalid_asset", "图片缓存校验失败。")
        return data

    def register(self, registry):
        registry.register(
            Tool(
                "find_pixiv_image",
                "按标签随机选择非露骨Pixiv插画的公开元数据，不发送图片。",
                TagArguments,
                self.find,
            )
        )
