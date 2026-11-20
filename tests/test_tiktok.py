import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yt_dlp

from reelbot.bot import ReelBot, _register_commands
from reelbot.downloader import Downloader, DownloadError, LoginRequired, resolve_tiktok_url
from reelbot.ig_auth import SessionPool
from reelbot.links import TikTokLink, find_link, sanitize_url


VIDEO = "https://www.tiktok.com/@some.user/video/7170520270497680683"


@pytest.mark.parametrize("url, expected", [
    (VIDEO + "?is_from_webapp=1&sender_device=pc&utm_source=share#tracking", VIDEO),
    ("http://m.tiktok.com/@some.user/video/7170520270497680683/", VIDEO),
    ("tiktok.com/@some.user/video/7170520270497680683", VIDEO),
    ("https://vm.tiktok.com/ZTR45GpSF/?_t=123&_r=1", "https://vm.tiktok.com/ZTR45GpSF/"),
    ("https://VT.TIKTOK.COM/ZSe4FqkKd#tracking", "https://vt.tiktok.com/ZSe4FqkKd/"),
    ("https://www.tiktok.com/t/ZTRC5xgJp?share_app_id=123", "https://www.tiktok.com/t/ZTRC5xgJp/"),
    ("https://www.tiktok.com/share/video/7170520270497680683?tracking=1", "https://www.tiktok.com/@/video/7170520270497680683"),
])
def test_shared_tiktok_links_are_sanitized(url, expected):
    link = find_link(f"watch this: <{url}>")
    assert isinstance(link, TikTokLink)
    assert link.url == expected
    assert sanitize_url(url) == expected
    assert sanitize_url(expected) == expected
    assert link.embed_url("instagramfix.com") == expected


@pytest.mark.parametrize("url", [
    "https://evil-tiktok.com/@some.user/video/123",
    "https://www.tiktok.com.evil.example/@some.user/video/123",
    "https://evil.example/tiktok.com/@some.user/video/123",
    "https://evil.example@www.tiktok.com/@some.user/video/123",
    "https://www.tiktok.com/@some.user",
    "https://www.tiktok.com/@some.user/live",
    "https://www.tiktok.com/@some.user/photo/123",
    "https://www.tiktok.com/@some.user/video/123abc",
])
def test_unsupported_tiktok_urls_rejected(url):
    assert find_link(url) is None
    with pytest.raises(ValueError):
        sanitize_url(url)


def test_first_supported_link_in_message_wins():
    ig = "https://www.instagram.com/reel/ABC/"
    assert isinstance(find_link(f"{VIDEO} then {ig}"), TikTokLink)
    assert find_link(f"{ig} then {VIDEO}").platform == "instagram"


def fake_response(status, location=None):
    class Response:
        status_code = status
        headers = {"Location": location} if location else {}
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    return Response()


def fake_session(monkeypatch, responses):
    seen = []
    class Session:
        trust_env = True
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def get(self, url, **kwargs):
            assert self.trust_env is False
            assert kwargs["allow_redirects"] is False
            seen.append(url)
            return responses.pop(0)
    monkeypatch.setattr("reelbot.downloader.requests.Session", Session)
    return seen


def test_short_redirect_targets_are_sanitized_before_following(monkeypatch):
    seen = fake_session(monkeypatch, [
        fake_response(301, "https://www.tiktok.com/t/SECOND/?_t=tracking"),
        fake_response(302, VIDEO + "?_t=tracking#fragment"),
    ])
    assert resolve_tiktok_url("https://vm.tiktok.com/FIRST/?_r=1") == VIDEO
    assert seen == ["https://vm.tiktok.com/FIRST/", "https://www.tiktok.com/t/SECOND/"]


@pytest.mark.parametrize("target", ["http://127.0.0.1/private", "https://www.instagram.com/reel/ABC/", "https://evil.example/"])
def test_short_link_never_requests_redirect_outside_tiktok(monkeypatch, target):
    seen = fake_session(monkeypatch, [fake_response(302, target)])
    with pytest.raises(DownloadError):
        resolve_tiktok_url("https://vt.tiktok.com/FIRST/")
    assert seen == ["https://vt.tiktok.com/FIRST/"]


def test_short_redirect_loop_is_bounded(monkeypatch):
    seen = fake_session(monkeypatch, [fake_response(302, "https://vm.tiktok.com/LOOP/")] * 5)
    with pytest.raises(DownloadError, match="too many redirects"):
        resolve_tiktok_url("https://vm.tiktok.com/LOOP/")
    assert len(seen) == 5


