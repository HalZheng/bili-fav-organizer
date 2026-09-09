"""「稍后再看」清理功能的单元测试

复现并验证修复「视频加入目标收藏夹后未从稍后再看列表删除」的 bug。

背景：
  B站「稍后再看」是独立于收藏夹的特殊列表（API: /x/v2/history/toview）。
  add_resources 把视频加入收藏夹后，视频仍留在稍后再看列表中。
  必须单独调用 /x/v2/history/toview/del (aid + csrf，该老接口只认 aid) 才能从稍后再看移除。

覆盖场景：
  1. crawler.delete_watchlater API 参数正确性（aid + csrf + 正确 endpoint）
  2. crawler.delete_watchlater 对各种返回码的处理
  3. organizer._cleanup_watchlater_if_needed 对稍后再看/非稍后再看视频的区分
  4. incremental_organize add-only 分支：稍后再看视频 add 后应触发清理
  5. incremental_organize add-only 分支：非稍后再看视频 add 后不应触发清理
  6. reorder_folder_full add_new_to_temp：稍后再看视频 add 到临时桶后应触发清理
  7. _organize_single_category_inner auto 模式：稍后再看视频 add 后应触发清理

所有测试用 mock，不真实请求 B站 API。
运行方式：
  python -m pytest test_watchlater_cleanup.py -v
  python -m unittest test_watchlater_cleanup -v
"""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from config import BiliConfig
from crawler import BiliCrawler
from models import BiliVideo, BiliUP, BiliFolder
from organizer import BiliOrganizer
from reorder_state import BucketReorderState, ReorderState


# ─── 辅助构造函数 ───

def _make_config() -> BiliConfig:
    """构造测试用配置"""
    return BiliConfig()


def _make_organizer() -> BiliOrganizer:
    """构造一个不真实联网的 BiliOrganizer（绕过 __init__ 的依赖创建）。

    crawler / reorder_state_manager / state_manager 全部替换为 AsyncMock，
    避免触发任何 B站 API 或文件 IO。
    """
    config = _make_config()
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


def _make_crawler() -> BiliCrawler:
    """构造一个不真实联网的 BiliCrawler（绕过 __init__ 的依赖创建）。"""
    crawler = BiliCrawler.__new__(BiliCrawler)
    crawler.config = _make_config()
    crawler._last_post_time = 0.0
    crawler._post_min_interval = 0.0
    crawler.rate_limiter = MagicMock()
    crawler.rate_limiter.__aenter__ = AsyncMock(return_value=crawler.rate_limiter)
    crawler.rate_limiter.__aexit__ = AsyncMock(return_value=None)
    crawler.rate_limiter.report_412 = AsyncMock()
    crawler.rate_limiter.report_success = MagicMock()
    crawler.last_post_412 = False
    return crawler


def _make_watchlater_video(
    avid: int = 100001,
    bvid: str = "BV1TestVideo01",
    title: str = "测试稍后再看视频",
) -> BiliVideo:
    """构造一个来自「稍后再看」的视频"""
    return BiliVideo(
        id=avid,
        bvid=bvid,
        title=title,
        upper=BiliUP(mid=123, name="测试UP"),
        type=2,
        source_folder_id=0,  # 稍后再看的特殊标识
        source_folder_title="稍后再看",
    )


def _make_folder_video(
    avid: int = 200001,
    bvid: str = "BV2TestVideo02",
    title: str = "测试收藏夹视频",
    source_folder_id: int = 4054866095,
) -> BiliVideo:
    """构造一个来自普通收藏夹的视频"""
    return BiliVideo(
        id=avid,
        bvid=bvid,
        title=title,
        upper=BiliUP(mid=456, name="普通UP"),
        type=2,
        source_folder_id=source_folder_id,
        source_folder_title="tmp_测试桶",
    )


# ─── crawler.delete_watchlater API 测试 ───

