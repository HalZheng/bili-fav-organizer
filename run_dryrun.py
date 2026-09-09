"""Dry-run 预览脚本 - 不执行实际操作，仅展示分类和排序结果"""

import sys
import os
import asyncio

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

sys.path.insert(0, os.path.dirname(__file__))

from config import BiliConfig
from classifier import FunnelClassifier, enrich_video_from_csv
from organizer import BiliOrganizer
from models import BiliVideo, BiliUP
from bucket_config import BUCKET_NAMES
from sorter import FolderSorter
from pathlib import Path
import pandas as pd


def _safe_int(val, default=0):
    if val is None or (isinstance(val, float) and (val != val)):
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def load_videos_from_csv(csv_path: str) -> list[BiliVideo]:
    df = pd.read_csv(csv_path, dtype={"up_mid": "Int64"})
    print(f"[加载] 从 {csv_path} 读取 {len(df)} 条记录")

    videos = []
    for _, row in df.iterrows():
        v = BiliVideo(
            bvid=str(row.get("bvid", "") or ""),
            id=_safe_int(row.get("avid", 0)),
            title=str(row.get("title", "") or ""),
            intro=str(row.get("intro", "") or ""),
            attr=_safe_int(row.get("attr", 0)),
            pubtime=_safe_int(row.get("pubtime", 0)),
            fav_time=_safe_int(row.get("fav_time", 0)),
            duration=_safe_int(row.get("duration_sec", 0)),
            page=_safe_int(row.get("page_count", 1), default=1),
            view_count=_safe_int(row.get("view_count", 0)),
            upper=BiliUP(
                mid=_safe_int(row.get("up_mid", 0)),
                name=str(row.get("up_name", "") or ""),
            ),
            source_folder_id=_safe_int(row.get("source_folder_id", 0)),
            source_folder_title=str(row.get("source_folder_title", "") or ""),
        )
        enrich_video_from_csv(v, row.to_dict())
        if row.get("category") and str(row.get("category", "")).strip():
            v.category = str(row.get("category", "")).strip()
        videos.append(v)

    valid_count = sum(1 for v in videos if v.is_valid)
    print(f"[加载] 有效视频: {valid_count} 条 | 失效: {len(videos) - valid_count} 条")
    return videos


async def main():
    config = BiliConfig()

    # Step 1: 加载数据
    csv_path = Path(config.OUTPUT_DIR) / "videos.csv"
    if not csv_path.exists():
        print(f"[ERROR] 未找到 {csv_path}")
        return

    videos = load_videos_from_csv(str(csv_path))
    if not videos:
        print("[ERROR] 未加载到任何视频数据")
        return

    # Step 2: 三层漏斗分流
    print(f"\n{'=' * 60}")
    print("  Step 2: 三层漏斗分流")
    print("=" * 60)

    funnel = FunnelClassifier(config)
    classified = funnel.classify_all(videos)

    # Step 3: 桶内排序
    print(f"\n{'=' * 60}")
    print("  Step 3: 桶内精细化排序")
    print("=" * 60)

    sorter = FolderSorter(archive_months=config.ARCHIVE_MONTHS)
    for bucket_name, bucket_videos in classified.items():
        sorted_videos = sorter.sort_folder(bucket_videos)
        classified[bucket_name] = sorted_videos
        summary = sorter.get_sort_summary(sorted_videos)
        print(
            f"  {bucket_name}: Top={summary['top']} | Main={summary['main']} | "
            f"Bottom={summary['bottom']} | 高频UP主={len(summary['frequent_ups'])}"
        )

    # Step 4: dry-run 预览
    print(f"\n{'=' * 60}")
    print("  Step 4: 整理预览 (dry-run)")
    print("=" * 60)

    organizer = BiliOrganizer(config)
    try:
        plan = await organizer.full_organize(
            classified, folder_prefix="", mode="auto", dry_run=True
        )
        for cat, info in plan["categories"].items():
            blocks = info.get("blocks", {})
            print(
                f"  {cat}: {info['video_count']} 条 "
                f"(Top={blocks.get('top', 0)} | Main={blocks.get('main', 0)} | Bottom={blocks.get('bottom', 0)}) "
                f"→ 收藏夹 id={info['target_folder']}"
            )
    finally:
        await organizer.close()

    # 输出各桶示例视频
    print(f"\n{'=' * 60}")
    print("  各桶示例视频 (前5条)")
    print("=" * 60)
    for bucket_name in BUCKET_NAMES:
        if bucket_name in classified:
            vids = classified[bucket_name][:5]
            print(f"\n  [{bucket_name}] ({len(classified[bucket_name])} 条)")
            for v in vids:
                block = getattr(v, "block_type", "main")
                print(f"    [{block}] {v.upper.name}: {v.title[:60]}")


if __name__ == "__main__":
    asyncio.run(main())
