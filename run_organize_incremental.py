"""增量整理脚本 - 只移动分类变化的视频 + 添加新增视频

相比 run_organize_rebuild.py 的全量 add 方案，大幅减少 API 调用次数：
  - 全量: 5563 次 add
  - 增量: 657 次 move (批量50条/次 → 14次) + 273 次 add = 287 次

原理:
  1. 从 move_state.json 提取上次分类结果 (avid → last_category)
  2. 从 classified_sorted_videos.csv 提取这次分类结果 (avid → curr_category)
  3. 分类不变 → skip
  4. 分类变化 → move_resources 批量从旧桶移到新桶
  5. 新增视频 → add_resources 逐个添加
  6. 最后清理稍后再看列表

注意: 不新建临时桶，不删除旧桶，不重命名。直接在现有桶上操作。
排序效果: 新 move/add 的视频会排到目标桶最前面（mtime最新），已有视频保持原顺序。
"""
import asyncio
import json
import sys
import os
import time
from pathlib import Path

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

sys.path.insert(0, os.path.dirname(__file__))

from config import BiliConfig
from crawler import BiliCrawler


# add 间隔秒数
ADD_DELAY = 0.2

# move 批次大小
MOVE_BATCH_SIZE = 50

# 状态文件
STATE_FILE = "output/incremental_state.json"

# 每 N 条保存一次状态
SAVE_INTERVAL = 10


