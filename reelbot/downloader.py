"""Instagram media download (yt-dlp + gallery-dl) with an ffmpeg re-encode step for oversized videos."""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yt_dlp

from .links import ReelLink

log = logging.getLogger(__name__)

AUDIO_KBPS = 96
TARGET_FRACTION = 0.9  # aim for 90% of the upload limit to leave container overhead
MIN_VIDEO_KBPS = 100  # below this the result is unwatchable, so give up instead
MAX_SOURCE_BYTES = 300 * 1024 * 1024  # refuse absurdly large sources outright
MAX_ITEMS = 20  # Instagram carousels hold at most 20 items
FFMPEG_TIMEOUT = 240
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".m4v"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic"}
_LOGIN_HINTS = ("login", "log in", "rate-limit", "rate limit", "cookies", "401", "403", "429")


class DownloadError(Exception):
    """A failure with a short, user-safe reason as its message."""


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


def _friendly_error(msg: str) -> DownloadError:
    msg = msg.lower()
    if any(hint in msg for hint in _LOGIN_HINTS):
        return DownloadError("Instagram wants a login (cookies missing or expired)")
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
    def __init__(self, cookies: Path | None = None, max_concurrent: int = 2, timeout: float = 90) -> None:
        self.cookies = cookies
        self.timeout = timeout
        self._sem = asyncio.Semaphore(max_concurrent)
        self.has_ffmpeg = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
        self.has_gallery_dl = importlib.util.find_spec("gallery_dl") is not None
        if not self.has_ffmpeg:
            log.warning("ffmpeg/ffprobe not found; oversized videos can't be shrunk")
        if not self.has_gallery_dl:
            log.warning("gallery-dl not installed; photo posts will fall back to embed links")

    async def fetch(self, link: ReelLink, workdir: Path, limit_bytes: int) -> Media:
        """Download every photo/video in ``link`` into ``workdir``, each fitting ``limit_bytes``."""
        await self._sem.acquire()
        threads: list[asyncio.Future] = []
        try:
            files, errors = await self._download_any(link, workdir, threads)
            media = await self._fit_all(files, workdir, limit_bytes)
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
        self, link: ReelLink, workdir: Path, threads: list[asyncio.Future]
    ) -> tuple[list[Path], list[str]]:
        """Try each backend in turn. gallery-dl goes first for /p/ posts since they may hold photos."""
        backends = [("yt-dlp", self._ytdlp)]
        if self.has_gallery_dl:
            gallery = ("gallery-dl", self._gallery_dl)
            backends = [gallery, *backends] if link.kind == "p" else [*backends, gallery]

        errors: list[DownloadError] = []
        for name, backend in backends:
            outdir = workdir / name
            outdir.mkdir()
            try:
                files = await backend(link.url, outdir, threads)
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

    async def _ytdlp(self, url: str, outdir: Path, threads: list[asyncio.Future]) -> list[Path]:
        fut = asyncio.ensure_future(asyncio.to_thread(self._ytdlp_sync, url, outdir))
        threads.append(fut)
        try:
            # shield: a timeout must not cancel the future, because the worker
            # thread keeps running and must keep holding its semaphore slot.
            return await asyncio.wait_for(asyncio.shield(fut), self.timeout)
        except asyncio.TimeoutError:
            raise DownloadError("download timed out") from None

    def _ytdlp_sync(self, url: str, outdir: Path) -> list[Path]:
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
        }
        if self.cookies:
            opts["cookiefile"] = str(self.cookies)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.extract_info(url, download=True)
        except yt_dlp.utils.DownloadError as e:
            raise _friendly_error(str(e)) from e
        return self._collect(outdir)

    async def _gallery_dl(self, url: str, outdir: Path, threads: list[asyncio.Future]) -> list[Path]:
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

    async def _fit_all(self, files: list[Path], workdir: Path, limit_bytes: int) -> Media:
        kept: list[Path] = []
        skipped = 0
        for i, path in enumerate(files[:MAX_ITEMS]):
            if path.stat().st_size <= limit_bytes:
                kept.append(path)
            elif is_video(path) and self.has_ffmpeg:
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
