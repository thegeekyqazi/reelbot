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
