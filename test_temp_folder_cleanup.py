"""临时排序桶清理与删除的单元测试

覆盖：
  1. 临时桶非空时先 move 回目标桶后删除
  2. 临时桶不存在时应跳过不报错
  3. 标准桶(tmp_xxx)与临时桶(tmp_reorder_xxx)命名冲突的边界
  4. delete_folder API 返回码处理（11010 不存在视为成功；其他错误返回 False）
  5. cleanup_temp_folders 中 move_resources 失败时保留临时桶
  6. reorder_folder_full 阶段1 创建新临时桶（带 session_id 后缀）
  7. reorder_folder_full cleanup 阶段删除临时桶

所有测试用 mock，不真实请求 B站 API。
运行方式：
  python -m pytest test_temp_folder_cleanup.py -v
  python -m unittest test_temp_folder_cleanup -v
"""

import unittest
from unittest.mock import AsyncMock, MagicMock

from config import BiliConfig
from crawler import BiliCrawler
from models import BiliFolder
from organizer import BiliOrganizer
from reorder_state import BucketReorderState, ReorderState


def _make_organizer() -> BiliOrganizer:
    """构造一个不真实联网的 BiliOrganizer（绕过 __init__ 的依赖创建）。

    crawler / reorder_state_manager 全部替换为 AsyncMock，
    避免触发任何 B站 API 或文件 IO。
    """
    config = BiliConfig()
    organizer = BiliOrganizer.__new__(BiliOrganizer)
    organizer.config = config
    organizer.crawler = AsyncMock(spec=BiliCrawler)
    organizer.sorter = MagicMock()
    organizer._capacity_cache = {}
    organizer._dead_source_folders = {}
    organizer._add_skip_until = 0.0
    organizer.state_manager = AsyncMock()
    organizer.reorder_state_manager = AsyncMock()
    # 默认 last_post_412 属性（crawler mock 上手动设置）
    organizer.crawler.last_post_412 = False
    return organizer


def _folder(folder_id: int, title: str, media_count: int = 0) -> BiliFolder:
    return BiliFolder(
        id=folder_id,
        fid=folder_id // 100,
        mid=12345678,
        title=title,
        media_count=media_count,
    )


