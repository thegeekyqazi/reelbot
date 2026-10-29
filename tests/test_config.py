import pytest

from reelbot.config import ConfigError, load_config

BASE = {"DISCORD_TOKEN": "x" * 10, "OWNER_ID": "111"}


def test_defaults():
    c = load_config(BASE)
    assert c.owner_id == 111
    assert c.max_upload_bytes == 10 * 1024 * 1024
    assert c.embed_fixer_domain == "instagramfix.com"
    assert c.max_concurrent_downloads == 2
    assert c.allow_everyone is False
    assert c.allowed_user_ids == ()


@pytest.mark.parametrize("missing", ["DISCORD_TOKEN", "OWNER_ID"])
def test_required(missing):
    env = {k: v for k, v in BASE.items() if k != missing}
    with pytest.raises(ConfigError, match=missing):
        load_config(env)


def test_placeholder_token_rejected():
    with pytest.raises(ConfigError, match="DISCORD_TOKEN"):
        load_config({**BASE, "DISCORD_TOKEN": "paste-your-bot-token-here"})


def test_parses_optional_values(tmp_path):
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n")
    c = load_config({
        **BASE,
        "ALLOWED_USER_IDS": "222, 333,",
        "ALLOW_EVERYONE": "true",
        "IG_COOKIES": str(cookies),
        "MAX_UPLOAD_MB": "25",
        "DATA_DIR": str(tmp_path),
    })
    assert c.allowed_user_ids == (222, 333)
    assert c.allow_everyone is True
    assert c.ig_cookies == cookies
    assert c.allowlist_path == tmp_path / "allowlist.json"


@pytest.mark.parametrize(
    "key, value",
    [("OWNER_ID", "abc"), ("ALLOWED_USER_IDS", "1,x"), ("ALLOW_EVERYONE", "maybe"),
     ("MAX_UPLOAD_MB", "0"), ("IG_COOKIES", "/does/not/exist")],
)
def test_invalid_values(key, value):
    with pytest.raises(ConfigError):
        load_config({**BASE, key: value})
