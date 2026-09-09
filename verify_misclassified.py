"""验证脚本：测试 122 个误分类 BV 号在更新后的分类器中的改善情况"""

import csv
import sys
import os

# 确保项目根目录在 sys.path 中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from models import BiliVideo, BiliUP
from classifier import Layer1WhitelistClassifier, Layer2StructuralClassifier, enrich_video_from_csv
from config import BiliConfig

# ============================================================
# 122 个误分类 BV 号及其期望正确分类
# ============================================================
MISCLASSIFIED = {
    # Should be 知识科普 (was 生活娱乐)
    "BV1SGXsYxESV": "tmp_知识科普", "BV1zMqsYNERc": "tmp_知识科普", "BV1YoQcB9ELf": "tmp_知识科普",
    "BV1i9iKBTEam": "tmp_知识科普", "BV1H4UbBMExm": "tmp_知识科普", "BV1XHmHBeEQk": "tmp_知识科普",
    "BV1awbWz3EfN": "tmp_知识科普", "BV1n9JAzkEhC": "tmp_知识科普", "BV13KE8ziEZc": "tmp_知识科普",
    # Should be 数码科技 (was 生活娱乐)
    "BV1aTVgzvERp": "tmp_数码科技",
    # Should be 影视解说 (was 生活娱乐)
    "BV1b14y1H7ht": "tmp_影视解说", "BV1riBmYMEmX": "tmp_影视解说",
    "BV1YYaSzdEEp": "tmp_影视解说", "BV1ouyYBhEzD": "tmp_影视解说",
    "BV1q4dPB2EJ6": "tmp_影视解说", "BV1nM4y187AN": "tmp_影视解说", "BV1bFcZzaEmP": "tmp_影视解说",
    "BV1wH4y1Z7Bo": "tmp_影视解说",
    # Should be 游戏资讯 (was 生活娱乐)
    "BV1vF5d6DEC8": "tmp_游戏资讯", "BV1J7v4BbEY4": "tmp_游戏资讯",
    # Should be 编程开发 (was 生活娱乐)
    "BV1rP411e7Us": "tmp_编程开发", "BV1oj411D7jk": "tmp_编程开发", "BV1ZK411S7s3": "tmp_编程开发",
    "BV1RY4y1t7nv": "tmp_编程开发", "BV1zU4y1L7Go": "tmp_编程开发",
    # Should be 汽车运动 (was 生活娱乐)
    "BV1aV1xBzEK9": "tmp_汽车运动", "BV1AqVY6PENy": "tmp_汽车运动", "BV1Bbf1BTEFK": "tmp_汽车运动",
    "BV1veF9zfEX3": "tmp_汽车运动",
    # Should be 社会观察 (was 生活娱乐)
    "BV1jj421R7w6": "tmp_社会观察",
    # Should be 投资财经 (was 生活娱乐)
    "BV1cee7zXEXV": "tmp_投资财经",
    # Should be 播客访谈 (was 社会观察)
    "BV1WH4y1c7GP": "tmp_播客访谈",
    # Should be 知识科普 (was 社会观察)
    "BV1mMGt69ENw": "tmp_知识科普",
    # Should be 生活娱乐 (was 数码科技)
    "BV1DJ4m137TV": "tmp_生活娱乐", "BV132421A7VN": "tmp_生活娱乐", "BV1ZM4m1y7aD": "tmp_生活娱乐",
    "BV1hFX6BFECi": "tmp_生活娱乐", "BV1omoHYzEST": "tmp_生活娱乐", "BV1h4cTeaEFE": "tmp_生活娱乐",
    # Should be 播客访谈 (was 数码科技)
    "BV1t64bepEkr": "tmp_播客访谈", "BV1o34y1K7n3": "tmp_播客访谈", "BV1yE421P7f6": "tmp_播客访谈",
    # Should be 社会观察 (was 数码科技)
    "BV1n4TvzTEgd": "tmp_社会观察",
    # Should be 播客访谈 (was 知识科普)
    "BV1aN5d67EKt": "tmp_播客访谈",
    # Should be 游戏资讯 (was 知识科普)
    "BV1414y1d7V8": "tmp_游戏资讯",
    # Should be 编程开发 (was 知识科普)
    "BV1Yt4y1p7Z9": "tmp_编程开发", "BV1be4y1V7QS": "tmp_编程开发",
    # Should be 数码科技 (was 编程开发)
    "BV1z729BYEde": "tmp_数码科技", "BV1V523BiEX7": "tmp_数码科技",
    "BV13yu1zpEfF": "tmp_数码科技", "BV137hHzgEF8": "tmp_数码科技",
    # Should be 影视解说 (was 游戏实况)
    "BV1nEreBYEDd": "tmp_影视解说",
    # Should be 游戏资讯 (was 游戏实况)
    "BV1uejUzGETb": "tmp_游戏资讯", "BV1xh4y1a7dE": "tmp_游戏资讯", "BV1DrUVBkEu5": "tmp_游戏资讯",
    "BV1MddhBrEqq": "tmp_游戏资讯", "BV1t3rSBWE9x": "tmp_游戏资讯", "BV1RazcBoEeB": "tmp_游戏资讯",
    "BV1Ya41177BE": "tmp_游戏资讯", "BV18DAkzFEjF": "tmp_游戏资讯",
    "BV1ctr7BpEEc": "tmp_游戏资讯", "BV1FQcbzNE6f": "tmp_游戏资讯",
    "BV16m4y157S9": "tmp_游戏资讯", "BV1Y5411k7CH": "tmp_游戏资讯",
    # Should be 生活娱乐 (was 游戏实况)
    "BV1UmfoBXExo": "tmp_生活娱乐",
    # Should be 知识科普 (was 游戏实况)
    "BV1u8tEzXEyH": "tmp_知识科普",
    # Should be 社会观察 (was 投资财经) - 财经小辉辉a videos
    "BV1WLPeevEEL": "tmp_社会观察", "BV1Yez3YoEfd": "tmp_社会观察", "BV1bmzmY9EQb": "tmp_社会观察",
    "BV1GDzhYmEXt": "tmp_社会观察", "BV1vEkGYqE3r": "tmp_社会观察", "BV1c1BMYfEr5": "tmp_社会观察",
    "BV1ZqqUYaEKY": "tmp_社会观察", "BV1Q5qrYNEus": "tmp_社会观察", "BV1cPizY3EEC": "tmp_社会观察",
    "BV1ipCpYBEt1": "tmp_社会观察", "BV1fKCzYWEF2": "tmp_社会观察", "BV1KJFVeBEUj": "tmp_社会观察",
    "BV1F1ffYWE6T": "tmp_社会观察", "BV1TmfGYiE6a": "tmp_社会观察", "BV1y8w8eQEhz": "tmp_社会观察",
    "BV1AKcReHEi2": "tmp_社会观察", "BV1MawVejEAA": "tmp_社会观察", "BV1idP5eiEsB": "tmp_社会观察",
    "BV1K39PYhErS": "tmp_社会观察", "BV1ubPxe9EVf": "tmp_社会观察", "BV182KNeBELP": "tmp_社会观察",
    "BV1hEKsebEhY": "tmp_社会观察", "BV1bcNBesEGz": "tmp_社会观察", "BV1XCNHeDE4A": "tmp_社会观察",
    "BV1pzP2eqEu4": "tmp_社会观察", "BV1ypFVevEyd": "tmp_社会观察", "BV1cMZ3YnEJC": "tmp_社会观察",
    "BV1oXo2YGE7k": "tmp_社会观察", "BV19ZXkYUESw": "tmp_社会观察", "BV1YjXMYUE2V": "tmp_社会观察",
    "BV1TnXLYFEX6": "tmp_社会观察", "BV11hQoYzEHU": "tmp_社会观察", "BV1JEXNYDE5v": "tmp_社会观察",
    "BV1R6QNYrEFm": "tmp_社会观察", "BV1xMQHYBEw6": "tmp_社会观察", "BV1MoR5YYEVt": "tmp_社会观察",
    "BV1Wx9JYKEum": "tmp_社会观察", "BV1fqZ2YjEKF": "tmp_社会观察", "BV1sFfKY5Em8": "tmp_社会观察",
    "BV1UaEBzSEiM": "tmp_社会观察", "BV1chEEzDEXn": "tmp_社会观察", "BV1Dj5TzRE76": "tmp_社会观察",
    "BV1E7VazMEmQ": "tmp_社会观察", "BV1AEVmzXEPV": "tmp_社会观察", "BV1gnGbz1Eto": "tmp_社会观察",
    "BV1BHL2zUESm": "tmp_社会观察", "BV1LcG1zYEEG": "tmp_社会观察", "BV1X7Gbz2Ecg": "tmp_社会观察",
    "BV1dx7LzaEd3": "tmp_社会观察", "BV1WwjqzvEK5": "tmp_社会观察", "BV1XAjmzCEeQ": "tmp_社会观察",
    "BV1thJHzgEdy": "tmp_社会观察", "BV1P9MAzpEsG": "tmp_社会观察", "BV1jyM3zhExK": "tmp_社会观察",
    "BV1Lc7WzQEb5": "tmp_社会观察",
    # Should be 播客访谈 (was 投资财经)
    "BV1esutzGE23": "tmp_播客访谈",
}


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