class TestDeleteWatchlaterApi(unittest.IsolatedAsyncioTestCase):
    """crawler.delete_watchlater 的 API 调用与返回码处理测试"""

    async def test_delete_watchlater_success_code_0(self):
        """API 返回 code=0 时删除成功，返回 True。"""
        crawler = _make_crawler()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"code": 0, "message": "0", "ttl": 1}
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        crawler._get_client = AsyncMock(return_value=mock_client)
        crawler._bvid_to_avid = AsyncMock(return_value=100001)

        result = await crawler.delete_watchlater(avid=100001)

        self.assertTrue(result)
        # 验证调用的是正确的 endpoint
        mock_client.post.assert_awaited_once()
        call_args = mock_client.post.await_args
        self.assertEqual(call_args.args[0], "/x/v2/history/toview/del")
        # 传了 avid 就不应再走 bvid→avid 转换
        crawler._bvid_to_avid.assert_not_awaited()

    async def test_delete_watchlater_sends_aid_and_csrf(self):
        """验证请求参数包含 aid（字符串形式）和 csrf（bili_jct）。"""
        crawler = _make_crawler()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"code": 0, "message": "0"}
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        crawler._get_client = AsyncMock(return_value=mock_client)
        crawler._bvid_to_avid = AsyncMock(return_value=100001)

        await crawler.delete_watchlater(avid=100001)

        call_args = mock_client.post.await_args
        # data 参数是第二个位置参数或 kwargs["data"]
        data = call_args.kwargs.get("data") or call_args.args[1]
        # 老接口只认 aid（字符串形式），不再用 bvid
        self.assertEqual(data["aid"], "100001")
        self.assertNotIn("bvid", data)
        self.assertEqual(data["csrf"], crawler.config.BILI_JCT)
        crawler._bvid_to_avid.assert_not_awaited()

    async def test_delete_watchlater_empty_bvid_returns_false(self):
        """bvid 和 avid 均为空时应返回 False，不发起请求。"""
        crawler = _make_crawler()
        mock_client = AsyncMock()
        crawler._get_client = AsyncMock(return_value=mock_client)
        crawler._bvid_to_avid = AsyncMock(return_value=0)

        result = await crawler.delete_watchlater()

        self.assertFalse(result)
        mock_client.post.assert_not_awaited()
        crawler._bvid_to_avid.assert_not_awaited()

    async def test_delete_watchlater_412_returns_false(self):
        """412 风控时应返回 False，设置 last_post_412 标志。"""
        crawler = _make_crawler()
        mock_resp = MagicMock()
        mock_resp.status_code = 412
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        crawler._get_client = AsyncMock(return_value=mock_client)
        crawler._bvid_to_avid = AsyncMock(return_value=100001)

        result = await crawler.delete_watchlater(avid=100001)

        self.assertFalse(result)
        self.assertTrue(crawler.last_post_412)
        crawler._bvid_to_avid.assert_not_awaited()

    async def test_delete_watchlater_non_retryable_code_returns_false(self):
        """-101(未登录)/-111(csrf失败)/-403(权限不足) 等不可恢复错误返回 False。"""
        crawler = _make_crawler()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"code": -403, "message": "权限不足"}
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        crawler._get_client = AsyncMock(return_value=mock_client)
        crawler._bvid_to_avid = AsyncMock(return_value=100001)

        result = await crawler.delete_watchlater(avid=100001)

        self.assertFalse(result)
        crawler._bvid_to_avid.assert_not_awaited()

    async def test_delete_watchlater_other_error_returns_false(self):
        """其他非 0 code（如视频不在列表）返回 False。"""
        crawler = _make_crawler()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"code": 10003, "message": "不存在该稿件"}
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        crawler._get_client = AsyncMock(return_value=mock_client)
        crawler._bvid_to_avid = AsyncMock(return_value=100001)

        result = await crawler.delete_watchlater(avid=100001)

        self.assertFalse(result)
        crawler._bvid_to_avid.assert_not_awaited()

    async def test_delete_watchlater_json_parse_failure_returns_false(self):
        """响应 JSON 解析失败时返回 False。"""
        crawler = _make_crawler()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.side_effect = ValueError("invalid json")
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        crawler._get_client = AsyncMock(return_value=mock_client)
        crawler._bvid_to_avid = AsyncMock(return_value=100001)

        result = await crawler.delete_watchlater(avid=100001)

        self.assertFalse(result)
        crawler._bvid_to_avid.assert_not_awaited()

    async def test_delete_watchlater_bvid_to_avid_conversion(self):
        """只传 bvid 时，内部调用 _bvid_to_avid 转换为 aid，再发起 POST。"""
        crawler = _make_crawler()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"code": 0, "message": "0"}
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        crawler._get_client = AsyncMock(return_value=mock_client)
        # 直接 mock _bvid_to_avid 方法本身，避免深入其内部依赖
        crawler._bvid_to_avid = AsyncMock(return_value=100001)

        result = await crawler.delete_watchlater(bvid="BV1AbcDefGh")

        self.assertTrue(result)
        crawler._bvid_to_avid.assert_awaited_once_with("BV1AbcDefGh")
        mock_client.post.assert_awaited_once()
        call_args = mock_client.post.await_args
        data = call_args.kwargs.get("data") or call_args.args[1]
        self.assertEqual(data["aid"], "100001")
        self.assertEqual(data["csrf"], crawler.config.BILI_JCT)

    async def test_delete_watchlater_bvid_to_avid_failure(self):
        """只传 bvid 但 _bvid_to_avid 返回 0 时，应返回 False 且不发起 POST。"""
        crawler = _make_crawler()
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=MagicMock())
        crawler._get_client = AsyncMock(return_value=mock_client)
        crawler._bvid_to_avid = AsyncMock(return_value=0)

        result = await crawler.delete_watchlater(bvid="BV1NoAid")

        self.assertFalse(result)
        crawler._bvid_to_avid.assert_awaited_once_with("BV1NoAid")
        mock_client.post.assert_not_awaited()


