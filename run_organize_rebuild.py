"""整理脚本 - 纯 add + 重命名 + 删旧桶模式

设计理念：
  1. 对每个分类桶：新建临时桶 → 按排序逆序逐个 add → 删旧桶 → 重命名临时桶
  2. 全部 add 完成后，逐个删除稍后再看视频（从稍后再看列表移除已分类视频）
  3. 断点续爬：通过 rebuild_state.json 记录进度

相比旧 full_organize 的优势：
  - 纯 add 操作，不涉及 move 的源/目标双向验证
  - 数据干净（删旧桶后保证唯一，无重复）
  - 排序严格保证（全量按序 add，mtime 递增）
  - 逻辑极简，代码量大幅减少

API 调用次数估算（5288 条视频）：
  - 13 新建 + 5288 add + 13 删除 + 13 重命名 = 5327 次
  - 间隔 0.5s/条 → 约 44 分钟
  - 稍后再看删除 275 条 * 3s = 14 分钟
  - 总计约 58 分钟
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


# add 间隔秒数（避免412，实测0.5s安全）
ADD_DELAY = 0.2

# delete_watchlater 间隔秒数（复用 _wait_post_interval=3s）
WATCHLATER_DELETE_DELAY = 0.5

# 状态文件
STATE_FILE = "output/rebuild_state.json"

# 每 N 条保存一次状态
SAVE_INTERVAL = 20


def load_state(state_path: str) -> dict:
    """加载断点续爬状态"""
    if not Path(state_path).exists():
        return {"categories": {}, "watchlater_deleted_bvids": [], "watchlater_cleared": False}
    try:
        with open(state_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {"categories": {}, "watchlater_deleted_bvids": [], "watchlater_cleared": False}


def save_state(state: dict, state_path: str):
    """保存状态"""
    Path(state_path).parent.mkdir(parents=True, exist_ok=True)
    with open(state_path, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


async def main():
    import pandas as pd

    config = BiliConfig()
    crawler = BiliCrawler(config)

    csv_path = Path(config.OUTPUT_DIR) / "classified_sorted_videos.csv"
    state_path = Path(config.OUTPUT_DIR) / "rebuild_state.json"
    state_path_obj = str(state_path)

    try:
        # === Step 1: 加载分类结果 ===
        print("\n=== Step 1: 加载分类结果 ===")
        df = pd.read_csv(csv_path, dtype={"up_mid": "Int64"})
        # 过滤失效视频
        df = df[df["is_valid"].astype(str).str.lower() == "true"]
        print(f"  有效视频: {len(df)} 条")

        # 按分类桶分组
        categories = {}
        for cat_name, group in df.groupby("category"):
            # 按排序顺序（block_rank 升序，block 内按 sort_idx）
            # 这里简单按 csv 行顺序，因为 run_classify.py 已排好序
            videos = []
            for _, row in group.iterrows():
                avid = int(row.get("avid", 0) or 0)
                bvid = str(row.get("bvid", ""))
                source = str(row.get("source_folder_title", ""))
                videos.append({
                    "avid": avid,
                    "bvid": bvid,
                    "title": str(row.get("title", ""))[:40],
                    "source": source,
                })
            categories[cat_name] = videos

        print(f"  分类桶数: {len(categories)}")
        total_videos = sum(len(v) for v in categories.values())
        print(f"  总视频数: {total_videos}")

        # 统计稍后再看视频
        watchlater_videos = [
            v for cat_videos in categories.values() for v in cat_videos
            if v["source"] == "稍后再看"
        ]
        print(f"  稍后再看视频: {len(watchlater_videos)} 条（将在 add 完成后从稍后再看列表删除）")

        # === Step 2: 加载断点状态 ===
        print(f"\n=== Step 2: 加载断点状态 ===")
        state = load_state(state_path_obj)
        completed_cats = [c for c, s in state["categories"].items() if s.get("status") == "completed"]
        print(f"  已完成桶: {len(completed_cats)} | 待处理: {len(categories) - len(completed_cats)}")
        if completed_cats:
            print(f"  已完成: {', '.join(completed_cats)}")

        # === Step 3: 逐桶处理 ===
        print(f"\n=== Step 3: 逐桶处理（新建→add→删旧→重命名）===")

        total_start = time.time()
        for cat_idx, (cat_name, videos) in enumerate(categories.items(), 1):
            if cat_name not in state["categories"]:
                state["categories"][cat_name] = {}
            cat_state = state["categories"][cat_name]

            if cat_state.get("status") == "completed":
                print(f"\n--- [{cat_idx}/{len(categories)}] {cat_name} ({len(videos)}条) [已完成，跳过] ---")
                continue

            print(f"\n--- [{cat_idx}/{len(categories)}] {cat_name} ({len(videos)}条) ---")
            cat_start = time.time()

            # 3.1 新建临时桶
            temp_name = f"{cat_name}_new"
            temp_folder_id = cat_state.get("temp_folder_id")
            if not temp_folder_id:
                print(f"  [新建] 创建临时桶: {temp_name}")
                temp_folder_id = await crawler.create_folder(temp_name, intro="")
                if not temp_folder_id:
                    print(f"  [ERROR] 创建临时桶失败，跳过此桶")
                    continue
                cat_state["temp_folder_id"] = temp_folder_id
                cat_state["status"] = "adding"
                cat_state["added_bvids"] = []
                save_state(state, state_path_obj)
            else:
                print(f"  [续爬] 复用临时桶: {temp_name} (id={temp_folder_id})")
                print(f"  [续爬] 已 add: {len(cat_state.get('added_bvids', []))} 条")

            added_bvids = set(cat_state.get("added_bvids", []))

            # 3.2 按逆序逐个 add（保证排序：最后 add 的 mtime 最新，排最前）
            # 注意：videos 已按 csv 行顺序（run_classify.py 排好序），第一个是 Top 区块
            # 逆序后从最后一个开始 add，最后一个 add 的是 Top 区块第一条 → mtime 最新 → 排最前
            videos_to_add = list(reversed(videos))
            total = len(videos_to_add)
            added_count = len(added_bvids)
            failed_count = 0

            for i, v in enumerate(videos_to_add, 1):
                if v["bvid"] in added_bvids:
                    continue

                if v["avid"] == 0:
                    print(f"  [SKIP] avid=0: {v['bvid']} | {v['title']}")
                    failed_count += 1
                    continue

                try:
                    ok = await crawler.add_resources(temp_folder_id, [f"{v['avid']}:2"])
                    if ok:
                        added_bvids.add(v["bvid"])
                        added_count += 1
                    else:
                        failed_count += 1
                        # 412 风控时 add_resources 返回 False，等待冷却
                        if crawler.last_post_412:
                            print(f"  [412] 等待冷却后继续... (已 add {added_count})")
                except Exception as e:
                    failed_count += 1
                    print(f"  [WARN] add 异常 {v['bvid']}: {e}")

                # 进度显示
                if i % 20 == 0 or i == total:
                    elapsed = time.time() - cat_start
                    rate = i / elapsed if elapsed > 0 else 0
                    eta = (total - i) / rate if rate > 0 else 0
                    print(f"  进度: {i}/{total} (成功 {added_count} 失败 {failed_count}) "
                          f"速率 {rate:.1f}条/s 预计剩余 {eta/60:.1f} 分钟")

                # 定期保存状态
                if i % SAVE_INTERVAL == 0:
                    cat_state["added_bvids"] = list(added_bvids)
                    save_state(state, state_path_obj)

                await asyncio.sleep(ADD_DELAY)

            # 保存最终 add 状态
            cat_state["added_bvids"] = list(added_bvids)
            cat_state["status"] = "added"
            save_state(state, state_path_obj)

            cat_elapsed = time.time() - cat_start
            print(f"  [完成] {cat_name} add 完成: {added_count}/{len(videos)} "
                  f"(失败 {failed_count}, 耗时 {cat_elapsed/60:.1f} 分钟)")

            # 3.3 删除旧桶（安全检查：失败率超过5%则跳过删除）
            fail_rate = failed_count / max(len(videos), 1)
            if fail_rate > 0.05:
                print(f"  [WARN] add 失败率 {fail_rate*100:.1f}% > 5%，跳过删除旧桶以保护数据")
                print(f"  [WARN] 临时桶 {temp_name} (id={temp_folder_id}) 保留，请手动检查")
                cat_state["status"] = "add_failed_high"
                save_state(state, state_path_obj)
                continue

            # 查找旧桶 ID
            all_folders = await crawler.get_tmp_folders()
            old_folder = next((f for f in all_folders if f.title == cat_name), None)
            if old_folder:
                print(f"  [删除] 旧桶: {cat_name} (id={old_folder.id})")
                # 等待一下避免连续 POST
                await asyncio.sleep(2)
                ok = await crawler.delete_folder(old_folder.id)
                if not ok:
                    print(f"  [WARN] 删除旧桶失败，但继续重命名临时桶")
            else:
                print(f"  [INFO] 旧桶 {cat_name} 不存在，跳过删除")

            # 3.4 重命名临时桶为目标名
            await asyncio.sleep(2)
            print(f"  [重命名] {temp_name} → {cat_name}")
            ok = await crawler.rename_folder(temp_folder_id, cat_name)
            if not ok:
                print(f"  [ERROR] 重命名失败！临时桶 {temp_name} (id={temp_folder_id}) 保留")
                cat_state["status"] = "rename_failed"
            else:
                cat_state["status"] = "completed"
                print(f"  [OK] {cat_name} 完成 ✓")

            save_state(state, state_path_obj)

        total_elapsed = time.time() - total_start
        print(f"\n=== 所有桶处理完成 (耗时 {total_elapsed/60:.1f} 分钟) ===")

        # === Step 4: 清理稍后再看列表 ===
        print(f"\n=== Step 4: 清理稍后再看列表 ({len(watchlater_videos)} 条) ===")

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

                if i % 10 == 0:
                    state["watchlater_deleted_bvids"] = list(deleted_bvids)
                    save_state(state, state_path_obj)

                await asyncio.sleep(WATCHLATER_DELETE_DELAY)

            state["watchlater_deleted_bvids"] = list(deleted_bvids)
            state["watchlater_cleared"] = True
            save_state(state, state_path_obj)

            wl_elapsed = time.time() - wl_start
            print(f"  [完成] 稍后再看清理完成: {len(deleted_bvids)}/{total_wl} (耗时 {wl_elapsed/60:.1f} 分钟)")

        # === 总结 ===
        print(f"\n=== 整理完成 ===")
        print(f"分类桶:")
        completed = sum(1 for c in state["categories"].values() if c.get("status") == "completed")
        print(f"  完成: {completed}/{len(categories)}")
        for cat_name in categories:
            cs = state["categories"].get(cat_name, {})
            status = cs.get("status", "?")
            added = len(cs.get("added_bvids", []))
            total = len(categories[cat_name])
            print(f"  {cat_name}: {status} ({added}/{total})")
        print(f"\n稍后再看清理: {len(deleted_bvids)}/{len(watchlater_videos)}")

    except Exception as e:
        print(f"\n[ERROR] {e}")
        import traceback
        traceback.print_exc()
        # 异常时保存状态，防止进度丢失
        try:
            save_state(state, state_path_obj)
            print(f"[状态已保存] {state_path_obj}")
        except Exception:
            pass
    finally:
        await crawler.close()


if __name__ == "__main__":
    asyncio.run(main())
