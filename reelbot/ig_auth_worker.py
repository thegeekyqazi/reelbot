"""Isolated login and download worker; stdout contains only a fixed status code."""
from __future__ import annotations

import contextlib
import http.cookiejar
import io
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch
from urllib.parse import urlparse


def proxy_send(proxy, send):
    """Force the assigned proxy even on sessions Instaloader creates for 2FA."""
    if urlparse(proxy).scheme not in ("http", "https", "socks5", "socks5h"):
        raise ValueError("a supported proxy is required")

    def locked(session, request, **kwargs):
        kwargs["proxies"] = {"http": proxy, "https": proxy}
        kwargs["verify"] = True
        return send(session, request, **kwargs)
    return locked


def login(payload, cookies: Path) -> str:
    import instaloader
    import requests
    from .ig_auth import totp

    # A process-local transport guard covers the initial login and all 2FA sessions.
    # No shared bot thread or environment variable is patched.
    with patch.object(requests.Session, "send", proxy_send(payload["proxy"], requests.Session.send)):
        loader = instaloader.Instaloader(quiet=True, max_connection_attempts=1, request_timeout=15)
        try:
            try:
                loader.login(payload["username"], payload["password"])
            except instaloader.TwoFactorAuthRequiredException:
                if not payload.get("totp_secret"):
                    return "challenge"
                loader.two_factor_login(totp(payload["totp_secret"]))
            jar = http.cookiejar.MozillaCookieJar()
            for cookie in loader.context._session.cookies:
                if cookie.value and cookie.domain.lstrip(".") in ("instagram.com", "www.instagram.com", "i.instagram.com"):
                    jar.set_cookie(cookie)
            if not any(c.name == "sessionid" and c.value for c in jar):
                return "login"
            fd, name = tempfile.mkstemp(dir=cookies.parent, prefix=".cookies-")
            os.close(fd)
            try:
                jar.save(name, ignore_discard=True, ignore_expires=True)
                os.replace(name, cookies)
            finally:
                Path(name).unlink(missing_ok=True)
            return "ok"
        except requests.RequestException:
            return "network"
        except instaloader.LoginException as exc:
            return "challenge" if any(word in str(exc).lower() for word in ("checkpoint", "challenge", "disabled", "suspended")) else "login"
        finally:
            loader.close()


def run(payload) -> str:
    from .downloader import Downloader, DownloadError, LoginRequired, RateLimited, RESTRICTED_MSG
    from .links import ReelLink, find_link, sanitize_url

    try:
        url = sanitize_url(payload["url"])
    except ValueError:
        return "download"
    if not isinstance(find_link(url), ReelLink):
        return "download"

    proxy = payload.get("proxy")
    if not proxy or urlparse(proxy).scheme not in ("http", "https", "socks5", "socks5h"):
        return "network"
    # gallery-dl inherits the same proxy; no environment-based direct bypass.
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ[key] = proxy
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = ""
    cookies = Path(payload["cookies"])
    if not cookies.exists():
        if not payload.get("username"):
            return "login"
        status = login(payload, cookies)
        if status != "ok":
            return status
    outdir = Path(payload["outdir"])
    downloader = Downloader()
    try:
        if downloader._ytdlp_sync(url, outdir, payload["limit"], proxy, cookies):
            return "ok"
    except LoginRequired as exc:
        if str(exc) == RESTRICTED_MSG:
            return "restricted"
        # Never hammer a stale session with password logins. Invalidate once;
        # the parent imposes a cooldown before a future login attempt.
        if payload.get("username"):
            cookies.unlink(missing_ok=True)
        return "login"
    except RateLimited:
        return "rate_limited"
    except DownloadError:
        pass
    if downloader.has_gallery_dl:
        # Authenticated fallback only; both extraction and media use proxy env.
        result = subprocess.run([
            sys.executable, "-m", "gallery_dl", "--config-ignore", "--quiet",
            "--no-mtime", "--no-part", "--directory", str(outdir),
            "--filename", "{num:>03}_{media_id}.{extension}", "--range", "1-20",
            "--http-timeout", "15", "-o", "extractor.instagram.videos=merged",
            "-o", f"cache.file={outdir / '.cache.sqlite3'}", "--cookies", str(cookies), url,
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=90)
        if result.returncode == 0 and downloader._collect(outdir):
            return "ok"
    return "download"


def main():
    logging.disable(logging.CRITICAL)
    try:
        payload = json.load(sys.stdin)
        # Libraries sometimes print login responses. Never forward any of that.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            status = run(payload)
    except Exception:
        status = "download"
    print(json.dumps({"status": status}))


if __name__ == "__main__":
    main()
