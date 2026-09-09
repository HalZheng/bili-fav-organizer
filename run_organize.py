"""执行实际整理操作 - 从CSV加载 → 分类 → 排序 → 创建收藏夹 → 添加视频

操作模式:
  auto - 自动模式：优先move，失败自动回退add（默认，推荐）
  add  - 仅添加视频到目标收藏夹
  move - 仅从源收藏夹移动到目标收藏夹
  copy - 仅从源收藏夹复制到目标收藏夹
"""

import sys
import os
import time
import asyncio

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

sys.path.insert(0, os.path.dirname(__file__))

from config import BiliConfig
from classifier import FunnelClassifier, enrich_video_from_csv
from organizer import BiliOrganizer
from models import BiliVideo, BiliUP
from bucket_config import BUCKET_NAMES
from pathlib import Path


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
        enrich_video_from_csv(v, row.to_dict())
        if row.get("category") and str(row.get("category", "")).strip():
            v.category = str(row.get("category", "")).strip()
        videos.append(v)

    valid_count = sum(1 for v in videos if v.is_valid)
    print(f"[加载] 有效视频: {valid_count} 条 | 失效: {len(videos) - valid_count} 条")
    return videos


def parse_args():
    """解析命令行参数
    
    支持的参数:
        mode: auto / add / move / copy (位置参数，可选)
        --resume: 直接恢复未完成任务（不询问）
        --fresh: 直接重新开始（不询问，归档旧状态）
        --incremental: 增量整理模式（重新爬取稍后再看 → 分类 → 增量整理）
        --no-reorder: 仅添加新视频不重排（与 --incremental 配合使用）
    """
    mode = "auto"  # 默认使用 auto 模式（优先move，失败回退add）
    resume_flag = False
    fresh_flag = False
    incremental_flag = False
    no_reorder_flag = False

    args = sys.argv[1:]
    for arg in args:
        arg_lower = arg.lower().strip("-")
        if arg_lower in ("auto", "add", "move", "copy"):
            mode = arg_lower
        elif arg_lower == "resume":
            resume_flag = True
        elif arg_lower == "fresh":
            fresh_flag = True
        elif arg_lower == "incremental":
            incremental_flag = True
        elif arg_lower in ("no-reorder", "noreorder"):
            no_reorder_flag = True
        else:
            print(f"[WARN] 未知参数 '{arg}'，支持: auto/add/move/copy --resume --fresh --incremental --no-reorder")

    return mode, resume_flag, fresh_flag, incremental_flag, no_reorder_flag


def _show_resume_summary(summary: dict):
    """展示恢复摘要（纯文本版本，不用 rich）"""
    print("\n" + "=" * 60)
    print("  ⚠ 检测到未完成的移动任务")
    print("=" * 60)
    print(f"  会话 ID: {summary.get('session_id', 'unknown')}")
    print(f"  创建时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(summary.get('created_at', 0)))}")
    print(f"  操作模式: {summary.get('mode', 'auto')}")
    print(f"  总分类: {summary.get('total_categories', 0)} | "
          f"已完成: {summary.get('completed_categories', 0)} | "
          f"未完成: {summary.get('incomplete_categories', 0)}")
    print(f"  总视频: {summary.get('total_videos', 0)} | "
          f"已完成: {summary.get('completed_videos', 0)} | "
          f"待处理: {summary.get('pending_videos', 0)} | "
          f"失败: {summary.get('failed_videos', 0)}")

    if summary.get("source_csv_changed"):
        print("  ⚠ 源数据文件已变化，恢复可能导致分类结果不一致")

    categories = summary.get("categories", [])
    if categories:
        print("\n  未完成分类进度:")
        print(f"  {'分类':<20} {'状态':<12} {'总数':>6} {'已完成':>6} {'待处理':>6} {'失败':>6}")
        print(f"  {'-'*20} {'-'*12} {'-'*6} {'-'*6} {'-'*6} {'-'*6}")
        for cat in categories:
            print(f"  {cat.get('name', ''):<20} {cat.get('status', ''):<12} "
                  f"{cat.get('total', 0):>6} {cat.get('completed', 0):>6} "
                  f"{cat.get('pending', 0):>6} {cat.get('failed', 0):>6}")