# ─── organizer._cleanup_watchlater_if_needed 测试 ───

class TestCleanupWatchlaterHelper(unittest.IsolatedAsyncioTestCase):
    """_cleanup_watchlater_if_needed 辅助方法测试"""

    async def test_watchlater_video_triggers_delete(self):
        """稍后再看视频应触发 crawler.delete_watchlater 调用。"""
        org = _make_organizer()
        org.crawler.delete_watchlater = AsyncMock(return_value=True)
        video = _make_watchlater_video(avid=100001, bvid="BV1CleanupTest")

        result = await org._cleanup_watchlater_if_needed(video)

        self.assertTrue(result)
        org.crawler.delete_watchlater.assert_awaited_once_with(bvid="BV1CleanupTest", avid=100001)

    async def test_non_watchlater_video_skips_delete(self):
        """非稍后再看视频（来自普通收藏夹）不应触发 delete_watchlater。"""
        org = _make_organizer()
        org.crawler.delete_watchlater = AsyncMock(return_value=True)
        video = _make_folder_video(source_folder_id=4054866095)

        result = await org._cleanup_watchlater_if_needed(video)

        self.assertTrue(result)
        org.crawler.delete_watchlater.assert_not_awaited()

    async def test_watchlater_video_without_bvid_returns_false(self):
        """稍后再看视频缺少 bvid 和 avid 时返回 False，不调用 API。"""
        org = _make_organizer()
        org.crawler.delete_watchlater = AsyncMock(return_value=True)
        video = _make_watchlater_video(avid=0, bvid="")

        result = await org._cleanup_watchlater_if_needed(video)

        self.assertFalse(result)
        org.crawler.delete_watchlater.assert_not_awaited()

    async def test_watchlater_cleanup_failure_returns_false(self):
        """delete_watchlater 失败时返回 False（但不应抛异常）。"""
        org = _make_organizer()
        org.crawler.delete_watchlater = AsyncMock(return_value=False)
        video = _make_watchlater_video(avid=100001, bvid="BV1FailTest")

        result = await org._cleanup_watchlater_if_needed(video)

        self.assertFalse(result)
        org.crawler.delete_watchlater.assert_awaited_once_with(bvid="BV1FailTest", avid=100001)

    async def test_watchlater_cleanup_exception_returns_false(self):
        """delete_watchlater 抛异常时应被捕获，返回 False。"""
        org = _make_organizer()
        org.crawler.delete_watchlater = AsyncMock(side_effect=Exception("网络错误"))
        video = _make_watchlater_video(bvid="BV1ExceptTest")

        result = await org._cleanup_watchlater_if_needed(video)

        self.assertFalse(result)

    async def test_only_source_folder_id_zero_without_title_skips(self):
        """source_folder_id=0 但 source_folder_title 不是「稍后再看」不应触发清理。
        避免误删其他 source_folder_id 恰好为 0 的视频。
        """
        org = _make_organizer()
        org.crawler.delete_watchlater = AsyncMock(return_value=True)
        video = BiliVideo(
            id=300001,
            bvid="BV1Edge01",
            title="边界视频",
            source_folder_id=0,
            source_folder_title="",  # 不是「稍后再看」
        )

        result = await org._cleanup_watchlater_if_needed(video)

        self.assertTrue(result)
        org.crawler.delete_watchlater.assert_not_awaited()


# ─── incremental_organize add-only 分支测试 ───

