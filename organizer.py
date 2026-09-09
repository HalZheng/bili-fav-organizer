"""B站收藏夹整理工具 - 整理操作模块

负责批量创建收藏夹、移动/添加视频、排序等操作。

排序感知移动原理:
  B站收藏夹默认按 mtime 倒序排列（最近加入的排最前）。
  要让视频按 Top→Main→Bottom 顺序显示，需要:
  1. 区块级: 先操作 Bottom，再 Main，最后 Top
  2. 区块内: 逐个移动，按显示顺序的逆序操作
     (最后移动的视频 mtime 最新，排在最前面)
  3. 每次移动间隔 ≥1s，确保 mtime 不同

操作模式:
  - add: 添加视频到目标收藏夹（源收藏夹不存在时使用）
  - move: 从源收藏夹移动到目标收藏夹
  - copy: 从源收藏夹复制到目标收藏夹
"""

import asyncio
import time
from pathlib import Path
from typing import Optional

from config import BiliConfig
from crawler import BiliCrawler
from models import BiliVideo, BiliFolder
from move_state import MoveStateManager, TERMINAL_STATUSES
from reorder_state import ReorderStateManager
from sorter import FolderSorter


class BiliOrganizer:
    """B站收藏夹整理器"""

    def __init__(self, config: BiliConfig):
        self.config = config
        self.crawler = BiliCrawler(config)
        self.sorter = FolderSorter(
            archive_months=config.ARCHIVE_MONTHS
        )
        # 容量检查缓存: {media_id: (count, timestamp)}
        self._capacity_cache: dict[int, tuple[int, float]] = {}
        # 已确认空的源收藏夹（跨分类共享），避免反复尝试 move
        self._dead_source_folders: dict[int, bool] = {}
        # add 操作 412 冷却：跳过 add 直到此时间戳
        self._add_skip_until: float = 0.0
        # 断点续移状态管理器
        self.state_manager = MoveStateManager(config)
        # 增量重排状态管理器
        self.reorder_state_manager = ReorderStateManager(config)

    async def close(self):
        await self.crawler.close()

    async def _cleanup_watchlater_if_needed(self, video: BiliVideo) -> bool:
        """如果视频来自稍后再看，从稍后再看列表删除

        B站「稍后再看」不是普通收藏夹，add_resources 只会把视频加入目标收藏夹，
        不会自动从稍后再看移除。需要单独调用 crawler.delete_watchlater 清理。

        稍后再看视频的标识：source_folder_id == 0 且 source_folder_title == "稍后再看"
        （见 crawler.get_watchlater 的实现）

        Args:
            video: 刚成功 add 到目标收藏夹的视频

        Returns:
            True 表示清理成功（或视频不来自稍后再看），False 表示清理失败
        """
        # 仅处理来自「稍后再看」的视频
        if video.source_folder_id != 0 or video.source_folder_title != "稍后再看":
            return True
        if not video.bvid and not video.id:
            print(f"  [WARN] 稍后再看视频缺少 bvid/avid，无法清理: {video.title}")
            return False
        try:
            # delete_watchlater 的老接口只认 aid，优先传 avid 避免额外查询
            success = await self.crawler.delete_watchlater(
                bvid=video.bvid, avid=video.id
            )
            if not success:
                print(
                    f"  [WARN] 从稍后再看删除失败（视频已加入目标收藏夹）: "
                    f"{video.bvid} | {video.title}"
                )
            return success
        except Exception as e:
            print(f"  [WARN] 从稍后再看删除异常: {video.bvid} | {e}")
            return False

    async def check_folder_capacity(self, media_id: int) -> int:
        """查询收藏夹当前视频数量（带缓存）

        Returns:
            当前视频数量，查询失败返回0
        """
        now = time.time()
        # 检查缓存
        if media_id in self._capacity_cache:
            count, ts = self._capacity_cache[media_id]
            if now - ts < self.config.CAPACITY_CACHE_TTL:
                return count
        # 缓存过期或不存在，查询API
        folders = await self.crawler.get_created_folders()
        for f in folders:
            # 更新所有文件夹的缓存
            self._capacity_cache[f.id] = (f.media_count, now)
        # 返回目标
        if media_id in self._capacity_cache:
            return self._capacity_cache[media_id][0]
        return 0

    async def resolve_target_folder(
        self,
        bucket_name: str,
        folder_map: dict[str, int],
        skip_capacity_check: bool = False,
    ) -> int:
        """解析目标收藏夹，容量达上限时自动路由至分卷桶

        Args:
            bucket_name: 桶名称 (如 tmp_编程开发)
            folder_map: {桶名称: media_id} 映射
            skip_capacity_check: 跳过容量检查（恢复模式时使用，避免不必要的 API 调用）

        Returns:
            目标收藏夹的 media_id
        """
        capacity_limit = self.config.FOLDER_CAPACITY_LIMIT

        # 先检查主桶
        if bucket_name not in folder_map:
            # 主桶不存在，需要创建
            media_id = await self.crawler.create_folder(
                bucket_name, intro=f"自动分类: {bucket_name}"
            )
            if media_id:
                folder_map[bucket_name] = media_id
                print(f"  ✓ 创建收藏夹: {bucket_name} (id={media_id})")
            else:
                print(f"  ✗ 创建失败: {bucket_name}")
                return None
            return media_id

        media_id = folder_map[bucket_name]

        # 恢复模式跳过容量检查，直接使用已知的目标收藏夹
        if skip_capacity_check:
            return media_id

        current_count = await self.check_folder_capacity(media_id)

        if current_count < capacity_limit:
            return media_id

        # 容量已达上限，查找或创建分卷桶
        volume = 2
        while True:
            volume_name = f"{bucket_name}_{volume}"
            if volume_name in folder_map:
                vol_media_id = folder_map[volume_name]
                vol_count = await self.check_folder_capacity(vol_media_id)
                if vol_count < capacity_limit:
                    print(f"  [分卷] {bucket_name} 已满({current_count})，路由至 {volume_name}({vol_count})")
                    return vol_media_id
            else:
                # 创建分卷桶
                vol_media_id = await self.auto_create_volume_folder(
                    volume_name, bucket_name
                )
                if vol_media_id:
                    folder_map[volume_name] = vol_media_id
                    print(f"  [分卷] {bucket_name} 已满({current_count})，创建 {volume_name} (id={vol_media_id})")
                    return vol_media_id
                else:
                    print(f"  ✗ 分卷桶创建失败: {volume_name}")
                    return media_id
            volume += 1

    async def auto_create_volume_folder(
        self,
        volume_name: str,
        base_bucket_name: str,
    ) -> Optional[int]:
        """自动创建分卷桶

        Args:
            volume_name: 分卷桶名称 (如 tmp_编程开发_2)
            base_bucket_name: 基础桶名称 (如 tmp_编程开发)

        Returns:
            新创建的 media_id，失败返回 None
        """
        # B站收藏夹标题限制20字符
        title = volume_name
        if len(title) > 20:
            title = title[:20]

        media_id = await self.crawler.create_folder(
            title, intro=f"自动分类分卷: {base_bucket_name}"
        )
        if media_id:
            print(f"  ✓ 创建分卷收藏夹: {title} (id={media_id})")
        return media_id

    async def create_category_folders(
        self,
        categories: list[str],
        prefix: str = "",
    ) -> dict[str, int]:
        """为每个分类创建收藏夹（已有同名收藏夹则复用）

        返回: {分类名: media_id}
        """
        # 先获取已有的收藏夹列表
        existing_folders = await self.crawler.get_created_folders()
        existing_map = {f.title: f.id for f in existing_folders}
        print(f"  [复用] 已有收藏夹: {len(existing_map)} 个")

        folder_map = {}

        for category in categories:
            title = f"{prefix}{category}" if prefix else category
            # B站收藏夹标题限制20字符
            if len(title) > 20:
                title = title[:20]

            # 检查是否已有同名收藏夹
            if title in existing_map:
                folder_map[category] = existing_map[title]
                print(f"  ✓ 复用收藏夹: {title} (id={existing_map[title]})")
            else:
                media_id = await self.crawler.create_folder(title, intro=f"自动分类: {category}")
                if media_id:
                    folder_map[category] = media_id
                    print(f"  ✓ 创建收藏夹: {title} (id={media_id})")
                else:
                    print(f"  ✗ 创建失败: {title}")

                await asyncio.sleep(self.config.REQUEST_DELAY)

        return folder_map

    async def _organize_single_category(
        self,
        category: str,
        videos: list[BiliVideo],
        folder_map: dict[str, int],
        mode: str,
        dry_run: bool,
    ) -> dict:
        """整理单个分类的视频（排序感知，串行执行）

        Args:
            mode: "auto"(自动: 优先move，失败回退add) | "add" | "move" | "copy"

        Returns:
            该分类的整理计划条目
        """
        return await self._organize_single_category_inner(
            category, videos, folder_map, mode, dry_run
        )

    async def _check_source_has_video(
        self,
        source_folder_id: int,
        avid: int,
        cache: dict[int, set[int]],
    ) -> bool | None:
        """检查源收藏夹是否还有指定视频（带缓存，避免反复查询同一源收藏夹）

        Args:
            source_folder_id: 源收藏夹 media_id
            avid: 视频 avid
            cache: {source_folder_id: set(avid)} 缓存字典（方法内原地更新）

        Returns:
            True 表示源收藏夹中仍有该视频
            False 表示源收藏夹确认无此视频
            None 表示 API 调用失败，无法确定（不缓存，调用方应跳过验证）
        """
        if source_folder_id <= 0:
            return False
        # 已确认死亡的源收藏夹（move 时返回 11010），直接返回 None
        # 避免反复查询已删除的收藏夹浪费 API 调用
        if self._dead_source_folders.get(source_folder_id):
            return None
        if source_folder_id not in cache:
            try:
                ids = await self.crawler.get_folder_video_ids(
                    source_folder_id, use_cache=True
                )
                if ids is None:
                    # API 失败，不缓存，返回 None 供调用方区分
                    return None
                cache[source_folder_id] = {
                    item["id"] for item in ids if "id" in item
                }
            except Exception:
                return None
        return avid in cache.get(source_folder_id, set())

    async def _load_all_bucket_ids(self, folder_map: dict[str, int]) -> dict[int, int]:
        """一次性缓存 {avid: media_id} 全桶映射，用于校验视频是否已被移到其它桶。

        仅在确需判定「不在源也不在目标」时懒加载（最多触发一次网络批量查询），
        查询失败则返回空字典（调用方降级为原逻辑，不阻断流程）。
        """
        cache = getattr(self, "_all_bucket_ids", None)
        if cache is not None:
            return cache
        self._all_bucket_ids: dict[int, int] = {}
        media_ids = list({mid for mid in folder_map.values() if mid})
        if not media_ids:
            return self._all_bucket_ids
        try:
            results = await self.crawler.get_folder_video_ids_batch(
                media_ids, use_cache=False
            )
            for mid, items in results.items():
                if not items:
                    continue
                for it in items:
                    if "id" in it:
                        self._all_bucket_ids[int(it["id"])] = mid
        except Exception:
            self._all_bucket_ids = {}
        return self._all_bucket_ids

    async def _organize_single_category_inner(
        self,
        category: str,
        videos: list[BiliVideo],
        folder_map: dict[str, int],
        mode: str,
        dry_run: bool,
    ) -> dict:
        """整理单个分类的内部实现（含断点续移状态管理）

        Args:
            mode: "auto"(自动: 优先move，失败回退add) | "add"(添加) | "move"(移动) | "copy"(复制)

        断点续移改造点:
        - 恢复模式检测：若分类已完成则整体跳过
        - 目标侧验证：恢复时强制刷新目标已有 ID
        - 逐条视频状态检查：跳过终态/不可重试的 failed 视频
        - 源/目标双向验证：判断视频实际位置，避免重复操作
        - 操作后记录结果到状态文件（崩溃安全）
        - 区块/分类完成时更新状态
        """
        # SubTask 3.3: 恢复模式 - 检查分类是否已完成
        is_resuming = False
        if not dry_run:
            try:
                if await self.state_manager.is_category_completed(category):
                    print(f"  [{category}] ★ 已完成（恢复模式跳过）")
                    return {
                        "target_folder": folder_map.get(category),
                        "video_count": len(videos),
                        "blocks": {"top": 0, "main": 0, "bottom": 0},
                        "videos": [],
                        "skipped_completed": True,
                    }
            except Exception:
                pass

            # 检测是否为恢复模式（状态管理器中该分类已有非 pending 视频）
            try:
                if self.state_manager.state is not None:
                    cat_state = self.state_manager.state.categories.get(category)
                    if cat_state is not None:
                        for blk in cat_state.blocks.values():
                            if any(v_rec.status != "pending" for v_rec in blk.videos):
                                is_resuming = True
                                break
            except Exception:
                pass

        tar_media_id = await self.resolve_target_folder(
            category, folder_map, skip_capacity_check=is_resuming
        )
        if tar_media_id is None:
            return None

        valid_videos = [v for v in videos if v.is_valid]

        # 按区块分组：Top / Main / Bottom
        blocks = {"top": [], "main": [], "bottom": []}
        for v in valid_videos:
            block = getattr(v, 'block_type', 'main') or 'main'
            blocks[block].append(v)

        cat_plan = {
            "target_folder": tar_media_id,
            "video_count": len(valid_videos),
            "blocks": {
                "top": len(blocks["top"]),
                "main": len(blocks["main"]),
                "bottom": len(blocks["bottom"]),
            },
            "videos": [
                {
                    "bvid": v.bvid,
                    "title": v.title,
                    "up": v.upper.name,
                    "block": getattr(v, 'block_type', 'main'),
                }
                for v in valid_videos
            ],
        }

        if not dry_run and valid_videos:

            # 关键：按 Bottom → Main → Top 倒序操作
            # B站收藏夹后加入的排前面，所以最后操作 Top 区块
            operation_order = [
                ("Bottom", blocks["bottom"]),
                ("Main", blocks["main"]),
                ("Top", blocks["top"]),
            ]

            # 预获取目标收藏夹已有视频 ID，跳过已存在的（避免重复操作浪费 API 调用）
            # SubTask 3.4: 恢复时强制刷新（use_cache=False），新任务用缓存
            existing_ids: set[int] = set()
            try:
                if is_resuming and self.config.RESUME_VERIFY_TARGET:
                    existing = await self.crawler.get_folder_video_ids(
                        tar_media_id, use_cache=False
                    )
                else:
                    existing = await self.crawler.get_folder_video_ids(tar_media_id)
                if existing is None:
                    print(f"  [{category}] 获取目标收藏夹视频列表失败，将不跳过已存在视频")
                else:
                    existing_ids = {item["id"] for item in existing if "id" in item}
                    if existing_ids:
                        print(f"  [{category}] 目标收藏夹已有 {len(existing_ids)} 条视频，将跳过")
            except Exception as e:
                print(f"  [{category}] 获取目标收藏夹视频列表失败: {e}，继续操作")

            # SubTask 3.8: 源侧验证缓存（避免对同一源收藏夹反复查询）
            source_ids_cache: dict[int, set[int]] = {}

            total_to_move = len(valid_videos)
            total_moved = 0
            move_ok = 0
            add_ok = 0
            skipped = 0
            failed = 0
            t_start = time.time()
            category_had_add_skip = False  # 标记是否有视频因 add 412 冷却被跳过

            for block_name, block_videos in operation_order:
                if not block_videos:
                    continue

                block_name_lower = block_name.lower()  # 状态管理器使用小写
                block_total = len(block_videos)
                block_done = 0
                print(f"  [{category}] 操作 {block_name} 区块 ({block_total} 条)...")

                # 逐个操作，按显示顺序的逆序操作
                # 排序结果 [v1, v2, v3] 中 v1 应排最前
                # 逆序操作 [v3, v2, v1]，v1 最后操作 → mtime 最新 → 排最前
                for idx, v in enumerate(reversed(block_videos)):
                    # SubTask 3.5: 查询视频在状态管理器中的状态（恢复模式）
                    video_status = None
                    if is_resuming:
                        try:
                            video_status = await self.state_manager.get_video_status(
                                category, block_name_lower, v.id
                            )
                        except Exception:
                            pass

                    # SubTask 3.5: 终态视频跳过（moved/added/copied/skipped/failed_permanent）
                    if video_status in TERMINAL_STATUSES:
                        skipped += 1
                        block_done += 1
                        total_moved += 1
                        continue

                    # SubTask 3.5: failed 状态且不允许重试 → 跳过
                    if video_status == "failed" and not self.config.RESUME_RETRY_FAILED:
                        skipped += 1
                        block_done += 1
                        total_moved += 1
                        continue

                    # SubTask 3.5: 目标侧验证 - 视频已在目标收藏夹中
                    if v.id in existing_ids:
                        if (mode in ("auto", "move")
                                and self.config.RESUME_VERIFY_SOURCE
                                and v.source_folder_id):
                            # 查询源收藏夹是否还有此视频
                            source_has = await self._check_source_has_video(
                                v.source_folder_id, v.id, source_ids_cache
                            )
                            try:
                                if source_has is None:
                                    # API 失败，无法验证源侧。视频已在目标，
                                    # 安全起见标记 skipped（不重复操作）
                                    await self.state_manager.mark_video_status(
                                        category, block_name_lower, v.id,
                                        "skipped", "skip", "源侧API失败，目标已有，跳过"
                                    )
                                elif not source_has:
                                    # 源无此视频 → 上次已移动
                                    await self.state_manager.mark_video_status(
                                        category, block_name_lower, v.id,
                                        "moved", "move", ""
                                    )
                                else:
                                    # 源有此视频 → 目标已有，跳过
                                    await self.state_manager.mark_video_status(
                                        category, block_name_lower, v.id,
                                        "skipped", "skip", ""
                                    )
                            except Exception:
                                pass
                        else:
                            try:
                                await self.state_manager.mark_video_status(
                                    category, block_name_lower, v.id,
                                    "skipped", "skip", ""
                                )
                            except Exception:
                                pass
                        skipped += 1
                        block_done += 1
                        total_moved += 1
                        continue

                    # SubTask 3.5: 源侧验证（仅 move 模式 + RESUME_VERIFY_SOURCE）
                    # 视频不在目标收藏夹，但需要 move，先检查源收藏夹是否还有此视频
                    if (mode in ("auto", "move")
                            and self.config.RESUME_VERIFY_SOURCE
                            and v.source_folder_id):
                        source_has = await self._check_source_has_video(
                            v.source_folder_id, v.id, source_ids_cache
                        )
                        if source_has is None:
                            # API 失败，无法验证源侧。
                            # 不标记 failed_permanent，直接尝试 move/add 操作
                            # （auto 模式下 move 失败会自动回退 add）
                            print(
                                f"  [{category}] ⚠ 源收藏夹 {v.source_folder_id} "
                                f"API 查询失败，跳过验证直接尝试操作: {v.title} ({v.bvid})"
                            )
                        elif not source_has:
                            # 源确认无此视频，重新检查目标（existing_ids 可能过期）
                            try:
                                fresh_existing = await self.crawler.get_folder_video_ids(
                                    tar_media_id, use_cache=False
                                )
                                if fresh_existing is None:
                                    fresh_ids = existing_ids
                                else:
                                    fresh_ids = {
                                        item["id"] for item in fresh_existing if "id" in item
                                    }
                            except Exception:
                                fresh_ids = existing_ids
                            if v.id in fresh_ids:
                                # 目标有此视频 → 已被处理
                                try:
                                    await self.state_manager.mark_video_status(
                                        category, block_name_lower, v.id,
                                        "moved", "move", ""
                                    )
                                except Exception:
                                    pass
                                skipped += 1
                                block_done += 1
                                total_moved += 1
                                continue
                            else:
                                # 源和目标都确认无此视频 → 但可能是更早的整理轮次已将其移到了其它桶
                                located_mid = None
                                try:
                                    bucket_ids = await self._load_all_bucket_ids(folder_map)
                                    located_mid = bucket_ids.get(v.id)
                                except Exception:
                                    located_mid = None
                                if located_mid is not None and located_mid != tar_media_id:
                                    # 视频在其它桶中（旧运行已移动），非丢失，仅是桶位与当前计划不符
                                    try:
                                        await self.state_manager.mark_video_status(
                                            category, block_name_lower, v.id,
                                            "skipped", "skip",
                                            f"视频已在其它收藏夹(media_id={located_mid})，非丢失"
                                        )
                                    except Exception:
                                        pass
                                    skipped += 1
                                    block_done += 1
                                    total_moved += 1
                                    print(
                                        f"  [{category}] ⚠ 视频已在其它桶(media_id={located_mid})，"
                                        f"跳过永久失败标记: avid={v.id} | {v.title} ({v.bvid})"
                                    )
                                    continue
                                # 真正不在任何桶 → 永久失败
                                try:
                                    await self.state_manager.mark_video_status(
                                        category, block_name_lower, v.id,
                                        "failed_permanent", "move",
                                        "视频不在源收藏夹且不在目标收藏夹"
                                    )
                                except Exception:
                                    pass
                                failed += 1
                                print(
                                    f"  [{category}] ✗ 视频不在源收藏夹且不在目标收藏夹: "
                                    f"avid={v.id} | {v.title} ({v.bvid})"
                                )
                                continue
                        # source_has is True → 源有此视频，继续执行 move/add

                    # 执行操作（原有逻辑保留）
                    resources = [f"{v.id}:{v.type}"]
                    success = False
                    op_label = ""

                    if mode == "auto":
                        # 自动模式：每条视频独立尝试 move，失败则回退 add
                        # 已确认空的源收藏夹直接跳过 move，省掉一次 API 调用
                        if v.source_folder_id and not self._dead_source_folders.get(v.source_folder_id):
                            op_label = "move"
                            success = await self.crawler.move_resources(
                                v.source_folder_id, tar_media_id, resources
                            )
                            if success:
                                move_ok += 1
                            elif not self.crawler.last_post_412:
                                # move 失败且非 412（如 11010 源不存在），标记源收藏夹为空
                                # 412 时不标记（源收藏夹可能正常，只是风控限流）
                                self._dead_source_folders[v.source_folder_id] = True
                        if not success:
                            # 检查 add 是否在 412 冷却期
                            if time.time() < self._add_skip_until:
                                # add 在冷却期，跳过此视频（保持 pending 状态，不记录 failed）
                                skipped += 1
                                total_moved += 1
                                category_had_add_skip = True
                                continue
                            op_label = "add"
                            success = await self.crawler.add_resources(
                                tar_media_id, resources
                            )
                            if success:
                                add_ok += 1
                            elif self.crawler.last_post_412:
                                # add 触发 412，设置冷却期，跳过后续 add
                                self._add_skip_until = time.time() + 120
                                print(f"  [{category}] ⚠ add 触发 412 风控，跳过后续 add 操作 120s")
                    elif mode == "add":
                        # 检查 add 是否在 412 冷却期
                        if time.time() < self._add_skip_until:
                            skipped += 1
                            total_moved += 1
                            category_had_add_skip = True
                            continue
                        op_label = "add"
                        success = await self.crawler.add_resources(
                            tar_media_id, resources
                        )
                        if success:
                            add_ok += 1
                        elif self.crawler.last_post_412:
                            self._add_skip_until = time.time() + 120
                            print(f"  [{category}] ⚠ add 触发 412 风控，跳过后续 add 操作 120s")
                    elif mode == "move":
                        op_label = "move"
                        success = await self.crawler.move_resources(
                            v.source_folder_id, tar_media_id, resources
                        )
                        if success:
                            move_ok += 1
                    else:  # copy
                        op_label = "copy"
                        success = await self.crawler.copy_resources(
                            v.source_folder_id, tar_media_id, resources
                        )
                        if success:
                            move_ok += 1

                    # 稍后再看视频成功加入目标收藏夹后，需从稍后再看列表删除
                    # B站「稍后再看」是独立列表，add/move 不会自动从稍后再看移除
                    if success and not dry_run:
                        await self._cleanup_watchlater_if_needed(v)

                    # SubTask 3.6: 记录操作结果到状态文件
                    if not dry_run:
                        try:
                            if success:
                                if op_label == "move":
                                    await self.state_manager.record_video_result(
                                        category, block_name_lower, v.id, "moved", "move"
                                    )
                                elif op_label == "add":
                                    await self.state_manager.record_video_result(
                                        category, block_name_lower, v.id, "added", "add"
                                    )
                                elif op_label == "copy":
                                    await self.state_manager.record_video_result(
                                        category, block_name_lower, v.id, "copied", "copy"
                                    )
                            else:
                                # 操作失败（可重试），记录 failed 状态
                                await self.state_manager.record_video_result(
                                    category, block_name_lower, v.id, "failed", op_label,
                                    "操作失败"
                                )
                        except Exception:
                            pass

                    if not success:
                        failed += 1
                        print(f"  [{category}] ✗ {op_label}失败: avid={v.id} | {v.title} ({v.bvid})")
                    else:
                        block_done += 1
                        total_moved += 1

                    # POST 间隔已由 crawler._wait_post_interval 控制（≥1s），
                    # 同时满足 mtime 排序需求，此处无需额外 sleep

                    # 每 20 条打印进度
                    if total_moved > 0 and total_moved % 20 == 0:
                        elapsed = time.time() - t_start
                        rate = total_moved / elapsed if elapsed > 0 else 0
                        eta = (total_to_move - total_moved - failed) / rate if rate > 0 else 0
                        print(
                            f"  [{category}] 进度: {total_moved + failed}/{total_to_move} "
                            f"(move={move_ok} add={add_ok} skip={skipped} fail={failed}) "
                            f"速率={rate:.2f}条/s, 预计剩余 {eta/60:.1f} 分钟"
                        )

                print(f"  [{category}] {block_name} 区块完成: {block_done}/{block_total}")

                # SubTask 3.7: 更新区块状态为 completed
                # 如果有视频因 add 412 冷却被跳过，不标记区块完成（保持 pending 供下次恢复）
                if not dry_run and not category_had_add_skip:
                    try:
                        await self.state_manager.update_block_status(
                            category, block_name_lower, "completed"
                        )
                    except Exception:
                        pass

            elapsed = time.time() - t_start
            print(
                f"  [{category}] ★ 全部完成: {total_moved}/{total_to_move} "
                f"(move={move_ok} add={add_ok} skip={skipped} fail={failed}, 耗时 {elapsed/60:.1f} 分钟)"
            )

            # SubTask 3.8: 更新分类状态
            # 如果有视频因 add 412 冷却被跳过，不强制标记分类完成
            if not dry_run:
                try:
                    await self.state_manager.update_category_status(category)
                    if not await self.state_manager.is_category_completed(category):
                        if category_had_add_skip:
                            print(f"  [{category}] ⚠ 有视频因 add 412 冷却被跳过，分类不标记完成，下次恢复可继续")
                        else:
                            # 所有区块已处理但分类未自动标记完成（可能仍有 failed 视频），
                            # 强制标记完成避免反复恢复
                            await self.state_manager.mark_category_completed(category)
                except Exception:
                    pass

        return cat_plan

    async def organize_by_category(
        self,
        classified_videos: dict[str, list[BiliVideo]],
        folder_map: dict[str, int],
        mode: str = "auto",
        dry_run: bool = True,
    ) -> dict:
        """按分类整理视频到对应收藏夹（排序感知，分类间串行）

        B站收藏夹默认按收藏时间倒序排列（后加入的排前面），
        因此按 Bottom → Main → Top 倒序操作，确保最终收藏夹内
        Top 区块排在最前面，Bottom 排在最后面。

        不同分类之间串行执行，安全稳定，避免并发导致的各种问题。

        Args:
            classified_videos: {分类名: [视频列表]}（需已排序：Top→Main→Bottom）
            folder_map: {分类名: media_id}
            mode: "auto"(自动: 优先move，失败回退add) | "add" | "move" | "copy"
            dry_run: 只预览不执行
        """
        mode_label = {"auto": "自动(move→add)", "add": "添加", "move": "移动", "copy": "复制"}.get(mode, mode)
        plan = {
            "mode": mode_label,
            "dry_run": dry_run,
            "categories": {},
        }

        # 串行执行不同分类的移动操作
        for category, videos in classified_videos.items():
            if category not in folder_map:
                print(f"  [SKIP] 分类 '{category}' 没有对应收藏夹")
                continue
            cat_plan = await self._organize_single_category(
                category, videos, folder_map, mode, dry_run
            )
            if cat_plan is not None:
                plan["categories"][category] = cat_plan

        return plan

    async def sort_folder_by_up_and_time(
        self,
        videos: list[BiliVideo],
    ) -> list[BiliVideo]:
        """按三区块分箱排序算法排序

        Top: 时效性内容 (收藏时间倒序)
        Main: UP主聚合+系列识别+发布时间升序
        Bottom: 归档内容 (收藏时间倒序)
        """
        sorted_videos = self.sorter.sort_folder(videos)
        return sorted_videos

    async def full_organize(
        self,
        classified_videos: dict[str, list[BiliVideo]],
        folder_prefix: str = "",
        mode: str = "auto",
        dry_run: bool = True,
    ) -> dict:
        """完整的整理流程

        1. 创建分类收藏夹（13个固定桶，已有则复用）
        2. 对每个桶内视频执行三区块排序
        3. 添加/移动/复制视频（含容量检查与动态分卷）
        4. 返回整理报告

        Args:
            mode: "auto"(自动: 优先move，失败回退add) | "add" | "move" | "copy"

        Returns:
            dry_run=False 时若检测到未完成任务，返回:
            {"interrupted": True, "resume_summary": {...}, "categories": {}}
            否则返回正常整理报告。
        """
        categories = list(classified_videos.keys())

        if dry_run:
            print("\n[预览模式] 以下操作不会实际执行\n")
            folder_map = {cat: f"preview_{i}" for i, cat in enumerate(categories)}
        else:
            # SubTask 3.2: 检测未完成的移动任务（断点续移）
            try:
                interrupted = await self.state_manager.detect_interrupted_task()
            except Exception:
                interrupted = None
            if interrupted:
                print(f"\n[中断检测] 发现未完成的移动任务 (session={interrupted.get('session_id', 'unknown')})")
                print(f"  总分类: {interrupted.get('total_categories', 0)}, "
                      f"已完成: {interrupted.get('completed_categories', 0)}, "
                      f"未完成: {interrupted.get('incomplete_categories', 0)}")
                print(f"  总视频: {interrupted.get('total_videos', 0)}, "
                      f"已完成: {interrupted.get('completed_videos', 0)}, "
                      f"待处理: {interrupted.get('pending_videos', 0)}, "
                      f"失败: {interrupted.get('failed_videos', 0)}")
                if interrupted.get("source_csv_changed"):
                    print("  ⚠ 源 CSV 文件已变化，恢复可能不一致")
                return {
                    "interrupted": True,
                    "resume_summary": interrupted,
                    "categories": {},
                }

            # SubTask 3.2: 创建新状态（自动归档旧状态）
            try:
                csv_path = Path(self.config.OUTPUT_DIR) / "videos.csv"
                csv_mtime = csv_path.stat().st_mtime if csv_path.exists() else 0.0
                await self.state_manager.create_new_state(
                    mode=mode, dry_run=dry_run,
                    source_csv="videos.csv", source_csv_mtime=csv_mtime,
                )
            except Exception as e:
                print(f"  [WARN] 状态管理初始化失败: {e}，继续执行但不支持断点续移")

            mode_label = {"auto": "自动(move→add)", "add": "添加", "move": "移动", "copy": "复制"}.get(mode, mode)
            print(f"\n[执行模式] 正在创建分类收藏夹... (操作模式: {mode_label})\n")
            folder_map = await self.create_category_folders(categories, folder_prefix)

        # 对每个分类内的视频执行三区块排序（并发）
        from concurrent.futures import ThreadPoolExecutor

        sort_tasks = []
        categories = list(classified_videos.keys())

        def sort_bucket(bucket_name, bucket_videos):
            """在线程中排序单个桶"""
            sorted_videos = self.sorter.sort_folder(bucket_videos)
            return bucket_name, sorted_videos

        loop = asyncio.get_event_loop()
        with ThreadPoolExecutor(max_workers=self.config.CONCURRENT_SORT_WORKERS) as executor:
            futures = [
                loop.run_in_executor(executor, sort_bucket, cat, classified_videos[cat])
                for cat in categories
            ]
            results = await asyncio.gather(*futures)

        for bucket_name, sorted_videos in results:
            classified_videos[bucket_name] = sorted_videos

        # SubTask 3.2: 为每个分类初始化状态（非 dry_run 时）
        if not dry_run:
            for cat, vids in classified_videos.items():
                if cat in folder_map:
                    blocks = {"top": [], "main": [], "bottom": []}
                    for v in vids:
                        block = getattr(v, 'block_type', 'main') or 'main'
                        if block in blocks:
                            blocks[block].append(v)
                    try:
                        await self.state_manager.init_category(
                            cat, folder_map[cat], cat, blocks
                        )
                    except Exception as e:
                        print(f"  [WARN] 初始化分类 {cat} 状态失败: {e}")

        # 执行整理（含容量检查与动态分卷）
        plan = await self.organize_by_category(
            classified_videos, folder_map, mode=mode, dry_run=dry_run
        )

        return plan

    # ===== 断点续移相关方法 =====

    async def resume_organize(
        self,
        classified_videos: dict[str, list[BiliVideo]],
        folder_map: dict[str, int],
    ) -> dict:
        """基于状态文件恢复执行未完成的移动任务

        Args:
            classified_videos: 原始分类结果（用于获取视频详情和排序）
            folder_map: {分类名: media_id}

        Returns:
            恢复结果报告:
            {
                "resumed_categories": [分类名...],
                "total_processed": int,
                "reorder_candidates": [分类名...],
            }
        """
        # 加载状态文件
        state = await self.state_manager.load_state()
        if state is None:
            print("[恢复] 未找到状态文件，无需恢复")
            return {
                "resumed_categories": [],
                "total_processed": 0,
                "reorder_candidates": [],
            }

        print(f"[恢复] 加载状态文件: session={state.session_id}, mode={state.mode}")
        print(f"  创建时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(state.created_at))}")

        # 检测源 CSV 是否变化
        try:
            if await self.state_manager.is_source_csv_changed():
                print("  ⚠ 源 CSV 文件已变化，恢复结果可能不一致")
        except Exception:
            pass

        resumed_categories = []
        total_processed = 0
        reorder_candidates = []
        resume_start_ts = time.time()

        # 记录恢复前各分类的已完成视频数（用于后续计算处理量和检测顺序破坏）
        progress_before: dict[str, dict] = {}
        for cat in classified_videos:
            try:
                progress_before[cat] = await self.state_manager.get_category_progress(cat)
            except Exception:
                progress_before[cat] = {"total": 0, "completed": 0, "pending": 0, "failed": 0, "status": "pending"}

        # 串行恢复每个未完成分类
        for category, videos in classified_videos.items():
            if category not in folder_map:
                print(f"  [SKIP] 分类 '{category}' 没有对应收藏夹")
                continue

            try:
                if await self.state_manager.is_category_completed(category):
                    print(f"  [{category}] 已完成，跳过")
                    continue
            except Exception:
                pass

            print(f"\n[恢复] 处理分类: {category}")
            await self._organize_single_category_inner(
                category, videos, folder_map, mode=state.mode, dry_run=False
            )
            resumed_categories.append(category)

            # 计算本次恢复处理的视频数
            try:
                progress_after = await self.state_manager.get_category_progress(category)
                processed = max(
                    0,
                    progress_after.get("completed", 0) - progress_before[category].get("completed", 0)
                )
                total_processed += processed
            except Exception:
                pass

            # 检测恢复期间是否有新操作导致排序破坏
            try:
                # 从状态文件获取目标收藏夹 ID
                cat_state = self.state_manager.state.categories.get(category)
                tar_id = cat_state.target_folder_id if cat_state else folder_map[category]
                if await self._detect_order_disruption(category, tar_id, since_ts=resume_start_ts):
                    reorder_candidates.append(category)
            except Exception:
                pass

        print(f"\n[恢复] 完成: 恢复了 {len(resumed_categories)} 个分类，处理 {total_processed} 条视频")
        if reorder_candidates:
            print(f"[恢复] 以下分类的收藏夹顺序可能被破坏，建议检查: {reorder_candidates}")

        return {
            "resumed_categories": resumed_categories,
            "total_processed": total_processed,
            "reorder_candidates": reorder_candidates,
        }

    async def _detect_order_disruption(
        self,
        category: str,
        target_folder_id: int,
        since_ts: float = 0.0,
    ) -> bool:
        """检测恢复期间目标收藏夹是否有新移动操作导致 mtime 排序破坏

        通过检查状态文件中该分类是否有 ts > since_ts 的 moved/added 记录判断。
        如果恢复期间有新操作（status 从 pending 变为 moved/added），返回 True。

        Args:
            category: 分类名
            target_folder_id: 目标收藏夹 ID（保留参数，当前实现未使用）
            since_ts: 起始时间戳，仅检查此时间之后的操作

        Returns:
            True 表示恢复期间有新操作，可能破坏排序
        """
        if self.state_manager.state is None:
            return False
        cat = self.state_manager.state.categories.get(category)
        if cat is None:
            return False
        for blk in cat.blocks.values():
            for v_rec in blk.videos:
                if v_rec.status in ("moved", "added") and v_rec.ts > since_ts:
                    return True
        return False

    async def reorder_folder(
        self,
        category: str,
        videos: list[BiliVideo],
        target_folder_id: int,
    ) -> int:
        """对排序破坏的收藏夹执行重排检测（保守版本，不实际执行重排）

        获取目标收藏夹视频列表（按 mtime 倒序，即当前显示顺序），
        与计划顺序（videos 列表，Top→Main→Bottom）比较，
        如果不一致，打印警告并返回需要重排的视频数。
        不实际执行重排（避免数据丢失风险）。

        Args:
            category: 分类名
            videos: 计划顺序的视频列表（已排序：Top→Main→Bottom）
            target_folder_id: 目标收藏夹 media_id

        Returns:
            顺序不一致的视频数（0 表示顺序一致）
        """
        # 获取目标收藏夹当前视频（按 mtime 倒序 = 显示顺序）
        try:
            target_videos = await self.crawler.get_folder_videos(
                target_folder_id, order="mtime"
            )
        except Exception as e:
            print(f"  [{category}] 获取目标收藏夹视频列表失败: {e}")
            return 0

        # 构建计划显示顺序：Top → Main → Bottom，区块内保持原序
        # （排序结果已是 Top→Main→Bottom，操作时倒序执行使 Top 的 mtime 最新）
        planned_avids: list[int] = []
        blocks = {"top": [], "main": [], "bottom": []}
        for v in videos:
            if not v.is_valid:
                continue
            block = getattr(v, 'block_type', 'main') or 'main'
            if block in blocks:
                blocks[block].append(v)
        for block_name in ("top", "main", "bottom"):
            planned_avids.extend(v.id for v in blocks[block_name])

        # 实际显示顺序（mtime 倒序，最新的在最前）
        actual_avids = [v.id for v in target_videos if v.is_valid]

        # 仅比较两边都有的视频
        planned_set = set(planned_avids)
        actual_set = set(actual_avids)
        common_planned = [a for a in planned_avids if a in actual_set]
        common_actual = [a for a in actual_avids if a in planned_set]

        # 逐位比较，统计不一致数
        mismatch_count = 0
        min_len = min(len(common_planned), len(common_actual))
        for i in range(min_len):
            if common_planned[i] != common_actual[i]:
                mismatch_count += 1
        # 长度差异也算不一致
        mismatch_count += abs(len(common_planned) - len(common_actual))

        if mismatch_count > 0:
            print(
                f"  [{category}] ⚠ 检测到 {mismatch_count} 条视频顺序不一致，"
                f"建议手动检查收藏夹顺序 (target_folder_id={target_folder_id})"
            )
            print(f"  [{category}] 如需自动重排，请确认后手动执行 remove+add 操作（有数据丢失风险）")
        else:
            print(f"  [{category}] ✓ 顺序一致，无需重排")

        return mismatch_count

    # ===== 增量整理与全桶重排 =====

    async def merge_and_resort(
        self, category: str, new_videos: list[BiliVideo], target_folder_id: int
    ) -> tuple[list[BiliVideo], bool]:
        """合并目标桶现有视频与新视频，去重后重新三区块排序

        Args:
            category: 分类名（用于日志）
            new_videos: 本次新增的视频列表
            target_folder_id: 目标桶 media_id

        Returns:
            (sorted_videos, need_reorder):
            - sorted_videos: 合并去重并排序后的完整视频列表
            - need_reorder: 是否需要重排（期望顺序与实际顺序不一致）
        """
        # 1. 获取目标桶现有视频（按 mtime 倒序 = 实际显示顺序）
        try:
            existing_videos = await self.crawler.get_folder_videos(
                target_folder_id, order="mtime"
            )
        except Exception as e:
            print(f"  [{category}] 获取目标桶现有视频失败: {e}")
            existing_videos = []

        # 2. 合并：现有视频 + 新视频，按 avid 去重，新视频信息优先
        # 先放现有再放新视频，新视频覆盖现有视频信息（新视频有最新 fav_time/pubtime，排序更准）
        merged_dict: dict[int, BiliVideo] = {}
        for v in existing_videos:
            merged_dict[v.id] = v
        for v in new_videos:
            merged_dict[v.id] = v
        merged_videos = list(merged_dict.values())

        # 3. 对合并后的列表重新三区块排序
        sorted_videos = self.sorter.sort_folder(merged_videos)

        # 4. 构建期望顺序 avid 列表（Top→Main→Bottom，区块内保持排序结果顺序）
        planned_avids: list[int] = []
        blocks = {"top": [], "main": [], "bottom": []}
        for v in sorted_videos:
            if not v.is_valid:
                continue
            block = getattr(v, 'block_type', 'main') or 'main'
            if block in blocks:
                blocks[block].append(v)
        for block_name in ("top", "main", "bottom"):
            planned_avids.extend(v.id for v in blocks[block_name])

        # 5. 构建实际顺序 avid 列表（目标桶现有视频的 mtime 倒序）
        actual_avids = [v.id for v in existing_videos if v.is_valid]

        # 6. 比较顺序
        mismatch_count = self._compare_order(planned_avids, actual_avids)

        # 7. 返回
        need_reorder = mismatch_count > 0
        if need_reorder:
            print(f"  [{category}] 检测到 {mismatch_count} 条视频顺序不一致，需要重排")
        else:
            print(f"  [{category}] ✓ 顺序一致，无需重排")

        return sorted_videos, need_reorder

    def _compare_order(self, planned_avids: list[int], actual_avids: list[int]) -> int:
        """逐位对比期望与实际顺序，返回不一致数

        仅比较两边都有的视频（交集），长度差异也算不一致。
        """
        planned_set = set(planned_avids)
        actual_set = set(actual_avids)
        common_planned = [a for a in planned_avids if a in actual_set]
        common_actual = [a for a in actual_avids if a in planned_set]

        mismatch_count = 0
        min_len = min(len(common_planned), len(common_actual))
        for i in range(min_len):
            if common_planned[i] != common_actual[i]:
                mismatch_count += 1
        # 长度差异也算不一致
        mismatch_count += abs(len(common_planned) - len(common_actual))

        return mismatch_count

    async def reorder_folder_full(
        self, category: str, sorted_videos: list[BiliVideo],
        target_folder_id: int, dry_run: bool = False,
        new_videos: list[BiliVideo] | None = None,
    ) -> bool:
        """全桶重排：临时桶中转，保证安全

        流程：
        1. 创建临时桶 tmp_reorder_{category}_{session_id}（标题超20字符截断）
        2. move_to_temp: 目标桶所有视频 move 到临时桶（批量50条，沿用 move_resources）
        2.5. add_new_to_temp: 将新视频（不在目标桶中的）ADD 到临时桶
        3. move_back: 按期望顺序逆序（Bottom→Main→Top，区块内逆序）逐条从临时桶 move 回目标桶
           每条间隔 self.config.REORDER_MTIME_INTERVAL 秒
        4. cleanup: 删除临时桶

        每个阶段更新 reorder_state。触发 412 时保持 in_progress 退出。
        支持断点续移：根据 reorder_state 的 phase 和 temp_folder_id 决定从哪个阶段恢复。

        Returns:
            True 表示重排成功，False 表示失败（412或其他错误）
        """
        if dry_run:
            print(f"  [{category}] [预览模式] 跳过全桶重排")
            return True

        # 获取 session_id
        if self.reorder_state_manager.state is None:
            print(f"  [{category}] 重排状态未初始化，无法执行全桶重排")
            return False
        session_id = self.reorder_state_manager.state.session_id

        # 断点续移：检查现有 bucket_state 决定从哪个阶段恢复
        bucket_state = await self.reorder_state_manager.get_bucket_state(category)
        resume_phase = None
        temp_folder_id = 0
        if bucket_state is not None and bucket_state.temp_folder_id:
            if bucket_state.phase == "move_to_temp":
                resume_phase = "move_to_temp"
                temp_folder_id = bucket_state.temp_folder_id
                print(f"  [{category}] 断点续移: 从 move_to_temp 恢复 (temp_folder_id={temp_folder_id})")
            elif bucket_state.phase == "move_back":
                resume_phase = "move_back"
                temp_folder_id = bucket_state.temp_folder_id
                print(f"  [{category}] 断点续移: 从 move_back 恢复 (temp_folder_id={temp_folder_id}, moved_back_count={bucket_state.moved_back_count})")
            elif bucket_state.phase == "cleanup":
                resume_phase = "cleanup"
                temp_folder_id = bucket_state.temp_folder_id
                print(f"  [{category}] 断点续移: 从 cleanup 恢复 (temp_folder_id={temp_folder_id})")

        # 阶段1: 创建临时桶（仅在非恢复时）
        # 临时桶命名: tmp_reorder_<category>_<session_id>（标题超20字符截断）
        if not temp_folder_id:
            temp_title = f"{self.config.REORDER_TEMP_PREFIX}{category}_{session_id}"
            if len(temp_title) > 20:
                temp_title = temp_title[:20]
            print(f"  [{category}] 创建临时桶: {temp_title}")
            temp_folder_id = await self.crawler.create_folder(
                temp_title, intro=f"重排临时桶: {category}"
            )
            if not temp_folder_id:
                print(f"  [{category}] ✗ 创建临时桶失败")
                return False

            # 记录 temp_folder_id 到 reorder_state
            try:
                await self.reorder_state_manager.update_bucket_phase(
                    category, "move_to_temp", temp_folder_id=temp_folder_id
                )
            except Exception as e:
                print(f"  [{category}] 更新重排状态失败: {e}")

        # 阶段2: move_to_temp - 目标桶所有视频 move 到临时桶（批量50条）
        # 恢复时复用 temp_folder_id，获取目标桶剩余视频继续 move
        if resume_phase is None or resume_phase == "move_to_temp":
            print(f"  [{category}] move_to_temp: 将目标桶视频移动到临时桶")
            try:
                target_ids = await self.crawler.get_folder_video_ids(target_folder_id)
            except Exception as e:
                print(f"  [{category}] 获取目标桶视频ID失败: {e}")
                return False

            if target_ids is None:
                print(f"  [{category}] 获取目标桶视频ID失败")
                return False

            # 构建 resources 列表（格式: "avid:type"，type 通常是 2=视频）
            all_resources = [
                f"{item['id']}:{item.get('type', 2)}"
                for item in target_ids if 'id' in item
            ]

            # 批量 move，每批50条
            batch_size = 50
            # 恢复时从已有计数累加
            moved_count = bucket_state.moved_to_temp_count if resume_phase == "move_to_temp" else 0
            for i in range(0, len(all_resources), batch_size):
                batch = all_resources[i:i + batch_size]
                success = await self.crawler.move_resources(
                    target_folder_id, temp_folder_id, batch
                )
                if not success:
                    if self.crawler.last_post_412:
                        print(f"  [{category}] ⚠ move_to_temp 触发 412 风控，保持 in_progress 退出")
                        return False
                    print(f"  [{category}] ✗ move_to_temp 失败 (batch {i//batch_size + 1})")
                    return False
                moved_count += len(batch)
                # 更新状态
                try:
                    await self.reorder_state_manager.update_bucket_phase(
                        category, "move_to_temp",
                        moved_to_temp_count=moved_count
                    )
                except Exception:
                    pass

            print(f"  [{category}] move_to_temp 完成: {moved_count} 条")

            # 阶段2.5: add_new_to_temp - 将新视频 ADD 到临时桶
            # 新视频来自「稍后再看」，不在目标桶中，move_to_temp 不会带走它们
            # 必须先 ADD 到临时桶，move_back 才能正确 move 回目标桶
            if new_videos:
                existing_ids_set = {
                    item["id"] for item in target_ids if "id" in item
                }
                new_to_add = [
                    v for v in new_videos
                    if v.is_valid and v.id not in existing_ids_set
                ]
                # 视频已在目标桶但可能仍在稍后再看（上次 add 成功但清理失败），补清理
                stale_in_target = [
                    v for v in new_videos
                    if v.is_valid and v.id in existing_ids_set
                    and v.source_folder_id == 0 and v.source_folder_title == "稍后再看"
                ]
                stale_cleaned_here = 0
                for v in stale_in_target:
                    if await self._cleanup_watchlater_if_needed(v):
                        stale_cleaned_here += 1
                if stale_cleaned_here > 0:
                    print(f"  [{category}] 补清理已在目标桶的稍后再看视频 {stale_cleaned_here} 条")
                if new_to_add:
                    print(f"  [{category}] add_new_to_temp: 添加 {len(new_to_add)} 条新视频到临时桶")
                    for v in new_to_add:
                        success = await self.crawler.add_resources(
                            temp_folder_id, [f"{v.id}:{v.type}"]
                        )
                        if not success:
                            if self.crawler.last_post_412:
                                print(f"  [{category}] ⚠ add_new_to_temp 触发 412 风控，保持 in_progress 退出")
                                return False
                            print(f"  [{category}] ✗ add_new_to_temp 失败: avid={v.id} | {v.title} ({v.bvid})")
                            return False
                        # 稍后再看视频 add 到临时桶后，从稍后再看列表删除
                        await self._cleanup_watchlater_if_needed(v)
                    print(f"  [{category}] add_new_to_temp 完成: {len(new_to_add)} 条")

        # 阶段3: move_back - 按期望顺序逆序逐条 move 回目标桶
        # cleanup 恢复时跳过（假设 move_back 已完成）
        if resume_phase != "cleanup":
            print(f"  [{category}] move_back: 按期望顺序逆序移动回目标桶")

            # 构建期望顺序的逆序列表
            # sorted_videos 已是 Top→Main→Bottom，区块内已排序
            # 逆序操作：reversed(bottom) + reversed(main) + reversed(top)
            # 这样最后操作 top 的第一条，mtime 最新排最前
            blocks = {"top": [], "main": [], "bottom": []}
            for v in sorted_videos:
                if not v.is_valid:
                    continue
                block = getattr(v, 'block_type', 'main') or 'main'
                if block in blocks:
                    blocks[block].append(v)

            operation_order = (
                list(reversed(blocks["bottom"])) +
                list(reversed(blocks["main"])) +
                list(reversed(blocks["top"]))
            )

            # 断点续移：跳过已 move 回的前 moved_back_count 个
            skip_count = bucket_state.moved_back_count if resume_phase == "move_back" else 0
            if skip_count > 0:
                print(f"  [{category}] 跳过已 move 回的 {skip_count} 条，继续 move_back")

            moved_back_count = skip_count
            total_to_move = len(operation_order)
            for v in operation_order[skip_count:]:
                resources = [f"{v.id}:{v.type}"]
                success = await self.crawler.move_resources(
                    temp_folder_id, target_folder_id, resources
                )
                if not success:
                    if self.crawler.last_post_412:
                        print(f"  [{category}] ⚠ move_back 触发 412 风控，保持 in_progress 退出")
                        return False
                    print(f"  [{category}] ✗ move_back 失败: avid={v.id} | {v.title} ({v.bvid})")
                    return False
                moved_back_count += 1
                # 更新状态
                try:
                    await self.reorder_state_manager.update_bucket_phase(
                        category, "move_back",
                        moved_back_count=moved_back_count
                    )
                except Exception:
                    pass
                # 进度输出（每10条打印一次）
                if moved_back_count % 10 == 0 or moved_back_count == total_to_move:
                    print(f"  [{category}] move_back 进度: {moved_back_count}/{total_to_move}")
                # 每条间隔 REORDER_MTIME_INTERVAL 秒，保证 mtime 递增
                await asyncio.sleep(self.config.REORDER_MTIME_INTERVAL)

            print(f"  [{category}] move_back 完成: {moved_back_count} 条")

        # 阶段4: cleanup - 删除临时桶（先检查是否为空，避免数据丢失）
        print(f"  [{category}] cleanup: 删除临时桶")
        try:
            await self.reorder_state_manager.update_bucket_phase(category, "cleanup")
        except Exception:
            pass

        # 检查临时桶是否为空（move_back 可能因 412/API 失败留下残留视频）
        # 直接删除非空临时桶会导致残留视频从目标桶中"消失"（数据丢失）
        try:
            remaining_ids = await self.crawler.get_folder_video_ids(temp_folder_id)
        except Exception as e:
            print(f"  [{category}] ⚠ 获取临时桶视频列表失败: {e}，仍尝试删除")
            remaining_ids = None

        if remaining_ids:
            # 临时桶非空，尝试 move 残留视频回目标桶（保险措施）
            remaining_resources = [
                f"{item['id']}:{item.get('type', 2)}"
                for item in remaining_ids if "id" in item
            ]
            print(
                f"  [{category}] ⚠ 临时桶仍有 {len(remaining_resources)} 条视频，"
                f"尝试回迁到目标桶后删除"
            )
            batch_size = 50
            move_ok = True
            for i in range(0, len(remaining_resources), batch_size):
                batch = remaining_resources[i:i + batch_size]
                if not await self.crawler.move_resources(
                    temp_folder_id, target_folder_id, batch
                ):
                    move_ok = False
                    break
            if not move_ok:
                # 回迁失败，保留临时桶避免数据丢失，等待下次 cleanup_temp_folders 处理
                print(
                    f"  [{category}] ⚠ 临时桶回迁失败，保留临时桶 (id={temp_folder_id}) "
                    f"等待下次清理"
                )
                return True

        # 临时桶此时已为空（move_back 已全部移回目标桶），删除临时桶
        delete_ok = await self.crawler.delete_folder(temp_folder_id)
        if not delete_ok:
            print(f"  [{category}] ⚠ 删除临时桶失败 (id={temp_folder_id})，但重排已完成")
        else:
            print(f"  [{category}] ✓ 临时桶已删除")

        return True

    async def cleanup_temp_folders(self) -> None:
        """扫描并处理残留临时桶

        调用 get_created_folders 获取所有收藏夹，匹配 REORDER_TEMP_PREFIX 前缀。
        - 有对应 in_progress 状态记录的桶 → 提示将续移，保留
        - 孤儿桶（无状态记录）：
          - 空桶 → 直接删除
          - 有视频 → 先 move 回同名目标桶，再删除
        """
        print("\n[清理] 扫描残留临时桶...")
        try:
            all_folders = await self.crawler.get_created_folders()
        except Exception as e:
            print(f"  [清理] 获取收藏夹列表失败: {e}")
            return

        prefix = self.config.REORDER_TEMP_PREFIX
        temp_folders = [f for f in all_folders if f.title.startswith(prefix)]

        if not temp_folders:
            print("  [清理] 未发现残留临时桶")
            return

        # 构建目标桶名→id 映射（非临时桶），用于孤儿桶回迁
        target_folder_map: dict[str, int] = {
            f.title: f.id for f in all_folders if not f.title.startswith(prefix)
        }

        print(f"  [清理] 发现 {len(temp_folders)} 个临时桶:")

        # 加载重排状态
        try:
            await self.reorder_state_manager.load_state()
        except Exception:
            pass

        state = self.reorder_state_manager.state
        orphan_count = 0
        deleted_count = 0
        for f in temp_folders:
            title = f.title
            # 尝试匹配 in_progress 桶（通过 temp_folder_id）
            matched = False
            if state is not None:
                for name, bucket in state.categories.items():
                    if bucket.status == "in_progress" and bucket.temp_folder_id == f.id:
                        print(
                            f"    • {title} (id={f.id}, {f.media_count}条) "
                            f"→ 对应桶 '{name}' 将续移"
                        )
                        matched = True
                        break
            if matched:
                continue

            # 孤儿桶：尝试自动清理
            orphan_count += 1
            base_name = title[len(prefix):]  # 去掉 tmp_reorder_ 前缀

            # 是否应当删除临时桶：仅在空桶或回迁成功时为 True
            # 非空桶回迁失败/找不到目标桶时保留，避免数据丢失
            should_delete = True

            # 有视频时先 move 回目标桶
            if f.media_count > 0:
                # 精确匹配目标桶名
                target_id = target_folder_map.get(base_name)
                # 模糊匹配（处理桶名截断）
                if target_id is None:
                    for name, tid in target_folder_map.items():
                        if base_name.startswith(name) or name.startswith(base_name):
                            target_id = tid
                            break

                if target_id:
                    print(
                        f"    • {title} (id={f.id}, {f.media_count}条) "
                        f"→ 孤儿桶，回迁视频到 '{base_name}' 后删除"
                    )
                    try:
                        video_ids = await self.crawler.get_folder_video_ids(f.id)
                        if video_ids:
                            resources = [
                                f"{item['id']}:{item.get('type', 2)}"
                                for item in video_ids if "id" in item
                            ]
                            # 批量 move 回目标桶，检查每批返回值
                            batch_size = 50
                            for i in range(0, len(resources), batch_size):
                                batch = resources[i:i + batch_size]
                                if not await self.crawler.move_resources(
                                    f.id, target_id, batch
                                ):
                                    print(
                                        f"      [WARN] 回迁失败 (batch {i//batch_size + 1})，"
                                        f"保留临时桶等待下次清理"
                                    )
                                    should_delete = False
                                    break
                    except Exception as e:
                        print(f"      [WARN] 回迁异常: {e}，保留临时桶等待下次清理")
                        should_delete = False
                else:
                    print(
                        f"    • {title} (id={f.id}, {f.media_count}条) "
                        f"→ ⚠ 孤儿桶，未找到目标桶 '{base_name}'，保留临时桶等待手动处理"
                    )
                    should_delete = False
            else:
                print(
                    f"    • {title} (id={f.id}, 0条) "
                    f"→ 空孤儿桶，直接删除"
                )

            # 删除临时桶（仅在空桶或回迁成功时）
            if should_delete:
                ok = await self.crawler.delete_folder(f.id)
                if ok:
                    deleted_count += 1
                else:
                    print(f"      [WARN] 删除失败，可稍后手动删除")

        if orphan_count > 0:
            print(f"  [清理] 孤儿桶: {orphan_count} 个，已删除: {deleted_count} 个")

    async def incremental_organize(
        self, classified_new_videos: dict[str, list[BiliVideo]],
        folder_map: dict[str, int], reorder: bool = True
    ) -> dict:
        """增量整理入口：按桶串行处理

        对每个桶：
        1. 检查 reorder_state 是否已完成 → 跳过
        2. 调用 merge_and_resort 合并排序
        3. reorder=True 且 need_reorder=True 时调用 reorder_folder_full
        4. reorder=False 时仅 add 新视频到目标桶（跳过已存在的）

        Returns:
            整理报告 dict，含每桶状态
        """
        report: dict = {"categories": {}}

        # 启动时检测未完成任务
        try:
            interrupted = await self.reorder_state_manager.detect_interrupted_task()
        except Exception:
            interrupted = None

        if interrupted:
            print(f"\n[增量整理] 检测到未完成任务 (session={interrupted.get('session_id', 'unknown')})")
            print(f"  总桶数: {interrupted.get('total_buckets', 0)}, "
                  f"已完成: {interrupted.get('completed_buckets', 0)}, "
                  f"未完成: {interrupted.get('incomplete_buckets', 0)}")
            # 自动续移，不询问用户（state 已在 detect_interrupted_task 中加载）
        else:
            # 创建新会话
            try:
                csv_path = Path(self.config.OUTPUT_DIR) / "videos.csv"
                csv_mtime = csv_path.stat().st_mtime if csv_path.exists() else 0.0
                await self.reorder_state_manager.create_new_state(
                    source_csv="videos.csv", source_csv_mtime=csv_mtime
                )
            except Exception as e:
                print(f"  [WARN] 重排状态初始化失败: {e}，继续执行但不支持断点续移")

        # 扫描残留临时桶
        await self.cleanup_temp_folders()

        # 按桶串行处理
        for category, new_videos in classified_new_videos.items():
            if category not in folder_map:
                print(f"  [SKIP] 分类 '{category}' 没有对应收藏夹")
                continue

            target_folder_id = folder_map[category]

            # 1. 检查是否已完成
            try:
                if await self.reorder_state_manager.is_bucket_completed(category):
                    print(f"  [{category}] ★ 已完成（跳过）")
                    report["categories"][category] = {
                        "status": "skipped",
                        "target_folder": target_folder_id,
                    }
                    continue
            except Exception:
                pass

            # 2. 初始化桶状态
            try:
                await self.reorder_state_manager.init_bucket(
                    category, target_folder_id, 0
                )
            except Exception as e:
                print(f"  [{category}] 初始化桶状态失败: {e}")

            # 3. 合并排序
            try:
                sorted_videos, need_reorder = await self.merge_and_resort(
                    category, new_videos, target_folder_id
                )
            except Exception as e:
                print(f"  [{category}] 合并排序失败: {e}")
                report["categories"][category] = {
                    "status": "failed",
                    "error": str(e),
                }
                continue

            # 4. 根据条件执行重排或仅添加
            if reorder and need_reorder:
                # 执行全桶重排
                print(f"  [{category}] 开始全桶重排...")
                success = await self.reorder_folder_full(
                    category, sorted_videos, target_folder_id,
                    dry_run=False, new_videos=new_videos,
                )
                if success:
                    try:
                        await self.reorder_state_manager.mark_bucket_completed(category)
                    except Exception:
                        pass
                    report["categories"][category] = {
                        "status": "reordered",
                        "target_folder": target_folder_id,
                        "video_count": len(sorted_videos),
                    }
                    print(f"  [{category}] ✓ 重排完成")
                else:
                    # 失败，保持 in_progress
                    report["categories"][category] = {
                        "status": "failed",
                        "target_folder": target_folder_id,
                        "video_count": len(sorted_videos),
                    }
                    print(f"  [{category}] ✗ 重排失败，保持 in_progress 状态")
            else:
                # 仅 add 新视频到目标桶（跳过已存在的）
                if not reorder:
                    print(f"  [{category}] 仅添加新视频（不重排）")
                else:
                    print(f"  [{category}] 顺序一致，仅添加新视频")
                try:
                    existing_ids_raw = await self.crawler.get_folder_video_ids(
                        target_folder_id
                    )
                    if existing_ids_raw is None:
                        existing_ids: set[int] = set()
                    else:
                        existing_ids = {
                            item["id"] for item in existing_ids_raw if "id" in item
                        }
                except Exception:
                    existing_ids = set()

                added_count = 0
                stale_cleaned = 0
                for v in new_videos:
                    if not v.is_valid:
                        continue
                    if v.id in existing_ids:
                        # 视频已在目标桶，但仍可能在稍后再看列表
                        # （上次 add 成功但 delete_watchlater 失败的情况），补清理
                        if v.source_folder_id == 0 and v.source_folder_title == "稍后再看":
                            if await self._cleanup_watchlater_if_needed(v):
                                stale_cleaned += 1
                        continue
                    resources = [f"{v.id}:{v.type}"]
                    success = await self.crawler.add_resources(
                        target_folder_id, resources
                    )
                    if success:
                        added_count += 1
                        # 稍后再看视频 add 后，从稍后再看列表删除
                        await self._cleanup_watchlater_if_needed(v)
                    else:
                        if self.crawler.last_post_412:
                            print(f"  [{category}] ⚠ add 触发 412 风控，跳过后续 add")
                            break
                        print(f"  [{category}] ✗ add 失败: avid={v.id} | {v.title} ({v.bvid})")

                try:
                    await self.reorder_state_manager.mark_bucket_completed(category)
                except Exception:
                    pass
                report["categories"][category] = {
                    "status": "added_only",
                    "target_folder": target_folder_id,
                    "added_count": added_count,
                    "stale_cleaned": stale_cleaned,
                    "video_count": len(sorted_videos),
                }
                msg = f"  [{category}] ✓ 添加完成: 新增 {added_count} 条"
                if stale_cleaned > 0:
                    msg += f"，补清理稍后再看 {stale_cleaned} 条"
                print(msg)

        return report