def _print_final_summary(plan: dict):
    """打印最终总结"""
    print(f"\n{'=' * 60}")
    print("  整理完成！结果汇总:")
    print("=" * 60)
    for cat, info in plan.get("categories", {}).items():
        blocks = info.get("blocks", {})
        print(
            f"  {cat}: {info['video_count']} 条 "
            f"(Top={blocks.get('top', 0)} | Main={blocks.get('main', 0)} | Bottom={blocks.get('bottom', 0)}) "
            f"→ 收藏夹 id={info['target_folder']}"
        )


async def _rebuild_folder_map(organizer, classified_videos: dict) -> dict:
    """从状态文件或已创建的收藏夹列表重建 folder_map

    优先从状态文件读取 target_folder_id（避免 API 调用），
    若状态文件不可用则回退到 get_created_folders API。
    """
    folder_map = {}

    # 优先从状态文件读取 target_folder_id
    try:
        state = await organizer.state_manager.load_state()
        if state and state.categories:
            for cat_name, cat_state in state.categories.items():
                if cat_state.target_folder_id:
                    folder_map[cat_name] = cat_state.target_folder_id
            if folder_map:
                print(f"  [恢复] 从状态文件加载 folder_map: {len(folder_map)} 个分类")
                return folder_map
    except Exception as e:
        print(f"  [恢复] 从状态文件加载 folder_map 失败: {e}，回退到 API 查询")

    # 回退：调用 API 获取收藏夹列表
    folders = await organizer.crawler.get_created_folders()
    for cat in classified_videos.keys():
        for f in folders:
            if f.title == cat:
                folder_map[cat] = f.id
                break
        if cat not in folder_map:
            for f in folders:
                if f.title == cat or f.title.startswith(f"{cat}_"):
                    folder_map[cat] = f.id
                    break
    return folder_map