class TestIncrementalOrganizeWatchlaterCleanup(unittest.IsolatedAsyncioTestCase):
    """incremental_organize add-only 分支的稍后再看清理测试

    这是用户反馈的核心 bug 场景：增量整理流程中，稍后再看视频被 add 到
    目标收藏夹后，未被从稍后再看列表删除。
    """

    def _setup_incremental_organizer(
        self, new_videos: list[BiliVideo], target_folder_id: int = 3987383195
    ) -> BiliOrganizer:
        """构造可执行 incremental_organize add-only 分支的 organizer"""
        org = _make_organizer()

        # reorder_state_manager: 无中断任务，创建新状态成功
        org.reorder_state_manager.detect_interrupted_task = AsyncMock(return_value=None)
        org.reorder_state_manager.create_new_state = AsyncMock()
        org.reorder_state_manager.is_bucket_completed = AsyncMock(return_value=False)
        org.reorder_state_manager.init_bucket = AsyncMock()
        org.reorder_state_manager.mark_bucket_completed = AsyncMock()

        # state 初始化（create_new_state 后会设置 state 属性）
        org.reorder_state_manager.state = ReorderState(session_id="test_session")

        # cleanup_temp_folders: 返回空列表（无临时桶）
        org.crawler.get_created_folders = AsyncMock(return_value=[])

        # merge_and_resort: mock 为返回 (videos, need_reorder=False)
        # 这样走 add-only 分支，不触发 reorder_folder_full
        org.merge_and_resort = AsyncMock(
            return_value=(new_videos, False)
        )

        # get_folder_video_ids: 目标桶为空（所有新视频都需要 add）
        org.crawler.get_folder_video_ids = AsyncMock(return_value=[])

        # add_resources: 默认成功
        org.crawler.add_resources = AsyncMock(return_value=True)

        # delete_watchlater: 默认成功（这是我们要验证被调用的方法）
        org.crawler.delete_watchlater = AsyncMock(return_value=True)

        return org

    async def test_add_only_cleans_watchlater_after_add(self):
        """稍后再看视频 add 到目标桶后，应调用 delete_watchlater 清理。"""
        video = _make_watchlater_video(avid=100001, bvid="BV1IncAdd01")
        org = self._setup_incremental_organizer([video])

        classified = {"tmp_测试桶": [video]}
        folder_map = {"tmp_测试桶": 3987383195}

        await org.incremental_organize(classified, folder_map, reorder=False)

        # add_resources 应被调用
        org.crawler.add_resources.assert_awaited_once()
        # 关键断言：delete_watchlater 应被调用，参数是视频的 bvid 和 avid
        org.crawler.delete_watchlater.assert_awaited_once_with(bvid="BV1IncAdd01", avid=100001)

    async def test_add_only_does_not_clean_non_watchlater_video(self):
        """非稍后再看视频 add 到目标桶后，不应调用 delete_watchlater。"""
        video = _make_folder_video(avid=200001, bvid="BV2IncAdd02", source_folder_id=4054866095)
        org = self._setup_incremental_organizer([video])

        classified = {"tmp_测试桶": [video]}
        folder_map = {"tmp_测试桶": 3987383195}

        await org.incremental_organize(classified, folder_map, reorder=False)

        org.crawler.add_resources.assert_awaited_once()
        # 关键断言：非稍后再看视频不应触发 delete_watchlater
        org.crawler.delete_watchlater.assert_not_awaited()

    async def test_add_only_multiple_watchlater_videos_all_cleaned(self):
        """多条稍后再看视频，每条 add 后都应调用 delete_watchlater。"""
        videos = [
            _make_watchlater_video(avid=100001, bvid="BV1Multi01"),
            _make_watchlater_video(avid=100002, bvid="BV1Multi02"),
            _make_watchlater_video(avid=100003, bvid="BV1Multi03"),
        ]
        org = self._setup_incremental_organizer(videos)

        classified = {"tmp_测试桶": videos}
        folder_map = {"tmp_测试桶": 3987383195}

        await org.incremental_organize(classified, folder_map, reorder=False)

        # add_resources 应被调用 3 次
        self.assertEqual(org.crawler.add_resources.await_count, 3)
        # delete_watchlater 应被调用 3 次，每次参数对应视频 bvid 和 avid
        self.assertEqual(org.crawler.delete_watchlater.await_count, 3)
        called_bvids = [
            call.kwargs["bvid"] for call in org.crawler.delete_watchlater.await_args_list
        ]
        self.assertEqual(set(called_bvids), {"BV1Multi01", "BV1Multi02", "BV1Multi03"})
        called_avids = [
            call.kwargs["avid"] for call in org.crawler.delete_watchlater.await_args_list
        ]
        self.assertEqual(set(called_avids), {100001, 100002, 100003})

    async def test_add_failure_skips_cleanup(self):
        """add_resources 失败时不应调用 delete_watchlater（视频未进入目标桶）。"""
        video = _make_watchlater_video(avid=100001, bvid="BV1FailAdd01")
        org = self._setup_incremental_organizer([video])
        org.crawler.add_resources = AsyncMock(return_value=False)
        org.crawler.last_post_412 = False

        classified = {"tmp_测试桶": [video]}
        folder_map = {"tmp_测试桶": 3987383195}

        await org.incremental_organize(classified, folder_map, reorder=False)

        org.crawler.add_resources.assert_awaited_once()
        # 关键断言：add 失败时不应清理稍后再看
        org.crawler.delete_watchlater.assert_not_awaited()

    async def test_add_success_but_cleanup_failure_does_not_crash(self):
        """add 成功但 delete_watchlater 失败时，流程不应中断（视频已在目标桶）。"""
        video = _make_watchlater_video(avid=100001, bvid="BV1CleanupFail")
        org = self._setup_incremental_organizer([video])
        org.crawler.delete_watchlater = AsyncMock(return_value=False)

        classified = {"tmp_测试桶": [video]}
        folder_map = {"tmp_测试桶": 3987383195}

        # 不应抛异常
        await org.incremental_organize(classified, folder_map, reorder=False)

        org.crawler.add_resources.assert_awaited_once()
        org.crawler.delete_watchlater.assert_awaited_once_with(bvid="BV1CleanupFail", avid=100001)
        # 桶仍应被标记完成（视频已加入目标桶，清理失败只是警告）
        org.reorder_state_manager.mark_bucket_completed.assert_awaited_once()

    async def test_existing_watchlater_video_gets_stale_cleanup(self):
        """已在目标桶的稍后再看视频应补清理（上次 add 成功但清理失败的情况）。"""
        video = _make_watchlater_video(avid=100001, bvid="BV1Exist01")
        org = self._setup_incremental_organizer([video])
        # 目标桶已包含此视频
        org.crawler.get_folder_video_ids = AsyncMock(
            return_value=[{"id": 100001, "type": 2}]
        )

        classified = {"tmp_测试桶": [video]}
        folder_map = {"tmp_测试桶": 3987383195}

        await org.incremental_organize(classified, folder_map, reorder=False)

        # 已存在的视频不应 add（已在目标桶）
        org.crawler.add_resources.assert_not_awaited()
        # 但应补清理稍后再看（上次可能清理失败）
        org.crawler.delete_watchlater.assert_awaited_once_with(
            bvid="BV1Exist01", avid=100001
        )

    async def test_existing_non_watchlater_video_not_cleaned(self):
        """已在目标桶的非稍后再看视频不应触发 cleanup（避免误删）。"""
        video = _make_folder_video(avid=200001, bvid="BV2ExistNoWl")
        org = self._setup_incremental_organizer([video])
        # 目标桶已包含此视频
        org.crawler.get_folder_video_ids = AsyncMock(
            return_value=[{"id": 200001, "type": 2}]
        )

        classified = {"tmp_测试桶": [video]}
        folder_map = {"tmp_测试桶": 3987383195}

        await org.incremental_organize(classified, folder_map, reorder=False)

        # 非稍后再看的已存在视频：不 add 也不 cleanup
        org.crawler.add_resources.assert_not_awaited()
        org.crawler.delete_watchlater.assert_not_awaited()


