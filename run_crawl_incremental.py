"""增量爬取脚本v2 - 极速版

核心策略：
1. IDs API（1次请求/收藏夹）获取每个收藏夹的 bvid 列表 → 无分页
2. 稍后再看 API（1次请求）获取列表
3. 对比已有 videos.csv：旧视频复用整行数据，只更新来源收藏夹
4. 新视频用 view API 获取完整信息（含 tags/tid）
5. 合并导出
"""
import asyncio
import sys
import os
import time
from pathlib import Path
from collections import Counter

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

sys.path.insert(0, os.path.dirname(__file__))

import pandas as pd
from config import BiliConfig
from crawler import BiliCrawler

OUTPUT_DIR = Path(__file__).parent / "output"
EXISTING_CSV = OUTPUT_DIR / "videos.csv"
DETAIL_DELAY = 1.5       # 视频详情API间隔秒数
DETAIL_BATCH_SIZE = 10   # 每批获取详情数
DETAIL_BATCH_PAUSE = 5   # 批间暂停秒数


def load_existing() -> dict:
    """加载已有CSV，返回 bvid → 行数据字典"""
    if not EXISTING_CSV.exists():
        return {}
    df = pd.read_csv(EXISTING_CSV, dtype=str)
    cache = {}
    for _, row in df.iterrows():
        bvid = str(row.get("bvid", "")).strip()
        if bvid:
            cache[bvid] = row.to_dict()
    print(f"[缓存] 已有 {len(cache)} 条视频数据")
    return cache


def detail_to_record(detail: dict, source_folder_id: str, source_folder_title: str) -> dict:
    """将 get_video_detail 返回的详情转为 CSV 行记录"""
    return {
        "bvid": detail.get("bvid", ""),
        "avid": str(detail.get("aid", "")),
        "title": detail.get("title", ""),
        "intro": detail.get("desc", ""),
        "up_mid": str(detail.get("owner_mid", "")),
        "up_name": detail.get("owner_name", ""),
        "duration_sec": str(detail.get("duration", "")),
        "page_count": str(detail.get("videos", "1")),
        "view_count": str(detail.get("stat_view", "")),
        "danmaku_count": str(detail.get("stat_danmaku", "")),
        "collect_count": str(detail.get("stat_favorite", "")),
        "pubtime": str(detail.get("pubdate", "")),
        "fav_time": "",  # view API 无收藏时间
        "is_valid": "True",
        "attr": "0",
        "source_folder_id": source_folder_id,
        "source_folder_title": source_folder_title,
        "url": f"https://www.bilibili.com/video/{detail.get('bvid', '')}",
        "tags": detail.get("tags", ""),
        "tid": str(detail.get("tid", "")),
        "tname": detail.get("tname", ""),
        "tid_v2": str(detail.get("tid_v2", "")),
        "tname_v2": detail.get("tname_v2", ""),
        "parent_tid": str(detail.get("tid_v2", "")),  # 用 tid_v2 作为 parent_tid 的近似
        "parent_name": "",
    }


