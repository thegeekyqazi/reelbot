import asyncio
import json
import http.cookiejar
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from reelbot.config import ConfigError, load_config
from reelbot.downloader import Downloader, DownloadError, LoginRequired
from reelbot.ig_auth import Account, SessionPool, load_accounts, totp
from reelbot.ig_auth_worker import proxy_send
from reelbot.links import ReelLink


def accounts_file(tmp_path, text="testuser:test-password\n"):
    path = tmp_path / "accounts.txt"
    path.write_text(text)
    return path


def test_parse_five_fields_and_do_not_keep_mail_credentials(tmp_path):
    path = accounts_file(tmp_path, "testuser:test-password:mail@example.test:mail-password:GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ\n")
    account, = load_accounts(path)
    assert account == Account("testuser", "test-password", "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ")
    assert not hasattr(account, "email")
    assert "test-password" not in repr(account)


def test_invalid_account_errors_do_not_include_secrets(tmp_path):
    path = accounts_file(tmp_path, "testuser:unique-password:bad!key-value\n")
    with pytest.raises(ConfigError) as exc:
        load_accounts(path)
    assert "unique-password" not in str(exc.value)
    assert "bad!key-value" not in str(exc.value)


def test_totp_rfc6238_vector():
    assert totp("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ", 59) == "287082"


def test_accounts_require_proxy(tmp_path):
    path = accounts_file(tmp_path)
    with pytest.raises(ConfigError, match="IG_PROXY"):
        load_config({"DISCORD_TOKEN": "test", "OWNER_ID": "111", "IG_ACCOUNTS": str(path)})
    config = load_config({"DISCORD_TOKEN": "test", "OWNER_ID": "111", "IG_ACCOUNTS": str(path), "IG_PROXY": "http://proxy.test:80"})
    assert config.ig_accounts == path


def test_refuse_cookie_download_without_proxy(tmp_path):
    with pytest.raises(DownloadError, match="require a proxy"):
        Downloader()._ytdlp_sync("https://www.instagram.com/p/TEST/", tmp_path, 1000, cookies=Path("cookies.txt"))


def test_anonymous_downloader_does_not_load_configured_cookies(tmp_path, monkeypatch):
    options = []
    class FakeYDL:
        def __init__(self, opts):
            options.append(opts)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def extract_info(self, *args, **kwargs):
            return {"thumbnails": []}
    monkeypatch.setattr("reelbot.downloader.yt_dlp.YoutubeDL", FakeYDL)
    Downloader(cookies=Path("private-cookies.txt"))._ytdlp_sync("https://www.instagram.com/p/TEST/", tmp_path, 1000)
    assert "cookiefile" not in options[0]


def test_proxy_transport_overrides_direct_and_environment_bypass(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "*")
    captured = []
    def send(session, request, **kwargs):
        captured.append(kwargs)
        return "ok"
    guarded = proxy_send("http://proxy.test:80", send)
    assert guarded(None, None, proxies={}, verify=False) == "ok"
    assert captured == [{"proxies": {"http": "http://proxy.test:80", "https": "http://proxy.test:80"}, "verify": True}]
    with pytest.raises(ValueError):
        proxy_send("", send)


def test_restricted_retry_and_public_download_does_not_authenticate(tmp_path):
    auth = SimpleNamespace(fetch=AsyncMock())
    d = Downloader(auth=auth)
    async def anon(url, outdir, *args):
        raise LoginRequired("needs login")
    async def authenticated(url, outdir, limit):
        path = outdir / "reel.mp4"
        path.write_bytes(b"video")
        return [path]
    d._ytdlp = anon
    auth.fetch.side_effect = authenticated
    asyncio.run(d.fetch(ReelLink("reel", "TEST"), tmp_path, 1000))
    auth.fetch.assert_awaited_once()
    auth.fetch.reset_mock()
    public_dir = tmp_path / "public"
    public_dir.mkdir()
    d._ytdlp = authenticated
    # Anonymous callback has an extra threads parameter.
    async def public(url, outdir, threads, limit, proxy=None):
        return await authenticated(url, outdir, limit)
    d._ytdlp = public
    asyncio.run(d.fetch(ReelLink("reel", "TEST"), public_dir, 1000))
    auth.fetch.assert_not_awaited()