# ─── reorder_folder_full add_new_to_temp 分支测试 ───

class TestReorderFolderFullWatchlaterCleanup(unittest.IsolatedAsyncioTestCase):
    """reorder_folder_full 的 add_new_to_temp 阶段稍后再看清理测试

    全桶重排流程中，新视频（来自稍后再看）会先 add 到临时桶，
    再 move 回目标桶。add 到临时桶后应从稍后再看删除。
    """

    def _setup_reorder_organizer(
        self,
        new_videos: list[BiliVideo],
        target_folder_id: int = 3987383195,
        temp_folder_id: int = 9990001,
        target_existing_ids: list[dict] = None,
    ) -> BiliOrganizer:
        """构造可执行 reorder_folder_full 的 organizer"""
        org = _make_organizer()

        # reorder_state_manager 已初始化
        state = ReorderState(session_id="test_reorder_session")
        org.reorder_state_manager.state = state
        org.reorder_state_manager.get_bucket_state = AsyncMock(return_value=None)
        org.reorder_state_manager.update_bucket_phase = AsyncMock()
        org.reorder_state_manager.mark_bucket_completed = AsyncMock()

        # crawler mocks
        org.crawler.create_folder = AsyncMock(return_value=temp_folder_id)
        # get_folder_video_ids 被多次调用：
        # 1. move_to_temp: get target_folder_id → target_existing_ids
        # 2. cleanup: get temp_folder_id → [] (空，已 move_back)
        if target_existing_ids is None:
            target_existing_ids = []
        org.crawler.get_folder_video_ids = AsyncMock(
            side_effect=[target_existing_ids, []]
        )
        org.crawler.move_resources = AsyncMock(return_value=True)
        org.crawler.add_resources = AsyncMock(return_value=True)
        org.crawler.delete_watchlater = AsyncMock(return_value=True)
        org.crawler.delete_folder = AsyncMock(return_value=True)

        return org

    async def test_add_new_to_temp_cleans_watchlater(self):
        """稍后再看视频 add 到临时桶后应调用 delete_watchlater。"""
        video = _make_watchlater_video(avid=100001, bvid="BV1Reorder01")
        org = self._setup_reorder_organizer(
            [video], target_existing_ids=[]  # 目标桶为空，video 是新视频
        )

        await org.reorder_folder_full(
            "tmp_测试桶",
            sorted_videos=[],  # move_back 无需操作
            target_folder_id=3987383195,
            dry_run=False,
            new_videos=[video],
        )

        # add_resources 应被调用（add 到临时桶）
        org.crawler.add_resources.assert_awaited_once()
        # 关键断言：delete_watchlater 应被调用
        org.crawler.delete_watchlater.assert_awaited_once_with(bvid="BV1Reorder01", avid=100001)
        # cleanup 阶段会删除临时桶
        org.crawler.delete_folder.assert_awaited_once()

    async def test_add_new_to_temp_skips_non_watchlater(self):
        """非稍后再看视频 add 到临时桶后不应调用 delete_watchlater。"""
        video = _make_folder_video(avid=200001, bvid="BV2Reorder02", source_folder_id=4054866095)
        org = self._setup_reorder_organizer(
            [video], target_existing_ids=[]
        )

        await org.reorder_folder_full(
            "tmp_测试桶",
            sorted_videos=[],
            target_folder_id=3987383195,
            dry_run=False,
            new_videos=[video],
        )

        org.crawler.add_resources.assert_awaited_once()
        org.crawler.delete_watchlater.assert_not_awaited()

    async def test_add_new_to_temp_failure_skips_cleanup(self):
        """add 到临时桶失败时不应调用 delete_watchlater，且应返回 False。"""
        video = _make_watchlater_video(avid=100001, bvid="BV1ReorderFail")
        org = self._setup_reorder_organizer([video])
        org.crawler.add_resources = AsyncMock(return_value=False)
        org.crawler.last_post_412 = False

        result = await org.reorder_folder_full(
            "tmp_测试桶",
            sorted_videos=[],
            target_folder_id=3987383195,
            dry_run=False,
            new_videos=[video],
        )

        self.assertFalse(result)
        org.crawler.add_resources.assert_awaited_once()
        # add 失败时不应清理稍后再看
        org.crawler.delete_watchlater.assert_not_awaited()

    async def test_existing_watchlater_video_in_target_gets_stale_cleanup(self):
        """已存在于目标桶的稍后再看视频不应被 add 到临时桶，但应补清理。"""
        video = _make_watchlater_video(avid=100001, bvid="BV1ExistReorder")
        # 目标桶已包含此视频
        org = self._setup_reorder_organizer(
            [video],
            target_existing_ids=[{"id": 100001, "type": 2}],
        )

        await org.reorder_folder_full(
            "tmp_测试桶",
            sorted_videos=[],
            target_folder_id=3987383195,
            dry_run=False,
            new_videos=[video],
        )

        # 视频已存在，不应 add 到临时桶
        org.crawler.add_resources.assert_not_awaited()
        # 但应补清理稍后再看
        org.crawler.delete_watchlater.assert_awaited_once_with(
            bvid="BV1ExistReorder", avid=100001
        )

    async def test_existing_non_watchlater_in_target_not_cleaned(self):
        """已存在于目标桶的非稍后再看视频不应触发 cleanup。"""
        video = _make_folder_video(avid=200001, bvid="BV2ExistReorder")
        # 目标桶已包含此视频
        org = self._setup_reorder_organizer(
            [video],
            target_existing_ids=[{"id": 200001, "type": 2}],
        )

        await org.reorder_folder_full(
            "tmp_测试桶",
            sorted_videos=[],
            target_folder_id=3987383195,
            dry_run=False,
            new_videos=[video],
        )

        # 非稍后再看的已存在视频：不 add 也不 cleanup
        org.crawler.add_resources.assert_not_awaited()
        org.crawler.delete_watchlater.assert_not_awaited()


