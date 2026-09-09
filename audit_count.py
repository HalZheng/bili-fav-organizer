"""对账：爬取CSV vs 线上13桶，找出计划外视频的明细身份（只读）"""
import asyncio
import sys
import os

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

sys.path.insert(0, os.path.dirname(__file__))

import pandas as pd
from config import BiliConfig
from crawler import BiliCrawler
from bucket_config import BUCKET_NAMES
from reconcile import build_folder_families


async def main():
    config = BiliConfig()
    crawler = BiliCrawler(config)

    v = pd.read_csv('output/videos.csv', dtype={'bvid': str})
    c = pd.read_csv('output/classified_sorted_videos.csv', dtype={'bvid': str})
    known = set(v['bvid']) | set(c['bvid'])
    plan_bvids = set(c['bvid'])

    family, folder_to_bucket = await build_folder_families(crawler)

    # 拉线上全部成员
    online = {}  # bvid -> set(桶名)
    avid_map = {}
    for name, media_ids in family.items():
        for mid in media_ids:
            ids = await crawler.get_folder_video_ids(mid, use_cache=False)
            for item in ids:
                bv = item.get("bvid")
                avid_map[bv] = item.get("id")
                online.setdefault(bv, set()).add(name)

    print(f"线上13桶总成员: {len(online)} 条唯一bvid")
    unplanned = {bv: buckets for bv, buckets in online.items() if bv not in plan_bvids}
    print(f"\n计划外(不在classified CSV)线上视频: {len(unplanned)} 条")
    for bv, buckets in sorted(unplanned.items()):
        in_crawl = bv in set(v['bvid'])
        row = v[v['bvid'] == bv]
        if not row.empty:
            title = str(row.iloc[0].get('title', ''))[:40]
            attr = row.iloc[0].get('attr')
            valid = row.iloc[0].get('is_valid')
            src = row.iloc[0].get('source_folder_title', '')
            print(f"  {bv} | 桶={sorted(buckets)} | 爬取时有(attr={attr},is_valid={valid},源={src}) | {title}")
        else:
            print(f"  {bv} | 桶={sorted(buckets)} | 爬取CSV无此记录(当日新增?)")

    # 反向：爬取到但线上没有的（计划内却在任何桶都找不到的）
    missing = plan_bvids - set(online.keys())
    print(f"\n计划内但线上任何桶都没有: {len(missing)} 条 {sorted(missing)[:10]}")

    await crawler.close()


if __name__ == "__main__":
    asyncio.run(main())
