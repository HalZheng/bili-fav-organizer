"""B站收藏夹整理工具 - 增量重排状态管理模块

负责按桶为单位的增量重排进度持久化、中断检测与断点续移。

状态文件结构（reorder_state.json）:
    ReorderState
      └─ categories: dict[str, BucketReorderState]
            └─ 单个桶的重排进度（phase 推进 + 计数）
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


@dataclass
class BucketReorderState:
    """单个桶的重排状态

    phase 推进顺序：fetch → merge → sort → move_to_temp → move_back → cleanup → done
    """
    status: str = "pending"  # pending/in_progress/completed
    target_folder_id: int = 0
    temp_folder_id: int = 0  # 重排中临时桶 id，完成后置 0
    total_videos: int = 0
    moved_to_temp_count: int = 0
    moved_back_count: int = 0
    phase: str = "fetch"  # fetch/merge/sort/move_to_temp/move_back/cleanup/done
    started_at: float = 0.0
    completed_at: float = 0.0


@dataclass
class ReorderState:
    """整个增量重排任务的状态"""
    version: int = 1
    session_id: str = ""
    created_at: float = 0.0
    last_updated: float = 0.0
    source_csv: str = "videos.csv"
    source_csv_mtime: float = 0.0
    mode: str = "incremental"
    categories: dict = field(default_factory=dict)  # {bucket_name: BucketReorderState}


class ReorderStateManager:
    """增量重排状态管理器

    负责按桶为单位的重排进度持久化、中断检测、断点续移。
    所有公开方法均为 async（涉及文件 IO 与锁）。
    """

    def __init__(self, config: BiliConfig):
        self.config = config
        self.state: Optional[ReorderState] = None
        self.state_path = Path(config.OUTPUT_DIR) / config.REORDER_STATE_FILE
        self._lock = asyncio.Lock()  # 保护并发写入

    # ===== 内部转换工具 =====

    @staticmethod
    def _bucket_from_dict(d: dict) -> BucketReorderState:
        return BucketReorderState(
            status=d.get("status", "pending"),
            target_folder_id=d.get("target_folder_id", 0),
            temp_folder_id=d.get("temp_folder_id", 0),
            total_videos=d.get("total_videos", 0),
            moved_to_temp_count=d.get("moved_to_temp_count", 0),
            moved_back_count=d.get("moved_back_count", 0),
            phase=d.get("phase", "fetch"),
            started_at=d.get("started_at", 0.0),
            completed_at=d.get("completed_at", 0.0),
        )

    @staticmethod
    def _state_from_dict(d: dict) -> ReorderState:
        return ReorderState(
            version=d.get("version", 1),
            session_id=d.get("session_id", ""),
            created_at=d.get("created_at", 0.0),
            last_updated=d.get("last_updated", 0.0),
            source_csv=d.get("source_csv", "videos.csv"),
            source_csv_mtime=d.get("source_csv_mtime", 0.0),
            mode=d.get("mode", "incremental"),
            categories={
                k: ReorderStateManager._bucket_from_dict(v)
                for k, v in d.get("categories", {}).items()
            },
        )

    # ===== 核心读写方法 =====

    async def load_state(self) -> Optional[ReorderState]:
        """从 JSON 文件加载状态，文件不存在返回 None。

        将 dict 递归转换为 dataclass（包括嵌套的 categories/bucket）。
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

        先写 reorder_state.json.tmp，再 os.replace 覆盖原文件（Windows 上原子操作）。
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
        source_csv: str,
        source_csv_mtime: float,
    ) -> ReorderState:
        """创建新的空状态。

        session_id 使用 datetime.now().strftime("%Y%m%d_%H%M%S")。
        如果旧状态文件存在，先调用 archive_state() 归档。
        然后初始化 self.state 并 save_state()。
        """
        if self.state_path.exists():
            await self.archive_state()
        session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        now = time.time()
        self.state = ReorderState(
            version=1,
            session_id=session_id,
            created_at=now,
            last_updated=now,
            source_csv=source_csv,
            source_csv_mtime=source_csv_mtime,
            mode="incremental",
            categories={},
        )
        await self.save_state()
        return self.state

    async def archive_state(self) -> None:
        """将当前状态文件重命名为 reorder_state_<session_id>.json.bak。

        保留最近 config.REORDER_STATE_BACKUP_KEEP 个备份，更早的删除。
        用 glob 查找 output 目录下所有 reorder_state_*.json.bak 文件，按修改时间排序。
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
        backup_path = self.state_path.parent / f"reorder_state_{session_id}.json.bak"
        os.replace(self.state_path, backup_path)
        # 清理多余备份：按修改时间倒序，保留最近 N 个
        keep = self.config.REORDER_STATE_BACKUP_KEEP
        backups = list(self.state_path.parent.glob("reorder_state_*.json.bak"))
        if len(backups) > keep:
            backups.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            for old in backups[keep:]:
                try:
                    old.unlink()
                except OSError:
                    pass

    # ===== 检测与查询 =====

    async def detect_interrupted_task(self) -> Optional[dict]:
        """检测是否存在未完成的重排任务。

        加载状态文件，如果有 status != "completed" 的桶，返回恢复摘要 dict。
        如果无状态文件或所有桶已完成，返回 None。
        """
        state = await self.load_state()
        if state is None:
            return None
        incomplete_buckets = []
        completed_buckets = 0
        for name, bucket in state.categories.items():
            if bucket.status == "completed":
                completed_buckets += 1
            else:
                incomplete_buckets.append({
                    "name": name,
                    "status": bucket.status,
                    "phase": bucket.phase,
                    "target_folder_id": bucket.target_folder_id,
                    "temp_folder_id": bucket.temp_folder_id,
                    "total_videos": bucket.total_videos,
                    "moved_to_temp_count": bucket.moved_to_temp_count,
                    "moved_back_count": bucket.moved_back_count,
                })
        if not incomplete_buckets:
            return None
        return {
            "session_id": state.session_id,
            "mode": state.mode,
            "created_at": state.created_at,
            "total_buckets": len(state.categories),
            "completed_buckets": completed_buckets,
            "incomplete_buckets": len(incomplete_buckets),
            "buckets": incomplete_buckets,
        }

    # ===== 初始化与记录 =====

    async def init_bucket(
        self,
        bucket_name: str,
        target_folder_id: int,
        total_videos: int,
    ) -> None:
        """初始化某个桶的重排状态。

        在 categories 中创建 BucketReorderState(status="pending")，
        记录 target_folder_id 与 total_videos，设置 started_at，然后 save_state()。
        """
        if self.state is None:
            return
        self.state.categories[bucket_name] = BucketReorderState(
            status="pending",
            target_folder_id=target_folder_id,
            temp_folder_id=0,
            total_videos=total_videos,
            moved_to_temp_count=0,
            moved_back_count=0,
            phase="fetch",
            started_at=time.time(),
            completed_at=0.0,
        )
        await self.save_state()

    async def update_bucket_phase(
        self,
        bucket_name: str,
        phase: str,
        temp_folder_id: int = None,
        moved_to_temp_count: int = None,
        moved_back_count: int = None,
    ) -> None:
        """更新指定桶的 phase 和可选字段。

        仅当对应参数非 None 时更新 temp_folder_id / moved_to_temp_count / moved_back_count。
        桶状态若为 pending 则提升为 in_progress。save_state()。
        """
        if self.state is None:
            return
        bucket = self.state.categories.get(bucket_name)
        if bucket is None:
            return
        bucket.phase = phase
        if temp_folder_id is not None:
            bucket.temp_folder_id = temp_folder_id
        if moved_to_temp_count is not None:
            bucket.moved_to_temp_count = moved_to_temp_count
        if moved_back_count is not None:
            bucket.moved_back_count = moved_back_count
        if bucket.status == "pending":
            bucket.status = "in_progress"
        await self.save_state()

    async def mark_bucket_completed(self, bucket_name: str) -> None:
        """强制标记桶完成。

        设置 status="completed", phase="done", completed_at=time.time(), temp_folder_id=0，
        然后 save_state()。
        """
        if self.state is None:
            return
        bucket = self.state.categories.get(bucket_name)
        if bucket is None:
            return
        bucket.status = "completed"
        bucket.phase = "done"
        bucket.completed_at = time.time()
        bucket.temp_folder_id = 0
        await self.save_state()

    # ===== 查询方法 =====

    async def is_bucket_completed(self, bucket_name: str) -> bool:
        """判断桶是否完成。"""
        if self.state is None:
            return False
        bucket = self.state.categories.get(bucket_name)
        if bucket is None:
            return False
        return bucket.status == "completed"

    async def get_bucket_state(self, bucket_name: str) -> Optional[BucketReorderState]:
        """获取桶状态，不存在返回 None。"""
        if self.state is None:
            return None
        return self.state.categories.get(bucket_name)