def test_proxy_login_required_reaches_authenticated_retry(tmp_path):
    auth = SimpleNamespace(fetch=AsyncMock(return_value=[]))
    d = Downloader(auth=auth, proxies=["http://proxy.test:80"])
    d._download_any = AsyncMock(side_effect=LoginRequired("restricted through proxy"))
    with pytest.raises(DownloadError):
        asyncio.run(d.fetch(ReelLink("p", "TEST"), tmp_path, 1000))
    auth.fetch.assert_awaited_once()


def test_session_binding_survives_restart_and_proxy_reordering(tmp_path):
    accounts = accounts_file(tmp_path)
    directory = tmp_path / "sessions"
    seen = []
    async def worker(payload):
        seen.append(payload)
        (Path(payload["outdir"]) / "reel.mp4").write_bytes(b"video")
        return {"status": "ok"}
    for proxies, subdir in [(["http://a.test:80", "http://b.test:80"], "first"), (["http://b.test:80", "http://a.test:80"], "second")]:
        pool = SessionPool(accounts, proxies, directory)
        pool._worker = worker
        outdir = tmp_path / subdir
        outdir.mkdir()
        asyncio.run(pool.fetch("https://www.instagram.com/p/TEST/", outdir, 1000))
    assert [p["proxy"] for p in seen] == ["http://a.test:80"] * 2
    assert len({p["cookies"] for p in seen}) == 1
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(next(directory.glob("*.json")).stat().st_mode) == 0o600


@pytest.mark.parametrize("status", ["login", "network", "challenge"])
def test_failed_account_not_retried_after_restart(tmp_path, status):
    path = accounts_file(tmp_path)
    directory = tmp_path / "sessions"
    pool = SessionPool(path, ["http://proxy.test:80"], directory)
    pool._worker = AsyncMock(return_value={"status": status})
    with pytest.raises(DownloadError):
        asyncio.run(pool.fetch("https://www.instagram.com/p/TEST/", tmp_path, 1000))
    restarted = SessionPool(path, ["http://proxy.test:80"], directory)
    restarted._worker = AsyncMock()
    with pytest.raises(DownloadError, match="no account session is ready"):
        asyncio.run(restarted.fetch("https://www.instagram.com/p/TEST/", tmp_path, 1000))
    restarted._worker.assert_not_awaited()


def test_next_account_can_be_used_without_repeated_login_of_failed_account(tmp_path):
    path = accounts_file(tmp_path, "first:pw\nsecond:pw\n")
    pool = SessionPool(path, ["http://a.test:80", "http://b.test:80"], tmp_path / "sessions")
    seen = []
    async def worker(payload):
        seen.append((payload["username"], payload["proxy"]))
        return {"status": "login"}
    pool._worker = worker
    for _ in range(2):
        with pytest.raises(DownloadError):
            asyncio.run(pool.fetch("https://www.instagram.com/p/TEST/", tmp_path, 1000))
    assert seen == [("first", "http://a.test:80"), ("second", "http://b.test:80")]


def test_healthy_account_is_reused_before_logging_in_another(tmp_path):
    path = accounts_file(tmp_path, "first:pw\nsecond:pw\n")
    pool = SessionPool(path, ["http://proxy.test:80"], tmp_path / "sessions")
    seen = []
    async def worker(payload):
        seen.append(payload["username"])
        (Path(payload["outdir"]) / "reel.mp4").write_bytes(b"video")
        return {"status": "ok"}
    pool._worker = worker
    async def probe():
        for _ in range(2):
            await pool.fetch("https://www.instagram.com/p/TEST/", tmp_path, 1000)
    asyncio.run(probe())
    assert seen == ["first", "first"]


