"""B站收藏夹整理工具 - 数据模型"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class BiliUP:
    """UP主信息"""
    mid: int = 0
    name: str = ""
    face: str = ""  # 头像URL


@dataclass
class BiliVideo:
    """B站视频信息"""
    # 基础信息
    id: int = 0  # avid
    bvid: str = ""
    title: str = ""
    intro: str = ""  # 简介
    cover: str = ""  # 封面URL

    # UP主
    upper: BiliUP = field(default_factory=BiliUP)

    # 时间
    ctime: int = 0  # 投稿时间戳
    pubtime: int = 0  # 发布时间戳
    fav_time: int = 0  # 收藏时间戳

    # 统计
    duration: int = 0  # 时长(秒)
    page: int = 1  # 分P数
    view_count: int = 0  # 播放量
    danmaku_count: int = 0  # 弹幕数
    collect_count: int = 0  # 收藏数

    # 状态
    attr: int = 0  # 0=正常, 9=UP删除, 1=其他删除
    type: int = 2  # 2=视频, 12=音频, 21=合集

    # 来源收藏夹
    source_folder_id: int = 0
    source_folder_title: str = ""

    # 分类结果
    category: str = ""  # 分类名

    # 排序辅助字段
    series_name: str = ""  # 系列名称（从标题提取）
    is_timely: bool = False  # 是否时效性视频
    block_type: str = ""  # 所属区块: "top" / "main" / "bottom"

    # OGV (非UP主内容) 字段
    ogv_type_name: str = ""  # OGV内容类型: 番剧/电影/纪录片/国创/电视剧
    ogv_type_id: int = 0  # OGV类型编号: 1=番剧, 2=电影, 3=纪录片, 4=国创, 5=电视剧
    season_id: int = 0  # 剧集季度ID

    @property
    def is_valid(self) -> bool:
        """视频是否有效(未失效)"""
        return self.attr == 0

    @property
    def is_ogv(self) -> bool:
        """是否为OGV内容(番剧/电影/纪录片/国创/电视剧等非UP主上传内容)"""
        return self.type == 24

    @property
    def url(self) -> str:
        """视频URL"""
        return f"https://www.bilibili.com/video/{self.bvid}" if self.bvid else ""

    def to_dict(self) -> dict:
        """转换为字典，方便导出"""
        return {
            "bvid": self.bvid,
            "avid": self.id,
            "title": self.title,
            "intro": self.intro,
            "up_mid": self.upper.mid,
            "up_name": self.upper.name,
            "duration_sec": self.duration,
            "page_count": self.page,
            "view_count": self.view_count,
            "danmaku_count": self.danmaku_count,
            "collect_count": self.collect_count,
            "pubtime": self.pubtime,
            "fav_time": self.fav_time,
            "is_valid": self.is_valid,
            "attr": self.attr,
            "source_folder_id": self.source_folder_id,
            "source_folder_title": self.source_folder_title,
            "url": self.url,
            "category": self.category,
            "series_name": self.series_name,
            "is_timely": self.is_timely,
            "block_type": self.block_type,
            "ogv_type_name": self.ogv_type_name,
            "ogv_type_id": self.ogv_type_id,
            "season_id": self.season_id,
            "is_ogv": self.is_ogv,
            # 富化列（由 enrich_video_from_csv 注入，补回以便 CSV 可重新分类/核对）
            "tid": getattr(self, "_tid", 0),
            "parent_tid": getattr(self, "_parent_tid", 0),
            "tid_v2": getattr(self, "_tid_v2", 0),
            "tname": getattr(self, "_tname", ""),
            "tname_v2": getattr(self, "_tname_v2", ""),
            "tags": getattr(self, "_tags", ""),
        }


@dataclass
class BiliFolder:
    """收藏夹信息"""
    id: int = 0  # media_id (完整id)
    fid: int = 0  # 原始id
    mid: int = 0  # 创建者mid
    title: str = ""
    intro: str = ""
    cover: str = ""
    media_count: int = 0  # 视频数量
    attr: int = 0  # 0=正常, 1=失效
    ctime: int = 0  # 创建时间
    mtime: int = 0  # 修改时间

    # 爬取的视频列表
    videos: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "media_id": self.id,
            "fid": self.fid,
            "title": self.title,
            "intro": self.intro,
            "media_count": self.media_count,
            "mid": self.mid,
            "ctime": self.ctime,
            "mtime": self.mtime,
        }
