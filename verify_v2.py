"""验证脚本 v2：测试用户反馈的误分类 BV 号在当前分类器中的表现"""

import csv
import sys
import os

# 确保项目根目录在 sys.path 中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from models import BiliVideo, BiliUP
from classifier import FunnelClassifier, enrich_video_from_csv
from config import BiliConfig

# ============================================================
# 用户反馈的误分类 BV 号及其期望正确分类（按桶分组）
# ============================================================
EXPECTED = {
    "tmp_知识科普": [
        "BV1SGXsYxESV", "BV1zMqsYNERc", "BV1YoQcB9ELf", "BV1i9iKBTEam",
        "BV1H4UbBMExm", "BV1XHmHBeEQk", "BV1awbWz3EfN", "BV1n9JAzkEhC",
        "BV13KE8ziEZc", "BV1mMGt69ENw",
    ],
    "tmp_数码科技": [
        "BV1aTVgzvERp", "BV1z729BYEde", "BV1V523BiEX7", "BV13yu1zpEfF",
        "BV137hHzgEF8",
    ],
    "tmp_影视解说": [
        "BV1b14y1H7ht", "BV1riBmYMEmX", "BV1YYaSzdEEp", "BV1ouyYBhEzD",
        "BV1q4dPB2EJ6", "BV1nM4y187AN", "BV1bFcZzaEmP", "BV1wH4y1Z7Bo",
        "BV1nEreBYEDd",
    ],
    "tmp_游戏资讯": [
        "BV1vF5d6DEC8", "BV1J7v4BbEY4", "BV1uejUzGETb", "BV1xh4y1a7dE",
        "BV1DrUVBkEu5", "BV1MddhBrEqq", "BV1t3rSBWE9x", "BV1RazcBoEeB",
        "BV1Ya41177BE", "BV18DAkzFEjF", "BV1ctr7BpEEc", "BV1FQcbzNE6f",
        "BV16m4y157S9", "BV1Y5411k7CH", "BV1414y1d7V8",
    ],
    "tmp_编程开发": [
        "BV1rP411e7Us", "BV1oj411D7jk", "BV1ZK411S7s3", "BV1RY4y1t7nv",
        "BV1zU4y1L7Go", "BV1Yt4y1p7Z9", "BV1be4y1V7QS",
    ],
    "tmp_汽车运动": [
        "BV1aV1xBzEK9", "BV1AqVY6PENy", "BV1Bbf1BTEFK", "BV1veF9zfEX3",
    ],
    "tmp_社会观察": [
        "BV1jj421R7w6", "BV1n4TvzTEgd", "BV1WLPeevEEL", "BV1Yez3YoEfd",
        "BV1bmzmY9EQb", "BV1GDzhYmEXt", "BV1vEkGYqE3r", "BV1c1BMYfEr5",
        "BV1ZqqUYaEKY", "BV1Q5qrYNEus", "BV1cPizY3EEC", "BV1ipCpYBEt1",
        "BV1fKCzYWEF2", "BV1KJFVeBEUj", "BV1F1ffYWE6T", "BV1TmfGYiE6a",
        "BV1y8w8eQEhz", "BV1AKcReHEi2", "BV1MawVejEAA", "BV1idP5eiEsB",
        "BV1K39PYhErS", "BV1ubPxe9EVf", "BV182KNeBELP", "BV1hEKsebEhY",
        "BV1bcNBesEGz", "BV1XCNHeDE4A", "BV1pzP2eqEu4", "BV1ypFVevEyd",
        "BV1cMZ3YnEJC", "BV1oXo2YGE7k", "BV19ZXkYUESw", "BV1YjXMYUE2V",
        "BV1TnXLYFEX6", "BV11hQoYzEHU", "BV1JEXNYDE5v", "BV1R6QNYrEFm",
        "BV1xMQHYBEw6", "BV1MoR5YYEVt", "BV1Wx9JYKEum", "BV1fqZ2YjEKF",
        "BV1sFfKY5Em8", "BV1UaEBzSEiM", "BV1chEEzDEXn", "BV1Dj5TzRE76",
        "BV1E7VazMEmQ", "BV1AEVmzXEPV", "BV1gnGbz1Eto", "BV1BHL2zUESm",
        "BV1LcG1zYEEG", "BV1X7Gbz2Ecg", "BV1dx7LzaEd3", "BV1WwjqzvEK5",
        "BV1XAjmzCEeQ", "BV1thJHzgEdy", "BV1P9MAzpEsG", "BV1jyM3zhExK",
        "BV1Lc7WzQEb5",
    ],
    "tmp_投资财经": ["BV1cee7zXEXV"],
    "tmp_播客访谈": [
        "BV1WH4y1c7GP", "BV1t64bepEkr", "BV1o34y1K7n3", "BV1yE421P7f6",
        "BV1aN5d67EKt", "BV1esutzGE23",
    ],
    "tmp_生活娱乐": [
        "BV1DJ4m137TV", "BV132421A7VN", "BV1ZM4m1y7aD", "BV1hFX6BFECi",
        "BV1omoHYzEST", "BV1h4cTeaEFE", "BV1UmfoBXExo",
    ],
    "tmp_知识科普_alt": ["BV1u8tEzXEyH"],  # 航空科普
}
# Merge alt buckets
EXPECTED["tmp_知识科普"].extend(EXPECTED.pop("tmp_知识科普_alt", []))


