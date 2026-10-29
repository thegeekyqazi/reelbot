import pytest

from reelbot.links import ReelLink, find_link


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
        "https://www.tiktok.com/@user/video/123",
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