# ─── _organize_single_category_inner auto 模式测试 ───

class TestOrganizeSingleCategoryWatchlaterCleanup(unittest.IsolatedAsyncioTestCase):
    """_organize_single_category_inner auto 模式的稍后再看清理测试

    auto 模式下，稍后再看视频 source_folder_id=0，move 条件 (if v.source_folder_id)
    为 False，直接走 add 分支。add 成功后应触发 delete_watchlater。
    """

    def _setup_organize_organizer(
        self, videos: list[BiliVideo], tar_media_id: int = 3987383195
    ) -> BiliOrganizer:
        """构造可执行 _organize_single_category_inner 的 organizer"""
        org = _make_organizer()

        # state_manager: 非恢复模式
        org.state_manager.is_category_completed = AsyncMock(return_value=False)
        org.state_manager.state = None  # 非恢复模式
        org.state_manager.mark_video_status = AsyncMock()
        org.state_manager.record_video_result = AsyncMock()
        org.state_manager.update_block_status = AsyncMock()
        org.state_manager.update_category_status = AsyncMock()
        org.state_manager.mark_category_completed = AsyncMock()
        org.state_manager.get_video_status = AsyncMock(return_value=None)
        org.state_manager.init_category = AsyncMock()

        # crawler mocks
        # resolve_target_folder: 直接返回已知 media_id（跳过容量检查）
        org.crawler.get_created_folders = AsyncMock(return_value=[])

        # get_folder_video_ids: 根据传入的 media_id 返回不同结果
        # - 目标桶(tar_media_id): 返回空（目标桶还没有这些视频）
        # - 源收藏夹(v.source_folder_id): 返回包含该视频（源侧验证通过）
        def _get_ids_side_effect(media_id, *args, **kwargs):
            for v in videos:
                if media_id == v.source_folder_id and v.source_folder_id != 0:
                    return [{"id": v.id, "type": v.type}]
            return []  # 目标桶为空
        org.crawler.get_folder_video_ids = AsyncMock(side_effect=_get_ids_side_effect)

        org.crawler.add_resources = AsyncMock(return_value=True)
        org.crawler.move_resources = AsyncMock(return_value=True)
        org.crawler.copy_resources = AsyncMock(return_value=True)
        org.crawler.delete_watchlater = AsyncMock(return_value=True)

        return org

    async def test_auto_mode_add_fallback_cleans_watchlater(self):
        """auto 模式下稍后再看视频（source_folder_id=0）走 add 分支，add 后应清理。"""
        video = _make_watchlater_video(avid=100001, bvid="BV1Auto01")
        org = self._setup_organize_organizer([video])

        folder_map = {"tmp_测试桶": 3987383195}

        await org._organize_single_category_inner(
            "tmp_测试桶", [video], folder_map, mode="auto", dry_run=False
        )

        # move 不应被调用（source_folder_id=0 跳过 move）
        org.crawler.move_resources.assert_not_awaited()
        # add 应被调用
        org.crawler.add_resources.assert_awaited_once()
        # 关键断言：delete_watchlater 应被调用
        org.crawler.delete_watchlater.assert_awaited_once_with(bvid="BV1Auto01", avid=100001)

    async def test_auto_mode_non_watchlater_uses_move_no_cleanup(self):
        """auto 模式下普通收藏夹视频走 move 分支，move 成功后不应调用 delete_watchlater。"""
        video = _make_folder_video(avid=200001, bvid="BV2Auto02", source_folder_id=4054866095)
        org = self._setup_organize_organizer([video])

        folder_map = {"tmp_测试桶": 3987383195}

        await org._organize_single_category_inner(
            "tmp_测试桶", [video], folder_map, mode="auto", dry_run=False
        )

        # move 应被调用
        org.crawler.move_resources.assert_awaited_once()
        # add 不应被调用
        org.crawler.add_resources.assert_not_awaited()
        # 非稍后再看视频不应触发 delete_watchlater
        org.crawler.delete_watchlater.assert_not_awaited()

    async def test_dry_run_skips_cleanup(self):
        """dry_run=True 时不应调用 add_resources 也不应调用 delete_watchlater。"""
        video = _make_watchlater_video(avid=100001, bvid="BV1Dry01")
        org = self._setup_organize_organizer([video])

        folder_map = {"tmp_测试桶": 3987383195}

        await org._organize_single_category_inner(
            "tmp_测试桶", [video], folder_map, mode="auto", dry_run=True
        )

        # dry_run 时不应有任何 API 调用
        org.crawler.add_resources.assert_not_awaited()
        org.crawler.delete_watchlater.assert_not_awaited()