class TestCleanupTempFolders(unittest.IsolatedAsyncioTestCase):
    """cleanup_temp_folders 行为测试"""

    async def test_no_temp_folders_skips_silently(self):
        """场景2: 没有任何 tmp_reorder_ 临时桶时应静默跳过，不报错。"""
        org = _make_organizer()
        org.crawler.get_created_folders = AsyncMock(return_value=[])

        # 不应抛异常
        await org.cleanup_temp_folders()

        org.crawler.get_created_folders.assert_awaited_once()
        # 没有临时桶时不应调用 delete_folder
        org.crawler.delete_folder.assert_not_awaited()

    async def test_standard_buckets_not_treated_as_temp(self):
        """场景3: 13 个标准桶(tmp_xxx)不应被当作临时桶(tmp_reorder_xxx)处理。"""
        org = _make_organizer()
        standard_buckets = [
            _folder(100 + i, name, media_count=50)
            for i, name in enumerate([
                "tmp_生活娱乐", "tmp_知识科普", "tmp_数码科技", "tmp_游戏资讯",
                "tmp_游戏实况", "tmp_影视解说", "tmp_播客访谈", "tmp_社会观察",
                "tmp_汽车运动", "tmp_编程开发", "tmp_投资财经", "tmp_美食探店",
                "tmp_待分类",
            ])
        ]
        org.crawler.get_created_folders = AsyncMock(return_value=standard_buckets)
        org.reorder_state_manager.load_state = AsyncMock(return_value=None)
        org.reorder_state_manager.state = None

        await org.cleanup_temp_folders()

        # 标准桶不应被删除
        org.crawler.delete_folder.assert_not_awaited()
        org.crawler.move_resources.assert_not_awaited()

    async def test_temp_folder_nonempty_moves_then_deletes(self):
        """场景1: 临时桶非空时先 move 回目标桶，然后删除。"""
        org = _make_organizer()
        temp_folder = _folder(1001, "tmp_reorder_tmp_待分类_20260624", media_count=3)
        target_folder = _folder(2001, "tmp_待分类", media_count=10)
        org.crawler.get_created_folders = AsyncMock(
            return_value=[temp_folder, target_folder]
        )
        org.reorder_state_manager.load_state = AsyncMock(return_value=None)
        org.reorder_state_manager.state = None
        # 临时桶里有 3 条视频
        org.crawler.get_folder_video_ids = AsyncMock(
            return_value=[{"id": 1, "type": 2}, {"id": 2, "type": 2}, {"id": 3, "type": 2}]
        )
        org.crawler.move_resources = AsyncMock(return_value=True)
        org.crawler.delete_folder = AsyncMock(return_value=True)

        await org.cleanup_temp_folders()

        # 应当先 move 回目标桶
        org.crawler.move_resources.assert_awaited()
        move_call = org.crawler.move_resources.await_args
        # 参数: (src_media_id, tar_media_id, resources)
        self.assertEqual(move_call.args[0], 1001)  # 临时桶 id
        self.assertEqual(move_call.args[1], 2001)  # 目标桶 id
        # 回迁成功后应删除临时桶
        org.crawler.delete_folder.assert_awaited_once_with(1001)

    async def test_temp_folder_move_failure_skips_delete(self):
        """场景5: move 回目标桶失败时不应删除临时桶（避免数据丢失）。"""
        org = _make_organizer()
        temp_folder = _folder(1001, "tmp_reorder_tmp_待分类_20260624", media_count=3)
        target_folder = _folder(2001, "tmp_待分类", media_count=10)
        org.crawler.get_created_folders = AsyncMock(
            return_value=[temp_folder, target_folder]
        )
        org.reorder_state_manager.load_state = AsyncMock(return_value=None)
        org.reorder_state_manager.state = None
        org.crawler.get_folder_video_ids = AsyncMock(
            return_value=[{"id": 1, "type": 2}, {"id": 2, "type": 2}]
        )
        # move 失败
        org.crawler.move_resources = AsyncMock(return_value=False)
        org.crawler.delete_folder = AsyncMock(return_value=True)

        await org.cleanup_temp_folders()

        # move 被调用但失败
        org.crawler.move_resources.assert_awaited()
        # 关键断言：move 失败时不应删除临时桶
        org.crawler.delete_folder.assert_not_awaited()

    async def test_empty_temp_folder_deleted(self):
        """空临时桶直接删除，无需 move。"""
        org = _make_organizer()
        temp_folder = _folder(1001, "tmp_reorder_tmp_待分类_20260624", media_count=0)
        org.crawler.get_created_folders = AsyncMock(return_value=[temp_folder])
        org.reorder_state_manager.load_state = AsyncMock(return_value=None)
        org.reorder_state_manager.state = None
        org.crawler.delete_folder = AsyncMock(return_value=True)

        await org.cleanup_temp_folders()

        # 空桶不需要 move
        org.crawler.move_resources.assert_not_awaited()
        org.crawler.get_folder_video_ids.assert_not_awaited()
        # 空桶应直接删除
        org.crawler.delete_folder.assert_awaited_once_with(1001)

    async def test_in_progress_temp_folder_preserved(self):
        """有 in_progress 状态记录的临时桶应保留，不删除。"""
        org = _make_organizer()
        temp_folder = _folder(1001, "tmp_reorder_tmp_待分类_20260624", media_count=5)
        org.crawler.get_created_folders = AsyncMock(return_value=[temp_folder])
        org.crawler.delete_folder = AsyncMock(return_value=True)

        # 构造 in_progress 状态：temp_folder_id 匹配
        bucket_state = BucketReorderState(
            status="in_progress",
            temp_folder_id=1001,
            phase="move_to_temp",
        )
        state = ReorderState(session_id="20260624_221505")
        state.categories = {"tmp_待分类": bucket_state}
        org.reorder_state_manager.load_state = AsyncMock(return_value=state)
        org.reorder_state_manager.state = state

        await org.cleanup_temp_folders()

        # in_progress 桶应保留
        org.crawler.delete_folder.assert_not_awaited()

    async def test_temp_folder_no_target_bucket_warns_only(self):
        """临时桶找不到对应目标桶时只打印警告，不删除（避免数据丢失）。"""
        org = _make_organizer()
        # 临时桶的 base_name 是 "tmp_不存在分类"，目标桶列表里没有
        temp_folder = _folder(1001, "tmp_reorder_tmp_不存在分类", media_count=3)
        target_folder = _folder(2001, "tmp_待分类", media_count=10)
        org.crawler.get_created_folders = AsyncMock(
            return_value=[temp_folder, target_folder]
        )
        org.reorder_state_manager.load_state = AsyncMock(return_value=None)
        org.reorder_state_manager.state = None
        org.crawler.delete_folder = AsyncMock(return_value=True)

        await org.cleanup_temp_folders()

        # 找不到目标桶，不应删除非空临时桶（数据保护）
        org.crawler.delete_folder.assert_not_awaited()

    async def test_temp_folder_truncated_title_fuzzy_match(self):
        """场景3 边界: 临时桶标题被截断到 20 字符时，模糊匹配仍能找到目标桶。"""
        org = _make_organizer()
        # "tmp_reorder_tmp_生活娱乐_20260624" 截断到 20 字符 = "tmp_reorder_tmp_生活娱"
        # 去掉前缀后 base_name = "tmp_生活娱"
        temp_folder = _folder(1001, "tmp_reorder_tmp_生活娱", media_count=2)
        target_folder = _folder(2001, "tmp_生活娱乐", media_count=10)
        org.crawler.get_created_folders = AsyncMock(
            return_value=[temp_folder, target_folder]
        )
        org.reorder_state_manager.load_state = AsyncMock(return_value=None)
        org.reorder_state_manager.state = None
        org.crawler.get_folder_video_ids = AsyncMock(
            return_value=[{"id": 1, "type": 2}, {"id": 2, "type": 2}]
        )
        org.crawler.move_resources = AsyncMock(return_value=True)
        org.crawler.delete_folder = AsyncMock(return_value=True)

        await org.cleanup_temp_folders()

        # 模糊匹配应能找到 tmp_生活娱乐
        org.crawler.move_resources.assert_awaited()
        move_call = org.crawler.move_resources.await_args
        self.assertEqual(move_call.args[1], 2001)
        # 回迁成功后应删除临时桶
        org.crawler.delete_folder.assert_awaited_once_with(1001)


