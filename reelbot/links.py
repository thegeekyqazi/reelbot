"""Instagram URL detection and embed-fixer rewriting."""

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


@dataclass(frozen=True)
class ReelLink:
    kind: str  # "reel", "p" or "tv" ("reels" is normalised to "reel")
    code: str

    @property
    def url(self) -> str:
        """Canonical Instagram URL with tracking params stripped."""
        return f"https://www.instagram.com/{self.kind}/{self.code}/"

    def embed_url(self, domain: str) -> str:
        """The same link on an embed-fixer domain, e.g. instagramfix.com."""
        return f"https://{domain}/{self.kind}/{self.code}/"


def find_link(text: str | None) -> ReelLink | None:
    """Return the first Instagram reel/post link in ``text``, or None."""
    if not text:
        return None
    m = _REEL_RE.search(text)
    if not m:
        return None
    kind = m.group("kind").lower()
    if kind == "reels":
        kind = "reel"
    return ReelLink(kind=kind, code=m.group("code"))
