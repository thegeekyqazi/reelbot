from pathlib import Path

import asyncio

from reelbot.bot import StatusMessage, attachment_names, plan_batches


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


def test_webm_keeps_its_extension():
    assert attachment_names([Path("0_abc.webm")]) == ["reel.webm"]
    assert attachment_names([Path("001_a.jpg"), Path("002_b.webm")]) == ["01.jpg", "02.webm"]


def test_status_sends_latest_line_and_stops_after_close():
    edits = []

    async def main():
        async def edit(content):
            await asyncio.sleep(0.01)
            edits.append(content)

        status = StatusMessage(edit)
        status.update("one")
        status.update("two")  # replaces "one" before it's sent
        await asyncio.sleep(0.05)
        status.update("three")  # still pending at close: dropped, the reply replaces it anyway
        await status.close()
        status.update("late")
        await asyncio.sleep(0.03)

    asyncio.run(main())
    assert edits == ["-# ⏳ two"]
