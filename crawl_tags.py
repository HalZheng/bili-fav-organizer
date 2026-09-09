"""为所有视频补充爬取标签(tags)和分区(tid/tname)信息

需要补充的字段:
- tags: 视频标签列表 (逗号分隔)
- tid: 分区ID
- tname: 分区名称
- tid_v2: 新版分区ID
- tname_v2: 新版分区名称

策略:
- 使用 /x/web-interface/view/detail/tag 获取标签(新接口，支持BGM)
- 使用 /x/web-interface/view 获取分区信息(tid/tname)
- 同一视频的详情和标签请求并发发出
- 多个视频之间并发处理，由 AsyncRateLimiter 控制速率
- 支持断点续爬（记录已爬取的bvid）
"""
import asyncio
import json
import re
import sys
import os
import time
from typing import Optional

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

sys.path.insert(0, os.path.dirname(__file__))

import pandas as pd
from crawler import BiliCrawler, enc_wbi
from config import BiliConfig


EXISTING_CSV = os.path.join(os.path.dirname(__file__), "output", "videos.csv")
PROGRESS_FILE = os.path.join(os.path.dirname(__file__), "output", "tag_progress.json")
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "output")

# B站分区映射表 (tid -> 分区名)
TID_MAP = {
    # 大区
    160: "生活", 4: "游戏", 5: "娱乐", 36: "知识", 181: "影视",
    3: "音乐", 1: "动画", 155: "时尚", 211: "美食", 223: "汽车",
    234: "运动", 188: "科技", 217: "动物圈", 129: "舞蹈", 167: "国创",
    119: "鬼畜", 177: "纪录片", 13: "番剧", 11: "电视剧", 23: "电影",
    # 生活区子分区
    138: "搞笑", 239: "家居房产", 161: "手工", 162: "绘画", 21: "日常",
    # 游戏区子分区
    17: "单机游戏", 65: "网络游戏", 172: "手机游戏", 171: "电子竞技",
    173: "桌游棋牌", 136: "音游", 121: "GMV", 19: "Mugen",
    # 娱乐区子分区
    71: "综艺", 137: "明星",
    # 知识区子分区
    201: "科学科普", 124: "社科·法律·心理", 228: "人文历史", 207: "财经商业",
    208: "校园学习", 209: "职业职场", 229: "设计·创意", 122: "野生技能协会",
    # 影视区子分区
    85: "短片", 182: "影视杂谈", 183: "影视剪辑", 184: "预告·资讯",
    # 音乐区子分区
    130: "音乐综合", 29: "音乐现场", 59: "演奏", 31: "翻唱",
    193: "MV", 30: "VOCALOID·UTAU", 194: "电音", 28: "原创音乐",
    # 动画区子分区
    24: "MAD·AMV", 25: "MMD·3D", 27: "综合", 47: "短片·手书·配音",
    210: "手办·模玩", 86: "特摄",
    # 时尚区子分区
    157: "美妆护肤", 158: "穿搭", 159: "时尚潮流",
    # 美食区子分区
    76: "美食制作", 212: "美食侦探", 213: "美食测评", 214: "田园美食", 215: "美食记录",
    # 汽车区子分区
    176: "汽车生活", 224: "汽车文化", 225: "汽车极客", 240: "摩托车",
    226: "智能出行", 227: "购车攻略",
    # 运动区子分区
    235: "篮球·足球", 164: "健身", 236: "竞技体育", 237: "运动文化", 238: "运动综合",
    # 科技区子分区
    95: "数码", 230: "软件应用", 231: "计算机技术", 232: "工业·工程·机械", 233: "极客DIY",
    # 动物圈子分区
    218: "喵星人", 219: "汪星人", 221: "野生动物", 222: "爬宠", 220: "大熊猫", 75: "动物综合",
    # 舞蹈区子分区
    20: "宅舞", 154: "舞蹈综合", 156: "舞蹈教程", 198: "街舞", 199: "明星舞蹈", 200: "中国舞",
    # 国创区子分区
    153: "国产动画", 168: "国产原创相关", 169: "布袋戏", 170: "资讯", 195: "动态漫·广播剧",
    # 鬼畜区子分区
    22: "鬼畜调教", 26: "音MAD", 126: "人力VOCALOID", 216: "鬼畜剧场", 127: "教程演示",
    # 纪录片区子分区
    37: "人文·历史", 178: "科学·探索·自然", 179: "军事", 180: "社会·美食·旅行",
    # 番剧区子分区
    51: "资讯", 152: "官方延伸", 32: "完结动画", 33: "连载动画",
    # 电视剧区子分区
    185: "国产剧", 187: "海外剧",
    # 电影区子分区
    83: "其他国家", 145: "欧美电影", 146: "日本电影", 147: "国产电影",
}

