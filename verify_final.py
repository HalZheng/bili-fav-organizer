"""最终核查：线上收藏夹实际状态 vs 分类计划对账（只读）

输出:
  1. 各桶线上数量 vs 计划数量
  2. 错位 / 重复 / 丢失明细统计
  3. 稍后再看残留
  4. 计划外残留（线上有但不在计划中的视频）
"""
import asyncio
import sys
import json
import time

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

sys.path.insert(0, __import__('os').path.dirname(__file__))

from pathlib import Path
import pandas as pd

from config import BiliConfig
from crawler import BiliCrawler
from bucket_config import BUCKET_NAMES
from reconcile import load_plan, build_folder_families, _safe_int


async def main():
    config = BiliConfig()
    crawler = BiliCrawler(config)
    t0 = time.time()

    plan = load_plan()
    print(f"[计划] {len(plan)} 条有效视频")

    family, folder_to_bucket = await build_folder_families(crawler)
    print(f"[桶族] {sum(len(v) for v in family.values())} 个收藏夹覆盖 {len(family)} 个桶")

    membership = {}
    for name, media_ids in family.items():
        for mid in media_ids:
            ids = await crawler.get_folder_video_ids(mid, use_cache=False)
            if ids is None:
                print(f"[ERROR] 获取收藏夹 {mid}({name}) 失败")
                return
            for item in ids:
                avid = item.get("id")
                if avid:
                    membership.setdefault(avid, set()).add(mid)

    watchlater = await crawler.get_watchlater()
    wl_avids = {v.id for v in watchlater if v.id}
    print(f"[稍后再看] 当前 {len(wl_avids)} 条")

    target_sets = {name: set(ids) for name, ids in family.items()}

    # 对账
    in_place = 0
    misplaced = []      # 在错误桶
    missing = []        # 不在任何桶
    in_other_only = []  # 只在计划外的桶（不在13桶族中不可能，membership只含13桶）
    dup_in_family = []  # 同时在多个桶
    wl_residual = []    # 计划内视频仍在稍后再看

    for avid, info in plan.items():
        in_folders = membership.get(avid, set())
        bucket = info["bucket"]
        if in_folders & target_sets[bucket]:
            in_place += 1
            strays = in_folders - target_sets[bucket]
            if strays:
                dup_in_family.append((info["bvid"], bucket, [folder_to_bucket.get(s) for s in strays]))
        elif in_folders:
            misplaced.append((info["bvid"], info["title"][:30], bucket,
                              [folder_to_bucket.get(s) for s in in_folders]))
        else:
            missing.append((info["bvid"], info["title"][:30], bucket))
        if avid in wl_avids:
            wl_residual.append(info["bvid"])

    # 计划外残留（线上13桶中有、但不在计划中的视频）
    plan_avids = set(plan.keys())
    unplanned = {}
    for avid, mids in membership.items():
        if avid not in plan_avids:
            for m in mids:
                b = folder_to_bucket.get(m, "?")
                unplanned[b] = unplanned.get(b, 0) + 1

    # 各桶数量对比
    print(f"\n{'=' * 72}")
    print("  各桶线上数量 vs 计划数量")
    print("=" * 72)
    print(f"  {'桶':<10} {'线上':>6} {'计划':>6} {'差异':>6}")
    total_online = 0
    for name in BUCKET_NAMES:
        online = sum(len([a for a, ms in membership.items() if mid in ms]) for mid in family[name])
        planned = sum(1 for i in plan.values() if i["bucket"] == name)
        total_online += online
        flag = "" if online == planned else "  ←"
        print(f"  {name:<10} {online:>6} {planned:>6} {online - planned:>+6}{flag}")

    unplanned_total = sum(unplanned.values())
    print(f"\n  线上总成员(含重复): {total_online} | 计划: {len(plan)} | 计划外残留: {unplanned_total}")

    print(f"\n{'=' * 72}")
    print("  对账结果")
    print("=" * 72)
    print(f"  已就位: {in_place} / {len(plan)} ({in_place/len(plan)*100:.2f}%)")
    print(f"  错位(在错误桶): {len(misplaced)}")
    for b, t, k, f in misplaced[:20]:
        print(f"    {b} [{k}] 在 {f} 应在 {k}")
    if len(misplaced) > 20:
        print(f"    ... 共 {len(misplaced)} 条")
    print(f"  重复(目标桶+其他桶并存): {len(dup_in_family)}")
    for b, k, f in dup_in_family[:20]:
        print(f"    {b} 在 {k} + {f}")
    if len(dup_in_family) > 20:
        print(f"    ... 共 {len(dup_in_family)} 条")
    print(f"  丢失(不在任何桶): {len(missing)}")
    for b, t, k in missing[:20]:
        print(f"    {b} → {k} | {t}")
    if len(missing) > 20:
        print(f"    ... 共 {len(missing)} 条")
    print(f"  稍后再看残留(计划内): {len(wl_residual)} {wl_residual[:10]}")
    print(f"  稍后再看剩余总数: {len(wl_avids)}")
    print(f"  计划外残留分布: {unplanned}")

    report = {
        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "plan_total": len(plan),
        "in_place": in_place,
        "misplaced": len(misplaced),
        "missing": len(missing),
        "dup_in_family": len(dup_in_family),
        "wl_residual_in_plan": len(wl_residual),
        "wl_total": len(wl_avids),
        "unplanned_leftovers": unplanned,
        "folder_online_counts": {
            name: sum(1 for a, ms in membership.items() if any(mid in ms for mid in family[name]))
            for name in BUCKET_NAMES
        },
        "elapsed_sec": round(time.time() - t0, 1),
    }
    Path("output").mkdir(exist_ok=True)
    out_name = f"output/verify_final_{time.strftime('%Y%m%d')}.json"
    Path(out_name).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[完成] 核查耗时 {report['elapsed_sec']}s，结果已存 {out_name}")

    await crawler.close()


if __name__ == "__main__":
    asyncio.run(main())
