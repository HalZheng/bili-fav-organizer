"""断点续移功能验证脚本"""
import asyncio
import os
import time
from pathlib import Path

from config import BiliConfig
from move_state import MoveStateManager, MoveState, VideoRecord, TERMINAL_STATUSES
from models import BiliVideo, BiliUP


async def test_state_lifecycle():
    print("=== Test: State lifecycle ===")
    config = BiliConfig()
    sm = MoveStateManager(config)

    # Clean up any existing state
    if sm.state_path.exists():
        sm.state_path.unlink()
    for bak in sm.state_path.parent.glob("move_state_*.json.bak"):
        bak.unlink()

    # Test 1: detect_interrupted_task with no state file
    result = await sm.detect_interrupted_task()
    assert result is None, f"Expected None, got {result}"
    print("1. detect_interrupted_task (no file) -> None OK")

    # Test 2: create_new_state
    state = await sm.create_new_state(
        mode="auto", dry_run=False,
        source_csv="videos.csv", source_csv_mtime=time.time()
    )
    assert state.session_id != ""
    assert state.mode == "auto"
    print(f"2. create_new_state -> session={state.session_id} OK")

    # Test 3: init_category with mock videos
    videos_by_block = {
        "top": [
            BiliVideo(id=101, bvid="BV001", title="Video 1", source_folder_id=1001),
            BiliVideo(id=102, bvid="BV002", title="Video 2", source_folder_id=1001),
        ],
        "main": [
            BiliVideo(id=201, bvid="BV003", title="Video 3", source_folder_id=1002),
        ],
        "bottom": [
            BiliVideo(id=301, bvid="BV004", title="Video 4", source_folder_id=1003),
        ],
    }
    await sm.init_category("tmp_test", target_folder_id=5001, target_folder_title="tmp_test", videos_by_block=videos_by_block)
    assert "tmp_test" in sm.state.categories
    cat = sm.state.categories["tmp_test"]
    assert len(cat.blocks["top"].videos) == 2
    assert len(cat.blocks["main"].videos) == 1
    assert len(cat.blocks["bottom"].videos) == 1
    assert all(v.status == "pending" for v in cat.blocks["top"].videos)
    print("3. init_category -> 4 videos (2 top, 1 main, 1 bottom) OK")

    # Test 4: record_video_result
    await sm.record_video_result("tmp_test", "top", avid=101, status="moved", op="move")
    await sm.record_video_result("tmp_test", "top", avid=102, status="skipped", op="skip")
    await sm.record_video_result("tmp_test", "main", avid=201, status="failed", op="move", error="412 rate limit")
    # bottom/301 still pending
    v101_status = await sm.get_video_status("tmp_test", "top", 101)
    v201_status = await sm.get_video_status("tmp_test", "main", 201)
    v301_status = await sm.get_video_status("tmp_test", "bottom", 301)
    assert v101_status == "moved"
    assert v201_status == "failed"
    assert v301_status == "pending"
    print("4. record_video_result -> moved/skipped/failed/pending OK")

    # Test 5: detect_interrupted_task with incomplete state
    result = await sm.detect_interrupted_task()
    assert result is not None
    assert result["total_categories"] == 1
    assert result["incomplete_categories"] == 1
    assert result["total_videos"] == 4
    assert result["completed_videos"] == 2  # moved + skipped
    assert result["pending_videos"] == 1
    assert result["failed_videos"] == 1
    print(f"5. detect_interrupted_task -> {result['completed_videos']} completed, {result['pending_videos']} pending, {result['failed_videos']} failed OK")

    # Test 6: get_pending_videos and get_failed_videos
    pending = await sm.get_pending_videos("tmp_test")
    failed = await sm.get_failed_videos("tmp_test")
    assert len(pending) == 1
    assert pending[0][1].avid == 301
    assert len(failed) == 1
    assert failed[0][1].avid == 201
    print("6. get_pending/failed_videos OK")

    # Test 7: Simulate reload (new manager instance)
    sm2 = MoveStateManager(config)
    state2 = await sm2.load_state()
    assert state2 is not None
    assert state2.session_id == state.session_id
    v101_status2 = await sm2.get_video_status("tmp_test", "top", 101)
    assert v101_status2 == "moved"
    print("7. reload state from file OK")

    # Test 8: mark_video_status (for source/target verification)
    await sm2.mark_video_status("tmp_test", "bottom", 301, status="moved", op="move")
    v301_status2 = await sm2.get_video_status("tmp_test", "bottom", 301)
    assert v301_status2 == "moved"
    print("8. mark_video_status OK")

    # Test 9: update_block_status
    await sm2.update_block_status("tmp_test", "top", "completed")
    # top block: 101=moved, 102=skipped -> all terminal -> should be completed
    assert sm2.state.categories["tmp_test"].blocks["top"].status == "completed"
    # Try to complete main block (has failed video) -> should NOT complete
    await sm2.update_block_status("tmp_test", "main", "completed")
    assert sm2.state.categories["tmp_test"].blocks["main"].status != "completed"
    print("9. update_block_status (terminal check) OK")

    # Test 10: mark_category_completed
    await sm2.mark_category_completed("tmp_test")
    assert sm2.state.categories["tmp_test"].status == "completed"
    is_completed = await sm2.is_category_completed("tmp_test")
    assert is_completed == True
    print("10. mark_category_completed OK")

    # Test 11: detect_interrupted_task after all completed
    result = await sm2.detect_interrupted_task()
    assert result is None, f"Expected None after all completed, got {result}"
    print("11. detect_interrupted_task (all completed) -> None OK")

    # Test 12: archive_state
    await sm2.archive_state()
    assert not sm2.state_path.exists()
    backups = list(sm2.state_path.parent.glob("move_state_*.json.bak"))
    assert len(backups) >= 1
    print(f"12. archive_state -> {len(backups)} backup(s) OK")

    # Cleanup
    for bak in sm2.state_path.parent.glob("move_state_*.json.bak"):
        bak.unlink()
    tmp = sm2.state_path.parent / "move_state.json.tmp"
    if tmp.exists():
        tmp.unlink()

    print("\n=== ALL STATE LIFECYCLE TESTS PASSED ===")


