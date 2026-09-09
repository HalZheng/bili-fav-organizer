"""B站收藏夹整理工具 - 移动状态管理模块

负责持久化移动任务进度、检测中断、恢复任务、源/目标侧双向验证。

状态文件结构（move_state.json）:
    MoveState
      └─ categories: dict[str, CategoryState]
            └─ blocks: dict[str, BlockState]   # keys: top/main/bottom
                  └─ videos: list[VideoRecord]
"""

import asyncio
import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from config import BiliConfig


# 视频终态集合：已处理完成（含永久失败），恢复时不再重试
TERMINAL_STATUSES = {"moved", "added", "copied", "skipped", "failed_permanent"}


@dataclass
class VideoRecord:
    """单条视频的操作记录"""
    avid: int = 0
    bvid: str = ""
    title: str = ""
    block: str = ""  # top/main/bottom
    status: str = "pending"  # pending/moved/added/copied/skipped/failed/failed_permanent
    op: str = ""  # move/add/copy/skip/null
    source_folder_id: int = 0
    ts: float = 0.0  # 操作时间戳
    error: str = ""  # 失败时的错误信息


@dataclass
class BlockState:
    """区块状态（top/main/bottom）"""
    status: str = "pending"  # pending/in_progress/completed
    videos: list = field(default_factory=list)


@dataclass
class CategoryState:
    """分类状态"""
    target_folder_id: int = 0
    target_folder_title: str = ""
    status: str = "pending"  # pending/in_progress/completed
    started_at: float = 0.0
    completed_at: float = 0.0
    blocks: dict = field(default_factory=dict)  # keys: top/main/bottom


@dataclass
class MoveState:
    """整个移动任务的状态"""
    version: int = 1
    session_id: str = ""
    created_at: float = 0.0
    last_updated: float = 0.0
    mode: str = "auto"  # auto/add/move/copy
    dry_run: bool = False
    source_csv: str = "videos.csv"
    source_csv_mtime: float = 0.0
    categories: dict = field(default_factory=dict)