# ─── 回归测试：证明 bug 已修复 ───

class TestWatchlaterBugRegression(unittest.IsolatedAsyncioTestCase):
    """回归测试：证明「稍后再看视频未被清理」的 bug 已修复

    这些测试模拟完整的增量整理流程（incremental_organize add-only 分支），
    验证稍后再看视频在被加入目标收藏夹后，确实被从稍后再看列表删除。
    如果修复被回退（删除 _cleanup_watchlater_if_needed 调用），
    这些测试会失败。
    """

    async def test_watchlater_video_full_flow_cleaned(self):
        """完整流程：稍后再看视频 → add 到目标桶 → 从稍后再看删除"""
        # 模拟稍后再看列表中的视频
        watchlater_video = _make_watchlater_video(
            avid=116810460500145,
            bvid="BV1d17Y6aELn",
            title="滴露广告翻车：为什么大品牌这么喜欢自毁长城？",
        )

        org = _make_organizer()
        # reorder_state_manager
        org.reorder_state_manager.detect_interrupted_task = AsyncMock(return_value=None)
        org.reorder_state_manager.create_new_state = AsyncMock()
        org.reorder_state_manager.is_bucket_completed = AsyncMock(return_value=False)
        org.reorder_state_manager.init_bucket = AsyncMock()
        org.reorder_state_manager.mark_bucket_completed = AsyncMock()
        org.reorder_state_manager.state = ReorderState(session_id="regression_test")

        # cleanup_temp_folders
        org.crawler.get_created_folders = AsyncMock(return_value=[])

        # merge_and_resort → (videos, need_reorder=False) 走 add-only
        org.merge_and_resort = AsyncMock(
            return_value=([watchlater_video], False)
        )

        # 目标桶为空
        org.crawler.get_folder_video_ids = AsyncMock(return_value=[])

        # add 成功
        org.crawler.add_resources = AsyncMock(return_value=True)

        # delete_watchlater 成功
        org.crawler.delete_watchlater = AsyncMock(return_value=True)

        classified = {"tmp_社会观察": [watchlater_video]}
        folder_map = {"tmp_社会观察": 3987383195}

        await org.incremental_organize(classified, folder_map, reorder=False)

        # 断言：视频被 add 到目标桶
        org.crawler.add_resources.assert_awaited_once()
        add_call = org.crawler.add_resources.await_args
        self.assertEqual(add_call.args[0], 3987383195)  # 目标桶 id

        # 关键断言：视频被从稍后再看删除
        org.crawler.delete_watchlater.assert_awaited_once_with(bvid="BV1d17Y6aELn", avid=116810460500145)

    async def test_mixed_videos_only_watchlater_cleaned(self):
        """混合场景：稍后再看视频 + 普通收藏夹视频，只有稍后再看视频被清理"""
        watchlater_v = _make_watchlater_video(
            avid=100001, bvid="BV1WatchLater01", title="稍后再看视频"
        )
        folder_v = _make_folder_video(
            avid=200001, bvid="BV2Folder02", title="收藏夹视频",
            source_folder_id=4054866095,
        )

        org = _make_organizer()
        org.reorder_state_manager.detect_interrupted_task = AsyncMock(return_value=None)
        org.reorder_state_manager.create_new_state = AsyncMock()
        org.reorder_state_manager.is_bucket_completed = AsyncMock(return_value=False)
        org.reorder_state_manager.init_bucket = AsyncMock()
        org.reorder_state_manager.mark_bucket_completed = AsyncMock()
        org.reorder_state_manager.state = ReorderState(session_id="mixed_test")

        org.crawler.get_created_folders = AsyncMock(return_value=[])
        org.merge_and_resort = AsyncMock(
            return_value=([watchlater_v, folder_v], False)
        )
        org.crawler.get_folder_video_ids = AsyncMock(return_value=[])
        org.crawler.add_resources = AsyncMock(return_value=True)
        org.crawler.delete_watchlater = AsyncMock(return_value=True)

        classified = {"tmp_测试桶": [watchlater_v, folder_v]}
        folder_map = {"tmp_测试桶": 3987383195}

        await org.incremental_organize(classified, folder_map, reorder=False)

        # 两条视频都被 add
        self.assertEqual(org.crawler.add_resources.await_count, 2)
        # 只有稍后再看视频触发 delete_watchlater
        self.assertEqual(org.crawler.delete_watchlater.await_count, 1)
        org.crawler.delete_watchlater.assert_awaited_once_with(bvid="BV1WatchLater01", avid=100001)


if __name__ == "__main__":
    unittest.main(verbosity=2)