async def crawl_watchlater_fresh(organizer) -> list[BiliVideo]:
    """重新爬取稍后再看列表，对新视频获取详情，覆盖 videos.csv
    
    Returns:
        稍后再看列表的视频列表（BiliVideo）
    """
    import pandas as pd

    DETAIL_DELAY = 1.5  # 视频详情API间隔秒数，避免412

    # Step 1: 获取稍后再看列表
    print("[爬取] 获取稍后再看列表...")
    videos = await organizer.crawler.get_watchlater()
    if not videos:
        print("[爬取] 稍后再看列表为空")
        return []
    print(f"[爬取] 稍后再看列表: {len(videos)} 条")

    # Step 2: 对每个视频获取详情（含 tid/tags）
    # 稍后再看 API 返回的视频缺少 tid/tags，必须调用 get_video_detail 补充
    # 参考 run_crawl_incremental.py 的 detail_to_record 实现
    print(f"[爬取] 获取视频详情（间隔 {DETAIL_DELAY}s/条）...")
    records = []
    enriched = 0
    total = len(videos)
    for i, v in enumerate(videos, 1):
        if not v.bvid:
            continue
        try:
            detail = await organizer.crawler.get_video_detail(v.bvid)
            if detail:
                # 构造 CSV 记录（含 tags/tid 等字段，enrich_video_from_csv 需要这些字段）
                record = {
                    "bvid": detail.get("bvid", v.bvid),
                    "avid": str(detail.get("aid", v.id)),
                    "title": detail.get("title", v.title),
                    "intro": detail.get("desc", v.intro),
                    "up_mid": str(detail.get("owner_mid", v.upper.mid)),
                    "up_name": detail.get("owner_name", v.upper.name),
                    "duration_sec": str(detail.get("duration", v.duration)),
                    "page_count": str(detail.get("videos", v.page)),
                    "view_count": str(detail.get("stat_view", v.view_count)),
                    "danmaku_count": str(detail.get("stat_danmaku", v.danmaku_count)),
                    "collect_count": str(detail.get("stat_favorite", v.collect_count)),
                    "pubtime": str(detail.get("pubdate", v.pubtime)),
                    "fav_time": str(v.fav_time),
                    "is_valid": "True",
                    "attr": "0",
                    "source_folder_id": str(v.source_folder_id),
                    "source_folder_title": v.source_folder_title,
                    "url": f"https://www.bilibili.com/video/{v.bvid}",
                    "tags": detail.get("tags", ""),
                    "tid": str(detail.get("tid", "")),
                    "tname": detail.get("tname", ""),
                    "tid_v2": str(detail.get("tid_v2", "")),
                    "tname_v2": detail.get("tname_v2", ""),
                    "parent_tid": str(detail.get("tid_v2", "")),  # 用 tid_v2 作为 parent_tid 的近似
                    "parent_name": "",
                }
                records.append(record)
                enriched += 1
            else:
                # 详情获取失败，用基础信息构造记录（tags/tid 为空）
                record = {
                    "bvid": v.bvid,
                    "avid": str(v.id),
                    "title": v.title,
                    "intro": v.intro,
                    "up_mid": str(v.upper.mid),
                    "up_name": v.upper.name,
                    "duration_sec": str(v.duration),
                    "page_count": str(v.page),
                    "view_count": str(v.view_count),
                    "danmaku_count": str(v.danmaku_count),
                    "collect_count": str(v.collect_count),
                    "pubtime": str(v.pubtime),
                    "fav_time": str(v.fav_time),
                    "is_valid": "True",
                    "attr": "0",
                    "source_folder_id": str(v.source_folder_id),
                    "source_folder_title": v.source_folder_title,
                    "url": f"https://www.bilibili.com/video/{v.bvid}",
                    "tags": "",
                    "tid": "0",
                    "tname": "",
                    "tid_v2": "0",
                    "tname_v2": "",
                    "parent_tid": "0",
                    "parent_name": "",
                }
                records.append(record)
                print(f"  [WARN] {v.bvid} 详情获取失败，使用基础信息")

            if i % 10 == 0:
                print(f"  进度: {i}/{total}")
            await asyncio.sleep(DETAIL_DELAY)
        except Exception as e:
            print(f"  [WARN] {v.bvid}: {e}")
            await asyncio.sleep(DETAIL_DELAY)

    print(f"[爬取] 详情获取完成: {enriched}/{total}")

    # Step 3: 导出 CSV（覆盖）
    csv_path = Path(organizer.config.OUTPUT_DIR) / "videos.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(records)
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    print(f"[爬取] 已导出 {len(records)} 条到 {csv_path}")

    # Step 4: 重新从 CSV 加载（这样 enrich_video_from_csv 会正确处理 tags/tid 等字段）
    reloaded = load_videos_from_csv(str(csv_path))
    return reloaded


