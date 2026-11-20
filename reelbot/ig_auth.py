"""Private account sessions, with a fixed proxy per account and bounded login attempts.

Authenticated network operations run in a disposable process. Secrets travel over
stdin, never command arguments, and raw library output is never forwarded to logs.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import signal
import struct
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .config import ConfigError

AUTH_TIMEOUT = 210
COOLDOWN = 3600
FAILURES = {
    "login": "Instagram login failed; that account is paused for an hour",
    "challenge": "Instagram requires an account check; that account is paused until reviewed",
    "network": "the account's proxy is unavailable; that account is paused for an hour",
    "rate_limited": "Instagram rate-limited the signed-in account; that account is paused for an hour",
    "restricted": "the signed-in account cannot access that post",
    "download": "the authenticated download failed",
    "timeout": "the authenticated download timed out; that account is paused for an hour",
}


@dataclass(repr=False, frozen=True)
class Account:
    username: str
    password: str
    totp_secret: str = ""


def load_accounts(path: Path) -> list[Account]:
    accounts = []
    for number, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = line.strip().split(":")
        if len(fields) not in (2, 3, 5) or not fields[0] or not fields[1]:
            raise ConfigError(f"Invalid IG_ACCOUNTS record on line {number}; expected 2, 3, or 5 colon-separated fields")
        secret = fields[-1].replace(" ", "").upper() if len(fields) in (3, 5) else ""
        if secret:
            try:
                base64.b32decode(secret + "=" * (-len(secret) % 8))
            except (ValueError, base64.binascii.Error):
                raise ConfigError(f"Invalid two-factor secret on IG_ACCOUNTS line {number}") from None
        accounts.append(Account(fields[0], fields[1], secret))
    if not accounts or len({a.username.lower() for a in accounts}) != len(accounts):
        raise ConfigError("IG_ACCOUNTS must contain distinct, nonempty account records")
    return accounts


def totp(secret: str, now: float | None = None) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    digest = hmac.new(key, struct.pack(">Q", int(time.time() if now is None else now) // 30), hashlib.sha1).digest()
    offset = digest[-1] & 15
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7fffffff
    return f"{value % 1000000:06d}"


def private_json(path: Path, data) -> None:
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".auth-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


class SessionPool:
    def __init__(self, accounts: Path | None, proxies, directory: Path, cookies: Path | None = None):
        if not proxies:
            raise ConfigError("Authenticated Instagram access requires IG_PROXY")
        self.accounts = load_accounts(accounts) if accounts else []
        self.proxies = tuple(proxies)
        self.directory = directory.resolve()
        self.cookies = cookies.resolve() if cookies else None
        self._lock = asyncio.Lock()
        self._next = 0

    async def fetch(self, url: str, outdir: Path, limit: int) -> list[Path]:
        from .downloader import DownloadError, Downloader
        from .links import ReelLink, find_link, sanitize_url

        try:
            url = sanitize_url(url)
        except ValueError:
            raise DownloadError("invalid Instagram authentication URL") from None
        if not isinstance(find_link(url), ReelLink):
            raise DownloadError("Instagram accounts can only be used for Instagram posts")

        async with self._lock:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.directory, 0o700)
            count = len(self.accounts) or 1
            for offset in range(count):
                index = (self._next + offset) % count
                account = self.accounts[index] if self.accounts else None
                key = hashlib.sha256((account.username.lower() if account else "external-cookies").encode()).hexdigest()[:20]
                state_path = self.directory / f"{key}.json"
                state = json.loads(state_path.read_text()) if state_path.exists() else {}
                if state.get("paused") or state.get("blocked_until", 0) > time.time():
                    continue
                # Keep the binding after restarts and account-file reordering. Never move
                # an existing session to another exit just because proxy order changes.
                binding = state.get("proxy_hash")
                proxy = next((p for p in self.proxies if hashlib.sha256(p.encode()).hexdigest() == binding), None) if binding else self.proxies[index % len(self.proxies)]
                if proxy is None:
                    continue
                self._next = (index + 1) % count
                cookiefile = self.directory / f"{key}.cookies.txt" if account else self.cookies
                if cookiefile is None:
                    raise DownloadError("no Instagram login session configured")
                state.update(proxy_hash=hashlib.sha256(proxy.encode()).hexdigest(), blocked_until=time.time() + COOLDOWN)
                private_json(state_path, state)  # also limits retries after cancellation/restarts
                payload = dict(proxy=proxy, cookies=str(cookiefile), url=url, outdir=str(outdir.resolve()), limit=limit)
                if account:
                    payload.update(username=account.username, password=account.password, totp_secret=account.totp_secret)
                try:
                    result = await self._worker(payload)
                except asyncio.TimeoutError:
                    raise DownloadError(FAILURES["timeout"]) from None
                status = result.get("status", "download")
                if status == "ok":
                    self._next = index  # reuse a healthy session before logging in another account
                    state["blocked_until"] = 0
                    private_json(state_path, state)
                    files = Downloader._collect(outdir)
                    if files:
                        return files
                    raise DownloadError(FAILURES["download"])
                if status == "challenge":
                    state["paused"] = True
                if status == "restricted":
                    state["blocked_until"] = 0
                private_json(state_path, state)
                raise DownloadError(FAILURES.get(status, FAILURES["download"]))
            raise DownloadError("no account session is ready (paused, cooling down, or missing its assigned proxy)")

    async def _worker(self, payload: dict) -> dict:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "reelbot.ig_auth_worker",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, start_new_session=True,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(json.dumps(payload).encode()), AUTH_TIMEOUT)
        except BaseException:
            # Also stop gallery-dl/ffmpeg children before temporary media is cleaned up.
            if proc.returncode is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            await proc.wait()
            raise
        try:
            result = json.loads(stdout)
            return result if proc.returncode == 0 and isinstance(result, dict) else {"status": "download"}
        except (ValueError, TypeError):
            return {"status": "download"}