def test_tiktok_download_is_anonymous_and_uses_canonical_url(tmp_path, monkeypatch):
    captured = {}
    class YDL:
        def __init__(self, opts):
            captured.update(opts)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def extract_info(self, url, **kwargs):
            assert url == VIDEO
            return {"formats": [{"format_id": "mp4"}], "id": "123"}
        def process_info(self, info):
            (tmp_path / "001_123.mp4").write_bytes(b"video")
        def prepare_filename(self, info):
            return str(tmp_path / "001_123.mp4")
    monkeypatch.setattr("reelbot.downloader.yt_dlp.YoutubeDL", YDL)
    d = Downloader(cookies=Path("instagram-cookies.txt"), proxies=["http://ig-proxy.test:80"])
    paths = d._ytdlp_sync(VIDEO + "?sender_device=pc", tmp_path, 1000)
    assert len(paths) == 1
    assert "cookiefile" not in captured
    assert captured["proxy"] == ""
    assert captured["noplaylist"] is True
    assert captured["ignore_no_formats_error"] is False


@pytest.mark.parametrize("message", ["HTTP Error 429", "login required", "HTTP Error 403", "private video"])
def test_tiktok_failures_do_not_use_instagram_accounts_or_proxies(tmp_path, monkeypatch, message):
    class YDL:
        def __init__(self, opts):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def extract_info(self, *args, **kwargs):
            raise yt_dlp.utils.DownloadError(message)
    monkeypatch.setattr("reelbot.downloader.yt_dlp.YoutubeDL", YDL)
    auth = SimpleNamespace(fetch=AsyncMock())
    d = Downloader(auth=auth, proxies=["http://ig-proxy.test:80"])
    d._via_proxies = AsyncMock()
    with pytest.raises(DownloadError, match="TikTok"):
        asyncio.run(d.fetch(find_link(VIDEO), tmp_path, 1000))
    auth.fetch.assert_not_awaited()
    d._via_proxies.assert_not_awaited()


def test_instagram_login_required_cannot_be_used_for_tiktok(tmp_path):
    auth = SimpleNamespace(fetch=AsyncMock())
    d = Downloader(auth=auth)
    d._download_any = AsyncMock(side_effect=LoginRequired("login"))
    with pytest.raises(LoginRequired):
        asyncio.run(d.fetch(find_link(VIDEO), tmp_path, 1000))
    auth.fetch.assert_not_awaited()


def test_auth_worker_rejects_tiktok_before_login(tmp_path, monkeypatch):
    from reelbot import ig_auth_worker
    monkeypatch.setattr(ig_auth_worker, "login", lambda *args: pytest.fail("Instagram login must not run"))
    assert ig_auth_worker.run(dict(url=VIDEO, proxy="http://ig-proxy.test:80", cookies=str(tmp_path / "missing.txt"), username="fake", password="fake")) == "download"


def test_session_pool_rejects_tiktok_before_worker(tmp_path):
    pool = SessionPool(None, ["http://ig-proxy.test:80"], tmp_path / "sessions")
    pool._worker = AsyncMock()
    with pytest.raises(DownloadError, match="only be used for Instagram"):
        asyncio.run(pool.fetch(VIDEO, tmp_path, 1000))
    pool._worker.assert_not_awaited()
    assert not pool.directory.exists()


def test_reject_instagram_credentials_in_direct_tiktok_download(tmp_path):
    d = Downloader()
    with pytest.raises(DownloadError, match="cannot be used for TikTok"):
        d._ytdlp_sync(VIDEO, tmp_path, 1000, proxy="http://ig-proxy.test:80", cookies=Path("private.txt"))


def test_tiktok_fallback_uses_clean_original_link():
    interaction = SimpleNamespace(edit_original_response=AsyncMock())
    bot = SimpleNamespace(config=SimpleNamespace(embed_fixer_domain="instagramfix.com"))
    asyncio.run(ReelBot._send_fallback(bot, interaction, find_link(VIDEO + "?_t=tracking"), "download failed"))
    content = interaction.edit_original_response.call_args.kwargs["content"]
    assert VIDEO in content
    assert "instagramfix" not in content and "tracking" not in content


def test_tiktok_command_routes_through_existing_access_control(tmp_path):
    from reelbot.allowlist import Allowlist
    # No Discord connection: invoke the command callback registered by the real bot.
    import discord
    from discord import app_commands
    client = discord.Client(intents=discord.Intents.none())
    bot = SimpleNamespace(tree=app_commands.CommandTree(client), handle_reel=AsyncMock())
    _register_commands(bot)
    interaction = SimpleNamespace()
    asyncio.run(bot.tree.get_command("reel").callback(interaction, VIDEO))
    bot.handle_reel.assert_awaited_once_with(interaction, VIDEO, "slash")
    denied = SimpleNamespace(user=SimpleNamespace(id=2), response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()))
    private = SimpleNamespace(allowlist=Allowlist(tmp_path / "allow.json", 1), downloader=SimpleNamespace(fetch=AsyncMock()))
    asyncio.run(ReelBot.handle_reel(private, denied, VIDEO, "slash"))
    denied.response.send_message.assert_awaited_once_with("This bot is private.", ephemeral=True)
    private.downloader.fetch.assert_not_awaited()
