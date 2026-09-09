"""重新排序已有收藏夹内的视频

修复收藏夹内视频顺序，确保同一UP主的视频放在一起。

原理:
  1. 将目标收藏夹内所有视频移到临时收藏夹
  2. 对视频重新排序（UP主频次降序聚合）
  3. 按排序结果的逆序，逐个移回目标收藏夹
  4. 每次移动间隔1s，确保B站mtime不同
  5. 自动删除临时收藏夹

支持断点续排: 如果上次中断，会先恢复临时收藏夹中的残留视频。
"""

import sys
import os
import asyncio
import time

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

sys.path.insert(0, os.path.dirname(__file__))

from collections import Counter
from pathlib import Path

from config import BiliConfig
from crawler import BiliCrawler
from sorter import FolderSorter
from bucket_config import BUCKET_NAMES

TEMP_FOLDER_TITLE = "tmp_排序临时"


def sort_videos_for_folder(videos: list) -> list:
    """对视频列表重新排序: UP主频次降序 → 同UP主内收藏时间倒序

    这是全局排序，不区分Top/Main/Bottom区块，
    因为B站收藏夹内无法区分区块，统一按UP主聚合排序。
    """
    if not videos:
        return []

    up_counter = Counter(v.upper.mid for v in videos)
    up_by_freq = sorted(up_counter.keys(), key=lambda mid: up_counter[mid], reverse=True)

    result = []
    for up_mid in up_by_freq:
        up_videos = [v for v in videos if v.upper.mid == up_mid]
        up_videos.sort(key=lambda v: v.fav_time, reverse=True)
        result.extend(up_videos)

    return result


async def recover_temp_folder(crawler: BiliCrawler, temp_media_id: int, folder_name_to_id: dict):
    """恢复临时收藏夹中的残留视频到对应的目标收藏夹"""
    print(f"\n[恢复] 检查临时收藏夹 (id={temp_media_id}) 中的残留视频...")
    temp_videos = await crawler.get_folder_videos(temp_media_id)

    if not temp_videos:
        print("  临时收藏夹为空，无需恢复")
        return

    print(f"  发现 {len(temp_videos)} 条残留视频，正在恢复...")

    # 从各目标收藏夹中查找这些视频的归属
    # 简单策略：按收藏夹遍历，把视频移到第一个非临时收藏夹
    # 更好的策略：用CSV映射
    csv_path = Path(crawler.config.OUTPUT_DIR) / "classified_sorted_videos.csv"
    avid_to_category = {}
    if csv_path.exists():
        import pandas as pd
        df = pd.read_csv(csv_path)
        for _, row in df.iterrows():
            avid = int(row.get("avid", 0))
            cat = str(row.get("category", ""))
            if avid and cat:
                avid_to_category[avid] = cat

    by_target: dict[int, list] = {}
    unknown = []
    for v in temp_videos:
        cat = avid_to_category.get(v.id, "")
        target_id = folder_name_to_id.get(cat)
        if target_id:
            if target_id not in by_target:
                by_target[target_id] = []
            by_target[target_id].append(v)
        else:
            unknown.append(v)

    recovered = 0
    for target_id, vids in by_target.items():
        resources = [f"{v.id}:2" for v in vids]
        batch_size = 50
        for i in range(0, len(resources), batch_size):
            batch = resources[i:i + batch_size]
            success = await crawler.move_resources(temp_media_id, target_id, batch)
            if success:
                recovered += len(batch)
            else:
                print(f"  [ERROR] 恢复到 folder_id={target_id} 失败")
            await asyncio.sleep(1.0)

    if unknown:
        print(f"  [WARN] {len(unknown)} 条视频无法确定归属，留在临时收藏夹")

    print(f"  恢复完成: {recovered} 条视频已归位")