# 大区映射 (子分区tid -> 大区tid)
PARENT_MAP = {
    138: 160, 239: 160, 161: 160, 162: 160, 21: 160,  # 生活
    17: 4, 65: 4, 172: 4, 171: 4, 173: 4, 136: 4, 121: 4, 19: 4,  # 游戏
    71: 5, 137: 5,  # 娱乐
    201: 36, 124: 36, 228: 36, 207: 36, 208: 36, 209: 36, 229: 36, 122: 36,  # 知识
    85: 181, 182: 181, 183: 181, 184: 181,  # 影视
    130: 3, 29: 3, 59: 3, 31: 3, 193: 3, 30: 3, 194: 3, 28: 3,  # 音乐
    24: 1, 25: 1, 27: 1, 47: 1, 210: 1, 86: 1,  # 动画
    157: 155, 158: 155, 159: 155,  # 时尚
    76: 211, 212: 211, 213: 211, 214: 211, 215: 211,  # 美食
    176: 223, 224: 223, 225: 223, 240: 223, 226: 223, 227: 223,  # 汽车
    235: 234, 164: 234, 236: 234, 237: 234, 238: 234,  # 运动
    95: 188, 230: 188, 231: 188, 232: 188, 233: 188,  # 科技
    218: 217, 219: 217, 221: 217, 222: 217, 220: 217, 75: 217,  # 动物圈
    20: 129, 154: 129, 156: 129, 198: 129, 199: 129, 200: 129,  # 舞蹈
    153: 167, 168: 167, 169: 167, 170: 167, 195: 167,  # 国创
    22: 119, 26: 119, 126: 119, 216: 119, 127: 119,  # 鬼畜
    37: 177, 178: 177, 179: 177, 180: 177,  # 纪录片
    51: 13, 152: 13, 32: 13, 33: 13,  # 番剧
    185: 11, 187: 11,  # 电视剧
    83: 23, 145: 23, 146: 23, 147: 23,  # 电影
}

PROGRESS_SAVE_INTERVAL = 100


async def fetch_video_tags(crawler: BiliCrawler, bvid: str) -> dict:
    """并发获取单个视频的详情和标签"""
    detail_task = crawler._request('/x/web-interface/view', {'bvid': bvid}, use_wbi=True)
    tag_task = crawler._request('/x/web-interface/view/detail/tag', {'bvid': bvid}, use_wbi=True)

    detail_data, tag_data_resp = await asyncio.gather(detail_task, tag_task, return_exceptions=True)

    # 处理异常
    if isinstance(detail_data, Exception):
        raise detail_data
    if isinstance(tag_data_resp, Exception):
        raise tag_data_resp

    # 解析详情
    info = detail_data.get('data', {})
    tid = info.get('tid', 0)
    tname = info.get('tname', '') or TID_MAP.get(tid, '')
    tid_v2 = info.get('tid_v2', 0)
    tname_v2 = info.get('tname_v2', '')
    parent_tid = PARENT_MAP.get(tid, tid)
    parent_name = TID_MAP.get(parent_tid, '')

    if not tname and tid in TID_MAP:
        tname = TID_MAP[tid]
    if not tname_v2 and tid_v2 in TID_MAP:
        tname_v2 = TID_MAP[tid_v2]

    # 解析标签
    tags_list = tag_data_resp.get('data', []) or []
    tag_names = []
    for tag_item in tags_list:
        tn = tag_item.get('tag_name', '')
        if tn:
            tag_names.append(tn)
    tags_str = ','.join(tag_names)

    return {
        'tags': tags_str,
        'tid': tid,
        'tname': tname,
        'tid_v2': tid_v2,
        'tname_v2': tname_v2,
        'parent_tid': parent_tid,
        'parent_name': parent_name,
    }


