"""一次性爬取脚本 - 顺序爬取收藏夹视频列表 + 补充视频详情(tid/tags)

支持断点续爬：
  - 收藏夹列表爬取：通过 SKIP_FOLDER_IDS 跳过已完成的收藏夹
  - 视频详情补充：通过检查已有 videos.csv 中的 bvid 跳过已获取详情的视频

数据来源：
  1. 收藏夹列表 API (/x/v3/fav/resource/list) → 基础信息(标题/UP主/时间等)
  2. 视频详情 API (/x/web-interface/view) → tid/tname/tags 等分区标签信息
"""
import asyncio
import json
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


# 已经爬完的收藏夹ID（如果中途失败，把已完成的加到这里跳过）
SKIP_FOLDER_IDS = set()

# 视频详情API间隔秒数（避免412）
DETAIL_DELAY = 0.5

# 每 N 条保存一次CSV（断点续爬保护）
SAVE_INTERVAL = 50


def load_existing_records(csv_path: str) -> dict:
    """从已有的 videos.csv 加载记录，返回 {bvid: record_dict} 用于断点续爬"""
    if not Path(csv_path).exists():
        return {}
    import pandas as pd
    try:
        df = pd.read_csv(csv_path, dtype={"up_mid": "Int64"})
        records = {}
        for _, row in df.iterrows():
            bvid = str(row.get("bvid", "") or "")
            if bvid:
                records[bvid] = row.to_dict()
        print(f"[续爬] 从已有 CSV 加载 {len(records)} 条记录（将跳过详情获取）")
        return records
    except Exception as e:
        print(f"[续爬] 加载已有 CSV 失败: {e}，将从头开始")
        return {}


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
        "source_folder_id": str(video.source_folder_id),
        "source_folder_title": video.source_folder_title,
        "url": f"https://www.bilibili.com/video/{video.bvid}",
        "tags": detail.get("tags", ""),
        "tid": str(detail.get("tid", "")),
        "tname": detail.get("tname", ""),
        "tid_v2": str(detail.get("tid_v2", "")),
        "tname_v2": detail.get("tname_v2", ""),
        "parent_tid": str(detail.get("tid_v2", "")),
        "parent_name": "",
    }


def save_records_to_csv(records: list, csv_path: str):
    """保存 records 到 CSV"""
    import pandas as pd
    df = pd.DataFrame(records)
    csv_path_obj = Path(csv_path)
    csv_path_obj.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(csv_path_obj, index=False, encoding="utf-8-sig")