async def reorder_folder(
    crawler: BiliCrawler,
    bucket_name: str,
    target_media_id: int,
    temp_media_id: int,
):
    """重新排序一个收藏夹"""
    # 获取收藏夹内所有视频
    videos = await crawler.get_folder_videos(target_media_id)
    if not videos:
        print(f"  [{bucket_name}] 收藏夹为空，跳过")
        return

    total = len(videos)

    # 统计UP主聚合情况
    up_counter = Counter(v.upper.name for v in videos)
    top_ups = up_counter.most_common(5)
    print(
        f"  [{bucket_name}] {total} 条 | UP主数={len(up_counter)} | "
        f"TOP5: {', '.join(f'{n}({c})' for n, c in top_ups)}"
    )

    # 重新排序: UP主频次降序 → 同UP主内收藏时间倒序
    sorted_videos = sort_videos_for_folder(videos)

    # Step 1: 将所有视频移到临时收藏夹（批量）
    print(f"  [{bucket_name}] 移动到临时收藏夹...")
    resources = [f"{v.id}:2" for v in videos]
    batch_size = 50
    for i in range(0, len(resources), batch_size):
        batch = resources[i:i + batch_size]
        success = await crawler.move_resources(target_media_id, temp_media_id, batch)
        if not success:
            print(f"  [ERROR] 移动到临时收藏夹失败 (batch {i // batch_size + 1})，跳过此收藏夹")
            return
        await asyncio.sleep(1.0)

    # Step 2: 按逆序逐个移回
    # 排序结果 [v1, v2, v3] 中 v1 应排最前
    # 逆序移动 [v3, v2, v1]，v1 最后移动 → mtime 最新 → 排最前
    total_moved = 0
    total_errors = 0
    t_start = time.time()

    for v in reversed(sorted_videos):
        resources = [f"{v.id}:2"]
        success = await crawler.move_resources(temp_media_id, target_media_id, resources)

        if not success:
            total_errors += 1
            if total_errors <= 3:
                print(f"  [ERROR] 移回失败: {v.title[:30]} ({v.bvid})")
            elif total_errors == 4:
                print(f"  [ERROR] 过多失败，后续错误省略...")
        else:
            total_moved += 1

        await asyncio.sleep(1.0)

        if total_moved % 50 == 0:
            elapsed = time.time() - t_start
            rate = total_moved / elapsed if elapsed > 0 else 0
            eta = (total - total_moved - total_errors) / rate if rate > 0 else 0
            print(
                f"  [{bucket_name}] 进度: {total_moved}/{total} "
                f"(错误={total_errors}, {rate:.1f}条/s, 剩余 {eta/60:.1f} 分钟)"
            )

    elapsed = time.time() - t_start
    print(
        f"  [{bucket_name}] 完成: {total_moved}/{total} "
        f"(错误={total_errors}, 耗时 {elapsed/60:.1f} 分钟)"
    )


async def main():
    print("=" * 60)
    print("  B站收藏夹重排 - UP主聚合排序")
    print("=" * 60)

    config = BiliConfig()
    crawler = BiliCrawler(config)

    try:
        # 1. 获取当前收藏夹列表
        print("\n[查询] 获取收藏夹列表...")
        all_folders = await crawler.get_created_folders()
        folder_name_to_id = {f.title: f.id for f in all_folders}

        for bucket_name in BUCKET_NAMES:
            fid = folder_name_to_id.get(bucket_name, "未找到")
            print(f"  {bucket_name}: folder_id={fid}")

        # 2. 查找或创建临时收藏夹
        temp_media_id = folder_name_to_id.get(TEMP_FOLDER_TITLE)
        if temp_media_id:
            print(f"\n[复用] 找到已有临时收藏夹 id={temp_media_id}")
            await recover_temp_folder(crawler, temp_media_id, folder_name_to_id)
        else:
            print("\n[创建] 临时收藏夹...")
            temp_media_id = await crawler.create_folder(
                TEMP_FOLDER_TITLE, intro="排序用临时收藏夹，完成后自动删除"
            )
            if not temp_media_id:
                print("[ERROR] 创建临时收藏夹失败")
                return
            print(f"  临时收藏夹 id={temp_media_id}")

        # 3. 逐个收藏夹重排
        print(f"\n{'=' * 60}")
        print("  开始重排")
        print("=" * 60)

        for bucket_name in BUCKET_NAMES:
            target_media_id = folder_name_to_id.get(bucket_name)
            if not target_media_id:
                print(f"  [SKIP] 未找到收藏夹: {bucket_name}")
                continue

            await reorder_folder(crawler, bucket_name, target_media_id, temp_media_id)
            print()

        # 4. 清理临时收藏夹
        print("\n[清理] 删除临时收藏夹...")
        temp_videos = await crawler.get_folder_videos(temp_media_id)
        if temp_videos:
            print(f"  [WARN] 临时收藏夹仍有 {len(temp_videos)} 条视频，跳过删除")
        else:
            success = await crawler.delete_folder(temp_media_id)
            if success:
                print("  临时收藏夹已删除")
            else:
                print("  临时收藏夹删除失败，请手动删除")

        print("\n" + "=" * 60)
        print("  重排完成！")
        print("=" * 60)

    finally:
        await crawler.close()


if __name__ == "__main__":
    asyncio.run(main())
