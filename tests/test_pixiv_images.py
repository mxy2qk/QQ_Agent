import asyncio
import base64
import hashlib
import io
import json
import os
import time
from contextlib import suppress

import httpx
import pytest
from PIL import Image, UnidentifiedImageError
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from qq_agent.channels.onebot import OneBotAdapter
from qq_agent.config import Settings
from qq_agent.messages import IncomingMessage
from qq_agent.persistence import open_persistence
from qq_agent.pixiv_images import PixivImages, checked_url, prepare_image, query_tags, safe_work
from qq_agent.service import AgentService
from qq_agent.tools import ToolFailure, default_registry


def png():
    out = io.BytesIO()
    Image.new("RGB", (100, 100), (30, 120, 180)).save(out, format="PNG")
    return out.getvalue()


ROW = {
    "id": "123",
    "xRestrict": 0,
    "illustType": 0,
    "aiType": 1,
    "restrict": 0,
    "tags": ["風景"],
    "title": "test",
}
DETAIL = {
    **ROW,
    "tags": {"tags": [{"tag": "風景"}]},
    "userName": "作者",
    "urls": {"regular": "https://i.pximg.net/img/123_p0_master1200.jpg"},
}


def source(tmp_path, handler):
    s = PixivImages(
        Settings(
            _env_file=None, pixiv_images_enabled=True, pixiv_cache_dir=str(tmp_path / "cache")
        ),
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    s.interval = 0
    return s


def response(request):
    assert not request.headers.get("Authorization") and not request.headers.get("Referer")
    if request.url.path == "/robots.txt":
        return httpx.Response(200, text="User-agent: *\nAllow: /")
    if "/search/" in request.url.path:
        assert request.url.params["mode"] == "safe"
        return httpx.Response(200, json={"error": False, "body": {"illust": {"data": [ROW]}}})
    if "/ajax/illust/" in request.url.path:
        return httpx.Response(200, json={"error": False, "body": DETAIL})
    return httpx.Response(200, content=png())


async def test_public_source_cache_and_checked_asset(tmp_path):
    s = source(tmp_path, response)
    try:
        result = await s.pick("风景")
        data = s.load(result["image_id"])
        assert data.startswith(b"\xff\xd8") and len(data) <= 2 * 1024 * 1024
        assert hashlib.sha256(data).hexdigest() == result["image_id"]
        assert result["source_url"] == "https://www.pixiv.net/artworks/123"
        with pytest.raises(ToolFailure):
            s.load("../secrets")
        (s.root / (result["image_id"] + ".jpg")).write_bytes(b"corrupted")
        with pytest.raises(ToolFailure):
            s.load(result["image_id"])
    finally:
        await s.close()


@pytest.mark.parametrize(
    "change",
    [
        {"xRestrict": 1},
        {"xRestrict": 2},
        {"xRestrict": False},
        {"xRestrict": None},
        {"aiType": 2},
        {"aiType": True},
        {"illustType": 2},
        {"tags": ["R-18"]},
        {"isMasked": True},
    ],
)
def test_reject_unsupported_or_unknown_metadata(change):
    assert not safe_work({**ROW, **change})


@pytest.mark.parametrize(
    "url",
    [
        "http://i.pximg.net/a.jpg",
        "https://127.0.0.1/a",
        "https://i.pximg.net.evil.test/a",
        "https://user:password@i.pximg.net/a",
        "file:///C:/secret",
    ],
)
def test_only_fixed_https_sources(url):
    with pytest.raises(ToolFailure):
        checked_url(url)


async def test_restricted_source_does_not_retry_or_change_headers(tmp_path):
    calls = []

    def handler(r):
        calls.append(r)
        return httpx.Response(403)

    s = source(tmp_path, handler)
    try:
        for _ in range(2):
            with pytest.raises(ToolFailure, match="受限"):
                await s.pick("风景")
        assert len(calls) == 1
    finally:
        await s.close()


async def test_robots_and_no_match_do_not_download_images(tmp_path):
    def blocked(r):
        return httpx.Response(200, text="User-agent: *\nDisallow: /ajax/")

    s = source(tmp_path, blocked)
    try:
        with pytest.raises(ToolFailure, match="规则"):
            await s.find("风景")
    finally:
        await s.close()
    s = source(tmp_path, response)
    try:
        with pytest.raises(ToolFailure, match="没有符合"):
            await s.pick("风景", ["123"])
    finally:
        await s.close()


def test_bad_images_and_tags_rejected():
    for value in ("R18", "nsfw", "a " * 6):
        with pytest.raises(ToolFailure):
            query_tags(value)
    assert query_tags("初音未来 风景") == "初音ミク 風景"
    with pytest.raises(UnidentifiedImageError):
        prepare_image(b"not an image")
    with pytest.raises(ToolFailure):
        prepare_image(b"x" * (8 * 1024 * 1024 + 1))


class NoModel:
    async def generate(self, *args, **kwargs):
        pytest.fail("tag must not invoke model")


def message(text="/tag 风景", mid="tag", user="200", private=False):
    return IncomingMessage(
        platform="qq",
        chat_type="private" if private else "group",
        chat_id=user if private else "100",
        user_id=user,
        message_id=mid,
        text=text,
        is_mentioned=not private,
        public_response=not private,
    )


def cfg(tmp_path):
    return Settings(
        _env_file=None,
        database_path=str(tmp_path / "a.db"),
        checkpoint_db_path=str(tmp_path / "c.db"),
        qq_self_id="900",
        qq_allowed_groups=["100"],
        qq_allowed_group_users=["200", "201"],
        qq_allowed_users=["200"],
        qq_private_enabled=True,
        qq_reply_interval_seconds=0,
    )


@pytest.mark.parametrize("private", [False, True])
async def test_tag_plaintext_and_image_send_with_dedupe_and_cooldown(tmp_path, private):
    s = source(tmp_path, response)
    try:
        async with open_persistence(cfg(tmp_path)) as p:
            svc = AgentService(NoModel(), default_registry(), cfg(tmp_path), p)
            svc.image_source = s
            adapter = OneBotAdapter(svc, cfg(tmp_path), p.database)
            sent = []

            async def call(action, params):
                sent.append((action, params))
                return {"message_id": 1}

            adapter.call = call
            m = message(private=private)
            await adapter.process(m)
            await adapter.process(m)
            assert len(sent) == 1
            assert sent[0][0] == ("send_private_msg" if private else "send_group_msg")
            segments = sent[0][1]["message"]
            assert (
                segments[0]["type"] == "text"
                and "pixiv.net/artworks/123" in segments[0]["data"]["text"]
            )
            assert len(segments[0]["data"]["text"]) <= 70
            assert segments[1]["type"] == "image"
            assert base64.b64decode(segments[1]["data"]["file"][9:]).startswith(b"\xff\xd8")
            assert "冷却" in (await svc.handle(message(mid="second", private=private))).text
            assert p.database.turns(m) == []
    finally:
        await s.close()


async def test_source_failure_is_a_text_reply_and_unauthorized_is_rejected(tmp_path):
    s = source(tmp_path, lambda r: httpx.Response(403))
    try:
        async with open_persistence(cfg(tmp_path)) as p:
            svc = AgentService(NoModel(), default_registry(), cfg(tmp_path), p)
            svc.image_source = s
            bad = await svc.handle(message(user="202"))
            assert bad.status == "error" and not bad.image_id
            r = await svc.handle(message())
            assert r.status == "error" and "受限" in r.text and not r.image_id
    finally:
        await s.close()


async def test_cache_expiry_and_recent_work_exclusion(tmp_path):
    s = source(tmp_path, response)
    try:
        work = await s.pick("风景")
        path = s.root / (work["image_id"] + ".jpg")
        os.utime(path, (time.time() - 86401, time.time() - 86401))
        with pytest.raises(ToolFailure):
            s.load(work["image_id"])
        s.cleanup()
        assert not path.exists()
        async with open_persistence(cfg(tmp_path)) as p:
            svc = AgentService(NoModel(), default_registry(), cfg(tmp_path), p)
            svc.image_source = s
            assert (await svc.handle(message())).image_id
            # Different authorized member shares the same group's 7-day dedupe.
            result = await svc.handle(message(user="201", mid="other"))
            assert not result.image_id and "没有符合" in result.text
    finally:
        await s.close()


async def test_local_websocket_image_segment_and_ack(tmp_path):
    s = source(tmp_path, response)
    got = asyncio.Event()
    packets = []

    async def server(ws):
        async for raw in ws:
            request = json.loads(raw)
            data = {"user_id": 900} if request["action"] == "get_login_info" else {"message_id": 12}
            await ws.send(
                json.dumps({"echo": request["echo"], "status": "ok", "retcode": 0, "data": data})
            )
            if request["action"] == "get_login_info":
                await ws.send(
                    json.dumps(
                        {
                            "post_type": "message",
                            "self_id": 900,
                            "user_id": 200,
                            "group_id": 100,
                            "message_type": "group",
                            "message_id": 1,
                            "sender": {"user_id": 200},
                            "message": [
                                {"type": "at", "data": {"qq": "900"}},
                                {"type": "text", "data": {"text": "/tag 风景"}},
                            ],
                        }
                    )
                )
            else:
                packets.append(request)
                got.set()

    try:
        async with open_persistence(cfg(tmp_path)) as p, serve(server, "127.0.0.1", 0) as local:
            svc = AgentService(NoModel(), default_registry(), cfg(tmp_path), p)
            svc.image_source = s
            adapter = OneBotAdapter(svc, cfg(tmp_path), p.database)
            async with connect(
                f"ws://127.0.0.1:{local.sockets[0].getsockname()[1]}", proxy=None
            ) as ws:
                task = asyncio.create_task(adapter.session(ws))
                try:
                    await asyncio.wait_for(got.wait(), 3)
                    assert packets[0]["action"] == "send_group_msg"
                    image = packets[0]["params"]["message"][1]
                    assert image["type"] == "image" and image["data"]["file"].startswith(
                        "base64://"
                    )
                finally:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
    finally:
        await s.close()
