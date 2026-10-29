from pathlib import Path

from reelbot.bot import attachment_names, plan_batches


def test_single_batch():
    assert plan_batches([1, 2, 3], limit=100) == [[0, 1, 2]]


def test_splits_on_size():
    assert plan_batches([60, 30, 20, 90], limit=100) == [[0, 1], [2], [3]]


def test_splits_on_file_count():
    batches = plan_batches([1] * 23, limit=1000)
    assert [len(b) for b in batches] == [10, 10, 3]
    assert [i for b in batches for i in b] == list(range(23))


def test_lone_video_is_reel_mp4():
    assert attachment_names([Path("0_abc.mp4")]) == ["reel.mp4"]


def test_carousel_names_keep_order_and_type():
    paths = [Path("001_a.jpeg"), Path("002_b.mp4"), Path("shrunk_002.mp4"), Path("004_c.webp")]
    assert attachment_names(paths) == ["01.jpg", "02.mp4", "03.mp4", "04.webp"]


def test_lone_photo_keeps_extension():
    assert attachment_names([Path("001_a.jpg")]) == ["01.jpg"]
