"""Instagram media download (yt-dlp + gallery-dl) with an ffmpeg re-encode step for oversized videos."""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import random
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Callable
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yt_dlp

from .links import ReelLink

log = logging.getLogger(__name__)

AUDIO_KBPS = 96
OPUS_KBPS = 64  # audio bitrate when remuxing a VP9 rendition into WebM
TARGET_FRACTION = 0.9  # aim for 90% of the upload limit to leave container overhead
MIN_VIDEO_KBPS = 100  # below this the result is unwatchable, so give up instead
MAX_SOURCE_BYTES = 300 * 1024 * 1024  # refuse absurdly large sources outright
MAX_ITEMS = 20  # Instagram carousels hold at most 20 items
FFMPEG_TIMEOUT = 240
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".m4v"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic"}
_RATE_LIMIT_HINTS = ("429", "too many requests", "rate-limit", "rate limit", "rate-limited")
_LOGIN_HINTS = ("login", "log in", "cookies", "401", "403")


class DownloadError(Exception):
    """A failure with a short, user-safe reason as its message."""


Progress = Callable[[str], None]  # receives short, user-facing status lines; must not block


def _no_progress(_: str) -> None:
    pass


# Errors about the post itself: another proxy won't change them.
RESTRICTED_MSG = "Instagram only shows that post to logged-in users (age-restricted or sensitive)"
_FINAL_ERRORS = ("the post is private", "no video in that post", RESTRICTED_MSG)
_RESTRICTED_HINTS = ("isn't available to everyone", "certain audiences", "age-restricted", "restricted video")
PROXY_ROUND_SECONDS = 150  # give up on proxies after this long, even if some are untried
PROXY_SOCKET_TIMEOUT = 10  # dead proxies should fail fast


class RateLimited(DownloadError):
    """Instagram refused us for making too many requests; other backends on the same IP will too."""


@dataclass
class Media:
    files: list[Path]  # in post order, each no larger than the upload limit
    skipped: int = 0  # items dropped because they couldn't be made small enough
    errors: list[str] = field(default_factory=list)


def compute_video_kbps(limit_bytes: int, duration_s: float, fraction: float = TARGET_FRACTION) -> int:
    """Video bitrate (kbps) that makes a ``duration_s`` clip fit in ``fraction`` of the limit."""
    total_kbps = limit_bytes * fraction * 8 / duration_s / 1000
    return int(total_kbps - AUDIO_KBPS)


