"""yt-dlp download wrapper with an ffmpeg re-encode step for oversized videos."""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

import yt_dlp

log = logging.getLogger(__name__)

AUDIO_KBPS = 96
TARGET_FRACTION = 0.9  # aim for 90% of the upload limit to leave container overhead
MIN_VIDEO_KBPS = 100  # below this the result is unwatchable, so give up instead
MAX_SOURCE_BYTES = 300 * 1024 * 1024  # refuse absurdly large sources outright
FFMPEG_TIMEOUT = 240
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".m4v"}


class DownloadError(Exception):
    """A failure with a short, user-safe reason as its message."""


def compute_video_kbps(limit_bytes: int, duration_s: float, fraction: float = TARGET_FRACTION) -> int:
    """Video bitrate (kbps) that makes a ``duration_s`` clip fit in ``fraction`` of the limit."""
    total_kbps = limit_bytes * fraction * 8 / duration_s / 1000
    return int(total_kbps - AUDIO_KBPS)


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
        if not self.has_ffmpeg:
            log.warning("ffmpeg/ffprobe not found; oversized videos can't be shrunk")

    async def fetch(self, url: str, workdir: Path, limit_bytes: int) -> Path:
        """Download ``url`` into ``workdir`` and return a file no larger than ``limit_bytes``."""
        await self._sem.acquire()
        fut: asyncio.Future[Path] | None = None
        try:
            fut = asyncio.ensure_future(asyncio.to_thread(self._download, url, workdir))
            try:
                # shield: a timeout must not cancel the future, because the worker
                # thread keeps running and must keep holding its semaphore slot.
                path = await asyncio.wait_for(asyncio.shield(fut), self.timeout)
            except asyncio.TimeoutError:
                raise DownloadError("download timed out") from None
            return await self._fit(path, workdir, limit_bytes)
        finally:
            if fut is not None and not fut.done():
                fut.add_done_callback(self._release_after_thread)
            else:
                self._sem.release()

    def _release_after_thread(self, fut: asyncio.Future) -> None:
        if not fut.cancelled():
            fut.exception()  # mark as retrieved so asyncio doesn't warn
        self._sem.release()

    def _download(self, url: str, workdir: Path) -> Path:
        opts = {
            "format": "best[ext=mp4]/best",
            "outtmpl": str(workdir / "%(id)s.%(ext)s"),
            "noplaylist": True,
            "playlist_items": "1",  # carousels: first item only
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
                info = ydl.extract_info(url, download=True)
        except yt_dlp.utils.DownloadError as e:
            msg = str(e).lower()
            if "login" in msg or "rate-limit" in msg or "cookies" in msg:
                raise DownloadError("Instagram wants a login (cookies missing or expired)") from e
            if "private" in msg:
                raise DownloadError("the post is private") from e
            raise DownloadError("couldn't download that post") from e

        for entry in [info, *(info.get("entries") or [])] if info else []:
            for dl in (entry or {}).get("requested_downloads") or []:
                fp = dl.get("filepath")
                if fp and Path(fp).is_file():
                    return Path(fp)
        videos = [p for p in workdir.iterdir() if p.suffix.lower() in VIDEO_EXTS]
        if not videos:
            raise DownloadError("no video found in that post")
        return max(videos, key=lambda p: p.stat().st_size)

    async def _fit(self, path: Path, workdir: Path, limit_bytes: int) -> Path:
        size = path.stat().st_size
        if size <= limit_bytes:
            return path
        if not self.has_ffmpeg:
            raise DownloadError("the video is too large to upload")

        log.info("Shrinking %s (%.1f MB) to fit %.1f MB", path.name, size / 2**20, limit_bytes / 2**20)
        duration = await self._probe_duration(path)
        out = workdir / "shrunk.mp4"
        for fraction in (TARGET_FRACTION, TARGET_FRACTION * 0.8):
            kbps = compute_video_kbps(limit_bytes, duration, fraction)
            if kbps < MIN_VIDEO_KBPS:
                raise DownloadError("the video is too long to fit under the upload limit")
            await self._run(
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
        stdout = await self._run(
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

    async def _run(self, *cmd: str) -> str:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), FFMPEG_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise DownloadError("compressing the video timed out") from None
        except asyncio.CancelledError:
            proc.kill()
            raise
        if proc.returncode != 0:
            log.warning("%s failed (%s): %s", cmd[0], proc.returncode, stderr.decode(errors="replace")[-500:])
            raise DownloadError("compressing the video failed")
        return stdout.decode(errors="replace")
