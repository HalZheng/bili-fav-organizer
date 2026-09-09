"""补充爬取稍后再看列表 + 详情 + tags，追加到 videos.csv

背景:
  run_crawl.py 重写时遗漏了爬取稍后再看列表。
  本脚本单独爬取稍后再看视频，补充详情和 tags，追加到现有 videos.csv。

断点续爬:
  通过检查 videos.csv 中 source_folder_title="稍后再看" 的 bvid 跳过已获取的视频。
"""
import asyncio
import sys
import os
import time

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

sys.path.insert(0, os.path.dirname(__file__))

from pathlib import Path
from config import BiliConfig
from crawler import BiliCrawler
from models import BiliVideo


# 详情API间隔秒数（避免412）
DETAIL_DELAY = 0.5
TAGS_DELAY = 0.5


def build_record(video: BiliVideo, detail: dict) -> dict:
    """合并列表信息和详情信息，构造完整 record"""
    return {
        "bvid": video.bvid,
        "avid": str(video.id),
        "title": detail.get("title", video.title),
        "intro": detail.get("desc", video.intro),
        "up_mid": str(detail.get("owner_mid", video.upper.mid)),
        "up_name": detail.get("owner_name", video.upper.name),
        "duration_sec": str(detail.get("duration", video.duration)),
        "page_count": str(detail.get("videos", video.page)),
        "view_count": str(detail.get("stat_view", video.view_count)),
        "danmaku_count": str(detail.get("stat_danmaku", video.danmaku_count)),
        "collect_count": str(detail.get("stat_favorite", video.collect_count)),
        "pubtime": str(detail.get("pubdate", video.pubtime)),
        "fav_time": str(video.fav_time),
        "is_valid": "True" if video.is_valid else "False",
        "attr": str(video.attr),
        "source_folder_id": "0",
        "source_folder_title": "稍后再看",
        "url": f"https://www.bilibili.com/video/{video.bvid}",
        "tags": detail.get("tags", ""),
        "tid": str(detail.get("tid", "")),
        "tname": detail.get("tname", ""),
        "tid_v2": str(detail.get("tid_v2", "")),
        "tname_v2": detail.get("tname_v2", ""),
        "parent_tid": str(detail.get("tid_v2", "")),
        "parent_name": "",
    }


async def main():
    config = BiliConfig()
    crawler = BiliCrawler(config)

    csv_path = Path(config.OUTPUT_DIR) / "videos.csv"
    if not csv_path.exists():
        print(f"[ERROR] CSV 不存在: {csv_path}")
        return

    import pandas as pd

    # 读取已有 CSV，获取已有 bvid（全表去重，避免已在收藏夹的视频被重复追加）
    print(f"[加载] 读取 {csv_path}")
    df_existing = pd.read_csv(csv_path, dtype={"up_mid": "Int64"})
    all_existing_bvids = set(
        str(bvid) for bvid in df_existing["bvid"]
        if str(bvid) and str(bvid) != "nan"
    )
    # 稍后再看来源的行数仅作统计展示
    wl_existing = set(
        str(bvid) for bvid in df_existing[df_existing["source_folder_title"] == "稍后再看"]["bvid"]
        if str(bvid) and str(bvid) != "nan"
    )
    print(f"[续爬] CSV 已有 {len(all_existing_bvids)} 条，其中稍后再看来源 {len(wl_existing)} 条")

    # Step 1: 获取稍后再看列表
    print(f"\n=== Step 1: 获取稍后再看列表 ===")
    watchlater_videos = await crawler.get_watchlater()
    print(f"  稍后再看列表共 {len(watchlater_videos)} 条")

    # 过滤已爬取的（按全表 bvid 去重；已在收藏夹的视频保留其收藏夹源记录，
    # 整理后由独立的稍后再看清理流程处理其稍后再看残留）
    need_crawl = [v for v in watchlater_videos if v.bvid not in all_existing_bvids]
    overlap = len(watchlater_videos) - len(need_crawl)
    print(f"  需补充详情: {len(need_crawl)} 条（另有 {overlap} 条已在收藏夹，跳过）")

    if not need_crawl:
        print("[完成] 所有稍后再看视频已爬取，无需补充")
        return

    print(f"  预计耗时: {len(need_crawl) * (DETAIL_DELAY + TAGS_DELAY) / 60:.1f} 分钟")

    # Step 2: 逐个补充详情 + tags
    print(f"\n=== Step 2: 补充详情 + tags (间隔 {DETAIL_DELAY}s/条) ===")
    new_records = []
    enriched = 0
    failed = 0
    total = len(need_crawl)
    start_time = time.time()

    for i, video in enumerate(need_crawl, 1):
        try:
            # 获取详情（含 tags，get_video_detail 已封装 _fetch_video_tags）
            detail = await crawler.get_video_detail(video.bvid)
            if detail:
                record = build_record(video, detail)
                new_records.append(record)
                enriched += 1
            else:
                record = build_record(video, {})
                new_records.append(record)
                failed += 1
                print(f"  [WARN] {video.bvid} 详情获取失败，使用基础信息")
        except Exception as e:
            record = build_record(video, {})
            new_records.append(record)
            failed += 1
            if "412" in str(e) or "风控" in str(e):
                print(f"  [412] 触发风控，等待 60s 后继续...")
                await asyncio.sleep(60)
            else:
                print(f"  [WARN] {video.bvid}: {e}")

        # 进度显示
        if i % 10 == 0 or i == total:
            elapsed = time.time() - start_time
            rate = i / elapsed if elapsed > 0 else 0
            eta = (total - i) / rate if rate > 0 else 0
            print(f"  进度: {i}/{total} (成功 {enriched} 失败 {failed}) "
                  f"速率 {rate:.1f}条/s 预计剩余 {eta/60:.1f} 分钟")

        # 详情 API 已包含 tags 获取，无需单独调用
        await asyncio.sleep(DETAIL_DELAY)

    # Step 3: 追加到 CSV
    print(f"\n=== Step 3: 追加到 {csv_path} ===")
    df_new = pd.DataFrame(new_records)
    df_combined = pd.concat([df_existing, df_new], ignore_index=True)
    df_combined.to_csv(csv_path, index=False, encoding="utf-8-sig")
    print(f"  [OK] 已追加 {len(new_records)} 条，CSV 总行数: {len(df_combined)}")

    # 覆盖率检查
    total_rows = len(df_combined)
    wl_rows = df_combined[df_combined["source_folder_title"] == "稍后再看"]
    has_tid = sum(1 for _, r in wl_rows.iterrows() if str(r.get("tid", "")).strip() and str(r.get("tid", "")) != "0")
    has_tags = sum(1 for _, r in wl_rows.iterrows() if str(r.get("tags", "")).strip())
    print(f"  稍后再看视频: {len(wl_rows)} 条")
    print(f"  tid 覆盖率: {has_tid}/{len(wl_rows)} ({has_tid/max(len(wl_rows),1)*100:.1f}%)")
    print(f"  tags 覆盖率: {has_tags}/{len(wl_rows)} ({has_tags/max(len(wl_rows),1)*100:.1f}%)")

    await crawler.close()


if __name__ == "__main__":
    asyncio.run(main())