async def main():
    config = BiliConfig()
    crawler = BiliCrawler(config)

    csv_path = Path(config.OUTPUT_DIR) / "videos.csv"

    try:
        # === Step 1: 获取tmp收藏夹列表 ===
        print("\n=== Step 1: 获取tmp收藏夹列表 ===")
        tmp_folders = await crawler.get_tmp_folders()
        print(f"找到 {len(tmp_folders)} 个tmp收藏夹:")
        total_expected = 0
        for f in tmp_folders:
            skip = " [SKIP]" if f.id in SKIP_FOLDER_IDS else ""
            print(f"  {f.title} ({f.media_count} 条){skip}")
            if f.id not in SKIP_FOLDER_IDS:
                total_expected += f.media_count
        print(f"预计需爬取 {total_expected} 条视频")

        # === Step 2: 逐个爬取收藏夹视频列表 ===
        print(f"\n=== Step 2: 爬取收藏夹视频列表 ===")
        all_videos = []
        for i, folder in enumerate(tmp_folders):
            if folder.id in SKIP_FOLDER_IDS:
                print(f"\n--- 跳过: {folder.title} ---")
                continue

            print(f"\n--- [{i+1}/{len(tmp_folders)}] 爬取: {folder.title} ({folder.media_count}条) ---")
            try:
                videos = await crawler.get_folder_videos(folder.id)
                folder.videos = videos
                all_videos.extend(videos)
                print(f"  ✓ 完成: {len(videos)} 条")
            except Exception as e:
                print(f"  ✗ 失败: {e}")
                print(f"  已爬取 {len(all_videos)} 条列表数据")
                break

        if not all_videos:
            print("未爬取到任何数据")
            return

        valid_count = sum(1 for v in all_videos if v.is_valid)
        print(f"\n列表爬取完成: 共 {len(all_videos)} 条 (有效 {valid_count} 条)")

        # === Step 3: 补充视频详情(tid/tags) ===
        print(f"\n=== Step 3: 补充视频详情 (间隔 {DETAIL_DELAY}s/条) ===")

        # 断点续爬：加载已有记录
        existing_records = load_existing_records(str(csv_path))
        existing_bvids = set(existing_records.keys())

        # 统计需要获取详情的视频
        need_detail = [v for v in all_videos if v.bvid and v.bvid not in existing_bvids]
        already_done = len(all_videos) - len(need_detail)
        print(f"  已有详情: {already_done} 条 | 需获取: {len(need_detail)} 条")
        print(f"  预计耗时: {len(need_detail) * DETAIL_DELAY / 60:.1f} 分钟")

        # 合并已有记录和新记录
        all_records = []
        # 先添加已有记录（保持顺序）
        # 注意：source_folder_id / source_folder_title / fav_time / is_valid
        # 以本次列表爬取的实时归属为准，避免旧记录的过期源信息
        # （例如视频已从稍后再看落入某桶，但旧记录仍标记为稍后再看）
        for v in all_videos:
            if v.bvid in existing_records:
                rec = dict(existing_records[v.bvid])
                rec["source_folder_id"] = str(v.source_folder_id)
                rec["source_folder_title"] = v.source_folder_title
                rec["fav_time"] = str(v.fav_time)
                rec["is_valid"] = "True" if v.is_valid else "False"
                all_records.append(rec)

        # 逐个获取详情
        enriched = 0
        failed = 0
        total = len(need_detail)
        start_time = time.time()

        for i, video in enumerate(need_detail, 1):
            if not video.bvid:
                continue
            try:
                detail = await crawler.get_video_detail(video.bvid)
                if detail:
                    record = build_record(video, detail)
                    all_records.append(record)
                    enriched += 1
                else:
                    # 详情获取失败，用列表信息构造空详情记录
                    record = build_record(video, {})
                    all_records.append(record)
                    failed += 1
                    print(f"  [WARN] {video.bvid} 详情获取失败，使用基础信息")
            except Exception as e:
                # 异常时用列表信息构造空详情记录
                record = build_record(video, {})
                all_records.append(record)
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

            # 定期保存（断点续爬保护）
            if i % SAVE_INTERVAL == 0:
                save_records_to_csv(all_records, str(csv_path))
                print(f"  [保存] 已保存 {len(all_records)} 条到 videos.csv")

            await asyncio.sleep(DETAIL_DELAY)

        # === Step 4: 导出最终数据 ===
        print(f"\n=== Step 4: 导出数据 ===")
        save_records_to_csv(all_records, str(csv_path))
        print(f"  [OK] 已导出 {len(all_records)} 条到 {csv_path}")

        # 导出收藏夹列表
        crawler.export_folders_json(tmp_folders)

        # 统计
        print(f"\n=== 爬取完成 ===")
        print(f"总计: {len(all_records)} 条")
        print(f"详情成功: {enriched} 条 | 失败: {failed} 条")

        # 检查 tid 覆盖率
        has_tid = sum(1 for r in all_records if str(r.get("tid", "")).strip() and str(r.get("tid", "")) != "0")
        has_tags = sum(1 for r in all_records if str(r.get("tags", "")).strip())
        print(f"tid 覆盖率: {has_tid}/{len(all_records)} ({has_tid/len(all_records)*100:.1f}%)")
        print(f"tags 覆盖率: {has_tags}/{len(all_records)} ({has_tags/len(all_records)*100:.1f}%)")

        # 分析
        all_videos_final = []
        from models import BiliUP
        from classifier import enrich_video_from_csv
        for r in all_records:
            v = BiliVideo(
                bvid=str(r.get("bvid", "")),
                id=int(r.get("avid", 0) or 0),
                title=str(r.get("title", "")),
                upper=BiliUP(
                    mid=int(r.get("up_mid", 0) or 0),
                    name=str(r.get("up_name", "")),
                ),
            )
            enrich_video_from_csv(v, r)
            all_videos_final.append(v)

        analysis = BiliCrawler.analyze_videos(all_videos_final)
        print(f"\n=== 数据分析 ===")
        print(f"UP主数量: {analysis['unique_ups']}")

        print(f"\nTOP 30 UP主:")
        for i, (up, count) in enumerate(analysis["up_ranking"][:30], 1):
            pct = count / max(analysis['total'], 1) * 100
            print(f"  {i:2d}. {up}: {count} ({pct:.1f}%)")

        print(f"\n收藏夹分布:")
        for folder, count in analysis["folder_distribution"]:
            print(f"  {folder}: {count}")

    except Exception as e:
        print(f"\n[ERROR] {e}")
        import traceback
        traceback.print_exc()
    finally:
        await crawler.close()


if __name__ == "__main__":
    asyncio.run(main())