async def main():
    config = BiliConfig()
    crawler = BiliCrawler(config)

    # 加载数据
    df = pd.read_csv(EXISTING_CSV)
    print(f"数据: {len(df)} 条")

    # 加载进度
    done_bvids = set()
    tag_data = {}  # bvid -> {tags, tid, tname, tid_v2, tname_v2, parent_tid, parent_name}
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, 'r', encoding='utf-8') as f:
            progress = json.load(f)
            done_bvids = set(progress.get("done", []))
            tag_data = progress.get("data", {})
        print(f"断点续爬: 已完成 {len(done_bvids)} 条")

    # 需要爬取的视频（只爬有效的）
    valid_df = df[df["is_valid"] == True] if "is_valid" in df.columns else df
    need_fetch = valid_df[~valid_df["bvid"].isin(done_bvids)]
    print(f"需要爬取: {len(need_fetch)} 条 (有效视频)")

    if len(need_fetch) == 0:
        print("全部已爬取，跳过")
    else:
        success = 0
        failed = 0
        t_start = time.time()
        completed_count = 0

        semaphore = asyncio.Semaphore(config.MAX_CONCURRENT_REQUESTS)
        progress_lock = asyncio.Lock()

        async def process_one(row):
            nonlocal success, failed, completed_count
            bvid = row["bvid"]
            if not bvid or bvid in done_bvids:
                return None

            async with semaphore:
                try:
                    result = await fetch_video_tags(crawler, bvid)
                    async with progress_lock:
                        tag_data[bvid] = result
                        done_bvids.add(bvid)
                        success += 1
                        completed_count += 1

                        # 进度
                        if completed_count % 50 == 0 or completed_count == len(need_fetch):
                            elapsed = time.time() - t_start
                            rate = completed_count / elapsed * 60 if elapsed > 0 else 0
                            eta = (len(need_fetch) - completed_count) / rate if rate > 0 else 0
                            print(f"  进度: {completed_count}/{len(need_fetch)} | 成功: {success} | 失败: {failed} | {rate:.0f}/min | ETA: {eta:.0f}min")

                        # 保存进度(每100条)
                        if completed_count % PROGRESS_SAVE_INTERVAL == 0:
                            with open(PROGRESS_FILE, 'w', encoding='utf-8') as f:
                                json.dump({"done": list(done_bvids), "data": tag_data}, f, ensure_ascii=False)

                    return bvid
                except Exception as e:
                    async with progress_lock:
                        failed += 1
                        completed_count += 1
                    print(f"  [ERROR] {bvid}: {e}")
                    return None

        tasks = [process_one(row) for _, row in need_fetch.iterrows()]
        await asyncio.gather(*tasks)

        print(f"\n爬取完成: 成功 {success}, 失败 {failed}")

        # 保存最终进度
        with open(PROGRESS_FILE, 'w', encoding='utf-8') as f:
            json.dump({"done": list(done_bvids), "data": tag_data}, f, ensure_ascii=False)

    # 合并到CSV
    print(f"\n合并标签和分区数据到CSV...")
    df['tags'] = df['bvid'].map(lambda bvid: tag_data.get(bvid, {}).get('tags', ''))
    df['tid'] = df['bvid'].map(lambda bvid: tag_data.get(bvid, {}).get('tid', 0))
    df['tname'] = df['bvid'].map(lambda bvid: tag_data.get(bvid, {}).get('tname', ''))
    df['tid_v2'] = df['bvid'].map(lambda bvid: tag_data.get(bvid, {}).get('tid_v2', 0))
    df['tname_v2'] = df['bvid'].map(lambda bvid: tag_data.get(bvid, {}).get('tname_v2', ''))
    df['parent_tid'] = df['bvid'].map(lambda bvid: tag_data.get(bvid, {}).get('parent_tid', 0))
    df['parent_name'] = df['bvid'].map(lambda bvid: tag_data.get(bvid, {}).get('parent_name', ''))

    # 统计
    has_tags = (df['tags'] != '').sum()
    has_tid = (df['tid'] != 0).sum()
    print(f"有标签: {has_tags}/{len(df)}")
    print(f"有分区: {has_tid}/{len(df)}")

    # 分区分布
    print(f"\n=== 大区分布 ===")
    parent_counts = df[df['parent_name'] != '']['parent_name'].value_counts()
    for name, count in parent_counts.items():
        pct = count / len(df) * 100
        print(f"  {name}: {count} ({pct:.1f}%)")

    # 导出
    # 清理非法字符
    illegal_re = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]')
    for col in df.select_dtypes(include=["object"]).columns:
        df[col] = df[col].apply(
            lambda x: illegal_re.sub('', x) if isinstance(x, str) else x
        )

    # 重新计算辅助列
    df["pubtime_str"] = pd.to_datetime(df["pubtime"], unit="s", errors="coerce")
    df["fav_time_str"] = pd.to_datetime(df["fav_time"], unit="s", errors="coerce")
    if "duration_sec" in df.columns:
        df["duration_min"] = (df["duration_sec"] / 60).round(1)

    df.to_csv(EXISTING_CSV, index=False, encoding="utf-8-sig")
    print(f"\nCSV已导出: {EXISTING_CSV}")

    df.to_excel(os.path.join(OUTPUT_DIR, "videos.xlsx"), index=False, engine="openpyxl")
    print(f"Excel已导出")

    await crawler.close()


if __name__ == "__main__":
    asyncio.run(main())