async def main():
    config = BiliConfig()
    crawler = BiliCrawler(config)
    existing = load_existing()

    # 收集数据
    bvid_folders: dict[str, list[str]] = {}   # bvid → [folder_title, ...]
    folder_id_map: dict[str, str] = {}         # folder_title → folder_id(str)
    new_bvids: list[str] = []
    all_bvids: set[str] = set()

    try:
        # === 1. 获取收藏夹列表 ===
        print("\n=== 1. 获取收藏夹列表 ===")
        all_folders = await crawler.get_created_folders()
        tmp_folders = [f for f in all_folders if f.title.lower().startswith(config.FOLDER_PREFIX.lower())]
        print(f"找到 {len(tmp_folders)} 个 tmp_* 收藏夹:")
        for f in tmp_folders:
            print(f"  {f.title:30s} ({f.media_count:5d} 条)")
            folder_id_map[f.title] = str(f.id)

        # === 2. IDs API 获取每个收藏夹的视频列表 ===
        print("\n=== 2. 获取各收藏夹视频列表 (IDs API, 无分页) ===")
        for folder in tmp_folders:
            try:
                ids = await crawler.get_folder_video_ids(folder.id)
                if ids is None:
                    print(f"  ✗ {folder.title}: API 调用失败，跳过")
                    continue
                bvids_in_folder = [item.get("bvid", "") for item in ids if item.get("bvid")]
                new_count = sum(1 for b in bvids_in_folder if b not in existing)
                print(f"  ✓ {folder.title}: {len(bvids_in_folder)} 条 (新: {new_count})")

                for bvid in bvids_in_folder:
                    bvid_folders.setdefault(bvid, []).append(folder.title)
                    all_bvids.add(bvid)
                    if bvid not in existing:
                        new_bvids.append(bvid)
            except Exception as e:
                print(f"  ✗ {folder.title}: {e}")

        # === 3. 获取稍后再看 ===
        print("\n=== 3. 获取稍后再看 ===")
        try:
            wl_videos = await crawler.get_watchlater()
            wl_new = 0
            for v in wl_videos:
                if v.bvid:
                    bvid_folders.setdefault(v.bvid, []).append("稍后再看")
                    all_bvids.add(v.bvid)
                    if v.bvid not in existing:
                        new_bvids.append(v.bvid)
                        wl_new += 1
            print(f"  ✓ 稍后再看: {len(wl_videos)} 条 (新: {wl_new})")
        except Exception as e:
            print(f"  ✗ 稍后再看: {e}")

        # === 4. 去重统计 ===
        new_bvids = list(dict.fromkeys(new_bvids))  # 去重保序
        print(f"\n=== 4. 统计 ===")
        print(f"去重后总视频: {len(all_bvids)} 条")
        print(f"已有缓存: {len(all_bvids) - len(new_bvids)} 条")
        print(f"新增视频: {len(new_bvids)} 条")

        if not all_bvids:
            print("无数据，退出")
            return

        # === 5. 对新增视频获取详情 ===
        new_details: dict[str, dict] = {}
        if new_bvids:
            print(f"\n=== 5. 补充新视频详情 ({len(new_bvids)} 条) ===")
            enriched = 0
            for batch_start in range(0, len(new_bvids), DETAIL_BATCH_SIZE):
                batch = new_bvids[batch_start:batch_start + DETAIL_BATCH_SIZE]
                print(f"  批次 {batch_start // DETAIL_BATCH_SIZE + 1}/{(len(new_bvids) + DETAIL_BATCH_SIZE - 1) // DETAIL_BATCH_SIZE}: {len(batch)} 条")

                for bvid in batch:
                    try:
                        detail = await crawler.get_video_detail(bvid)
                        if detail:
                            new_details[bvid] = detail
                            enriched += 1
                        await asyncio.sleep(DETAIL_DELAY)
                    except Exception as e:
                        print(f"    [WARN] {bvid}: {e}")
                        await asyncio.sleep(DETAIL_DELAY)

                if batch_start + DETAIL_BATCH_SIZE < len(new_bvids):
                    print(f"    暂停 {DETAIL_BATCH_PAUSE}s...")
                    await asyncio.sleep(DETAIL_BATCH_PAUSE)

            print(f"  ✓ 详情获取: {enriched}/{len(new_bvids)}")
        else:
            print("\n无新增视频，跳过详情获取")

        # === 6. 构建最终 CSV ===
        print("\n=== 6. 构建数据集 ===")
        records = []

        for bvid in all_bvids:
            if bvid in existing:
                # 旧视频：复用缓存数据，更新来源收藏夹
                rec = dict(existing[bvid])
                folders = bvid_folders.get(bvid, [])
                non_wl = [f for f in folders if f != "稍后再看"]
                src = non_wl[0] if non_wl else (folders[0] if folders else rec.get("source_folder_title", ""))
                rec["source_folder_title"] = src
                if src in folder_id_map:
                    rec["source_folder_id"] = folder_id_map[src]
                records.append(rec)
            elif bvid in new_details:
                # 新视频：从详情构建记录
                folders = bvid_folders.get(bvid, [])
                non_wl = [f for f in folders if f != "稍后再看"]
                src = non_wl[0] if non_wl else (folders[0] if folders else "稍后再看")
                src_id = folder_id_map.get(src, "")
                rec = detail_to_record(new_details[bvid], src_id, src)
                records.append(rec)
            # 如果既不在缓存也不在详情中（理论上不应该），跳过

        # === 7. 导出 ===
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        df = pd.DataFrame(records)
        csv_path = OUTPUT_DIR / "videos.csv"
        df.to_csv(csv_path, index=False, encoding="utf-8-sig")
        print(f"  ✓ 导出 {len(records)} 条到 {csv_path}")

        # 来源分布
        source_counter = Counter(r.get("source_folder_title", "") for r in records)
        print(f"\n=== 按来源分布 ===")
        for source, count in source_counter.most_common():
            print(f"  {source}: {count} 条")

        print(f"\n=== 完成！ ===")

    except Exception as e:
        print(f"\n[ERROR] {e}")
        import traceback
        traceback.print_exc()
    finally:
        await crawler.close()


if __name__ == "__main__":
    asyncio.run(main())
