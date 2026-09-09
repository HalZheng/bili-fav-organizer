"""非交互式运行脚本 - 跳过爬取，直接从CSV加载并执行分流+排序+预览"""

import sys
import os
import time
import json
from pathlib import Path

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

sys.path.insert(0, os.path.dirname(__file__))

from config import BiliConfig
from models import BiliVideo, BiliUP
from classifier import FunnelClassifier, enrich_video_from_csv
from sorter import FolderSorter
from bucket_config import BUCKETS, BUCKET_NAMES, DEFAULT_BUCKET


def _safe_int(val, default=0):
    """安全地将值转为int，处理NaN和None"""
    if val is None or (isinstance(val, float) and (val != val)):  # NaN check
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def load_videos_from_csv(csv_path: str) -> list[BiliVideo]:
    """从CSV文件加载视频数据"""
    import pandas as pd
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
        # 从CSV补充分区/标签信息
        enrich_video_from_csv(v, row.to_dict())
        # 保留已有的category（如果有的话）
        if row.get("category") and str(row.get("category", "")).strip():
            v.category = str(row.get("category", "")).strip()
        videos.append(v)

    valid_count = sum(1 for v in videos if v.is_valid)
    print(f"[加载] 有效视频: {valid_count} 条 | 失效: {len(videos) - valid_count} 条")
    return videos


def main():
    print("=" * 60)
    print("  B站收藏夹整理 - 非交互式运行 (跳过爬取)")
    print("=" * 60)

    config = BiliConfig()

    # Step 1: 从CSV加载数据
    csv_path = Path(config.OUTPUT_DIR) / "videos.csv"
    if not csv_path.exists():
        print(f"[ERROR] 未找到 {csv_path}，请先爬取数据")
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

    # 展示分流结果
    print(f"\n{'─' * 50}")
    print(f"  分流结果汇总")
    print(f"{'─' * 50}")
    total = 0
    for bucket_name in BUCKET_NAMES:
        if bucket_name in classified:
            count = len(classified[bucket_name])
            up_count = len(set(v.upper.name for v in classified[bucket_name]))
            print(f"  {bucket_name:20s}: {count:5d} 条视频  ({up_count} 位UP主)")
            total += count
    print(f"  {'─' * 40}")
    print(f"  {'合计':20s}: {total:5d} 条")

    # Step 3: 桶内精细化排序
    print(f"\n{'=' * 60}")
    print("  Step 3: 桶内精细化排序")
    print("=" * 60)

    sorter = FolderSorter(archive_months=config.ARCHIVE_MONTHS)

    for bucket_name, bucket_videos in classified.items():
        sorted_videos = sorter.sort_folder(bucket_videos)
        classified[bucket_name] = sorted_videos
        summary = sorter.get_sort_summary(sorted_videos)
        print(
            f"  {bucket_name:20s}: Top={summary['top']:3d} | Main={summary['main']:3d} | "
            f"Bottom={summary['bottom']:3d} | 高频UP主={len(summary['frequent_ups'])}"
        )

    # Step 4: 导出结果
    print(f"\n{'=' * 60}")
    print("  Step 4: 导出结果")
    print("=" * 60)

    import pandas as pd

    # 导出分类+排序结果
    all_videos_sorted = []
    for bucket_name in BUCKET_NAMES:
        if bucket_name in classified:
            all_videos_sorted.extend(classified[bucket_name])

    output_path = Path(config.OUTPUT_DIR) / "classified_sorted_videos.csv"
    records = [v.to_dict() for v in all_videos_sorted]
    df = pd.DataFrame(records)
    df.to_csv(output_path, index=False, encoding="utf-8-sig")
    print(f"  [OK] 已导出: {output_path} ({len(records)} 条)")

    # 导出各桶的详细统计
    bucket_stats = {}
    for bucket_name in BUCKET_NAMES:
        if bucket_name in classified:
            vids = classified[bucket_name]
            summary = sorter.get_sort_summary(vids)
            up_counter = {}
            for v in vids:
                if v.upper.name not in up_counter:
                    up_counter[v.upper.name] = 0
                up_counter[v.upper.name] += 1
            top_ups = sorted(up_counter.items(), key=lambda x: -x[1])[:10]
            bucket_stats[bucket_name] = {
                "total": len(vids),
                "top_block": summary["top"],
                "main_block": summary["main"],
                "bottom_block": summary["bottom"],
                "frequent_ups": summary["frequent_ups"],
                "top_10_ups": [{"name": name, "count": count} for name, count in top_ups],
            }

    stats_path = Path(config.OUTPUT_DIR) / "bucket_stats.json"
    with open(stats_path, "w", encoding="utf-8") as f:
        json.dump(bucket_stats, f, ensure_ascii=False, indent=2)
    print(f"  [OK] 已导出桶统计: {stats_path}")

    # 展示高频UP主
    print(f"\n{'=' * 60}")
    print("  各桶 TOP 5 高频UP主")
    print("=" * 60)
    for bucket_name in BUCKET_NAMES:
        if bucket_name in classified:
            vids = classified[bucket_name]
            from collections import Counter
            up_counter = Counter(v.upper.name for v in vids)
            top5 = up_counter.most_common(5)
            if top5:
                print(f"\n  [{bucket_name}]")
                for name, count in top5:
                    print(f"    {name}: {count} 条")

    print(f"\n{'=' * 60}")
    print("  分流+排序完成！下一步:")
    print("  1. 查看 output/classified_sorted_videos.csv 确认分类结果")
    print("  2. 查看 output/bucket_stats.json 了解各桶统计")
    print("  3. 确认无误后，运行 run_organize_from_classified.py 执行实际整理操作")
    print("=" * 60)


if __name__ == "__main__":
    main()