def classify_l1_l2(video: BiliVideo, layer1: Layer1WhitelistClassifier, layer2: Layer2StructuralClassifier) -> str:
    """仅使用 Layer 1 + Layer 2 分类，跳过 Layer 3 LLM"""
    result = layer1.classify(video)
    if result:
        return result
    result = layer2.classify(video)
    if result:
        return result
    return "tmp_待分类"


def analyze_root_cause(video: BiliVideo, row: dict, expected: str, actual: str, layer2: Layer2StructuralClassifier) -> str:
    """分析误分类的根本原因"""
    tid = int(row.get("tid", 0) or 0)
    tid_v2 = int(row.get("tid_v2", 0) or 0)
    parent_tid = int(row.get("parent_tid", 0) or 0)
    tname = row.get("tname", "")
    tname_v2 = row.get("tname_v2", "")
    tags = row.get("tags", "")
    title = video.title

    # 检查分区是否指向了错误的桶
    tid_bucket = None
    for check_tid in [tid, tid_v2, parent_tid]:
        if check_tid and check_tid in layer2.tid_bucket_map:
            tid_bucket = layer2.tid_bucket_map[check_tid]
            break
    if tid_bucket is None:
        for check_tid in [tid, tid_v2, parent_tid]:
            if check_tid and check_tid in layer2.weak_tid_bucket_map:
                tid_bucket = layer2.weak_tid_bucket_map[check_tid]
                break

    # 检查标签匹配
    tag_matches = {}
    tags_str = getattr(video, '_tags', '')
    if tags_str:
        feature_words = set()
        for tag in tags_str.split(','):
            tag = tag.strip()
            if tag:
                feature_words.add(tag.lower())
        for bucket_name, keyword_set in layer2._tag_keyword_sets.items():
            matched = feature_words & keyword_set
            if matched:
                tag_matches[bucket_name] = matched

    # 检查标题匹配
    title_lower = title.lower()
    title_matches = {}
    for bucket_name, keyword_set in layer2._title_keyword_sets.items():
        for keyword in keyword_set:
            if keyword in title_lower:
                if bucket_name not in title_matches:
                    title_matches[bucket_name] = set()
                title_matches[bucket_name].add(keyword)

    # 分析原因
    reasons = []

    # 1. 分区误导
    if tid_bucket and tid_bucket != expected:
        reasons.append(f"分区误导(tid={tid}/{tname}, tid_v2={tid_v2}/{tname_v2}, parent={parent_tid} → {tid_bucket})")

    # 2. 标签指向错误桶
    wrong_tag_buckets = {b: m for b, m in tag_matches.items() if b != expected}
    if wrong_tag_buckets:
        for b, m in wrong_tag_buckets.items():
            reasons.append(f"标签指向错误桶({b}: {','.join(list(m)[:5])})")

    # 3. 标签未命中期望桶
    if expected not in tag_matches and expected not in title_matches:
        reasons.append(f"标签/标题均未命中期望桶({expected})")

    # 4. 标题关键词指向错误桶
    wrong_title_buckets = {b: m for b, m in title_matches.items() if b != expected}
    if wrong_title_buckets:
        for b, m in wrong_title_buckets.items():
            reasons.append(f"标题关键词指向错误桶({b}: {','.join(list(m)[:5])})")

    # 5. 标签太弱（命中数不足阈值）
    if expected in tag_matches and len(tag_matches[expected]) < 2:
        reasons.append(f"期望桶标签命中不足阈值({len(tag_matches[expected])}个: {','.join(list(tag_matches[expected])[:5])})")

    if not reasons:
        reasons.append("原因未明")

    return "; ".join(reasons)