def load_videos_from_csv(csv_path: str) -> dict:
    """从 CSV 加载视频数据，返回 {bvid: (BiliVideo, row_dict)}"""
    videos = {}
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            bvid = row.get("bvid", "")
            if not bvid:
                continue
            video = BiliVideo(
                id=int(row.get("avid", 0) or 0),
                bvid=bvid,
                title=row.get("title", ""),
                intro=row.get("intro", ""),
                upper=BiliUP(
                    mid=int(row.get("up_mid", 0) or 0),
                    name=row.get("up_name", ""),
                ),
                duration=int(row.get("duration_sec", 0) or 0),
                page=int(row.get("page_count", 1) or 1),
                view_count=int(row.get("view_count", 0) or 0),
                danmaku_count=int(row.get("danmaku_count", 0) or 0),
                collect_count=int(row.get("collect_count", 0) or 0),
                pubtime=int(row.get("pubtime", 0) or 0),
                fav_time=int(row.get("fav_time", 0) or 0),
                attr=int(row.get("attr", 0) or 0),
                type=int(row.get("type", 2) or 2),
                source_folder_id=int(row.get("source_folder_id", 0) or 0),
                source_folder_title=row.get("source_folder_title", ""),
            )
            # 从 CSV 补充分区和标签信息
            enrich_video_from_csv(video, row)
            videos[bvid] = (video, row)
    return videos


def classify_l1_l2(video: BiliVideo, classifier: FunnelClassifier) -> str:
    """仅使用 Layer 1 + Layer 2 分类，跳过 Layer 3 LLM"""
    result = classifier.layer1.classify(video)
    if result:
        return result
    result = classifier.layer2.classify(video)
    if result:
        return result
    return "tmp_待分类"


def main():
    csv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output", "videos.csv")
    print(f"读取 CSV: {csv_path}")
    all_videos = load_videos_from_csv(csv_path)
    print(f"共加载 {len(all_videos)} 条视频")

    # 初始化分类器
    config = BiliConfig()
    classifier = FunnelClassifier(config)

    # 展平 EXPECTED 为 {bvid: expected_bucket}
    bvid_expected = {}
    for bucket, bvids in EXPECTED.items():
        for bvid in bvids:
            bvid_expected[bvid] = bucket

    # 分类并比较
    results = []  # (bvid, title, expected, actual, match)
    not_found = []

    for bvid, expected in bvid_expected.items():
        if bvid not in all_videos:
            not_found.append(bvid)
            continue
        video, _ = all_videos[bvid]
        actual = classify_l1_l2(video, classifier)
        match = actual == expected
        results.append((bvid, video.title, expected, actual, match))

    # ============================================================
    # 输出结果
    # ============================================================
    total_tested = len(results)
    correct_count = sum(1 for r in results if r[4])
    wrong_count = total_tested - correct_count

    print()
    print("=" * 100)
    print("分类验证结果 (Layer 1 + Layer 2)")
    print("=" * 100)

    # 逐条结果表格
    header = f"{'BV号':<18} {'标题(截断)':<40} {'期望桶':<16} {'实际桶':<16} {'匹配'}"
    print(header)
    print("-" * 100)
    for bvid, title, expected, actual, match in results:
        truncated_title = (title[:37] + "...") if len(title) > 40 else title
        mark = "✓" if match else "✗"
        print(f"{bvid:<18} {truncated_title:<40} {expected:<16} {actual:<16} {mark}")

    # 汇总
    print()
    print("=" * 100)
    print("汇总统计")
    print("=" * 100)
    print(f"总测试数: {total_tested}")
    print(f"CSV中未找到: {len(not_found)}" + (f" ({', '.join(not_found)})" if not_found else ""))
    print(f"正确数: {correct_count}")
    print(f"错误数: {wrong_count}")
    if total_tested > 0:
        print(f"准确率: {100 * correct_count / total_tested:.1f}%")

    # 按期望桶分组统计
    print()
    print("-" * 60)
    print("按期望桶分组统计:")
    print("-" * 60)
    bucket_stats = {}
    for bvid, title, expected, actual, match in results:
        if expected not in bucket_stats:
            bucket_stats[expected] = {"correct": 0, "total": 0}
        bucket_stats[expected]["total"] += 1
        if match:
            bucket_stats[expected]["correct"] += 1

    for bucket in sorted(bucket_stats.keys()):
        c = bucket_stats[bucket]["correct"]
        t = bucket_stats[bucket]["total"]
        pct = 100 * c / t if t > 0 else 0
        print(f"  {bucket:<20} {c:>3}/{t:<3} 正确 ({pct:5.1f}%)")

    # 仍误分类的详情
    wrong_results = [(bvid, title, expected, actual) for bvid, title, expected, actual, match in results if not match]
    if wrong_results:
        print()
        print("=" * 100)
        print("仍误分类的视频详情:")
        print("=" * 100)
        for bvid, title, expected, actual in wrong_results:
            truncated_title = (title[:50] + "...") if len(title) > 50 else title
            print(f"  {bvid} | {truncated_title}")
            print(f"    期望: {expected}  实际: {actual}")

    print()
    print("=" * 100)
    print("验证完成")
    print("=" * 100)


if __name__ == "__main__":
    main()
