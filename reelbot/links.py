"""Instagram/TikTok media URL detection, tracking removal, and fallback links."""

from __future__ import annotations

import re
from dataclasses import dataclass

# Matches instagram.com/<kind>/<shortcode>, optionally with www./m., a scheme,
# or a leading username segment (instagram.com/<user>/reel/<id>). The lookbehind
# rejects lookalike hosts such as evil-instagram.com.
_REEL_RE = re.compile(
    r"(?<![A-Za-z0-9.-])(?:https?://)?(?:www\.|m\.)?instagram\.com/"
    r"(?:[A-Za-z0-9_.]+/)?"
    r"(?P<kind>reels?|p|tv)/"
    r"(?P<code>[A-Za-z0-9_-]+)",
    re.IGNORECASE,
)

_TIKTOK_RE = re.compile(
    r"(?<![A-Za-z0-9_./:@%+-])(?:https?://)?"
    r"(?:(?P<short_host>vm|vt)\.tiktok\.com/(?P<short_code>[A-Za-z0-9_-]+)"
    r"|(?:www\.|m\.)?tiktok\.com/"
    r"(?:(?:@(?P<user>[A-Za-z0-9_.-]+)?|share)/video/(?P<video_id>[0-9]+)"
    r"|t/(?P<share_code>[A-Za-z0-9_-]+)))"
    r"(?=$|[/?#\s<>\[\](){},.!;:'\"])",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ReelLink:
    kind: str  # "reel", "p" or "tv" ("reels" is normalised to "reel")
    code: str

    @property
    def platform(self) -> str:
        return "instagram"

    @property
    def url(self) -> str:
        """Canonical Instagram URL with tracking params stripped."""
        return f"https://www.instagram.com/{self.kind}/{self.code}/"

    def embed_url(self, domain: str) -> str:
        """The same link on an embed-fixer domain, e.g. instagramfix.com."""
        return f"https://{domain}/{self.kind}/{self.code}/"


@dataclass(frozen=True)
class TikTokLink:
    code: str
    username: str | None = None
    host: str = "www.tiktok.com"

    @property
    def platform(self) -> str:
        return "tiktok"

    @property
    def is_short(self) -> bool:
        return self.username is None

    @property
    def url(self) -> str:
        if self.username is not None:
            return f"https://www.tiktok.com/@{self.username}/video/{self.code}"
        path = f"t/{self.code}" if self.host == "www.tiktok.com" else self.code
        return f"https://{self.host}/{path}/"

    def embed_url(self, domain: str) -> str:
        # The configured embed-fixer domain is Instagram-specific.
        return self.url


MediaLink = ReelLink | TikTokLink


def _from_match(match: re.Match, platform: str) -> MediaLink:
    if platform == "tiktok":
        if match.group("video_id"):
            return TikTokLink(match.group("video_id"), match.group("user") or "")
        return TikTokLink(
            match.group("short_code") or match.group("share_code"),
            host=f"{match.group('short_host').lower()}.tiktok.com" if match.group("short_host") else "www.tiktok.com",
        )
    kind = match.group("kind").lower()
    return ReelLink("reel" if kind == "reels" else kind, match.group("code"))


def find_link(text: str | None) -> MediaLink | None:
    """Return the first supported media link in ``text``, or None."""
    if not text:
        return None
    matches = [(match, platform) for pattern, platform in ((_REEL_RE, "instagram"), (_TIKTOK_RE, "tiktok"))
               if (match := pattern.search(text)) is not None]
    if not matches:
        return None
    match, platform = min(matches, key=lambda item: item[0].start())
    return _from_match(match, platform)


def sanitize_url(url: str) -> str:
    """Return only the canonical media URL, dropping every query and fragment.

    Rebuild from the post type and shortcode instead of maintaining a list of
    tracking parameters. Validate the entire URL so unrelated hosts and paths
    cannot be sent to a downloader through this entry point.
    """
    clean = url.strip().split("?", 1)[0].split("#", 1)[0].rstrip("/")
    for pattern, platform in ((_REEL_RE, "instagram"), (_TIKTOK_RE, "tiktok")):
        if match := pattern.fullmatch(clean):
            return _from_match(match, platform).url
    raise ValueError("Expected an Instagram post or TikTok video URL")