def test_worker_never_prints_library_output_or_exception(monkeypatch, capsys):
    import io
    from reelbot import ig_auth_worker
    def fail(payload):
        print("secret-cookie")
        raise RuntimeError("secret-password")
    monkeypatch.setattr(ig_auth_worker, "run", fail)
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    ig_auth_worker.main()
    assert capsys.readouterr().out.strip() == '{"status": "download"}'


def test_login_and_two_factor_use_forced_proxy_and_save_private_cookies(tmp_path, monkeypatch):
    import instaloader
    import requests
    from reelbot.ig_auth_worker import login
    sent = []
    def transport(session, request, **kwargs):
        sent.append(kwargs["proxies"])
    monkeypatch.setattr(requests.Session, "send", transport)
    cookie = http.cookiejar.Cookie(0, "sessionid", "test-session-value", None, False,
        ".instagram.com", True, True, "/", True, True, None, True, None, None, {})
    steps = []
    class Loader:
        def __init__(self, **kwargs):
            self.context = SimpleNamespace(_session=requests.Session())
        def login(self, username, password):
            steps.append((username, password))
            requests.Session().get("https://www.instagram.com/")
            raise instaloader.TwoFactorAuthRequiredException()
        def two_factor_login(self, code):
            assert code.isdigit() and len(code) == 6
            requests.Session().post("https://www.instagram.com/accounts/login/ajax/two_factor/")
            self.context._session.cookies.set_cookie(cookie)
        def close(self):
            pass
    monkeypatch.setattr(instaloader, "Instaloader", Loader)
    path = tmp_path / "cookies.txt"
    proxy = "http://proxy.test:80"
    assert login(dict(username="testuser", password="test-password", proxy=proxy,
        totp_secret="GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"), path) == "ok"
    assert sent == [{"http": proxy, "https": proxy}] * 2
    assert steps == [("testuser", "test-password")]
    jar = http.cookiejar.MozillaCookieJar(str(path))
    jar.load(ignore_discard=True, ignore_expires=True)
    assert any(c.name == "sessionid" for c in jar)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_auth_worker_reuses_cookies_without_password_login(tmp_path, monkeypatch):
    from reelbot import ig_auth_worker
    path = tmp_path / "cookies.txt"
    path.write_text("test")
    monkeypatch.setattr(ig_auth_worker, "login", lambda *args: pytest.fail("should reuse session"))
    seen = []
    def fetch(self, url, outdir, limit, proxy, cookies):
        seen.append((proxy, cookies))
        assert os.environ["NO_PROXY"] == ""
        assert os.environ["HTTPS_PROXY"] == proxy
        return [outdir / "video.mp4"]
    import os
    monkeypatch.setattr(Downloader, "_ytdlp_sync", fetch)
    # run() is normally isolated; undo its environment changes in this test.
    with monkeypatch.context() as env_patch:
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy", "NO_PROXY", "no_proxy"):
            env_patch.setenv(key, "previous")
        assert ig_auth_worker.run(dict(proxy="http://proxy.test:80", cookies=str(path),
            url="https://www.instagram.com/p/TEST/", outdir=str(tmp_path), limit=1000)) == "ok"
    assert seen == [("http://proxy.test:80", path)]


def test_slot_held_until_every_timed_out_worker_finishes(tmp_path):
    async def probe():
        d = Downloader(max_concurrent=1)
        loop = asyncio.get_running_loop()
        first, second = loop.create_future(), loop.create_future()
        async def download(link, workdir, threads, limit, progress):
            threads.extend([first, second])
            raise DownloadError("timed out")
        d._download_any = download
        with pytest.raises(DownloadError):
            await d.fetch(ReelLink("p", "TEST"), tmp_path, 1000)
        first.set_result([])
        await asyncio.sleep(0)
        assert d._sem.locked()
        second.set_exception(RuntimeError("finished with error"))
        await asyncio.sleep(0)
        assert not d._sem.locked()
    asyncio.run(probe())