async def main():
    mode, resume_flag, fresh_flag, incremental_flag, no_reorder_flag = parse_args()

    # 互斥检查
    if incremental_flag and fresh_flag:
        print("[ERROR] --incremental 与 --fresh 互斥，请单独使用")
        return

    if incremental_flag:
        await main_incremental(no_reorder_flag)
        return

    mode_label = {"auto": "自动(move→add)", "add": "添加", "move": "移动", "copy": "复制"}[mode]

    print("=" * 60)
    print(f"  B站收藏夹整理 - 执行实际操作 ({mode_label}模式)")
    print("=" * 60)

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

    organizer = BiliOrganizer(config)
    try:
        for bucket_name in BUCKET_NAMES:
            if bucket_name in classified:
                sorted_videos = await organizer.sort_folder_by_up_and_time(
                    classified[bucket_name]
                )
                classified[bucket_name] = sorted_videos

        # Step 4: 执行实际整理
        print(f"\n{'=' * 60}")
        print(f"  Step 4: 执行实际整理 ({mode_label}模式)")
        print("=" * 60)

        # --fresh: 在 full_organize 调用前归档旧状态，避免触发中断检测
        if fresh_flag:
            await organizer.state_manager.archive_state()

        plan = await organizer.full_organize(
            classified,
            folder_prefix="",
            mode=mode,
            dry_run=False,
        )

        # 检查是否检测到未完成任务
        if plan.get("interrupted"):
            summary = plan.get("resume_summary", {})
            _show_resume_summary(summary)

            # 决定用户选择
            if resume_flag:
                choice = "1"  # 直接恢复
            elif fresh_flag:
                # fresh_flag 已归档旧状态，理论上不应进入此分支；保险起见退出
                choice = "3"
            else:
                # 交互询问
                print("\n选择操作:")
                print("  [1] 恢复未完成的任务")
                print("  [2] 重新开始")
                print("  [3] 退出")
                choice = input("请输入选择 (1/2/3, 默认1): ").strip() or "1"

            if choice == "1":
                # 恢复：重建 folder_map
                folder_map = await _rebuild_folder_map(organizer, classified)
                resume_result = await organizer.resume_organize(classified, folder_map)
                print(f"\n恢复完成！恢复分类: {len(resume_result.get('resumed_categories', []))}, "
                      f"处理视频: {resume_result.get('total_processed', 0)}")
                if resume_result.get("reorder_candidates"):
                    print(f"⚠ 以下分类顺序可能需要检查: {resume_result['reorder_candidates']}")
            elif choice == "2":
                # 重新开始：归档旧状态
                await organizer.state_manager.archive_state()
                plan = await organizer.full_organize(
                    classified,
                    folder_prefix="",
                    mode=mode,
                    dry_run=False,
                )
                _print_final_summary(plan)
            else:
                print("已取消")
        else:
            _print_final_summary(plan)
    finally:
        await organizer.close()


async def main_incremental(no_reorder: bool):
    """增量整理主流程

    流程：
      1. 清理残留临时桶
      2. 重新爬取稍后再看列表（覆盖 videos.csv）
      3. 三层漏斗分类
      4. 重建 folder_map（从已创建的收藏夹列表）
      5. 增量整理（合并排序 + 全桶重排，或仅添加不重排）
    """
    print("=" * 60)
    print(f"  B站收藏夹整理 - 增量模式{'（仅添加不重排）' if no_reorder else '（全桶重排）'}")
    print("=" * 60)

    config = BiliConfig()
    organizer = BiliOrganizer(config)
    try:
        # Step 1: 清理残留临时桶
        print(f"\n{'=' * 60}")
        print("  Step 1: 检查残留临时桶")
        print("=" * 60)
        await organizer.cleanup_temp_folders()

        # Step 2: 重新爬取稍后再看列表
        print(f"\n{'=' * 60}")
        print("  Step 2: 重新爬取稍后再看列表")
        print("=" * 60)
        videos = await crawl_watchlater_fresh(organizer)
        if not videos:
            print("[INFO] 稍后再看列表为空，无需整理")
            return

        # Step 3: 三层漏斗分类
        print(f"\n{'=' * 60}")
        print("  Step 3: 三层漏斗分流")
        print("=" * 60)
        funnel = FunnelClassifier(config)
        classified = funnel.classify_all(videos)

        # Step 4: 重建 folder_map（从已创建的收藏夹列表）
        print(f"\n{'=' * 60}")
        print("  Step 4: 获取目标桶映射")
        print("=" * 60)
        folder_map = await _rebuild_folder_map(organizer, classified)

        # Step 5: 增量整理
        print(f"\n{'=' * 60}")
        print(f"  Step 5: 增量整理{'（仅添加）' if no_reorder else '（全桶重排）'}")
        print("=" * 60)
        result = await organizer.incremental_organize(
            classified, folder_map, reorder=not no_reorder
        )

        # 打印报告
        print(f"\n{'=' * 60}")
        print("  增量整理完成！结果汇总:")
        print("=" * 60)
        for cat, info in result.get("categories", {}).items():
            print(f"  {cat}: {info.get('status', 'unknown')}")
    finally:
        await organizer.close()


if __name__ == "__main__":
    asyncio.run(main())
