"""收尾纠偏：对齐B站线上收藏夹与分类计划

主整理（run_organize_from_classified.py）完成后运行，处理其遗留问题：
  1. 错位：视频落在非目标桶 → move 到目标桶
  2. 重复：视频同时在目标桶和其他桶 → 从其他桶删除
  3. 丢失：视频不在任何桶（如 add 因 11203 容量满失败）→ add 到目标桶
  4. 稍后再看残留：视频已在目标桶但仍在稍后再看列表 → 从稍后再看删除

只处理 13 个目标桶（含分卷 _2/_3...），不触碰用户其他收藏夹。

两阶段执行：先纠错位/去重（腾出容量），再补齐缺失，避免 B站 1000 上限冲突。

断点续跑：进度保存到 output/reconcile_state.json，重跑自动跳过已处理视频；
操作失败的视频不标记完成，下次重跑会自动重试（幂等操作）。

用法: python reconcile.py
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
import pandas as pd

from config import BiliConfig
from crawler import BiliCrawler
from bucket_config import BUCKET_NAMES

STATE_FILE = Path("output") / "reconcile_state.json"
SAVE_INTERVAL = 20  # 每处理 N 条保存一次进度


def _safe_int(val, default=0):
    if val is None or (isinstance(val, float) and (val != val)):
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def load_plan():
    """读取分类计划: {avid: {"bvid", "bucket", "title"}}"""
    csv_path = Path("output") / "classified_sorted_videos.csv"
    df = pd.read_csv(csv_path, dtype={"up_mid": "Int64"})
    plan = {}
    for _, row in df.iterrows():
        if str(row.get("is_valid", "True")) not in ("True", "true", True):
            continue
        avid = _safe_int(row.get("avid", 0))
        if avid and row.get("category"):
            plan[avid] = {
                "bvid": str(row.get("bvid", "")),
                "bucket": str(row["category"]),
                "title": str(row.get("title", ""))[:40],
            }
    return plan


async def build_folder_families(crawler: BiliCrawler):
    """构建 {bucket_name: [media_id...]}（主桶 + 分卷）与 {media_id: bucket_name}"""
    folders = await crawler.get_created_folders()
    family = {name: [] for name in BUCKET_NAMES}
    folder_to_bucket = {}
    for f in folders:
        for name in BUCKET_NAMES:
            if f.title == name or f.title.startswith(name + "_"):
                family[name].append(f.id)
                folder_to_bucket[f.id] = name
                break
    return family, folder_to_bucket


async def main():
    config = BiliConfig()
    crawler = BiliCrawler(config)

    stats = {
        "start_time": time.time(),
        "moved": 0,        # 错位 move（从错误桶移到目标桶）
        "removed_dup": 0,  # 重复（目标桶已有，从其他桶删除）
        "added": 0,        # 丢失/仅在稍后再看 → add 到目标桶
        "wl_cleaned": 0,   # 从稍后再看列表删除
        "skipped_ok": 0,   # 已在目标桶，无需处理
        "failed": 0,
    }

    # 加载进度
    done = set()
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            done = set(state.get("done", []))
            print(f"[续跑] 已处理 {len(done)} 条，跳过")
        except Exception:
            done = set()

    def save_state():
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(
            json.dumps({"done": list(done), "stats": stats}, ensure_ascii=False),
            encoding="utf-8",
        )

    # 1. 分类计划
    plan = load_plan()
    print(f"[计划] {len(plan)} 条有效视频")

    # 2. 桶族与线上成员
    family, folder_to_bucket = await build_folder_families(crawler)
    missing_buckets = [n for n, ids in family.items() if not ids]
    if missing_buckets:
        print(f"[ERROR] 缺少目标桶: {missing_buckets}")
        return
    print(f"[桶族] {sum(len(v) for v in family.values())} 个收藏夹覆盖 13 个桶")

    membership = {}  # avid -> set(media_id)
    for name, media_ids in family.items():
        for mid in media_ids:
            ids = await crawler.get_folder_video_ids(mid, use_cache=False)
            if ids is None:
                print(f"[ERROR] 获取收藏夹 {mid}({name}) 视频列表失败，中止")
                return
            for item in ids:
                avid = item.get("id")
                if avid:
                    membership.setdefault(avid, set()).add(mid)
            print(f"  {name} (id={mid}): {len(ids)} 条")

    # 3. 稍后再看当前列表
    watchlater = await crawler.get_watchlater()
    wl_avids = {v.id for v in watchlater if v.id}
    print(f"[稍后再看] 当前 {len(wl_avids)} 条")

    target_family_sets = {name: set(ids) for name, ids in family.items()}
    total = len(plan)

    # 各收藏夹实时计数（容量感知调度用）
    folder_counts: dict[int, int] = {}
    for mids in membership.values():
        for mid in mids:
            folder_counts[mid] = folder_counts.get(mid, 0) + 1
    FOLDER_CAP = 1000  # B站单收藏夹上限

    # ========== 阶段A: 错位/重复纠偏（容量感知多轮） ==========
    # B站原生 move 接口在目标夹满时直接 11203（不因源夹同步减一放行），
    # 且各桶计划数均 ≤1000，因此按"先做无需容量/目标未满的操作腾出满桶空间"
    # 多轮推进；全部卡死时逐出一条（从源夹删除，阶段B补齐）破解循环依赖。
    print(f"\n=== 阶段A: 错位/重复纠偏（容量感知多轮） ===")
    a_count = 0
    MAX_ROUNDS = 30
    for round_no in range(1, MAX_ROUNDS + 1):
        progress = 0
        deferred = []  # 目标夹满，本轮延后的 (avid, info, bucket)
        for avid, info in plan.items():
            if avid in done:
                continue
            bucket = info["bucket"]
            in_folders = membership.get(avid, set())
            if not in_folders:
                continue  # 不在任何桶 → 阶段B补齐
            in_target = bool(in_folders & target_family_sets[bucket])
            strays = in_folders - target_family_sets[bucket]
            if not strays:
                done.add(avid)  # 已在目标桶且无杂散
                progress += 1
                continue
            tar = family[bucket][0]
            if not in_target and folder_counts.get(tar, 0) >= FOLDER_CAP:
                deferred.append((avid, info, bucket))
                continue
            a_count += 1
            video_ok = True
            first_move_done = in_target  # 若已在目标桶，全部杂散走删除
            for stray in sorted(strays):
                stray_bucket = folder_to_bucket.get(stray, "?")
                res = [f"{avid}:2"]
                if first_move_done:
                    ok = await crawler.remove_resources(stray, res)
                    if ok:
                        stats["removed_dup"] += 1
                        folder_counts[stray] = folder_counts.get(stray, 0) - 1
                        in_folders.discard(stray)
                    else:
                        video_ok = False
                    print(f"  [去重] {info['bvid']} 从 {stray_bucket} 删除{'✓' if ok else '✗'} | {info['title']}")
                else:
                    ok = await crawler.move_resources(stray, tar, res)
                    if ok:
                        stats["moved"] += 1
                        folder_counts[stray] = folder_counts.get(stray, 0) - 1
                        folder_counts[tar] = folder_counts.get(tar, 0) + 1
                        in_folders.discard(stray)
                        in_folders.add(tar)
                        first_move_done = True
                    else:
                        video_ok = False
                    print(f"  [错位] {info['bvid']} {stray_bucket} → {bucket}{'✓' if ok else '✗'} | {info['title']}")
            if video_ok:
                done.add(avid)
                progress += 1
                if progress % SAVE_INTERVAL == 0:
                    save_state()
        print(f"  [轮次 {round_no}] 完成 {progress} 条 | 延后 {len(deferred)} 条 | "
              f"累计 move={stats['moved']} 去重={stats['removed_dup']}")
        save_state()
        if not deferred:
            break
        if progress == 0:
            # 无进展且有延后 → 循环依赖死锁，逐出一条腾容量（阶段B补齐）
            ev_avid, ev_info, ev_bucket = deferred[0]
            ev_folders = membership.get(ev_avid, set())
            stray = sorted(ev_folders)[0]
            stray_bucket = folder_to_bucket.get(stray, "?")
            ok = await crawler.remove_resources(stray, [f"{ev_avid}:2"])
            if ok:
                folder_counts[stray] = folder_counts.get(stray, 0) - 1
                ev_folders.discard(stray)
                print(f"  [逐出] {ev_info['bvid']} 从 {stray_bucket} 删除（腾容量，阶段B补齐到 {ev_bucket}）✓")
            else:
                print(f"  [逐出] {ev_info['bvid']} 失败，中止阶段A")
                break
    else:
        print(f"  [WARN] 阶段A达到最大轮次 {MAX_ROUNDS}，剩余延后项交给阶段B")
    print(f"  阶段A处理 {a_count} 条: move={stats['moved']} 去重={stats['removed_dup']}")

    # ========== 阶段B: 补齐缺失 + 稍后再看清理 ==========
    print(f"\n=== 阶段B: 补齐缺失 + 稍后再看清理 ===")
    b_count = 0
    for avid, info in plan.items():
        if avid in done and avid not in wl_avids:
            continue
        bucket = info["bucket"]
        in_folders = membership.get(avid, set())
        in_target = bool(in_folders & target_family_sets[bucket])
        video_ok = True
        if not in_target:
            b_count += 1
            ok = await crawler.add_resources(family[bucket][0], [f"{avid}:2"])
            if ok:
                stats["added"] += 1
            else:
                video_ok = False
            print(f"  [补齐] {info['bvid']} → {bucket} {'✓' if ok else '✗'} | {info['title']}")
        if avid in wl_avids:
            ok = await crawler.delete_watchlater(bvid=info["bvid"], avid=avid)
            if ok:
                stats["wl_cleaned"] += 1
            else:
                video_ok = False
            print(f"  [稍后再看] 清理 {info['bvid']} {'✓' if ok else '✗'}")
        if video_ok:
            done.add(avid)
            if b_count % SAVE_INTERVAL == 0:
                save_state()
    save_state()
    stats["skipped_ok"] = len(done) - stats["moved"] - stats["removed_dup"] - stats["added"]

    elapsed = time.time() - stats["start_time"]
    print(f"\n{'=' * 60}")
    print(f"  纠偏完成，耗时 {elapsed/60:.1f} 分钟")
    print(f"  错位move: {stats['moved']} | 去重删除: {stats['removed_dup']}")
    print(f"  补齐add: {stats['added']} | 稍后再看清理: {stats['wl_cleaned']}")
    print(f"  已就位: {stats['skipped_ok']} | 失败: {stats['failed']}")
    print(f"{'=' * 60}")

    await crawler.close()


if __name__ == "__main__":
    asyncio.run(main())