def is_video(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTS


def is_media(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTS | IMAGE_EXTS


def _image_area(url: str) -> float | None:
    """Pixel area encoded in an Instagram CDN URL's ``stp`` param; inf for the original, None if cropped.

    ``stp`` looks like ``c0.40.1216.1216a_dst-jpg_e35_s1080x1080_tt6``: a ``c…`` token means a
    square crop, ``s``/``p<W>x<H>`` a resize, and neither means the untouched original.
    """
    stp = parse_qs(urlparse(url).query).get("stp", [""])[0]
    tokens = stp.split("_")
    if any(re.fullmatch(r"c[\d.]+a?", t) for t in tokens):
        return None
    for t in tokens:
        if m := re.fullmatch(r"[sp](\d+)x(\d+)", t):
            return int(m[1]) * int(m[2])
    return float("inf")


def best_image_url(thumbnails: list[dict]) -> str | None:
    """The largest uncropped image among yt-dlp's thumbnails for an Instagram photo."""
    urls = [t["url"] for t in thumbnails if t.get("url")]
    scored = [(area, url) for url in urls if (area := _image_area(url)) is not None]
    if scored:
        return max(scored, key=lambda s: s[0])[1]
    return urls[-1] if urls else None


def _video_container(vcodec: str | None) -> str | None:
    """Container a DASH video stream is sent in, or None if it shouldn't be used.

    Apple's player (Discord on iOS/iPadOS) shows a frozen frame for VP9 in mp4 but plays
    VP9 in WebM, so VP9 goes to WebM (with the audio re-encoded to Opus) and H.264 to mp4.
    """
    codec = (vcodec or "").lower()
    if codec.startswith(("avc1", "avc3", "h264")):
        return "mp4"
    if codec.startswith(("vp09", "vp9")):
        return "webm"
    return None


@dataclass
class Rendition:
    video_id: str
    audio_id: str
    container: str  # "mp4" or "webm"


def pick_smaller_formats(formats: list[dict], duration: float | None, limit_bytes: int) -> Rendition | None:
    """The best separate video+audio pair that fits ``limit_bytes``, or None.

    Instagram serves lower-bitrate DASH renditions next to the full progressive mp4, so an
    oversized reel can usually be fetched smaller and remuxed without re-encoding the video.
    """
    if not duration or duration <= 0:
        return None
    audios = [f for f in formats if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")]
    videos = [
        f for f in formats
        if f.get("acodec") == "none" and _video_container(f.get("vcodec")) and f.get("tbr")
    ]
    if not audios or not videos:
        return None
    audio = max(audios, key=lambda f: f.get("tbr") or f.get("abr") or 0)
    audio_kbps = max(audio.get("tbr") or audio.get("abr") or 0, OPUS_KBPS)
    budget_kbps = limit_bytes * TARGET_FRACTION * 8 / duration / 1000 - audio_kbps
    fitting = [f for f in videos if f["tbr"] <= budget_kbps]
    if not fitting:
        return None
    # Highest bitrate wins; on a tie prefer H.264, which needs no audio re-encode.
    video = max(fitting, key=lambda f: (f["tbr"], _video_container(f["vcodec"]) == "mp4"))
    return Rendition(video["format_id"], audio["format_id"], _video_container(video["vcodec"]))


def _probe_duration_sync(path: Path) -> float | None:
    """Duration in seconds via ffprobe, for use inside worker threads; None if unreadable."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
        ).stdout
        return float(out.strip())
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def image_ext(data: bytes, url: str) -> str:
    """File extension from the image's magic bytes; Instagram serves JPEGs under .heic URLs."""
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG"):
        return ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data.startswith(b"GIF8"):
        return ".gif"
    ext = Path(urlparse(url).path).suffix.lower()
    return ext if ext in IMAGE_EXTS else ".jpg"


def _friendly_error(msg: str) -> DownloadError:
    msg = msg.lower()
    if any(hint in msg for hint in _RATE_LIMIT_HINTS):
        return RateLimited("Instagram is rate-limiting the bot, try again later")
    if any(hint in msg for hint in _LOGIN_HINTS):
        return DownloadError("Instagram wants a login (cookies missing or expired)")
    if any(hint in msg for hint in _RESTRICTED_HINTS):
        return DownloadError(RESTRICTED_MSG)
    if "private" in msg:
        return DownloadError("the post is private")
    if "no video" in msg:
        return DownloadError("no video in that post")
    return DownloadError("couldn't download that post")


class _YtdlpLogger:
    """Route yt-dlp's output into our logger at a low level instead of stdout."""

    def debug(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    info = debug

    def warning(self, msg: str) -> None:
        log.info("yt-dlp: %s", msg)

    def error(self, msg: str) -> None:
        log.warning("yt-dlp: %s", msg)


class Downloader:
    def __init__(
        self,
        cookies: Path | None = None,
        max_concurrent: int = 2,
        timeout: float = 90,
        proxies: list[str] | tuple[str, ...] = (),
    ) -> None:
        self.cookies = cookies
        # Only used to retry after a rate limit, to save proxy bandwidth.
        self.proxies = list(proxies)
        # Round-robin, from a random start so daily restarts don't always begin at proxy 1.
        self._next_proxy = random.randrange(len(self.proxies)) if self.proxies else 0
        self.timeout = timeout
        self._sem = asyncio.Semaphore(max_concurrent)
        self.has_ffmpeg = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
        self.has_gallery_dl = importlib.util.find_spec("gallery_dl") is not None
        if not self.has_ffmpeg:
            log.warning("ffmpeg/ffprobe not found; oversized videos can't be shrunk")
        if not self.has_gallery_dl:
            log.warning("gallery-dl not installed; only yt-dlp will be used")

    async def fetch(
        self, link: ReelLink, workdir: Path, limit_bytes: int, progress: Progress = _no_progress
    ) -> Media:
        """Download every photo/video in ``link`` into ``workdir``, each fitting ``limit_bytes``."""
        if self._sem.locked():
            progress("Waiting for another download to finish…")
        await self._sem.acquire()
        threads: list[asyncio.Future] = []
        try:
            files, errors = await self._download_any(link, workdir, threads, limit_bytes, progress)
            media = await self._fit_all(files, workdir, limit_bytes, progress)
            media.errors = errors
            return media
        finally:
            # A timed-out yt-dlp thread can't be killed; keep its slot taken until it ends.
            running = next((t for t in threads if not t.done()), None)
            if running is not None:
                running.add_done_callback(self._release_after_thread)
            else:
                self._sem.release()

    def _release_after_thread(self, fut: asyncio.Future) -> None:
        if not fut.cancelled():
            fut.exception()  # mark as retrieved so asyncio doesn't warn
        self._sem.release()

    async def _download_any(
        self,
        link: ReelLink,
        workdir: Path,
        threads: list[asyncio.Future],
        limit_bytes: int,
        progress: Progress = _no_progress,
    ) -> tuple[list[Path], list[str]]:
        """Try each backend in turn.

        yt-dlp handles photos too, without a login. gallery-dl is only used with cookies,
        and then goes first for /p/ posts.
        """
        backends = [("yt-dlp", self._ytdlp)]
        # Anonymous gallery-dl only ever hit Instagram's login wall and then hung until the
        # timeout, so it's only worth trying with cookies.
        if self.has_gallery_dl and self.cookies:
            gallery = ("gallery-dl", self._gallery_dl)
            first = link.kind == "p"
            backends = [gallery, *backends] if first else [*backends, gallery]

        errors: list[DownloadError] = []
        for n, (name, backend) in enumerate(backends):
            outdir = workdir / name
            outdir.mkdir()
            if n:
                progress(f"First try failed ({errors[-1]}), trying another downloader…")
            try:
                files = await backend(link.url, outdir, threads, limit_bytes)
            except RateLimited as e:
                # Every backend shares this IP, so trying the next one only makes the user wait.
                log.info("%s failed for %s: %s", name, link.url, e)
                if not self.proxies:
                    raise
                return await self._via_proxies(link, workdir, threads, limit_bytes, progress), [str(e)]
            except DownloadError as e:
                log.info("%s failed for %s: %s", name, link.url, e)
                errors.append(e)
                continue
            if files:
                log.info("%s got %d item(s) from %s", name, len(files), link.url)
                return files, [str(e) for e in errors]
            errors.append(DownloadError("nothing downloadable in that post"))
        # Prefer the most actionable reason, e.g. a login problem over a generic failure.
        generic = "couldn't download that post"
        raise next((e for e in errors if str(e) != generic), errors[0])

    async def _via_proxies(
        self,
        link: ReelLink,
        workdir: Path,
        threads: list[asyncio.Future],
        limit_bytes: int,
        progress: Progress,
    ) -> list[Path]:
        """Retry yt-dlp through each proxy in turn until one works or time runs out."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + PROXY_ROUND_SECONDS
        total = len(self.proxies)
        last: DownloadError = RateLimited("Instagram is rate-limiting the bot, try again later")
        for attempt in range(1, total + 1):
            if loop.time() >= deadline:
                log.info("Giving up on proxies for %s after %d attempt(s)", link.url, attempt - 1)
                break
            index = self._next_proxy % total
            self._next_proxy += 1
            progress(f"Instagram is rate-limiting the bot. Retrying through proxy {attempt}/{total}…")
            outdir = workdir / f"proxy-{attempt}"
            outdir.mkdir()
            # Log the proxy's position only: the URL carries its password.
            try:
                files = await self._ytdlp(link.url, outdir, threads, limit_bytes, proxy=self.proxies[index])
            except DownloadError as e:
                log.info("yt-dlp via proxy #%d failed for %s: %s", index + 1, link.url, e)
                if str(e) in _FINAL_ERRORS:
                    raise
                last = e
                continue
            if files:
                log.info("yt-dlp via proxy #%d got %d item(s) from %s", index + 1, len(files), link.url)
                return files
            last = DownloadError("nothing downloadable in that post")
        if isinstance(last, RateLimited) or str(last) == "download timed out":
            raise RateLimited("Instagram is rate-limiting the bot and every proxy, try again later")
        raise last

    async def _ytdlp(
        self, url: str, outdir: Path, threads: list[asyncio.Future], limit_bytes: int, proxy: str | None = None
    ) -> list[Path]:
        fut = asyncio.ensure_future(asyncio.to_thread(self._ytdlp_sync, url, outdir, limit_bytes, proxy))
        threads.append(fut)
        try:
            # shield: a timeout must not cancel the future, because the worker
            # thread keeps running and must keep holding its semaphore slot.
            return await asyncio.wait_for(asyncio.shield(fut), self.timeout)
        except asyncio.TimeoutError:
            raise DownloadError("download timed out") from None

    def _ytdlp_sync(self, url: str, outdir: Path, limit_bytes: int, proxy: str | None = None) -> list[Path]:
        opts = {
            "format": "best[ext=mp4]/best",
            "outtmpl": str(outdir / "%(playlist_index|0)03d_%(id)s.%(ext)s"),
            "playlistend": MAX_ITEMS,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "logger": _YtdlpLogger(),
            "cachedir": False,
            "socket_timeout": 20,
            "retries": 2,
            "max_filesize": MAX_SOURCE_BYTES,
            # Photos have no formats; resolve first, then download videos and save photos separately.
            "ignore_no_formats_error": True,
        }
        if self.cookies:
            opts["cookiefile"] = str(self.cookies)
        if proxy:
            opts["proxy"] = proxy
            opts["socket_timeout"] = PROXY_SOCKET_TIMEOUT
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                entries = info.get("entries") or [info]
                for entry in list(entries)[:MAX_ITEMS]:
                    if not entry:
                        continue
                    if entry.get("formats"):
                        ydl.process_info(entry)
                        self._refetch_smaller(opts, ydl.prepare_filename(entry), entry, limit_bytes)
                    else:
                        self._save_photo(ydl, entry, outdir)
        except yt_dlp.utils.DownloadError as e:
            raise _friendly_error(str(e)) from e
        return self._collect(outdir)

    def _refetch_smaller(self, opts: dict, path: str, entry: dict, limit_bytes: int) -> None:
        """Swap an oversized video for a smaller Instagram rendition, without re-encoding the video.

        Leaves the original in place if nothing fits, so ``_fit_all`` can still re-encode it.
        """
        original = Path(path)
        if not self.has_ffmpeg or not original.is_file() or original.stat().st_size <= limit_bytes:
            return
        # yt-dlp often has no duration for Instagram; the downloaded file always does.
        duration = entry.get("duration") or _probe_duration_sync(original)
        rendition = pick_smaller_formats(entry["formats"], duration, limit_bytes)
        if rendition is None:
            return
        stem = original.stem + ".small"
        streams = original.with_name(stem + ".%(format_id)s.%(ext)s")
        stream_opts = {**opts, "format": f"{rendition.video_id},{rendition.audio_id}", "outtmpl": str(streams)}
        merged = original.with_name(f"{stem}.{rendition.container}")
        try:
            with yt_dlp.YoutubeDL(stream_opts) as ydl:
                ydl.process_ie_result(dict(entry), download=True)
            video = next(original.parent.glob(f"{stem}.{rendition.video_id}.*"))
            audio = next(original.parent.glob(f"{stem}.{rendition.audio_id}.*"))
            audio_args = ["-c:a", "copy"] if rendition.container == "mp4" else ["-c:a", "libopus", "-b:a", f"{OPUS_KBPS}k"]
            subprocess.run(
                ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
                 "-i", str(video), "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0",
                 "-c:v", "copy", *audio_args, "-movflags", "+faststart", str(merged)],
                check=True, capture_output=True, timeout=FFMPEG_TIMEOUT,
            )
        except (yt_dlp.utils.DownloadError, StopIteration, OSError, subprocess.SubprocessError) as e:
            log.info("Smaller rendition failed for %s: %s", entry.get("id"), e)
        else:
            if merged.stat().st_size <= limit_bytes:
                log.info(
                    "Using %s+%s (%s) for %s (%.1f MB instead of %.1f MB)",
                    rendition.video_id, rendition.audio_id, rendition.container, entry.get("id"),
                    merged.stat().st_size / 2**20, original.stat().st_size / 2**20,
                )
                original.unlink()
                merged.rename(original.with_suffix(f".{rendition.container}"))
        # Drop leftovers (separate streams, a still-too-big merge) so they aren't posted.
        for leftover in original.parent.glob(stem + "*"):
            leftover.unlink(missing_ok=True)

    @staticmethod
    def _save_photo(ydl: yt_dlp.YoutubeDL, entry: dict, outdir: Path) -> None:
        url = best_image_url(entry.get("thumbnails") or [])
        if url is None:
            return
        try:
            with ydl.urlopen(url) as resp:
                data = resp.read(MAX_SOURCE_BYTES + 1)
        except Exception as e:
            raise yt_dlp.utils.DownloadError(f"photo download failed: {e}") from e
        if len(data) > MAX_SOURCE_BYTES:
            return
        # Same naming as outtmpl so photos and videos sort into post order.
        name = f"{entry.get('playlist_index') or 0:03}_{entry.get('id', 'photo')}"
        (outdir / (name + image_ext(data, url))).write_bytes(data)

    async def _gallery_dl(self, url: str, outdir: Path, threads: list[asyncio.Future], limit_bytes: int) -> list[Path]:
        cmd = [
            sys.executable, "-m", "gallery_dl",
            "--config-ignore", "--quiet", "--no-mtime", "--no-part",
            "--directory", str(outdir),
            "--filename", "{num:>03}_{media_id}.{extension}",
            "--range", f"1-{MAX_ITEMS}",
            "--http-timeout", "20",
            # Plain progressive mp4s (video + audio in one file) instead of DASH merging.
            "-o", "extractor.instagram.videos=merged",
            "-o", f"cache.file={outdir / '.cache.sqlite3'}",
        ]
        if self.cookies:
            cmd += ["--cookies", str(self.cookies)]
        cmd.append(url)
        try:
            returncode, _, stderr = await self._exec(cmd, self.timeout)
        except asyncio.TimeoutError:
            raise DownloadError("download timed out") from None
        files = self._collect(outdir)
        if returncode != 0 and not files:
            log.warning("gallery-dl exited %s: %s", returncode, stderr[-500:])
            raise _friendly_error(stderr)
        return files

    @staticmethod
    def _collect(outdir: Path) -> list[Path]:
        return sorted(p for p in outdir.iterdir() if p.is_file() and is_media(p))

    async def _fit_all(
        self, files: list[Path], workdir: Path, limit_bytes: int, progress: Progress = _no_progress
    ) -> Media:
        kept: list[Path] = []
        skipped = 0
        for i, path in enumerate(files[:MAX_ITEMS]):
            if path.stat().st_size <= limit_bytes:
                kept.append(path)
            elif is_video(path) and self.has_ffmpeg:
                progress("The video is too big for Discord. Compressing it, this can take a few minutes…")
                try:
                    kept.append(await self._shrink(path, workdir / f"shrunk_{i:03}.mp4", limit_bytes))
                except DownloadError as e:
                    log.info("Dropping %s: %s", path.name, e)
                    skipped += 1
            else:
                skipped += 1
        if not kept:
            if len(files) == 1 and is_video(files[0]) and not self.has_ffmpeg:
                raise DownloadError("the video is too large to upload")
            raise DownloadError("the media is too large to upload")
        return Media(files=kept, skipped=skipped)

    async def _shrink(self, path: Path, out: Path, limit_bytes: int) -> Path:
        log.info("Shrinking %s (%.1f MB) to fit %.1f MB", path.name, path.stat().st_size / 2**20, limit_bytes / 2**20)
        duration = await self._probe_duration(path)
        for fraction in (TARGET_FRACTION, TARGET_FRACTION * 0.8):
            kbps = compute_video_kbps(limit_bytes, duration, fraction)
            if kbps < MIN_VIDEO_KBPS:
                raise DownloadError("the video is too long to fit under the upload limit")
            await self._ffmpeg(
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
                "-i", str(path),
                "-vf", "scale='min(720,iw)':-2",
                "-c:v", "libx264", "-preset", "veryfast",
                "-b:v", f"{kbps}k", "-maxrate", f"{kbps}k", "-bufsize", f"{kbps * 2}k",
                "-c:a", "aac", "-b:a", f"{AUDIO_KBPS}k",
                "-movflags", "+faststart", "-threads", "2",
                str(out),
            )
            if out.stat().st_size <= limit_bytes:
                return out
        raise DownloadError("the video is still too large after compressing")

    async def _probe_duration(self, path: Path) -> float:
        stdout = await self._ffmpeg(
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        )
        try:
            duration = float(stdout.strip())
        except ValueError:
            raise DownloadError("couldn't read the video's length") from None
        if duration <= 0:
            raise DownloadError("couldn't read the video's length")
        return duration

    async def _ffmpeg(self, *cmd: str) -> str:
        try:
            returncode, stdout, stderr = await self._exec(list(cmd), FFMPEG_TIMEOUT)
        except asyncio.TimeoutError:
            raise DownloadError("compressing the video timed out") from None
        if returncode != 0:
            log.warning("%s failed (%s): %s", cmd[0], returncode, stderr[-500:])
            raise DownloadError("compressing the video failed")
        return stdout

    @staticmethod
    async def _exec(cmd: list[str], timeout: float) -> tuple[int, str, str]:
        """Run a subprocess, killing it on timeout or cancellation."""
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
        except BaseException:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            raise
        return proc.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")
