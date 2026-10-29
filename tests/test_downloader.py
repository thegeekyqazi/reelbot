from reelbot.downloader import AUDIO_KBPS, compute_video_kbps


def test_bitrate_fits_limit():
    limit = 10 * 1024 * 1024
    duration = 60.0
    kbps = compute_video_kbps(limit, duration)
    total_bytes = (kbps + AUDIO_KBPS) * 1000 / 8 * duration
    assert total_bytes <= limit * 0.9
    assert total_bytes > limit * 0.85


def test_longer_videos_get_lower_bitrate():
    limit = 10 * 1024 * 1024
    assert compute_video_kbps(limit, 120) < compute_video_kbps(limit, 30)


import asyncio
from pathlib import Path

import pytest

from reelbot.downloader import Downloader, DownloadError
from reelbot.links import ReelLink


def _write(path: Path, size: int) -> Path:
    path.write_bytes(b"\0" * size)
    return path


def _downloader(order: list[str], results: dict) -> Downloader:
    """A Downloader whose backends are fakes that record call order."""
    d = Downloader()
    d.has_gallery_dl = True

    def fake(name):
        async def backend(url, outdir, threads):
            order.append(name)
            result = results[name]
            if isinstance(result, Exception):
                raise result
            return [_write(outdir / fname, 10) for fname in result]
        return backend

    d._ytdlp = fake("yt-dlp")
    d._gallery_dl = fake("gallery-dl")
    return d


def run(coro):
    return asyncio.run(coro)


def test_photo_posts_try_gallery_dl_first(tmp_path):
    order = []
    d = _downloader(order, {"gallery-dl": ["001_a.jpg", "002_b.mp4"], "yt-dlp": ["x.mp4"]})
    media = run(d.fetch(ReelLink("p", "ABC"), tmp_path, 1000))
    assert order == ["gallery-dl"]
    assert [p.name for p in media.files] == ["001_a.jpg", "002_b.mp4"]


def test_reels_try_ytdlp_first_then_fall_back(tmp_path):
    order = []
    d = _downloader(order, {"yt-dlp": DownloadError("couldn't download that post"), "gallery-dl": ["001_a.mp4"]})
    media = run(d.fetch(ReelLink("reel", "ABC"), tmp_path, 1000))
    assert order == ["yt-dlp", "gallery-dl"]
    assert len(media.files) == 1


def test_empty_result_falls_back(tmp_path):
    order = []
    d = _downloader(order, {"gallery-dl": [], "yt-dlp": ["0_a.mp4"]})
    run(d.fetch(ReelLink("p", "ABC"), tmp_path, 1000))
    assert order == ["gallery-dl", "yt-dlp"]


def test_most_useful_error_is_reported(tmp_path):
    d = _downloader([], {
        "yt-dlp": DownloadError("couldn't download that post"),
        "gallery-dl": DownloadError("Instagram wants a login (cookies missing or expired)"),
    })
    with pytest.raises(DownloadError, match="login"):
        run(d.fetch(ReelLink("reel", "ABC"), tmp_path, 1000))


def test_without_gallery_dl_only_ytdlp_runs(tmp_path):
    order = []
    d = _downloader(order, {"yt-dlp": ["0_a.mp4"], "gallery-dl": ["001_a.jpg"]})
    d.has_gallery_dl = False
    run(d.fetch(ReelLink("p", "ABC"), tmp_path, 1000))
    assert order == ["yt-dlp"]


def test_oversized_images_are_skipped(tmp_path):
    d = Downloader()
    small = _write(tmp_path / "001.jpg", 100)
    big = _write(tmp_path / "002.jpg", 5000)
    media = run(d._fit_all([small, big], tmp_path, 1000))
    assert media.files == [small]
    assert media.skipped == 1


def test_everything_too_large_raises(tmp_path):
    d = Downloader()
    big = _write(tmp_path / "001.jpg", 5000)
    with pytest.raises(DownloadError, match="too large"):
        run(d._fit_all([big], tmp_path, 1000))


def test_semaphore_released_after_fetch(tmp_path):
    d = _downloader([], {"gallery-dl": ["001_a.jpg"], "yt-dlp": []})
    run(d.fetch(ReelLink("p", "ABC"), tmp_path, 1000))
    assert not d._sem.locked() and d._sem._value == 2