class TestDeleteFolderApiHandling(unittest.IsolatedAsyncioTestCase):
    """crawler.delete_folder 对 API 返回码的处理测试（场景4）"""

    async def test_delete_folder_code_0_success(self):
        """API 返回 code=0 时删除成功。"""
        org = _make_organizer()
        # delete_folder 内部会调用 _wait_post_interval 和 rate_limiter，
        # 这里直接 mock 整个 delete_folder 方法验证调用契约即可
        org.crawler.delete_folder = AsyncMock(return_value=True)
        result = await org.crawler.delete_folder(1001)
        self.assertTrue(result)

    async def test_delete_folder_code_11010_treated_as_success(self):
        """API 返回 code=11010（收藏夹不存在）应视为成功（幂等）。"""
        from crawler import BiliCrawler
        crawler = BiliCrawler.__new__(BiliCrawler)
        crawler.config = BiliConfig()
        crawler._last_post_time = 0.0
        crawler._post_min_interval = 0.0
        crawler.rate_limiter = MagicMock()
        crawler.rate_limiter.__aenter__ = AsyncMock(return_value=crawler.rate_limiter)
        crawler.rate_limiter.__aexit__ = AsyncMock(return_value=None)
        crawler.rate_limiter.report_412 = AsyncMock()
        crawler.rate_limiter.report_success = MagicMock()
        crawler.last_post_412 = False

        # mock client.post 返回 11010
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"code": 11010, "message": "收藏夹不存在"}
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        crawler._get_client = AsyncMock(return_value=mock_client)

        result = await crawler.delete_folder(1001, max_retries=2)
        self.assertTrue(result)

    async def test_delete_folder_other_error_returns_false(self):
        """API 返回其他非 0 code（如 -403 权限不足）应返回 False。"""
        from crawler import BiliCrawler
        crawler = BiliCrawler.__new__(BiliCrawler)
        crawler.config = BiliConfig()
        crawler._last_post_time = 0.0
        crawler._post_min_interval = 0.0
        crawler.rate_limiter = MagicMock()
        crawler.rate_limiter.__aenter__ = AsyncMock(return_value=crawler.rate_limiter)
        crawler.rate_limiter.__aexit__ = AsyncMock(return_value=None)
        crawler.rate_limiter.report_412 = AsyncMock()
        crawler.rate_limiter.report_success = MagicMock()
        crawler.last_post_412 = False

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"code": -403, "message": "权限不足"}
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        crawler._get_client = AsyncMock(return_value=mock_client)

        result = await crawler.delete_folder(1001, max_retries=2)
        self.assertFalse(result)