class MoveStateManager:
    """移动状态管理器

    负责移动任务的进度持久化、中断检测、恢复执行。
    所有公开方法均为 async（涉及文件 IO 与锁）。
    """

    def __init__(self, config: BiliConfig):
        self.config = config
        self.state: Optional[MoveState] = None
        self.state_path = Path(config.OUTPUT_DIR) / config.MOVE_STATE_FILE
        self._lock = asyncio.Lock()  # 保护并发写入

    # ===== 内部转换工具 =====

    @staticmethod
    def _video_from_dict(d: dict) -> VideoRecord:
        return VideoRecord(
            avid=d.get("avid", 0),
            bvid=d.get("bvid", ""),
            title=d.get("title", ""),
            block=d.get("block", ""),
            status=d.get("status", "pending"),
            op=d.get("op", ""),
            source_folder_id=d.get("source_folder_id", 0),
            ts=d.get("ts", 0.0),
            error=d.get("error", ""),
        )

    @staticmethod
    def _block_from_dict(d: dict) -> BlockState:
        return BlockState(
            status=d.get("status", "pending"),
            videos=[MoveStateManager._video_from_dict(v) for v in d.get("videos", [])],
        )

    @staticmethod
    def _category_from_dict(d: dict) -> CategoryState:
        return CategoryState(
            target_folder_id=d.get("target_folder_id", 0),
            target_folder_title=d.get("target_folder_title", ""),
            status=d.get("status", "pending"),
            started_at=d.get("started_at", 0.0),
            completed_at=d.get("completed_at", 0.0),
            blocks={
                k: MoveStateManager._block_from_dict(v)
                for k, v in d.get("blocks", {}).items()
            },
        )

    @staticmethod
    def _state_from_dict(d: dict) -> MoveState:
        return MoveState(
            version=d.get("version", 1),
            session_id=d.get("session_id", ""),
            created_at=d.get("created_at", 0.0),
            last_updated=d.get("last_updated", 0.0),
            mode=d.get("mode", "auto"),
            dry_run=d.get("dry_run", False),
            source_csv=d.get("source_csv", "videos.csv"),
            source_csv_mtime=d.get("source_csv_mtime", 0.0),
            categories={
                k: MoveStateManager._category_from_dict(v)
                for k, v in d.get("categories", {}).items()
            },
        )

    # ===== 核心读写方法 =====

    async def load_state(self) -> Optional[MoveState]:
        """从 JSON 文件加载状态，文件不存在返回 None。

        将 dict 递归转换为 dataclass（包括嵌套的 categories/blocks/videos）。
        """
        if not self.state_path.exists():
            return None
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.state = self._state_from_dict(data)
            return self.state
        except (json.JSONDecodeError, OSError):
            return None

    async def save_state(self) -> None:
        """原子写入状态文件。

        先写 move_state.json.tmp，再 os.replace 覆盖原文件（Windows 上原子操作）。
        更新 last_updated。使用 async with self._lock 保护并发写入。
        """
        if self.state is None:
            return
        async with self._lock:
            self.state.last_updated = time.time()
            data = asdict(self.state)
            # 确保输出目录存在
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self.state_path.with_suffix(".json.tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self.state_path)  # 原子操作

    async def create_new_state(
        self,
        mode: str,
        dry_run: bool,
        source_csv: str,
        source_csv_mtime: float,
    ) -> MoveState:
        """创建新的空状态。

        session_id 使用 datetime.now().strftime("%Y%m%d_%H%M%S")。
        如果旧状态文件存在，先调用 archive_state() 归档。
        然后初始化 self.state 并 save_state()。
        """
        if self.state_path.exists():
            await self.archive_state()
        session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        now = time.time()
        self.state = MoveState(
            version=1,
            session_id=session_id,
            created_at=now,
            last_updated=now,
            mode=mode,
            dry_run=dry_run,
            source_csv=source_csv,
            source_csv_mtime=source_csv_mtime,
            categories={},
        )
        await self.save_state()
        return self.state

    async def archive_state(self) -> None:
        """将当前状态文件重命名为 move_state_<session_id>.json.bak。

        保留最近 config.MOVE_STATE_BACKUP_KEEP 个备份，更早的删除。
        用 glob 查找 output 目录下所有 move_state_*.json.bak 文件，按修改时间排序。
        """
        if not self.state_path.exists():
            return
        # 读取旧状态的 session_id 用于备份命名
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            session_id = data.get("session_id", "unknown")
        except (json.JSONDecodeError, OSError):
            session_id = "unknown"
        backup_path = self.state_path.parent / f"move_state_{session_id}.json.bak"
        os.replace(self.state_path, backup_path)
        # 清理多余备份：按修改时间倒序，保留最近 N 个
        keep = self.config.MOVE_STATE_BACKUP_KEEP
        backups = list(self.state_path.parent.glob("move_state_*.json.bak"))
        if len(backups) > keep:
            backups.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            for old in backups[keep:]:
                try:
                    old.unlink()
                except OSError:
                    pass

    # ===== 检测与查询 =====

    async def detect_interrupted_task(self) -> Optional[dict]:
        """检测是否存在未完成任务。

        加载状态文件，如果有 status != "completed" 的分类，返回恢复摘要 dict。
        如果无状态文件或所有分类已完成，返回 None。
        """
        state = await self.load_state()
        if state is None:
            return None
        incomplete_categories = []
        total_videos = 0
        completed_videos = 0
        pending_videos = 0
        failed_videos = 0
        completed_categories = 0
        for name, cat in state.categories.items():
            cat_total = 0
            cat_completed = 0
            cat_pending = 0
            cat_failed = 0
            for block in cat.blocks.values():
                for v in block.videos:
                    cat_total += 1
                    if v.status in TERMINAL_STATUSES:
                        cat_completed += 1
                    elif v.status == "pending":
                        cat_pending += 1
                    elif v.status == "failed":
                        cat_failed += 1
            total_videos += cat_total
            completed_videos += cat_completed
            pending_videos += cat_pending
            failed_videos += cat_failed
            if cat.status == "completed":
                completed_categories += 1
            else:
                incomplete_categories.append({
                    "name": name,
                    "status": cat.status,
                    "total": cat_total,
                    "completed": cat_completed,
                    "pending": cat_pending,
                    "failed": cat_failed,
                })
        if not incomplete_categories:
            return None
        # 检测源数据是否变化
        source_csv_changed = False
        csv_path = Path(self.config.OUTPUT_DIR) / state.source_csv
        if csv_path.exists():
            try:
                current_mtime = csv_path.stat().st_mtime
                source_csv_changed = abs(current_mtime - state.source_csv_mtime) > 1.0
            except OSError:
                source_csv_changed = False
        return {
            "session_id": state.session_id,
            "mode": state.mode,
            "created_at": state.created_at,
            "total_categories": len(state.categories),
            "completed_categories": completed_categories,
            "incomplete_categories": len(incomplete_categories),
            "total_videos": total_videos,
            "completed_videos": completed_videos,
            "pending_videos": pending_videos,
            "failed_videos": failed_videos,
            "source_csv_changed": source_csv_changed,
            "categories": incomplete_categories,
        }

    # ===== 初始化与记录 =====

    async def init_category(
        self,
        category_name: str,
        target_folder_id: int,
        target_folder_title: str,
        videos_by_block: dict,
    ) -> None:
        """初始化某分类的状态。

        videos_by_block: {"top": [BiliVideo...], "main": [...], "bottom": [...]}
        为每个视频创建 VideoRecord(status="pending")，从 BiliVideo 提取 avid/bvid/title/source_folder_id。
        """
        if self.state is None:
            return
        blocks = {}
        for block_name, videos in videos_by_block.items():
            video_records = []
            for v in videos:
                video_records.append(VideoRecord(
                    avid=getattr(v, "id", 0),
                    bvid=getattr(v, "bvid", ""),
                    title=getattr(v, "title", ""),
                    block=block_name,
                    status="pending",
                    op="",
                    source_folder_id=getattr(v, "source_folder_id", 0),
                    ts=0.0,
                    error="",
                ))
            blocks[block_name] = BlockState(status="pending", videos=video_records)
        self.state.categories[category_name] = CategoryState(
            target_folder_id=target_folder_id,
            target_folder_title=target_folder_title,
            status="pending",
            started_at=time.time(),
            completed_at=0.0,
            blocks=blocks,
        )
        await self.save_state()

    async def record_video_result(
        self,
        category: str,
        block: str,
        avid: int,
        status: str,
        op: str,
        error: str = "",
    ) -> None:
        """更新单条视频状态并原子写入。

        在 self.state.categories[category].blocks[block].videos 中找到 avid 匹配的视频记录，
        更新 status/op/ts/error，然后 save_state()。
        """
        if self.state is None:
            return
        cat = self.state.categories.get(category)
        if cat is None:
            return
        blk = cat.blocks.get(block)
        if blk is None:
            return
        for v in blk.videos:
            if v.avid == avid:
                v.status = status
                v.op = op
                v.ts = time.time()
                v.error = error
                break
        await self.save_state()

    async def update_block_status(self, category: str, block: str, status: str) -> None:
        """更新区块状态。

        如果 status=="completed"，检查是否所有视频都是终态（非 pending/failed）。
        若仍有非终态视频，不标记完成。
        """
        if self.state is None:
            return
        cat = self.state.categories.get(category)
        if cat is None:
            return
        blk = cat.blocks.get(block)
        if blk is None:
            return
        if status == "completed":
            # 检查所有视频是否终态（非 pending/failed）
            all_terminal = all(v.status in TERMINAL_STATUSES for v in blk.videos)
            if not all_terminal:
                return  # 仍有 pending/failed 视频，不标记完成
        blk.status = status

    async def update_category_status(self, category: str) -> None:
        """根据区块状态推断分类状态。

        所有区块 completed → 分类 completed，设置 completed_at。
        """
        if self.state is None:
            return
        cat = self.state.categories.get(category)
        if cat is None:
            return
        if not cat.blocks:
            return
        all_completed = all(b.status == "completed" for b in cat.blocks.values())
        if all_completed:
            cat.status = "completed"
            cat.completed_at = time.time()

    async def mark_category_completed(self, category: str) -> None:
        """强制标记分类完成，设置 completed_at，save_state()。"""
        if self.state is None:
            return
        cat = self.state.categories.get(category)
        if cat is None:
            return
        cat.status = "completed"
        cat.completed_at = time.time()
        await self.save_state()

    # ===== 查询方法 =====

    async def get_pending_videos(self, category: str) -> list:
        """返回该分类中所有 pending 状态的视频，格式 [(block_name, VideoRecord), ...]。"""
        if self.state is None:
            return []
        cat = self.state.categories.get(category)
        if cat is None:
            return []
        result = []
        for block_name, blk in cat.blocks.items():
            for v in blk.videos:
                if v.status == "pending":
                    result.append((block_name, v))
        return result

    async def get_failed_videos(self, category: str) -> list:
        """返回该分类中所有 failed 状态的视频，格式 [(block_name, VideoRecord), ...]。"""
        if self.state is None:
            return []
        cat = self.state.categories.get(category)
        if cat is None:
            return []
        result = []
        for block_name, blk in cat.blocks.items():
            for v in blk.videos:
                if v.status == "failed":
                    result.append((block_name, v))
        return result

    async def get_category_progress(self, category: str) -> dict:
        """返回 {"total": int, "completed": int, "pending": int, "failed": int, "status": str}。"""
        if self.state is None:
            return {"total": 0, "completed": 0, "pending": 0, "failed": 0, "status": "pending"}
        cat = self.state.categories.get(category)
        if cat is None:
            return {"total": 0, "completed": 0, "pending": 0, "failed": 0, "status": "pending"}
        total = 0
        completed = 0
        pending = 0
        failed = 0
        for blk in cat.blocks.values():
            for v in blk.videos:
                total += 1
                if v.status in TERMINAL_STATUSES:
                    completed += 1
                elif v.status == "pending":
                    pending += 1
                elif v.status == "failed":
                    failed += 1
        return {
            "total": total,
            "completed": completed,
            "pending": pending,
            "failed": failed,
            "status": cat.status,
        }

    async def is_category_completed(self, category: str) -> bool:
        """判断分类是否完成。"""
        if self.state is None:
            return False
        cat = self.state.categories.get(category)
        if cat is None:
            return False
        return cat.status == "completed"

    async def is_source_csv_changed(self) -> bool:
        """比较 Path(config.OUTPUT_DIR)/"videos.csv" 的 mtime 与 self.state.source_csv_mtime。

        如果状态未加载或文件不存在，返回 False。
        """
        if self.state is None:
            return False
        csv_path = Path(self.config.OUTPUT_DIR) / self.state.source_csv
        if not csv_path.exists():
            return False
        try:
            current_mtime = csv_path.stat().st_mtime
        except OSError:
            return False
        return abs(current_mtime - self.state.source_csv_mtime) > 1.0

    async def get_video_status(self, category: str, block: str, avid: int) -> Optional[str]:
        """查询单条视频的状态。"""
        if self.state is None:
            return None
        cat = self.state.categories.get(category)
        if cat is None:
            return None
        blk = cat.blocks.get(block)
        if blk is None:
            return None
        for v in blk.videos:
            if v.avid == avid:
                return v.status
        return None

    async def mark_video_status(
        self,
        category: str,
        block: str,
        avid: int,
        status: str,
        op: str = "",
        error: str = "",
    ) -> None:
        """直接设置视频状态（不通过操作结果），用于源/目标侧验证后的批量标记。save_state()。"""
        if self.state is None:
            return
        cat = self.state.categories.get(category)
        if cat is None:
            return
        blk = cat.blocks.get(block)
        if blk is None:
            return
        for v in blk.videos:
            if v.avid == avid:
                v.status = status
                v.op = op
                v.ts = time.time()
                v.error = error
                break
        await self.save_state()
