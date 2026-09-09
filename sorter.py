"""B站收藏夹整理工具 - 收藏夹内精细化排序模块

实现三区块分箱排序算法:
  Top 区块: 强时效近期内容 (收藏时间倒序)
  Main 区块: 主消费池 (UP主频次降序 → 系列识别 → 发布时间升序)
  Bottom 区块: 冷冻归档区 (收藏超6个月, 收藏时间倒序)
"""

import re
import time
from collections import Counter
from typing import Optional

from bucket_config import TIMELY_KEYWORDS, SERIES_PATTERNS
from models import BiliVideo


class FolderSorter:
    """收藏夹内精细化排序器"""

    def __init__(self, timely_keywords: list[str] = None,
                 series_patterns: list[str] = None,
                 archive_months: int = 6):
        """
        Args:
            timely_keywords: 时效性特征词库
            series_patterns: 系列识别正则列表
            archive_months: 归档月数阈值
        """
        self.timely_keywords = timely_keywords or TIMELY_KEYWORDS
        self.series_patterns = []
        for p in (series_patterns or SERIES_PATTERNS):
            try:
                self.series_patterns.append(re.compile(p))
            except re.error:
                pass
        self.archive_months = archive_months
        self.archive_seconds = archive_months * 30 * 24 * 3600

    def identify_timely(self, video: BiliVideo) -> bool:
        """识别时效性视频: 标题或标签命中时效词库"""
        title_lower = video.title.lower()
        tags_str = getattr(video, '_tags', '')

        for keyword in self.timely_keywords:
            kw_lower = keyword.lower()
            if kw_lower in title_lower:
                return True
            if tags_str and kw_lower in tags_str.lower():
                return True
        return False

    def identify_archived(self, video: BiliVideo, now: float = None) -> bool:
        """识别归档视频: 收藏时间距今 ≥ archive_months

        注意: fav_time == 0 表示收藏时间未知（如「稍后再看」来源），
        此时不能判定为归档——否则会把所有时间未知的视频沉入 Bottom 区块。
        收藏时间未知的视频应归入 Main 区块，交由时效/UP主聚合逻辑处理。
        """
        if now is None:
            now = time.time()
        if not video.fav_time or video.fav_time <= 0:
            return False
        return (now - video.fav_time) >= self.archive_seconds

    def extract_series(self, video: BiliVideo) -> str:
        """从标题提取系列名称

        返回系列标识字符串，若无系列特征则返回空字符串。
        OGV内容优先使用season_id作为系列标识。
        系列标识 = 去掉序号部分的标题前缀
        """
        if video.is_ogv and video.season_id:
            return f"ogv_season_{video.season_id}"

        title = video.title
        for pattern in self.series_patterns:
            match = pattern.search(title)
            if match:
                # 提取系列前缀（序号之前的部分）
                series_prefix = title[:match.start()].strip()
                # 清理尾部分隔符
                series_prefix = re.sub(r'[\s|｜—–·:：]+$', '', series_prefix)
                if series_prefix and len(series_prefix) >= 2:
                    return series_prefix
        return ""

    def _get_group_key(self, video: BiliVideo):
        """获取视频的分组键

        OGV内容按ogv_type_name分组，普通视频按upper.mid分组。
        """
        if video.is_ogv:
            return video.ogv_type_name
        return video.upper.mid

    def _sort_top_block(self, videos: list[BiliVideo]) -> list[BiliVideo]:
        """Top区块排序: 分组键频次降序 → 同组内收藏时间倒序"""
        if not videos:
            return []

        key_counter = Counter(self._get_group_key(v) for v in videos)
        keys_by_freq = sorted(key_counter.keys(), key=lambda k: key_counter[k], reverse=True)

        result = []
        for group_key in keys_by_freq:
            group_videos = [v for v in videos if self._get_group_key(v) == group_key]
            group_videos.sort(key=lambda v: v.fav_time, reverse=True)
            result.extend(group_videos)

        return result

    def _sort_bottom_block(self, videos: list[BiliVideo]) -> list[BiliVideo]:
        """Bottom区块排序: 与 Top 区块同序（分组键频次降序 → 同组内收藏时间倒序）"""
        return self._sort_top_block(videos)

    def _sort_main_block(self, videos: list[BiliVideo]) -> list[BiliVideo]:
        """Main区块排序: 高频UP主 → OGV类型块 → 零散UP主"""
        if not videos:
            return []

        # 步骤一: 分离普通视频和OGV视频
        normal_videos = [v for v in videos if not v.is_ogv]
        ogv_videos = [v for v in videos if v.is_ogv]

        # 步骤二: 普通视频按UP主频次处理
        up_counter = Counter(v.upper.mid for v in normal_videos)
        up_by_freq = sorted(up_counter.keys(), key=lambda mid: up_counter[mid], reverse=True)

        frequent_ups = {mid for mid in up_by_freq if up_counter[mid] >= 2}
        solitary_ups = {mid for mid in up_by_freq if up_counter[mid] == 1}

        result = []

        # 步骤三: 对每个高频UP主，内部按系列+发布时间排序
        for up_mid in frequent_ups:
            up_videos = [v for v in normal_videos if v.upper.mid == up_mid]
            up_videos = self._sort_uploader_block(up_videos)
            result.extend(up_videos)

        # 步骤四: OGV视频按ogv_type_name分组，组内按season_id再pubtime排序
        ogv_type_groups: dict[str, list[BiliVideo]] = {}
        for v in ogv_videos:
            type_name = v.ogv_type_name or "其他OGV"
            if type_name not in ogv_type_groups:
                ogv_type_groups[type_name] = []
            ogv_type_groups[type_name].append(v)

        for type_name, type_videos in ogv_type_groups.items():
            type_videos.sort(key=lambda v: (v.season_id, v.pubtime))
            result.extend(type_videos)

        # 步骤五: 零散UP主的视频（按发布时间升序）
        solitary_videos = [v for v in normal_videos if v.upper.mid in solitary_ups]
        solitary_videos.sort(key=lambda v: v.pubtime)
        result.extend(solitary_videos)

        return result

    def _sort_uploader_block(self, videos: list[BiliVideo]) -> list[BiliVideo]:
        """单个UP主块内排序: 系列识别 → 发布时间升序"""
        if not videos:
            return []

        # 提取系列信息
        for v in videos:
            v.series_name = self.extract_series(v)

        # 分为有系列和无系列两组
        series_groups: dict[str, list[BiliVideo]] = {}
        no_series: list[BiliVideo] = []

        for v in videos:
            if v.series_name:
                if v.series_name not in series_groups:
                    series_groups[v.series_name] = []
                series_groups[v.series_name].append(v)
            else:
                no_series.append(v)

        result = []

        # 系列视频: 按系列出现顺序，每个系列内按发布时间升序
        for series_name, series_videos in series_groups.items():
            series_videos.sort(key=lambda v: v.pubtime)
            result.extend(series_videos)

        # 无系列视频: 按发布时间升序
        no_series.sort(key=lambda v: v.pubtime)
        result.extend(no_series)

        return result

    def sort_folder(self, videos: list[BiliVideo], now: float = None) -> list[BiliVideo]:
        """三区块拼装排序入口

        Returns:
            排序后的视频列表: Top → Main → Bottom
        """
        if now is None:
            now = time.time()

        valid_videos = [v for v in videos if v.is_valid]

        # 分箱
        top_videos = []
        main_videos = []
        bottom_videos = []

        for v in valid_videos:
            is_timely = self.identify_timely(v) and not v.is_ogv
            is_archived = self.identify_archived(v, now)

            v.is_timely = is_timely

            if is_timely and not is_archived:
                v.block_type = "top"
                top_videos.append(v)
            elif is_archived:
                v.block_type = "bottom"
                bottom_videos.append(v)
            else:
                v.block_type = "main"
                main_videos.append(v)

        # 各区块内部排序
        top_sorted = self._sort_top_block(top_videos)
        main_sorted = self._sort_main_block(main_videos)
        bottom_sorted = self._sort_bottom_block(bottom_videos)

        # 拼装: Top → Main → Bottom
        return top_sorted + main_sorted + bottom_sorted

    def get_sort_summary(self, videos: list[BiliVideo]) -> dict:
        """获取排序摘要统计"""
        top_count = sum(1 for v in videos if v.block_type == "top")
        main_count = sum(1 for v in videos if v.block_type == "main")
        bottom_count = sum(1 for v in videos if v.block_type == "bottom")

        # Main区块UP主统计
        main_videos = [v for v in videos if v.block_type == "main"]
        up_counter = Counter(v.upper.name for v in main_videos)
        frequent_ups = {name: count for name, count in up_counter.most_common() if count >= 2}

        return {
            "total": len(videos),
            "top": top_count,
            "main": main_count,
            "bottom": bottom_count,
            "frequent_ups": frequent_ups,
            "solitary_count": sum(1 for c in up_counter.values() if c == 1),
        }
