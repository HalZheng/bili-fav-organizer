"""补充 tags 脚本 - 对已有 videos.csv 中 tags 为空的记录调用 tags API 补充

背景:
  B站 /x/web-interface/view 接口已不再返回 tag 字段（返回 None），
  需单独调用 /x/tag/archive/tags 获取视频 tags。
  本脚本仅补充 tags 字段，不重新获取其他详情，避免重复请求。

断点续爬:
  通过检查 videos.csv 中 tags 字段是否为空来决定是否需要补充。
  已有 tags 的记录会被跳过。
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


# tags API 间隔秒数（避免412）
TAGS_DELAY = 0.5

# 每 N 条保存一次CSV（断点续爬保护）
SAVE_INTERVAL = 50


async def main():
    config = BiliConfig()
    crawler = BiliCrawler(config)

    csv_path = Path(config.OUTPUT_DIR) / "videos.csv"
    if not csv_path.exists():
        print(f"[ERROR] CSV 不存在: {csv_path}")
        return

    import pandas as pd

    # 读取已有 CSV
    print(f"[加载] 读取 {csv_path}")
    df = pd.read_csv(csv_path, dtype={"up_mid": "Int64"})
    total = len(df)
    print(f"[加载] 共 {total} 条记录")

    # 找出 tags 为空的记录索引和 bvid
    need_tags_idx = []
    need_tags_bvid = []
    for idx, row in df.iterrows():
        tags_val = str(row.get("tags", "") or "").strip()
        if tags_val == "" or tags_val == "nan":
            bvid = str(row.get("bvid", "") or "").strip()
            if bvid and bvid != "nan":
                need_tags_idx.append(idx)
                need_tags_bvid.append(bvid)

    already_done = total - len(need_tags_idx)
    print(f"[统计] 已有 tags: {already_done} 条 | 需补充: {len(need_tags_idx)} 条")
    if not need_tags_idx:
        print("[完成] 所有记录已有 tags，无需补充")
        return

    print(f"[预估] 耗时约 {len(need_tags_idx) * TAGS_DELAY / 60:.1f} 分钟\n")

    enriched = 0
    failed = 0
    total_need = len(need_tags_idx)
    start_time = time.time()

    for i, (idx, bvid) in enumerate(zip(need_tags_idx, need_tags_bvid), 1):
        try:
            tag_list = await crawler._fetch_video_tags(bvid)
            tags = ",".join(t.get("tag_name", "") for t in tag_list if t.get("tag_name"))
            if tags:
                df.at[idx, "tags"] = tags
                enriched += 1
            else:
                # tags API 返回空（视频可能确实没有标签，或API失败）
                failed += 1
        except Exception as e:
            failed += 1
            if "412" in str(e) or "风控" in str(e):
                print(f"  [412] 触发风控，等待 60s 后继续... (bvid={bvid})")
                await asyncio.sleep(60)
            else:
                print(f"  [WARN] {bvid}: {e}")

        # 进度显示
        if i % 10 == 0 or i == total_need:
            elapsed = time.time() - start_time
            rate = i / elapsed if elapsed > 0 else 0
            eta = (total_need - i) / rate if rate > 0 else 0
            print(f"  进度: {i}/{total_need} (成功 {enriched} 失败 {failed}) "
                  f"速率 {rate:.1f}条/s 预计剩余 {eta/60:.1f} 分钟")

        # 定期保存（断点续爬保护）
        if i % SAVE_INTERVAL == 0:
            df.to_csv(csv_path, index=False, encoding="utf-8-sig")
            print(f"  [保存] 已保存进度 ({i}/{total_need})")

        await asyncio.sleep(TAGS_DELAY)

    # 最终保存
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    print(f"\n=== 补充完成 ===")
    print(f"总计: {total_need} 条")
    print(f"成功: {enriched} 条 | 失败: {failed} 条")

    # 覆盖率检查
    has_tags = df["tags"].notna() & (df["tags"].astype(str).str.strip() != "") & (df["tags"].astype(str).str.strip() != "nan")
    print(f"tags 覆盖率: {has_tags.sum()}/{total} ({has_tags.sum()/total*100:.1f}%)")

    await crawler.close()


if __name__ == "__main__":
    asyncio.run(main())