async def test_atomic_write_safety():
    """Test that atomic write doesn't corrupt the file"""
    print("\n=== Test: Atomic write safety ===")
    config = BiliConfig()
    sm = MoveStateManager(config)

    if sm.state_path.exists():
        sm.state_path.unlink()

    await sm.create_new_state("auto", False, "videos.csv", time.time())
    await sm.init_category("test_atomic", 9999, "test_atomic", {
        "top": [BiliVideo(id=1, bvid="BV1", title="T1")],
        "main": [],
        "bottom": [],
    })

    # Verify file is valid JSON
    import json
    with open(sm.state_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert "categories" in data
    assert "test_atomic" in data["categories"]
    print("Atomic write -> valid JSON OK")

    # Verify no .tmp file left behind
    tmp = sm.state_path.parent / "move_state.json.tmp"
    assert not tmp.exists(), ".tmp file left behind"
    print("No .tmp leftover OK")

    # Cleanup
    sm.state_path.unlink()
    print("=== ATOMIC WRITE TESTS PASSED ===")


async def test_resume_summary_format():
    """Test that resume summary has correct format for UI display"""
    print("\n=== Test: Resume summary format ===")
    config = BiliConfig()
    sm = MoveStateManager(config)

    if sm.state_path.exists():
        sm.state_path.unlink()

    await sm.create_new_state("auto", False, "videos.csv", time.time())

    # Create two categories, one completed, one incomplete
    await sm.init_category("tmp_completed", 1001, "tmp_completed", {
        "top": [BiliVideo(id=1, bvid="BV1", title="V1")],
        "main": [],
        "bottom": [],
    })
    await sm.record_video_result("tmp_completed", "top", 1, "moved", "move")
    await sm.mark_category_completed("tmp_completed")

    await sm.init_category("tmp_incomplete", 1002, "tmp_incomplete", {
        "top": [BiliVideo(id=2, bvid="BV2", title="V2")],
        "main": [BiliVideo(id=3, bvid="BV3", title="V3")],
        "bottom": [],
    })
    await sm.record_video_result("tmp_incomplete", "top", 2, "moved", "move")
    # id=3 still pending

    result = await sm.detect_interrupted_task()
    assert result is not None
    assert result["total_categories"] == 2
    assert result["completed_categories"] == 1
    assert result["incomplete_categories"] == 1
    assert result["total_videos"] == 3
    assert result["completed_videos"] == 2  # V1 moved + V2 moved
    assert result["pending_videos"] == 1  # V3
    assert len(result["categories"]) == 1  # only incomplete
    assert result["categories"][0]["name"] == "tmp_incomplete"
    print(f"Resume summary: {result['completed_categories']}/{result['total_categories']} cats, {result['completed_videos']}/{result['total_videos']} videos OK")

    # Cleanup
    await sm.archive_state()
    for bak in sm.state_path.parent.glob("move_state_*.json.bak"):
        bak.unlink()

    print("=== RESUME SUMMARY TESTS PASSED ===")


if __name__ == "__main__":
    asyncio.run(test_state_lifecycle())
    asyncio.run(test_atomic_write_safety())
    asyncio.run(test_resume_summary_format())
    print("\n\n##################################")
    print("# ALL VERIFICATION TESTS PASSED #")
    print("##################################")
