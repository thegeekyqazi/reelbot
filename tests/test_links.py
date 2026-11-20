import pytest

from reelbot.links import ReelLink, find_link, sanitize_url


@pytest.mark.parametrize(
    "text, kind, code",
    [
        ("https://www.instagram.com/reel/ABC123/", "reel", "ABC123"),
        ("https://instagram.com/reel/ABC123", "reel", "ABC123"),
        ("https://www.instagram.com/reels/ABC_12-3/", "reel", "ABC_12-3"),
        ("https://www.instagram.com/p/XyZ987/", "p", "XyZ987"),
        ("https://instagram.com/tv/TV0001/", "tv", "TV0001"),
        ("https://www.instagram.com/reel/ABC123/?igsh=abcdef&utm_source=ig_web", "reel", "ABC123"),
        ("https://instagram.com/reel/ABC123?igshid=xyz", "reel", "ABC123"),
        ("https://m.instagram.com/reel/ABC123/", "reel", "ABC123"),
        ("http://www.instagram.com/reel/ABC123/", "reel", "ABC123"),
        ("instagram.com/reel/ABC123", "reel", "ABC123"),
        ("https://www.instagram.com/some.user/reel/ABC123/", "reel", "ABC123"),
        ("look at this lol https://www.instagram.com/reel/ABC123/ so good", "reel", "ABC123"),
        ("HTTPS://WWW.INSTAGRAM.COM/REEL/ABC123/", "reel", "ABC123"),
    ],
)
def test_accepts_instagram_links(text, kind, code):
    assert find_link(text) == ReelLink(kind, code)


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "no links here",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://www.tiktok.com/@user/",
        "https://www.instagram.com/someuser/",
        "https://www.instagram.com/stories/someuser/123/",
        "https://notinstagram.com.evil.example/reel/ABC/",
        "https://evil-instagram.com/reel/ABC123/",
    ],
)
def test_rejects_other_links(text):
    assert find_link(text) is None


def test_first_link_wins():
    text = "https://instagram.com/reel/FIRST/ and https://instagram.com/reel/SECOND/"
    assert find_link(text).code == "FIRST"


def test_canonical_url_strips_tracking():
    link = find_link("https://instagram.com/reels/ABC123/?igsh=tracking")
    assert link.url == "https://www.instagram.com/reel/ABC123/"


@pytest.mark.parametrize("url, expected", [
    ("https://www.instagram.com/reel/ABC_12-3/?igsh=YXNk&utm_source=ig_web_copy_link&fbclid=tracking#shared", "https://www.instagram.com/reel/ABC_12-3/"),
    ("https://instagram.com/p/POST1?igshid=tracking&img_index=2", "https://www.instagram.com/p/POST1/"),
    ("http://m.instagram.com/reels/AbC/#tracking?igsh=value", "https://www.instagram.com/reel/AbC/"),
    (" https://www.instagram.com/some.user/reel/ABC/?unknown_future_tracker=1 ", "https://www.instagram.com/reel/ABC/"),
    ("instagram.com/tv/TV1?igsh=x%26utm_source%3Dtracking", "https://www.instagram.com/tv/TV1/"),
])
def test_sanitize_shared_url(url, expected):
    assert sanitize_url(url) == expected
    assert sanitize_url(expected) == expected
    assert find_link(url).url == expected


@pytest.mark.parametrize("url", [
    "https://evil-instagram.com/reel/ABC/?igsh=x",
    "https://instagram.com.evil.example/reel/ABC/",
    "https://evil.example/instagram.com/reel/ABC/",
    "https://www.instagram.com/someuser/?igsh=x",
    "https://www.instagram.com/reel/ABC/extra-path/",
])
def test_sanitizer_rejects_unrelated_urls(url):
    with pytest.raises(ValueError):
        sanitize_url(url)


def test_downloader_receives_only_sanitized_url(tmp_path, monkeypatch):
    from reelbot.downloader import Downloader
    received = []
    class FakeYDL:
        def __init__(self, opts):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def extract_info(self, url, **kwargs):
            received.append(url)
            return {"thumbnails": []}
    monkeypatch.setattr("reelbot.downloader.yt_dlp.YoutubeDL", FakeYDL)
    Downloader()._ytdlp_sync("https://instagram.com/reels/ABC/?igsh=private-tracker#shared", tmp_path, 1000)
    assert received == ["https://www.instagram.com/reel/ABC/"]


def test_authenticated_fallback_receives_only_sanitized_url(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from reelbot.downloader import Downloader, DownloadError
    from reelbot.ig_auth_worker import run
    cookies = tmp_path / "cookies.txt"
    cookies.touch()
    seen = []
    def ytdlp(self, url, *args):
        seen.append(url)
        raise DownloadError("try gallery-dl")
    def gallery(cmd, **kwargs):
        seen.append(cmd[-1])
        (tmp_path / "image.jpg").write_bytes(b"image")
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(Downloader, "_ytdlp_sync", ytdlp)
    monkeypatch.setattr("reelbot.ig_auth_worker.subprocess.run", gallery)
    monkeypatch.setattr("reelbot.downloader.importlib.util.find_spec", lambda _: object())
    # Worker normally runs in isolation; restore its environment after this test.
    with monkeypatch.context() as env_patch:
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy", "NO_PROXY", "no_proxy"):
            env_patch.setenv(key, "previous")
        assert run(dict(url="https://instagram.com/p/ABC/?igsh=private-tracker#shared",
            proxy="http://proxy.test:80", cookies=str(cookies), outdir=str(tmp_path), limit=1000)) == "ok"
    assert seen == ["https://www.instagram.com/p/ABC/"] * 2


@pytest.mark.parametrize(
    "text, expected",
    [
        ("https://www.instagram.com/reel/ABC123/?igsh=x", "https://instagramfix.com/reel/ABC123/"),
        ("https://www.instagram.com/reels/ABC123/", "https://instagramfix.com/reel/ABC123/"),
        ("https://www.instagram.com/p/POST1/", "https://instagramfix.com/p/POST1/"),
        ("https://www.instagram.com/tv/TV1/", "https://instagramfix.com/tv/TV1/"),
    ],
)
def test_embed_fixer_rewrite(text, expected):
    assert find_link(text).embed_url("instagramfix.com") == expected


def test_embed_fixer_custom_domain():
    assert find_link("instagram.com/reel/A1").embed_url("example.org") == "https://example.org/reel/A1/"