def main():
    csv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output", "videos.csv")
    print(f"读取 CSV: {csv_path}")
    all_videos = load_videos_from_csv(csv_path)
    print(f"共加载 {len(all_videos)} 条视频")

    # 初始化分类器
    config = BiliConfig()
    layer1 = Layer1WhitelistClassifier(config.UP_WHITELIST, config.MIXED_TYPE_UPS)
    layer2 = Layer2StructuralClassifier(
        config.TID_BUCKET_MAP,
        config.TAG_KEYWORD_DICT,
        config.TITLE_KEYWORD_DICT,
        config.TAG_HIT_THRESHOLD,
        config.WEAK_TID_BUCKET_MAP,
    )

    # 分类统计
    correct = []
    still_wrong = []
    not_found = []

    for bvid, expected in MISCLASSIFIED.items():
        if bvid not in all_videos:
            not_found.append(bvid)
            continue

        video, row = all_videos[bvid]
        actual = classify_l1_l2(video, layer1, layer2)

        if actual == expected:
            correct.append((bvid, video.title, expected))
        else:
            still_wrong.append((bvid, video.title, expected, actual, row, video))

    # ============================================================
    # 输出结果到文件（避免终端编码问题）
    # ============================================================
    output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output", "verify_result.txt")
    with open(output_path, "w", encoding="utf-8") as out:
        _p = lambda *args, **kwargs: print(*args, file=out, **kwargs)

        _p("\n" + "=" * 80)
        _p("分类验证结果汇总")
        _p("=" * 80)

        total = len(MISCLASSIFIED)
        found = total - len(not_found)
        _p(f"\n总测试数: {total}")
        _p(f"CSV中找到: {found}")
        _p(f"CSV中未找到: {len(not_found)}")
        if not_found:
            _p(f"  未找到的BV号: {', '.join(not_found)}")

        if found > 0:
            _p(f"\n✅ 已修正 (Layer1+Layer2 现在分类正确): {len(correct)} / {found} ({100*len(correct)/found:.1f}%)")
            _p(f"❌ 仍误分类: {len(still_wrong)} / {found} ({100*len(still_wrong)/found:.1f}%)")
        else:
            _p(f"\n⚠️ CSV中未找到任何测试BV号，无法计算准确率")

        # 按期望桶分组统计修正情况
        _p("\n" + "-" * 60)
        _p("按期望桶分组统计:")
        _p("-" * 60)
        expected_buckets = {}
        for bvid, expected in MISCLASSIFIED.items():
            if bvid in not_found:
                continue
            if expected not in expected_buckets:
                expected_buckets[expected] = {"correct": 0, "wrong": 0}
            is_correct = any(c[0] == bvid for c in correct)
            if is_correct:
                expected_buckets[expected]["correct"] += 1
            else:
                expected_buckets[expected]["wrong"] += 1

        for bucket in sorted(expected_buckets.keys()):
            c = expected_buckets[bucket]["correct"]
            w = expected_buckets[bucket]["wrong"]
            total_bucket = c + w
            _p(f"  {bucket}: {c}/{total_bucket} 已修正 ({100*c/total_bucket:.0f}%)")

        # 仍误分类的详细列表
        if still_wrong:
            _p("\n" + "=" * 80)
            _p("仍误分类的视频详情:")
            _p("=" * 80)

            for bvid, title, expected, actual, row, video in still_wrong:
                tid = row.get("tid", "")
                tname = row.get("tname", "")
                tags = row.get("tags", "")
                truncated_title = title[:50] + "..." if len(title) > 50 else title
                truncated_tags = tags[:80] + "..." if len(tags) > 80 else tags
                _p(f"\n  BV: {bvid}")
                _p(f"  标题: {truncated_title}")
                _p(f"  当前结果: {actual}  |  期望结果: {expected}")
                _p(f"  tid: {tid} ({tname})  |  tags: {truncated_tags}")

            # 根因分析
            _p("\n" + "=" * 80)
            _p("根因分析:")
            _p("=" * 80)

            root_cause_categories = {
                "分区误导": [],
                "标签指向错误桶": [],
                "标签/标题未命中期望桶": [],
                "标题关键词指向错误桶": [],
                "标签命中不足阈值": [],
                "原因未明": [],
            }

            for bvid, title, expected, actual, row, video in still_wrong:
                cause = analyze_root_cause(video, row, expected, actual, layer2)
                truncated_title = title[:40] + "..." if len(title) > 40 else title

                # 归类到根因大类
                if "分区误导" in cause:
                    root_cause_categories["分区误导"].append((bvid, truncated_title, actual, expected, cause))
                if "标签指向错误桶" in cause:
                    root_cause_categories["标签指向错误桶"].append((bvid, truncated_title, actual, expected, cause))
                if "标签/标题均未命中期望桶" in cause:
                    root_cause_categories["标签/标题未命中期望桶"].append((bvid, truncated_title, actual, expected, cause))
                if "标题关键词指向错误桶" in cause:
                    root_cause_categories["标题关键词指向错误桶"].append((bvid, truncated_title, actual, expected, cause))
                if "标签命中不足阈值" in cause:
                    root_cause_categories["标签命中不足阈值"].append((bvid, truncated_title, actual, expected, cause))
                if "原因未明" in cause:
                    root_cause_categories["原因未明"].append((bvid, truncated_title, actual, expected, cause))

            for category, items in root_cause_categories.items():
                if not items:
                    continue
                _p(f"\n  【{category}】({len(items)} 条)")
                for bvid, truncated_title, actual, expected, cause in items:
                    _p(f"    {bvid} | {actual}→{expected} | {truncated_title}")
                    _p(f"      详细: {cause}")

        # 修正成功的视频列表
        if correct:
            _p("\n" + "=" * 80)
            _p("已修正的视频列表:")
            _p("=" * 80)
            # 按期望桶分组
            by_bucket = {}
            for bvid, title, expected in correct:
                if expected not in by_bucket:
                    by_bucket[expected] = []
                by_bucket[expected].append((bvid, title))

            for bucket in sorted(by_bucket.keys()):
                items = by_bucket[bucket]
                _p(f"\n  {bucket} ({len(items)} 条):")
                for bvid, title in items:
                    truncated = title[:60] + "..." if len(title) > 60 else title
                    _p(f"    ✅ {bvid} | {truncated}")

        _p("\n" + "=" * 80)
        _p("验证完成")
        _p("=" * 80)

    print(f"结果已写入: {output_path}")


if __name__ == "__main__":
    main()