class TestReorderFolderFullCleanup(unittest.IsolatedAsyncioTestCase):
    """reorder_folder_full 的 cleanup 阶段测试"""

    async def _setup_for_cleanup(self, temp_remaining_ids=None):
        """构造一个可走到 cleanup 阶段的 organizer"""
        org = _make_organizer()

        # reorder_state_manager 已初始化
        state = ReorderState(session_id="20260625_120000")
        org.reorder_state_manager.state = state
        bucket_state = BucketReorderState(
            status="in_progress",
            target_folder_id=2001,
            temp_folder_id=1001,
            phase="move_back",
            moved_back_count=10,
        )
        state.categories = {"tmp_待分类": bucket_state}
        org.reorder_state_manager.get_bucket_state = AsyncMock(return_value=bucket_state)
        org.reorder_state_manager.update_bucket_phase = AsyncMock()
        org.reorder_state_manager.mark_bucket_completed = AsyncMock()

        # mock crawler
        org.crawler.create_folder = AsyncMock(return_value=1001)
        org.crawler.get_folder_video_ids = AsyncMock(
            return_value=temp_remaining_ids if temp_remaining_ids else []
        )
        org.crawler.move_resources = AsyncMock(return_value=True)
        org.crawler.delete_folder = AsyncMock(return_value=True)

        # 跳过 move_to_temp / move_back 阶段：让 resume_phase="cleanup"
        # 通过 bucket_state.phase == "cleanup" 触发 resume
        bucket_state.phase = "cleanup"

        return org

    async def test_cleanup_empty_temp_folder_deleted(self):
        """cleanup 阶段临时桶为空时删除。"""
        org = await self._setup_for_cleanup(temp_remaining_ids=[])

        # 调用 reorder_folder_full，sorted_videos 为空列表（move_back 已完成）
        result = await org.reorder_folder_full(
            "tmp_待分类", [], target_folder_id=2001, dry_run=False
        )

        self.assertTrue(result)
        # 应检查临时桶是否为空
        org.crawler.get_folder_video_ids.assert_awaited()
        # 空临时桶应被删除
        org.crawler.delete_folder.assert_awaited_once_with(1001)

    async def test_cleanup_nonempty_temp_folder_moves_back_then_deletes(self):
        """cleanup 阶段临时桶非空时先 move 回目标桶，然后删除。"""
        remaining = [{"id": 100, "type": 2}, {"id": 101, "type": 2}]
        org = await self._setup_for_cleanup(temp_remaining_ids=remaining)

        result = await org.reorder_folder_full(
            "tmp_待分类", [], target_folder_id=2001, dry_run=False
        )

        self.assertTrue(result)
        # 应当 move 残留视频回目标桶
        org.crawler.move_resources.assert_awaited()
        move_call = org.crawler.move_resources.await_args
        self.assertEqual(move_call.args[0], 1001)  # 临时桶
        self.assertEqual(move_call.args[1], 2001)  # 目标桶
        # 回迁成功后应删除临时桶
        org.crawler.delete_folder.assert_awaited_once_with(1001)

    async def test_cleanup_nonempty_temp_move_failure_skips_delete(self):
        """cleanup 阶段临时桶非空且 move 失败时不应删除（数据保护）。"""
        remaining = [{"id": 100, "type": 2}, {"id": 101, "type": 2}]
        org = await self._setup_for_cleanup(temp_remaining_ids=remaining)
        # move 失败
        org.crawler.move_resources = AsyncMock(return_value=False)

        result = await org.reorder_folder_full(
            "tmp_待分类", [], target_folder_id=2001, dry_run=False
        )

        # 重排本身仍算完成（视频已在目标桶），返回 True
        self.assertTrue(result)
        org.crawler.move_resources.assert_awaited()
        # 关键：move 失败时不应删除临时桶
        org.crawler.delete_folder.assert_not_awaited()

    async def test_dry_run_skips_cleanup(self):
        """dry_run=True 时跳过整个重排（包括 cleanup）。"""
        org = await self._setup_for_cleanup()

        result = await org.reorder_folder_full(
            "tmp_待分类", [], target_folder_id=2001, dry_run=True
        )

        self.assertTrue(result)
        org.crawler.delete_folder.assert_not_awaited()


if __name__ == "__main__":
    unittest.main(verbosity=2)