def load_state(state_path: str) -> dict:
    if not Path(state_path).exists():
        return {"moved_avids": [], "added_bvids": [], "watchlater_deleted_bvids": [], "watchlater_cleared": False}
    try:
        with open(state_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {"moved_avids": [], "added_bvids": [], "watchlater_deleted_bvids": [], "watchlater_cleared": False}


def save_state(state: dict, state_path: str):
    Path(state_path).parent.mkdir(parents=True, exist_ok=True)
    with open(state_path, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


async def main():
    import pandas as pd

    config = BiliConfig()
    crawler = BiliCrawler(config)

    csv_path = Path(config.OUTPUT_DIR) / "classified_sorted_videos.csv"
    move_state_path = Path(config.OUTPUT_DIR) / "move_state.json"
    state_path = Path(config.OUTPUT_DIR) / "incremental_state.json"
    state_path_str = str(state_path)

    try:
        # === Step 1: 加载上次分类和这次分类 ===
        print("\n=== Step 1: 加载分类数据 ===")

        # 上次分类（从 move_state.json）
        with open(move_state_path, 'r', encoding='utf-8') as f:
            ms = json.load(f)
        last_cat = {}  # avid(str) -> last category name
        for cat_name, cat_data in ms.get("categories", {}).items():
            for block_name, block in cat_data.get("blocks", {}).items():
                for v in block.get("videos", []):
                    avid = str(v.get("avid", ""))
                    if avid:
                        last_cat[avid] = cat_name
        print(f"  上次分类记录: {len(last_cat)} 条")

        # 这次分类（从 classified_sorted_videos.csv）
        df = pd.read_csv(csv_path, dtype={"up_mid": "Int64"})
        df = df[df["is_valid"].astype(str).str.lower() == "true"]
        print(f"  这次分类记录: {len(df)} 条")

        # === Step 2: 分析需要操作的视频 ===
        print(f"\n=== Step 2: 分析操作 ===")

        skip_count = 0
        move_groups = {}  # (src_cat, dst_cat) -> [videos]
        add_videos = []  # 新增视频
        watchlater_videos = []  # 稍后再看视频（需要从稍后再看删除）

        for _, row in df.iterrows():
            avid = str(int(row.get("avid", 0) or 0))
            curr_cat = row.get("category", "")
            source = str(row.get("source_folder_title", ""))
            bvid = str(row.get("bvid", ""))

            if source == "稍后再看":
                watchlater_videos.append({"avid": int(avid), "bvid": bvid, "category": curr_cat})

            if avid not in last_cat:
                # 新增视频 → add
                add_videos.append({"avid": int(avid), "bvid": bvid, "category": curr_cat, "source": source})
            elif last_cat[avid] != curr_cat:
                # 分类变化 → move
                src_cat = last_cat[avid]
                key = (src_cat, curr_cat)
                if key not in move_groups:
                    move_groups[key] = []
                move_groups[key].append({"avid": int(avid), "bvid": bvid})
            else:
                skip_count += 1

        total_move = sum(len(v) for v in move_groups.values())
        total_add = len(add_videos)
        print(f"  不需要操作: {skip_count} 条")
        print(f"  需要移动(move): {total_move} 条 ({len(move_groups)} 个源→目标组)")
        print(f"  需要添加(add): {total_add} 条")
        print(f"  稍后再看视频: {len(watchlater_videos)} 条")

        if total_move == 0 and total_add == 0:
            print("\n[完成] 无需操作")
            return

        # === Step 3: 加载断点状态 ===
        print(f"\n=== Step 3: 加载断点状态 ===")
        state = load_state(state_path_str)
        moved_avid_set = set(state.get("moved_avids", []))
        added_bvid_set = set(state.get("added_bvids", []))
        print(f"  已 move: {len(moved_avid_set)} | 已 add: {len(added_bvid_set)}")

        # === Step 4: 获取收藏夹 ID 映射 ===
        print(f"\n=== Step 4: 获取收藏夹 ID ===")
        folders = await crawler.get_tmp_folders()
        folder_map = {}  # title -> media_id
        for f in folders:
            folder_map[f.title] = f.id
        print(f"  收藏夹数: {len(folder_map)}")

        # === Step 5: 批量 move 分类变化的视频 ===
        print(f"\n=== Step 5: 批量 move ({total_move} 条) ===")
        move_start = time.time()
        move_done = 0
        move_failed = 0

        for (src_cat, dst_cat), videos in move_groups.items():
            src_id = folder_map.get(src_cat)
            dst_id = folder_map.get(dst_cat)
            if not src_id or not dst_id:
                print(f"  [SKIP] {src_cat}→{dst_cat}: 收藏夹不存在")
                continue

            # 过滤已 move 的
            need_move = [v for v in videos if str(v["avid"]) not in moved_avid_set]
            if not need_move:
                continue

            print(f"\n  [{src_cat} → {dst_cat}] {len(need_move)} 条")
            resources = [f"{v['avid']}:2" for v in need_move]

            # 批量 move（50条/次）
            for i in range(0, len(resources), MOVE_BATCH_SIZE):
                batch = resources[i:i + MOVE_BATCH_SIZE]
                batch_videos = need_move[i:i + MOVE_BATCH_SIZE]
                try:
                    ok = await crawler.move_resources(src_id, dst_id, batch)
                    if ok:
                        for v in batch_videos:
                            moved_avid_set.add(str(v["avid"]))
                        move_done += len(batch_videos)
                    else:
                        move_failed += len(batch_videos)
                        if crawler.last_post_412:
                            print(f"    [412] 触发风控，等待冷却...")
                except Exception as e:
                    move_failed += len(batch_videos)
                    print(f"    [ERROR] move 异常: {e}")

                print(f"    进度: {move_done + move_failed}/{total_move} (成功 {move_done} 失败 {move_failed})")
                state["moved_avids"] = list(moved_avid_set)
                save_state(state, state_path_str)

        move_elapsed = time.time() - move_start
        print(f"\n  [完成] move: {move_done} 成功, {move_failed} 失败 (耗时 {move_elapsed/60:.1f} 分钟)")

        # === Step 6: 逐个 add 新增视频 ===
        print(f"\n=== Step 6: 逐个 add ({total_add} 条) ===")
        add_start = time.time()
        add_done = 0
        add_failed = 0

        for i, v in enumerate(add_videos, 1):
            if v["bvid"] in added_bvid_set:
                continue

            dst_id = folder_map.get(v["category"])
            if not dst_id:
                print(f"  [SKIP] {v['category']} 收藏夹不存在")
                continue

            try:
                ok = await crawler.add_resources(dst_id, [f"{v['avid']}:2"])
                if ok:
                    added_bvid_set.add(v["bvid"])
                    add_done += 1
                else:
                    add_failed += 1
                    if crawler.last_post_412:
                        print(f"  [412] 等待冷却... (已 add {add_done})")
            except Exception as e:
                add_failed += 1
                print(f"  [WARN] add 异常 {v['bvid']}: {e}")

            if i % 10 == 0 or i == total_add:
                elapsed = time.time() - add_start
                rate = i / elapsed if elapsed > 0 else 0
                eta = (total_add - i) / rate if rate > 0 else 0
                print(f"  进度: {i}/{total_add} (成功 {add_done} 失败 {add_failed}) "
                      f"速率 {rate:.1f}条/s 预计剩余 {eta/60:.1f} 分钟")

            if i % SAVE_INTERVAL == 0:
                state["added_bvids"] = list(added_bvid_set)
                save_state(state, state_path_str)

            await asyncio.sleep(ADD_DELAY)

        state["added_bvids"] = list(added_bvid_set)
        save_state(state, state_path_str)

        add_elapsed = time.time() - add_start
        print(f"\n  [完成] add: {add_done} 成功, {add_failed} 失败 (耗时 {add_elapsed/60:.1f} 分钟)")

        # === Step 7: 清理稍后再看列表 ===
        print(f"\n=== Step 7: 清理稍后再看列表 ({len(watchlater_videos)} 条) ===")
        deleted_bvids = set(state.get("watchlater_deleted_bvids", []))
        if state.get("watchlater_cleared"):
            print(f"  [已完成] 稍后再看已清理")
        else:
            need_delete = [v for v in watchlater_videos if v["bvid"] not in deleted_bvids]
            print(f"  已删除: {len(deleted_bvids)} | 待删除: {len(need_delete)}")

            wl_start = time.time()
            total_wl = len(need_delete)
            for i, v in enumerate(need_delete, 1):
                try:
                    ok = await crawler.delete_watchlater(bvid=v["bvid"], avid=v["avid"])
                    if ok:
                        deleted_bvids.add(v["bvid"])
                except Exception as e:
                    print(f"  [WARN] 删除稍后再看异常 {v['bvid']}: {e}")

                if i % 10 == 0 or i == total_wl:
                    elapsed = time.time() - wl_start
                    rate = i / elapsed if elapsed > 0 else 0
                    eta = (total_wl - i) / rate if rate > 0 else 0
                    print(f"  进度: {i}/{total_wl} (成功 {len(deleted_bvids)}) "
                          f"速率 {rate:.1f}条/s 预计剩余 {eta/60:.1f} 分钟")

                if i % SAVE_INTERVAL == 0:
                    state["watchlater_deleted_bvids"] = list(deleted_bvids)
                    save_state(state, state_path_str)

            state["watchlater_deleted_bvids"] = list(deleted_bvids)
            state["watchlater_cleared"] = True
            save_state(state, state_path_str)

            wl_elapsed = time.time() - wl_start
            print(f"  [完成] 稍后再看清理: {len(deleted_bvids)}/{total_wl} (耗时 {wl_elapsed/60:.1f} 分钟)")

        # === 总结 ===
        print(f"\n=== 增量整理完成 ===")
        print(f"  move: {move_done} 成功, {move_failed} 失败")
        print(f"  add: {add_done} 成功, {add_failed} 失败")
        print(f"  稍后再看清理: {len(deleted_bvids)}/{len(watchlater_videos)}")

    except Exception as e:
        print(f"\n[ERROR] {e}")
        import traceback
        traceback.print_exc()
        try:
            save_state(state, state_path_str)
        except Exception:
            pass
    finally:
        await crawler.close()


if __name__ == "__main__":
    asyncio.run(main())
