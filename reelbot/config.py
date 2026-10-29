"""Load and validate configuration from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    discord_token: str
    owner_id: int
    allowed_user_ids: tuple[int, ...] = ()
    allow_everyone: bool = False
    ig_cookies: Path | None = None
    max_upload_mb: float = 10
    embed_fixer_domain: str = "instagramfix.com"
    data_dir: Path = Path("./data")
    max_concurrent_downloads: int = 2

    @property
    def max_upload_bytes(self) -> int:
        return int(self.max_upload_mb * 1024 * 1024)

    @property
    def allowlist_path(self) -> Path:
        return self.data_dir / "allowlist.json"


def _get(env: Mapping[str, str], name: str) -> str:
    return env.get(name, "").strip()


def _user_id(value: str, name: str) -> int:
    if not value.isdigit():
        raise ConfigError(f"{name} must be a numeric Discord user ID, got {value!r}")
    return int(value)


def _bool(value: str, name: str) -> bool:
    v = value.lower()
    if v in ("", "0", "false", "no", "off"):
        return False
    if v in ("1", "true", "yes", "on"):
        return True
    raise ConfigError(f"{name} must be true or false, got {value!r}")


def load_config(env: Mapping[str, str] | None = None) -> Config:
    env = os.environ if env is None else env
    errors: list[str] = []

    token = _get(env, "DISCORD_TOKEN")
    if not token or token == "paste-your-bot-token-here":
        errors.append("DISCORD_TOKEN is required (Developer Portal -> your app -> Bot -> Reset Token)")

    owner_raw = _get(env, "OWNER_ID")
    owner_id = 0
    if not owner_raw:
        errors.append("OWNER_ID is required (your own Discord user ID)")
    else:
        try:
            owner_id = _user_id(owner_raw, "OWNER_ID")
        except ConfigError as e:
            errors.append(str(e))

    if errors:
        raise ConfigError("Missing or invalid configuration:\n  - " + "\n  - ".join(errors))

    allowed = tuple(
        _user_id(part.strip(), "ALLOWED_USER_IDS")
        for part in _get(env, "ALLOWED_USER_IDS").split(",")
        if part.strip()
    )

    cookies_raw = _get(env, "IG_COOKIES")
    cookies = Path(cookies_raw) if cookies_raw else None
    if cookies is not None and not cookies.is_file():
        raise ConfigError(f"IG_COOKIES points to {cookies}, which does not exist")

    try:
        max_mb = float(_get(env, "MAX_UPLOAD_MB") or 10)
        concurrency = int(_get(env, "MAX_CONCURRENT_DOWNLOADS") or 2)
    except ValueError as e:
        raise ConfigError(f"Invalid number in configuration: {e}") from None
    if max_mb <= 0:
        raise ConfigError("MAX_UPLOAD_MB must be positive")
    if concurrency < 1:
        raise ConfigError("MAX_CONCURRENT_DOWNLOADS must be at least 1")

    return Config(
        discord_token=token,
        owner_id=owner_id,
        allowed_user_ids=allowed,
        allow_everyone=_bool(_get(env, "ALLOW_EVERYONE"), "ALLOW_EVERYONE"),
        ig_cookies=cookies,
        max_upload_mb=max_mb,
        embed_fixer_domain=_get(env, "EMBED_FIXER_DOMAIN") or "instagramfix.com",
        data_dir=Path(_get(env, "DATA_DIR") or "./data"),
        max_concurrent_downloads=concurrency,
    )
