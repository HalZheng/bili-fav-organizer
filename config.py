"""B站收藏夹整理工具 - 配置模块"""

import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class BiliConfig:
    """B站API配置"""

    # 认证信息 - 仅从环境变量读取；未设置时回退到 gitignored 的 config.secrets 模块
    # （凭证不再硬编码于本文件，避免被误提交/分享）
    SESSDATA: str = os.getenv("BILI_SESSDATA", "")
    BILI_JCT: str = os.getenv("BILI_BILI_JCT", "")
    BUVID3: str = os.getenv("BILI_BUVID3", "")
    DedeUserID: str = os.getenv("BILI_DEDEUSERID", "")

    # 爬取配置
    FOLDER_PREFIX: str = "tmp"  # 只爬取此前缀的收藏夹(小写匹配)
    PAGE_SIZE: int = 20  # B站API每页最大20
    REQUEST_DELAY: float = 0.0  # GET请求额外间隔(秒)，已由令牌桶控制，默认关闭
    MAX_RETRIES: int = 3  # 失败重试次数（412单独处理，不计入此值）
    RETRY_BACKOFF: float = 5.0  # 非412错误重试初始等待秒数

    # 并发配置（2026-07-21 降并发以避免 412 限流）
    MAX_CONCURRENT_REQUESTS: int = 1  # 最大并发请求数（降为1避免412）
    MAX_REQUESTS_PER_SECOND: int = 1  # 每秒最大请求数（令牌桶填充速率）
    CONCURRENT_FOLDERS: int = 1  # 收藏夹级最大并发数（降为1避免412）
    CONCURRENT_SORT_WORKERS: int = 4  # 排序线程池大小
    RATE_LIMIT_COOLDOWN: float = 60.0  # 412 退避冷却秒数（基础冷却翻倍）
    CAPACITY_CACHE_TTL: int = 60  # 容量检查缓存秒数

    # 断点续移配置
    MOVE_STATE_FILE: str = "move_state.json"  # 状态文件名（位于 OUTPUT_DIR 下）
    MOVE_STATE_BACKUP_KEEP: int = 3  # 保留的历史状态备份数
    RESUME_RETRY_FAILED: bool = True  # 恢复时是否重试 failed 状态的视频
    RESUME_VERIFY_SOURCE: bool = True  # 恢复时是否查询源收藏夹验证
    RESUME_VERIFY_TARGET: bool = True  # 恢复时是否查询目标收藏夹验证
    RESUME_OFFER_REORDER: bool = True  # 恢复后是否提示重排收藏夹

    # 增量重排配置
    REORDER_STATE_FILE: str = "reorder_state.json"  # 重排状态文件名（位于 OUTPUT_DIR 下）
    REORDER_TEMP_PREFIX: str = "tmp_reorder_"  # 临时桶命名前缀
    REORDER_MTIME_INTERVAL: float = 3.0  # 重排时每条 move 间隔秒数（保证 mtime 递增）
    REORDER_STATE_BACKUP_KEEP: int = 3  # 保留的历史重排状态备份数

    # 分类配置 - 规则式分类
    # 格式: {"UP主mid": "分类名"} 或 {"UP主名": "分类名"}
    UP_CATEGORY_MAP: dict = field(default_factory=dict)

    # 分类配置 - LLM分类
    LLM_API_URL: str = os.getenv("LLM_API_URL", "https://api.deepseek.com/v1/chat/completions")
    LLM_API_KEY: str = os.getenv("LLM_API_KEY", os.getenv("DEEPSEEK_API_KEY", ""))
    LLM_MODEL: str = os.getenv("LLM_MODEL", os.getenv("LITELLM_MODEL", "deepseek-v4-flash"))
    LLM_CATEGORIES: list = field(default_factory=list)  # 预定义分类列表

    # 三层分流配置
    # Layer 1: UP主白名单 (mid → 桶名称)
    UP_WHITELIST: dict = field(default_factory=dict)
    # Layer 1: 混合型UP主黑名单 (禁止加入白名单的mid集合)
    MIXED_TYPE_UPS: set = field(default_factory=set)
    # Layer 2: 分区→桶映射 (tid → 桶名称)
    TID_BUCKET_MAP: dict = field(default_factory=dict)
    # Layer 2: 标签关键词词典 (桶名称 → 关键词列表)
    TAG_KEYWORD_DICT: dict = field(default_factory=dict)
    # Layer 2: 标题关键词词典 (桶名称 → 关键词列表，高权重)
    TITLE_KEYWORD_DICT: dict = field(default_factory=dict)
    # Layer 2: 标签命中阈值
    TAG_HIT_THRESHOLD: int = 2
    # Layer 2: 弱分区→桶映射 (弱分区TID → 桶名称，仅作回退)
    WEAK_TID_BUCKET_MAP: dict = field(default_factory=dict)
    # 时效性特征词库
    TIMELY_KEYWORDS: list = field(default_factory=list)
    # 系列识别正则列表
    SERIES_PATTERNS: list = field(default_factory=list)
    # LLM 批处理大小
    LLM_BATCH_SIZE: int = 20
    # LLM 置信度阈值 ("low"/"medium" → 待分类)
    LLM_CONFIDENCE_THRESHOLD: str = "medium"
    # 收藏夹容量上限 (达到此值自动分卷)
    FOLDER_CAPACITY_LIMIT: int = 950
    # 归档月数阈值 (超过此月数的视频归入Bottom区块)
    ARCHIVE_MONTHS: int = 6
    # 13个固定桶名称列表
    BUCKET_NAMES: list = field(default_factory=list)
    # 默认兜底桶名称
    DEFAULT_BUCKET: str = "tmp_待分类"

    # 输出配置
    OUTPUT_DIR: str = os.path.join(os.path.dirname(__file__), "output")

    def __post_init__(self):
        """从 bucket_config 加载默认配置"""
        self._load_secrets()
        if not self.UP_WHITELIST or not self.TID_BUCKET_MAP:
            try:
                from bucket_config import (
                    UP_WHITELIST, MIXED_TYPE_UPS, TID_BUCKET_MAP,
                    TAG_KEYWORD_DICT, TITLE_KEYWORD_DICT, TIMELY_KEYWORDS, SERIES_PATTERNS,
                    BUCKET_NAMES, DEFAULT_BUCKET, WEAK_TID_BUCKET_MAP,
                )
                if not self.UP_WHITELIST:
                    self.UP_WHITELIST = UP_WHITELIST
                if not self.MIXED_TYPE_UPS:
                    self.MIXED_TYPE_UPS = MIXED_TYPE_UPS
                if not self.TID_BUCKET_MAP:
                    self.TID_BUCKET_MAP = TID_BUCKET_MAP
                if not self.WEAK_TID_BUCKET_MAP:
                    self.WEAK_TID_BUCKET_MAP = WEAK_TID_BUCKET_MAP
                if not self.TAG_KEYWORD_DICT:
                    self.TAG_KEYWORD_DICT = TAG_KEYWORD_DICT
                if not self.TITLE_KEYWORD_DICT:
                    self.TITLE_KEYWORD_DICT = TITLE_KEYWORD_DICT
                if not self.TIMELY_KEYWORDS:
                    self.TIMELY_KEYWORDS = TIMELY_KEYWORDS
                if not self.SERIES_PATTERNS:
                    self.SERIES_PATTERNS = SERIES_PATTERNS
                if not self.BUCKET_NAMES:
                    self.BUCKET_NAMES = BUCKET_NAMES
                if not self.DEFAULT_BUCKET or self.DEFAULT_BUCKET == "tmp_待分类":
                    self.DEFAULT_BUCKET = DEFAULT_BUCKET
                if not self.LLM_CATEGORIES:
                    self.LLM_CATEGORIES = BUCKET_NAMES
            except ImportError:
                pass

    def _load_secrets(self):
        """环境变量未提供时，回退读取 gitignored 的 bili_secrets.py。

        覆盖登录凭证（SESSDATA/BILI_JCT/BUVID3/DedeUserID）与 LLM 配置
        （LLM_API_KEY/LLM_API_URL/LLM_MODEL）。环境变量优先级高于文件。
        """
        import importlib.util
        import os
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bili_secrets.py")
        if not os.path.exists(path):
            return
        try:
            spec = importlib.util.spec_from_file_location("bili_secrets", path)
            secrets = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(secrets)
        except Exception:
            return
        # 登录凭证回退（仅当环境变量未提供时）
        if not self.SESSDATA and getattr(secrets, "SESSDATA", ""):
            self.SESSDATA = secrets.SESSDATA
        if not self.BILI_JCT and getattr(secrets, "BILI_JCT", ""):
            self.BILI_JCT = secrets.BILI_JCT
        if not self.BUVID3 and getattr(secrets, "BUVID3", ""):
            self.BUVID3 = secrets.BUVID3
        if not self.DedeUserID and getattr(secrets, "DedeUserID", ""):
            self.DedeUserID = secrets.DedeUserID
        # LLM 配置回退：环境变量 > bili_secrets > 代码默认
        if not os.getenv("LLM_API_KEY") and getattr(secrets, "LLM_API_KEY", ""):
            self.LLM_API_KEY = secrets.LLM_API_KEY
        if not os.getenv("LLM_API_URL") and getattr(secrets, "LLM_API_URL", ""):
            self.LLM_API_URL = secrets.LLM_API_URL
        if not os.getenv("LLM_MODEL") and getattr(secrets, "LLM_MODEL", ""):
            self.LLM_MODEL = secrets.LLM_MODEL

    @property
    def cookies(self) -> dict:
        """构造请求用的cookies字典"""
        cookies = {}
        if self.SESSDATA:
            cookies["SESSDATA"] = self.SESSDATA
        if self.BILI_JCT:
            cookies["bili_jct"] = self.BILI_JCT
        if self.BUVID3:
            cookies["buvid3"] = self.BUVID3
        if self.DedeUserID:
            cookies["DedeUserID"] = self.DedeUserID
        return cookies

    @property
    def is_authenticated(self) -> bool:
        """检查是否有必要的认证信息"""
        return bool(self.SESSDATA)
