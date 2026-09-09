"""从已分类的 classified_sorted_videos.csv 直接执行整理，跳过分类步骤

用法: python run_organize_from_classified.py [mode]
  mode: auto(默认) / add / move / copy
"""
import sys
import os
import asyncio
import time

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

sys.path.insert(0, os.path.dirname(__file__))

from pathlib import Path
from config import BiliConfig
from classifier import enrich_video_from_csv
from organizer import BiliOrganizer
from models import BiliVideo, BiliUP
from bucket_config import BUCKET_NAMES


def _safe_int(val, default=0):
    if val is None or (isinstance(val, float) and (val != val)):
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def load_classified_videos(csv_path: str) -> dict:
    """从已分类的 CSV 加载视频，按 category 分组"""
    import pandas as pd
    df = pd.read_csv(csv_path, dtype={"up_mid": "Int64"})
    print(f"[加载] 从 {csv_path} 读取 {len(df)} 条已分类记录")

    classified = {}
    for _, row in df.iterrows():
        category = str(row.get("category", "") or "").strip()
        if not category:
            category = "tmp_待分类"

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
        v.category = category
        enrich_video_from_csv(v, row.to_dict())

        if category not in classified:
            classified[category] = []
        classified[category].append(v)

    total = sum(len(v) for v in classified.values())
    print(f"[加载] 共 {len(classified)} 个桶, {total} 条视频")
    return classified


def parse_args():
    mode = "auto"
    args = sys.argv[1:]
    for arg in args:
        arg_lower = arg.lower().strip("-")
        if arg_lower in ("auto", "add", "move", "copy"):
            mode = arg_lower
    return mode


async def main():
    mode = parse_args()
    mode_label = {"auto": "自动(move→add)", "add": "添加", "move": "移动", "copy": "复制"}[mode]

    print("=" * 60)
    print(f"  B站收藏夹整理 - 从已分类数据执行 ({mode_label}模式)")
    print("=" * 60)

    config = BiliConfig()

    # Step 1: 加载已分类数据
    csv_path = Path(config.OUTPUT_DIR) / "classified_sorted_videos.csv"
    if not csv_path.exists():
        print(f"[ERROR] 未找到 {csv_path}，请先运行 run_classify.py")
        return

    classified = load_classified_videos(str(csv_path))
    if not classified:
        print("[ERROR] 未加载到任何视频数据")
        return

    # Step 2: 执行实际整理
    print(f"\n{'=' * 60}")
    print(f"  Step 2: 执行实际整理 ({mode_label}模式)")
    print("=" * 60)

    organizer = BiliOrganizer(config)
    try:
        plan = await organizer.full_organize(
            classified,
            folder_prefix="",
            mode=mode,
            dry_run=False,
        )

        # 检查是否检测到未完成任务
        if plan.get("interrupted"):
            print("\n[WARN] 检测到未完成任务，尝试恢复...")
            summary = plan.get("resume_summary", {})
            print(f"  会话 ID: {summary.get('session_id', 'unknown')}")
            print(f"  总分类: {summary.get('total_categories', 0)}")
            print(f"  未完成: {summary.get('incomplete_categories', 0)}")

            # 直接恢复
            folder_map = {}
            folders = await organizer.crawler.get_created_folders()
            for cat in classified.keys():
                for f in folders:
                    if f.title == cat:
                        folder_map[cat] = f.id
                        break
                if cat not in folder_map:
                    for f in folders:
                        if f.title == cat or f.title.startswith(f"{cat}_"):
                            folder_map[cat] = f.id
                            break

            resume_result = await organizer.resume_organize(classified, folder_map)
            print(f"\n恢复完成！恢复分类: {len(resume_result.get('resumed_categories', []))}")
            print(f"处理视频: {resume_result.get('total_processed', 0)}")
        else:
            # 打印最终总结
            print(f"\n{'=' * 60}")
            print("  整理完成！结果汇总:")
            print("=" * 60)
            for cat, info in plan.get("categories", {}).items():
                print(f"  {cat}: {info['video_count']} 条 → 收藏夹 id={info['target_folder']}")

    except Exception as e:
        print(f"\n[ERROR] 整理过程出错: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await organizer.close()


if __name__ == "__main__":
    asyncio.run(main())
