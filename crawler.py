"""B站收藏夹整理工具 - API爬虫模块

通过B站Web API获取收藏夹和视频数据。
支持WBI签名鉴权（2023年3月起B站部分接口需要）。
API文档: https://github.com/SocialSisterYi/bilibili-API-collect
"""

import asyncio
import json
import time
from functools import reduce
from hashlib import md5
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import httpx

from config import BiliConfig
from models import BiliFolder, BiliVideo, BiliUP
from rate_limiter import AsyncRateLimiter


# ─── WBI 签名相关 ───

# 重排映射表（B站固定的混淆表，64个元素）
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52
]


def get_mixin_key(orig: str) -> str:
    """对 imgKey+subKey 进行重排，取前32位作为签名密钥"""
    return reduce(lambda s, i: s + orig[i], MIXIN_KEY_ENC_TAB, '')[:32]


def enc_wbi(params: dict, img_key: str, sub_key: str) -> dict:
    """为请求参数进行WBI签名，返回带 w_rid 和 wts 的新参数"""
    mixin_key = get_mixin_key(img_key + sub_key)
    curr_time = round(time.time())

    # 复制参数，添加 wts
    params = dict(params)
    params['wts'] = curr_time

    # 按 key 排序
    params = dict(sorted(params.items()))

    # 过滤 value 中的 "!'()*" 字符
    params = {
        k: ''.join(filter(lambda chr: chr not in "!'()*", str(v)))
        for k, v in params.items()
    }

    # URL 编码 + 拼接 mixin_key → MD5
    query = urlencode(params)
    wbi_sign = md5((query + mixin_key).encode()).hexdigest()

    params['w_rid'] = wbi_sign
    return params


class BiliCrawler:
    """B站收藏夹爬虫（支持WBI签名）"""

    BASE_URL = "https://api.bilibili.com"

    def __init__(self, config: BiliConfig):
        self.config = config
        self.client: Optional[httpx.AsyncClient] = None
        # WBI签名密钥（每日更新，缓存于此）
        self._img_key: Optional[str] = None
        self._sub_key: Optional[str] = None
        self._wbi_key_time: float = 0  # 上次获取密钥的时间
        # 异步速率限制器
        self.rate_limiter = AsyncRateLimiter(
            config.MAX_CONCURRENT_REQUESTS,
            config.MAX_REQUESTS_PER_SECOND,
            config.RATE_LIMIT_COOLDOWN,
        )
        # POST 写操作的最小间隔（秒），避免 412
        # B站对写操作（move/add/copy）风控更严格，需要比读操作更保守
        # 注意：GET 读操作 0.5s 间隔安全，但 POST 写操作 0.5s 会触发 412
        self._last_post_time: float = 0
        self._post_min_interval: float = 2.0  # 两次 POST 之间至少间隔 2s（测试安全间隔）
        # 收藏夹视频ID缓存: {media_id: (video_ids_list, timestamp)}
        self._folder_ids_cache: dict[int, tuple[list[dict], float]] = {}
        # 上次 POST 操作是否触发 412（供调用方判断是否应跳过后续 POST）
        self.last_post_412: bool = False

    async def _get_client(self) -> httpx.AsyncClient:
        """获取或创建HTTP客户端"""
        if self.client is None or self.client.is_closed:
            self.client = httpx.AsyncClient(
                base_url=self.BASE_URL,
                cookies=self.config.cookies,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                    "Referer": "https://www.bilibili.com",
                },
                timeout=30.0,
                follow_redirects=True,
            )
        return self.client

    async def close(self):
        """关闭HTTP客户端"""
        if self.client and not self.client.is_closed:
            await self.client.aclose()

    async def _wait_post_interval(self):
        """确保两次 POST 写操作之间有最小间隔

        B站对写操作风控更严格，连续快速 POST 容易触发 412。
        此方法在每次 POST 前调用，保证距离上次 POST 至少间隔 _post_min_interval 秒。
        """
        now = time.time()
        elapsed = now - self._last_post_time
        if elapsed < self._post_min_interval:
            wait = self._post_min_interval - elapsed
            await asyncio.sleep(wait)
        self._last_post_time = time.time()

    async def _refresh_wbi_keys(self) -> tuple[str, str]:
        """从nav接口获取最新的WBI签名密钥（img_key, sub_key）
        密钥每日更新，缓存10小时
        """
        now = time.time()
        # 密钥缓存10小时（B站每日更新）
        if self._img_key and (now - self._wbi_key_time) < 36000:
            return self._img_key, self._sub_key

        client = await self._get_client()
        resp = await client.get("/x/web-interface/nav")
        data = resp.json()

        wbi_img = data.get("data", {}).get("wbi_img", {})
        img_url = wbi_img.get("img_url", "")
        sub_url = wbi_img.get("sub_url", "")

        if img_url and sub_url:
            self._img_key = img_url.rsplit('/', 1)[1].split('.')[0]
            self._sub_key = sub_url.rsplit('/', 1)[1].split('.')[0]
            self._wbi_key_time = now
            print(f"  [WBI] 密钥已刷新: img_key={self._img_key[:8]}... sub_key={self._sub_key[:8]}...")
        else:
            raise RuntimeError(f"无法获取WBI密钥: nav接口返回 {data.get('code')} - {data.get('message')}")

        return self._img_key, self._sub_key

    async def _request(self, url: str, params: dict = None, use_wbi: bool = True) -> dict:
        """发送请求，带WBI签名、重试和速率控制。412风控时自动退避。

        Args:
            url: API路径
            params: 请求参数
            use_wbi: 是否使用WBI签名（GET查询类API通常需要）
        """
        client = await self._get_client()

        # 准备参数
        if params is None:
            params = {}
        params = dict(params)  # 复制，避免修改原始参数

        # WBI签名
        if use_wbi:
            try:
                img_key, sub_key = await self._refresh_wbi_keys()
                params = enc_wbi(params, img_key, sub_key)
            except Exception as e:
                print(f"  [WBI] 签名失败，将无签名重试: {e}")

        for attempt in range(self.config.MAX_RETRIES):
            try:
                # 通过速率限制器获取请求槽位（令牌桶 + 并发控制）
                async with self.rate_limiter:
                    resp = await client.get(url, params=params)

                    # 412风控：报告给速率限制器并退避
                    if resp.status_code == 412:
                        await self.rate_limiter.report_412()
                        wait = self.config.RETRY_BACKOFF * (2 ** attempt)
                        print(f"  [412] 风控限流，等待 {wait:.0f}s 后重试 ({attempt + 1}/{self.config.MAX_RETRIES})...")
                        await asyncio.sleep(wait)
                        # 刷新WBI密钥后重试
                        if use_wbi:
                            try:
                                img_key, sub_key = await self._refresh_wbi_keys()
                                params_no_wbi = {k: v for k, v in params.items() if k not in ('w_rid', 'wts')}
                                params = enc_wbi(params_no_wbi, img_key, sub_key)
                            except Exception:
                                pass
                        continue

                    resp.raise_for_status()
                    data = resp.json()

                    if data.get("code") == 0:
                        self.rate_limiter.report_success()
                        return data
                    elif data.get("code") == -352:
                        # -352 表示需要WBI签名但签名缺失/错误
                        print(f"  [-352] WBI签名验证失败，刷新密钥重试...")
                        if use_wbi and attempt < self.config.MAX_RETRIES - 1:
                            # 强制刷新WBI密钥
                            self._img_key = None
                            self._sub_key = None
                            try:
                                img_key, sub_key = await self._refresh_wbi_keys()
                                params_no_wbi = {k: v for k, v in params.items() if k not in ('w_rid', 'wts')}
                                params = enc_wbi(params_no_wbi, img_key, sub_key)
                            except Exception:
                                pass
                            await asyncio.sleep(2)
                            continue
                        return data
                    elif data.get("code") == -403:
                        raise PermissionError(
                            f"权限不足，请检查SESSDATA是否有效: {data.get('message')}"
                        )
                    elif data.get("code") == -400:
                        raise ValueError(f"请求参数错误: {data.get('message')}")
                    else:
                        print(f"  [WARN] API返回非0 code: {data.get('code')} - {data.get('message')}")
                        return data

            except httpx.HTTPStatusError as e:
                if e.response.status_code == 412:
                    self.rate_limiter.report_412()
                    wait = self.config.RETRY_BACKOFF * (2 ** attempt)
                    print(f"  [412] 风控限流，等待 {wait:.0f}s 后重试 ({attempt + 1}/{self.config.MAX_RETRIES})...")
                    await asyncio.sleep(wait)
                    if use_wbi:
                        try:
                            self._img_key = None
                            img_key, sub_key = await self._refresh_wbi_keys()
                            params_no_wbi = {k: v for k, v in params.items() if k not in ('w_rid', 'wts')}
                            params = enc_wbi(params_no_wbi, img_key, sub_key)
                        except Exception:
                            pass
                else:
                    print(f"  [RETRY] 请求失败 ({attempt + 1}/{self.config.MAX_RETRIES}): {e}")
                    if attempt < self.config.MAX_RETRIES - 1:
                        await asyncio.sleep(2 ** attempt)
                    else:
                        raise
            except httpx.ConnectError as e:
                print(f"  [RETRY] 连接失败 ({attempt + 1}/{self.config.MAX_RETRIES}): {e}")
                if attempt < self.config.MAX_RETRIES - 1:
                    await asyncio.sleep(2 ** attempt)
                else:
                    raise

        return {}

    # ─── 稍后再看 ───

    async def get_watchlater(self) -> list[BiliVideo]:
        """获取稍后再看列表

        API: GET /x/v2/history/toview
        """
        client = await self._get_client()
        # 稍后再看 API 不需要 WBI 签名
        resp = await client.get("/x/v2/history/toview")
        data = resp.json()

        if data.get("code") != 0:
            print(f"  [WARN] 稍后再看API返回: {data.get('code')} - {data.get('message')}")
            return []

        items = data.get("data", {}).get("list", []) or []
        videos = []
        for item in items:
            owner = item.get("owner", {})
            stat = item.get("stat", {})
            video = BiliVideo(
                id=item.get("aid", 0),
                bvid=item.get("bvid", ""),
                title=item.get("title", ""),
                intro=item.get("desc", ""),
                cover=item.get("pic", ""),
                upper=BiliUP(
                    mid=owner.get("mid", 0),
                    name=owner.get("name", ""),
                    face=owner.get("face", ""),
                ),
                ctime=item.get("ctime", 0),
                pubtime=item.get("pubdate", 0),
                fav_time=0,  # 稍后再看没有收藏时间
                duration=item.get("duration", 0),
                page=item.get("videos", 1),
                view_count=stat.get("view", 0),
                danmaku_count=stat.get("danmaku", 0),
                collect_count=stat.get("favorite", 0),
                attr=0,
                type=item.get("type", 2),
                source_folder_id=0,  # 不属于收藏夹
                source_folder_title="稍后再看",
            )
            videos.append(video)
        return videos

    async def delete_watchlater(self, bvid: str = "", avid: int = 0) -> bool:
        """从稍后再看列表删除指定视频

        B站「稍后再看」是独立于收藏夹的特殊列表，add_resources 把视频加入收藏夹后，
        视频仍会留在稍后再看列表中，需要单独调用此接口删除。

        API: POST /x/v2/history/toview/del
        参数: aid + csrf（实测该老接口只认 aid，传 bvid 会返回 code=-400 请求错误）

        Args:
            bvid: 视频 BV 号（当 avid 未提供时，用于查询 aid）
            avid: 视频 AV 号（优先使用，避免额外 API 调用）

        Returns:
            True 表示删除成功，False 表示失败
        """
        # 解析出 aid
        aid = avid
        if not aid and bvid:
            aid = await self._bvid_to_avid(bvid)
        if not aid:
            print(f"  [WARN] delete_watchlater: 无法获取 avid (bvid={bvid})，跳过")
            return False

        data = {
            "aid": str(aid),
            "csrf": self.config.BILI_JCT,
        }

        try:
            # POST 写操作间隔控制
            await self._wait_post_interval()
            async with self.rate_limiter:
                client = await self._get_client()
                resp = await client.post(
                    "/x/v2/history/toview/del",
                    data=data,
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Referer": "https://www.bilibili.com",
                    },
                )
            if resp.status_code == 412:
                await self.rate_limiter.report_412()
                self.last_post_412 = True
                print(f"  [WARN] 删除稍后再看 {bvid or aid} 触发 412 风控")
                return False
            self.last_post_412 = False
            try:
                result = resp.json()
            except Exception:
                print(f"  [WARN] 删除稍后再看 {bvid or aid} 响应解析失败 (status={resp.status_code})")
                return False
            code = result.get("code", -1)
            msg = result.get("message", "")
            if code == 0:
                self.rate_limiter.report_success()
                return True
            # -101: 未登录 / -111: csrf校验失败 / -403: 权限不足 → 不重试
            non_retryable_codes = {-101, -111, -403}
            if code in non_retryable_codes:
                print(f"  [SKIP] 删除稍后再看不可恢复: code={code} {msg}")
                return False
            # 其他错误（如视频不在稍后再看列表）
            print(f"  [WARN] 删除稍后再看 {bvid or aid} 失败: code={code} {msg}")
            return False
        except Exception as e:
            print(f"  [ERROR] 删除稍后再看 {bvid or aid} 异常: {e}")
            return False

    async def _bvid_to_avid(self, bvid: str) -> int:
        """通过 bvid 查询 avid（GET /x/web-interface/view）

        用于 delete_watchlater 等只接受 aid 的老接口。
        Returns: avid，失败返回 0
        """
        if not bvid:
            return 0
        try:
            async with self.rate_limiter:
                client = await self._get_client()
                resp = await client.get(
                    "/x/web-interface/view",
                    params={"bvid": bvid},
                )
            if resp.status_code == 412:
                await self.rate_limiter.report_412()
                return 0
            data = resp.json()
            if data.get("code") == 0:
                return int(data.get("data", {}).get("aid", 0))
        except Exception as e:
            print(f"  [WARN] bvid→avid 转换失败 {bvid}: {e}")
        return 0

    # ─── 视频详情（补充 tags/tid 等） ───

    async def get_video_detail(self, bvid: str) -> dict:
        """获取单个视频的完整详情（含 tags/tid/tname/title/owner 等）

        数据来源:
          - GET /x/web-interface/view → tid/tid_v2/title/owner/stat 等
            注: B站 API 变更后该接口不再返回 tname/tname_v2（空串）和 tag（None），
            但 tid/tid_v2 仍然有效。
          - GET /x/tag/archive/tags → tags 列表（view 接口已不返回 tag 字段）

        Returns: 包含完整视频信息的字典，或空字典
        """
        params = {"bvid": bvid}
        data = await self._request("/x/web-interface/view", params, use_wbi=True)
        info = data.get("data") or {}
        if not info:
            return {}

        # 提取 tags（view 接口不返回 tag 字段，需单独调用 tags API）
        tag_list = info.get("tag") or []
        if not tag_list:
            # view 接口已不返回 tag，调用专用 tags API
            tag_list = await self._fetch_video_tags(bvid)
        tags = ",".join(t.get("tag_name", "") for t in tag_list if t.get("tag_name"))

        owner = info.get("owner") or {}
        stat = info.get("stat") or {}

        return {
            "aid": info.get("aid", 0),
            "bvid": info.get("bvid", bvid),
            "title": info.get("title", ""),
            "desc": info.get("desc", ""),
            "duration": info.get("duration", 0),
            "videos": info.get("videos", 1),
            "pubdate": info.get("pubdate", 0),
            "ctime": info.get("ctime", 0),
            "owner_mid": owner.get("mid", 0),
            "owner_name": owner.get("name", ""),
            "owner_face": owner.get("face", ""),
            "stat_view": stat.get("view", 0),
            "stat_danmaku": stat.get("danmaku", 0),
            "stat_favorite": stat.get("favorite", 0),
            "tags": tags,
            "tid": info.get("tid", 0),
            "tname": info.get("tname", ""),
            "tid_v2": info.get("tid_v2", 0),
            "tname_v2": info.get("tname_v2", ""),
        }

    async def _fetch_video_tags(self, bvid: str) -> list[dict]:
        """获取视频的 tags 列表

        API: GET /x/tag/archive/tags
        view 接口不再返回 tag 字段，需通过此专用接口获取。

        Returns: tag 字典列表，每个含 tag_id/tag_name 等字段；失败返回空列表
        """
        try:
            params = {"bvid": bvid}
            data = await self._request("/x/tag/archive/tags", params, use_wbi=True)
            tag_list = data.get("data") or []
            return tag_list if isinstance(tag_list, list) else []
        except Exception as e:
            # tags 获取失败不应影响其他字段
            return []

    # ─── 收藏夹列表 ───

    async def get_created_folders(self, up_mid: int = None) -> list[BiliFolder]:
        """获取用户创建的所有收藏夹

        API: GET /x/v3/fav/folder/created/list-all
        """
        params = {}
        if up_mid:
            params["up_mid"] = up_mid
        else:
            # 使用当前登录用户
            params["up_mid"] = self.config.DedeUserID

        data = await self._request("/x/v3/fav/folder/created/list-all", params)
        folders = []

        for item in data.get("data", {}).get("list", []) or []:
            folder = BiliFolder(
                id=item.get("id", 0),
                fid=item.get("fid", 0),
                mid=item.get("mid", 0),
                title=item.get("title", ""),
                intro=item.get("intro", ""),
                cover=item.get("cover", ""),
                media_count=item.get("media_count", 0),
                attr=item.get("attr", 0),
                ctime=item.get("ctime", 0),
                mtime=item.get("mtime", 0),
            )
            folders.append(folder)

        return folders

    async def get_tmp_folders(self, up_mid: int = None) -> list[BiliFolder]:
        """获取所有tmp前缀的收藏夹(大小写不敏感)"""
        all_folders = await self.get_created_folders(up_mid)
        prefix = self.config.FOLDER_PREFIX.lower()
        tmp_folders = [f for f in all_folders if f.title.lower().startswith(prefix)]
        return tmp_folders

    # ─── 收藏夹内容 ───

    async def get_folder_videos(
        self,
        media_id: int,
        order: str = "mtime",
        progress_callback=None,
    ) -> list[BiliVideo]:
        """获取收藏夹中的所有视频（顺序分页，避免412）

        API: GET /x/v3/fav/resource/list
        order: mtime(收藏时间), view(播放量), pubtime(投稿时间)

        先请求第1页获取总数，然后顺序请求剩余页面，最后合并结果。
        注: 原并发分页已改为顺序，避免短时间大量请求触发B站412风控。
        """
        ps = self.config.PAGE_SIZE

        # 1. 请求第1页，获取总数和 has_more
        first_params = {
            "media_id": media_id,
            "pn": 1,
            "ps": ps,
            "order": order,
            "platform": "web",
            "type": 0,
        }
        first_data = await self._request("/x/v3/fav/resource/list", first_params)
        first_page = first_data.get("data")
        # 处理 API 返回 data:null 或 data 缺失的情况
        if first_page is None:
            return []

        folder_info = first_page.get("info") or {}
        folder_title = folder_info.get("title", "")
        total = folder_info.get("media_count", 0) or 0

        # 解析第1页视频
        videos = self._parse_video_page(first_page, media_id, folder_title)

        if progress_callback:
            await progress_callback(len(videos), total, folder_title)

        # 2. 计算剩余页数
        has_more = first_page.get("has_more", False)
        if not has_more:
            return videos

        total_pages = (total + ps - 1) // ps
        remaining_pages = list(range(2, total_pages + 1))

        if not remaining_pages:
            return videos

        # 3. 顺序请求剩余页面（避免并发触发412风控）
        for pn in remaining_pages:
            params = {
                "media_id": media_id,
                "pn": pn,
                "ps": ps,
                "order": order,
                "platform": "web",
                "type": 0,
            }
            data = await self._request("/x/v3/fav/resource/list", params)
            page_data = data.get("data", {})
            if page_data is None:
                page_videos = []
            else:
                page_videos = self._parse_video_page(page_data, media_id, folder_title)
            if progress_callback:
                await progress_callback(len(page_videos), 0, folder_title)
            videos.extend(page_videos)
            # 页间小延迟，避免请求过快触发风控
            await asyncio.sleep(0.5)

        return videos

    def _parse_video_page(
        self, page_data: dict, media_id: int, folder_title: str
    ) -> list[BiliVideo]:
        """解析单页视频数据为 BiliVideo 列表"""
        videos = []
        medias = page_data.get("medias") or []

        for item in medias:
            upper_data = item.get("upper") or {}
            cnt_info = item.get("cnt_info") or {}

            video = BiliVideo(
                id=item.get("id", 0),
                bvid=item.get("bvid", ""),
                title=item.get("title", ""),
                intro=item.get("intro", ""),
                cover=item.get("cover", ""),
                upper=BiliUP(
                    mid=upper_data.get("mid", 0),
                    name=upper_data.get("name", ""),
                    face=upper_data.get("face", ""),
                ),
                ctime=item.get("ctime", 0),
                pubtime=item.get("pubtime", 0),
                fav_time=item.get("fav_time", 0),
                duration=item.get("duration", 0),
                page=item.get("page", 1),
                view_count=cnt_info.get("play", 0),
                danmaku_count=cnt_info.get("danmaku", 0),
                collect_count=cnt_info.get("collect", 0),
                attr=item.get("attr", 0),
                type=item.get("type", 2),
                source_folder_id=media_id,
                source_folder_title=folder_title,
                ogv_type_name=(item.get("ogv") or {}).get("type_name", ""),
                ogv_type_id=(item.get("ogv") or {}).get("type_id", 0),
                season_id=(item.get("ogv") or {}).get("season_id", 0),
            )
            videos.append(video)

        return videos

    async def get_folder_video_ids(
        self, media_id: int, use_cache: bool = True
    ) -> list[dict] | None:
        """使用IDs API一次性获取收藏夹所有视频ID（无分页）

        API: GET /x/v3/fav/resource/ids
        Returns: list of {"id": avid, "bvid": bvid, "type": type} dicts
                 None 表示 API 调用失败（不缓存，调用方可重试）
                 [] 表示收藏夹确实为空（会缓存）
        """
        # 检查缓存
        if use_cache:
            now = time.time()
            if media_id in self._folder_ids_cache:
                cached_ids, ts = self._folder_ids_cache[media_id]
                if now - ts < self.config.CAPACITY_CACHE_TTL:
                    return cached_ids

        params = {
            "media_id": media_id,
            "platform": "web",
        }
        try:
            data = await self._request("/x/v3/fav/resource/ids", params)
        except Exception as e:
            print(f"  [WARN] 获取收藏夹 {media_id} 视频ID列表失败: {e}")
            return None  # API 失败，不缓存，返回 None 供调用方区分

        # _request 在重试耗尽时返回 {}，非零 code 也可能返回 data
        # 只有 code==0 才是真正成功，否则视为失败
        if data.get("code") != 0:
            print(
                f"  [WARN] 获取收藏夹 {media_id} 视频ID列表失败: "
                f"code={data.get('code')} - {data.get('message')}"
            )
            return None  # API 错误，不缓存

        result = data.get("data", [])
        if not isinstance(result, list):
            result = []

        # 只缓存成功结果（code==0），避免 API 抖动导致的空列表被长期缓存
        self._folder_ids_cache[media_id] = (result, time.time())
        return result

    async def get_folder_video_ids_batch(
        self, media_ids: list[int], use_cache: bool = True
    ) -> dict[int, list[dict] | None]:
        """并发查询多个收藏夹的视频ID集合

        Args:
            media_ids: 收藏夹 media_id 列表
            use_cache: 是否使用缓存

        Returns:
            {media_id: [video_id dicts] | None} 映射，None 表示 API 失败
        """
        if not media_ids:
            return {}

        semaphore = asyncio.Semaphore(self.config.CONCURRENT_FOLDERS)

        async def fetch_one(mid: int) -> tuple[int, list[dict] | None]:
            async with semaphore:
                ids = await self.get_folder_video_ids(mid, use_cache=use_cache)
                return mid, ids

        results = await asyncio.gather(*[fetch_one(mid) for mid in media_ids])
        return dict(results)

    # ─── 收藏夹操作 ───

    async def create_folder(self, title: str, intro: str = "", privacy: int = 0) -> Optional[int]:
        """创建新收藏夹，返回media_id"""
        client = await self._get_client()
        data = {
            "title": title,
            "intro": intro,
            "privacy": privacy,
            "csrf": self.config.BILI_JCT,
        }
        resp = await client.post(
            "/x/v3/fav/folder/add",
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        result = resp.json()
        if result.get("code") == 0:
            return result.get("data", {}).get("id")
        else:
            print(f"  [ERROR] 创建收藏夹失败: {result.get('message')}")
            return None

    async def copy_resources(
        self,
        src_media_id: int,
        tar_media_id: int,
        resources: list[str],
    ) -> bool:
        """批量复制视频到目标收藏夹（走 rate_limiter 限速）

        resources格式: ["avid:type", ...]，type: 2=视频
        """
        # B站API单次最多处理约50条
        batch_size = 50
        for i in range(0, len(resources), batch_size):
            batch = resources[i:i + batch_size]
            data = {
                "src_media_id": src_media_id,
                "tar_media_id": tar_media_id,
                "mid": self.config.DedeUserID,
                "resources": ",".join(batch),
                "platform": "web",
                "csrf": self.config.BILI_JCT,
            }
            # POST 写操作间隔控制 + rate_limiter 限速
            await self._wait_post_interval()
            async with self.rate_limiter:
                client = await self._get_client()
                resp = await client.post(
                    "/x/v3/fav/resource/copy",
                    data=data,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
            result = resp.json()
            if result.get("code") != 0:
                print(f"  [ERROR] 复制失败 (batch {i//batch_size + 1}): {result.get('message')}")
                return False
        return True

    async def move_resources(
        self,
        src_media_id: int,
        tar_media_id: int,
        resources: list[str],
    ) -> bool:
        """批量移动视频到目标收藏夹（走 rate_limiter 限速，含重试）"""
        batch_size = 50
        for i in range(0, len(resources), batch_size):
            batch = resources[i:i + batch_size]
            data = {
                "src_media_id": src_media_id,
                "tar_media_id": tar_media_id,
                "mid": self.config.DedeUserID,
                "resources": ",".join(batch),
                "platform": "web",
                "csrf": self.config.BILI_JCT,
            }
            for attempt in range(self.config.MAX_RETRIES):
                try:
                    # POST 写操作间隔控制
                    await self._wait_post_interval()
                    # 走 rate_limiter 限速
                    async with self.rate_limiter:
                        client = await self._get_client()
                        resp = await client.post(
                            "/x/v3/fav/resource/move",
                            data=data,
                            headers={"Content-Type": "application/x-www-form-urlencoded"},
                        )
                    if resp.status_code == 412:
                        # 412 风控：交由 rate_limiter 统一管理，直接返回让下一条视频等待冷却
                        await self.rate_limiter.report_412()
                        self.last_post_412 = True
                        return False
                    self.last_post_412 = False
                    result = resp.json()
                    if result.get("code") != 0:
                        msg = result.get('message', '')
                        code = result.get('code', 0)
                        # 不可恢复错误：不重试，立即返回
                        # -403: 权限不足; 内容不存在/已失效: 资源已删除或不在源收藏夹
                        # 11203: 目标收藏夹容量已达上限，重试必然失败
                        non_retryable_codes = {-403, -404, 11203}
                        non_retryable_keywords = ["不存在", "已失效", "已删除"]
                        is_non_retryable = (
                            code in non_retryable_codes or
                            any(kw in msg for kw in non_retryable_keywords)
                        )
                        if is_non_retryable:
                            print(f"  [SKIP] 移动不可恢复: code={code} {msg}")
                            return False
                        if attempt < self.config.MAX_RETRIES - 1:
                            print(f"  [RETRY] 移动失败: {msg} ({attempt + 1}/{self.config.MAX_RETRIES})")
                            await asyncio.sleep(2 ** attempt)
                            continue
                        print(f"  [ERROR] 移动失败 (batch {i//batch_size + 1}): {msg}")
                        return False
                    self.rate_limiter.report_success()
                    break
                except Exception as e:
                    if attempt < self.config.MAX_RETRIES - 1:
                        print(f"  [RETRY] 移动异常: {e} ({attempt + 1}/{self.config.MAX_RETRIES})")
                        await asyncio.sleep(2 ** attempt)
                        continue
                    print(f"  [ERROR] 移动异常 (batch {i//batch_size + 1}): {e}")
                    return False
        return True

    async def add_resources(
        self,
        tar_media_id: int,
        resources: list[str],
    ) -> bool:
        """添加视频到目标收藏夹（无需源收藏夹，走 rate_limiter 限速，含重试）

        用于视频不在任何收藏夹中的场景（如源收藏夹已删除）。
        调用B站 /medialist/gateway/coll/resource/deal 接口。

        Args:
            tar_media_id: 目标收藏夹 id
            resources: ["avid:type", ...]，type: 2=视频（仅取第一个）
        """
        # 从 "avid:type" 格式中提取 avid 和 type
        if not resources:
            return False
        res_str = resources[0]
        parts = res_str.split(":")
        rid = int(parts[0]) if parts else 0
        res_type = int(parts[1]) if len(parts) > 1 else 2

        if rid == 0:
            print(f"  [WARN] 无效的 avid: {res_str}")
            return False

        data = {
            "rid": rid,
            "type": res_type,
            "add_media_ids": str(tar_media_id),
            "del_media_ids": "",
            "csrf": self.config.BILI_JCT,
        }

        for attempt in range(self.config.MAX_RETRIES):
            try:
                # POST 写操作间隔控制
                await self._wait_post_interval()
                # 走 rate_limiter 限速
                async with self.rate_limiter:
                    client = await self._get_client()
                    resp = await client.post(
                        "/medialist/gateway/coll/resource/deal",
                        data=data,
                        headers={
                            "Content-Type": "application/x-www-form-urlencoded",
                            "Referer": "https://www.bilibili.com",
                        },
                    )
                if resp.status_code == 412:
                    # 412 风控：交由 rate_limiter 统一管理，直接返回让下一条等待冷却
                    await self.rate_limiter.report_412()
                    self.last_post_412 = True
                    return False
                self.last_post_412 = False
                # 安全解析 JSON
                try:
                    result = resp.json()
                except Exception:
                    wait = self.config.RETRY_BACKOFF * (2 ** attempt)
                    print(f"  [WARN] 添加响应异常(status={resp.status_code})，等待 {wait:.0f}s ({attempt + 1}/{self.config.MAX_RETRIES})...")
                    await asyncio.sleep(wait)
                    continue
                code = result.get("code", -1)
                msg = result.get("message", "")
                if code == 0:
                    self.rate_limiter.report_success()
                    return True
                # 11201: 已收藏过，不算失败
                if code == 11201:
                    self.rate_limiter.report_success()
                    return True
                # -101: 未登录 / -111: csrf校验失败 / -403: 权限不足 → 不重试
                # 11203: 收藏夹容量已达上限 → 重试必然失败，不重试
                non_retryable_codes = {-101, -111, -403, -404, 11203}
                non_retryable_keywords = ["不存在", "已失效", "已删除"]
                is_non_retryable = (
                    code in non_retryable_codes or
                    any(kw in msg for kw in non_retryable_keywords)
                )
                if is_non_retryable:
                    print(f"  [SKIP] 添加不可恢复: code={code} {msg}")
                    return False
                # 其他错误 → 重试
                if attempt < self.config.MAX_RETRIES - 1:
                    print(f"  [RETRY] 添加失败(code={code}): {msg} ({attempt + 1}/{self.config.MAX_RETRIES})")
                    await asyncio.sleep(2 ** attempt)
                    continue
                print(f"  [ERROR] 添加失败(code={code}): {msg}")
                return False
            except Exception as e:
                if attempt < self.config.MAX_RETRIES - 1:
                    print(f"  [RETRY] 添加异常: {e} ({attempt + 1}/{self.config.MAX_RETRIES})")
                    await asyncio.sleep(2 ** attempt)
                    continue
                print(f"  [ERROR] 添加异常: {e}")
                return False
        return False

    async def remove_resources(
        self,
        media_id: int,
        resources: list[str],
    ) -> bool:
        """从收藏夹中删除视频"""
        client = await self._get_client()
        batch_size = 50
        for i in range(0, len(resources), batch_size):
            batch = resources[i:i + batch_size]
            data = {
                "media_id": media_id,
                "resources": ",".join(batch),
                "platform": "web",
                "csrf": self.config.BILI_JCT,
            }
            # POST 写操作间隔控制（批量删除同样受 412 风控约束）
            await self._wait_post_interval()
            resp = await client.post(
                "/x/v3/fav/resource/batch-del",
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            result = resp.json()
            if result.get("code") != 0:
                print(f"  [ERROR] 删除失败 (batch {i//batch_size + 1}): {result.get('message')}")
                return False
            await asyncio.sleep(self.config.REQUEST_DELAY)
        return True

    async def rename_folder(self, media_id: int, new_title: str, max_retries: int = 3) -> bool:
        """重命名收藏夹（调用 /x/v3/fav/folder/edit 接口）

        Args:
            media_id: 收藏夹 id
            new_title: 新标题
            max_retries: 最大重试次数

        Returns:
            True 表示重命名成功，False 表示失败
        """
        data = {
            "media_id": media_id,
            "title": new_title,
            "intro": "",
            "privacy": 0,
            "csrf": self.config.BILI_JCT,
        }
        for attempt in range(max_retries):
            try:
                # POST 写操作间隔控制
                await self._wait_post_interval()
                async with self.rate_limiter:
                    client = await self._get_client()
                    resp = await client.post(
                        "/x/v3/fav/folder/edit",
                        data=data,
                        headers={"Content-Type": "application/x-www-form-urlencoded"},
                    )
                if resp.status_code == 412:
                    await self.rate_limiter.report_412()
                    self.last_post_412 = True
                    if attempt < max_retries - 1:
                        wait = 60 * (attempt + 1)
                        print(f"  [WARN] 重命名收藏夹 {media_id} 触发 412，等待 {wait}s 后重试 ({attempt + 1}/{max_retries})")
                        await asyncio.sleep(wait)
                        continue
                    print(f"  [WARN] 重命名收藏夹 {media_id} 触发 412 风控，已达最大重试次数")
                    return False
                self.last_post_412 = False
                result = resp.json()
                code = result.get("code", 0)
                msg = result.get("message", "")
                if code == 0:
                    self.rate_limiter.report_success()
                    return True
                # 收藏夹不存在 → 视为失败
                if code == 11010 or "不存在" in msg:
                    print(f"  [ERROR] 收藏夹 {media_id} 不存在，无法重命名")
                    return False
                # 其他错误：重试
                if attempt < max_retries - 1:
                    wait = 10 * (attempt + 1)
                    print(f"  [WARN] 重命名收藏夹 {media_id} 失败: {msg}，等待 {wait}s 后重试 ({attempt + 1}/{max_retries})")
                    await asyncio.sleep(wait)
                    continue
                print(f"  [ERROR] 重命名收藏夹 {media_id} 失败: {msg}")
                return False
            except Exception as e:
                if attempt < max_retries - 1:
                    wait = 10 * (attempt + 1)
                    print(f"  [WARN] 重命名收藏夹 {media_id} 异常: {e}，等待 {wait}s 后重试 ({attempt + 1}/{max_retries})")
                    await asyncio.sleep(wait)
                    continue
                print(f"  [ERROR] 重命名收藏夹 {media_id} 异常: {e}")
                return False
        return False

    async def delete_folder(self, media_id: int, max_retries: int = 3) -> bool:
        """删除收藏夹（用于清理重排临时桶），含重试

        经过大量 POST 操作后调用容易触发 412，因此加入重试+退避。
        收藏夹已不存在（11010）视为成功（幂等）。

        Args:
            media_id: 要删除的收藏夹 media_id
            max_retries: 最大重试次数

        Returns:
            True 表示删除成功，False 表示失败
        """
        data = {
            "media_ids": str(media_id),
            "csrf": self.config.BILI_JCT,
        }
        for attempt in range(max_retries):
            try:
                # POST 写操作间隔控制
                await self._wait_post_interval()
                async with self.rate_limiter:
                    client = await self._get_client()
                    resp = await client.post(
                        "/x/v3/fav/folder/del",
                        data=data,
                        headers={"Content-Type": "application/x-www-form-urlencoded"},
                    )
                if resp.status_code == 412:
                    await self.rate_limiter.report_412()
                    self.last_post_412 = True
                    if attempt < max_retries - 1:
                        wait = 60 * (attempt + 1)
                        print(f"  [WARN] 删除收藏夹 {media_id} 触发 412，等待 {wait}s 后重试 ({attempt + 1}/{max_retries})")
                        await asyncio.sleep(wait)
                        continue
                    print(f"  [WARN] 删除收藏夹 {media_id} 触发 412 风控，已达最大重试次数")
                    return False
                self.last_post_412 = False
                result = resp.json()
                code = result.get("code", 0)
                msg = result.get("message", "")
                if code == 0:
                    self.rate_limiter.report_success()
                    return True
                # 收藏夹不存在（可能已被删除）→ 视为成功
                if code == 11010 or "不存在" in msg:
                    print(f"  [INFO] 收藏夹 {media_id} 不存在（可能已删除），视为成功")
                    self.rate_limiter.report_success()
                    return True
                # 其他错误：重试
                if attempt < max_retries - 1:
                    wait = 10 * (attempt + 1)
                    print(f"  [WARN] 删除收藏夹 {media_id} 失败: {msg}，等待 {wait}s 后重试 ({attempt + 1}/{max_retries})")
                    await asyncio.sleep(wait)
                    continue
                print(f"  [ERROR] 删除收藏夹 {media_id} 失败: {msg}")
                return False
            except Exception as e:
                if attempt < max_retries - 1:
                    wait = 10 * (attempt + 1)
                    print(f"  [WARN] 删除收藏夹 {media_id} 异常: {e}，等待 {wait}s 后重试 ({attempt + 1}/{max_retries})")
                    await asyncio.sleep(wait)
                    continue
                print(f"  [ERROR] 删除收藏夹 {media_id} 异常: {e}")
                return False
        return False

    # ─── 数据导出 ───

    def export_videos_csv(self, videos: list[BiliVideo], filename: str = "videos.csv"):
        """导出视频列表为CSV"""
        import pandas as pd

        output_path = Path(self.config.OUTPUT_DIR) / filename
        output_path.parent.mkdir(parents=True, exist_ok=True)

        records = [v.to_dict() for v in videos]
        df = pd.DataFrame(records)

        # 格式化时间列
        if not df.empty:
            df["pubtime_str"] = pd.to_datetime(df["pubtime"], unit="s", errors="coerce")
            df["fav_time_str"] = pd.to_datetime(df["fav_time"], unit="s", errors="coerce")
            # 时长格式化
            df["duration_min"] = (df["duration_sec"] / 60).round(1)

        df.to_csv(output_path, index=False, encoding="utf-8-sig")
        print(f"  [OK] 已导出 {len(videos)} 条视频到 {output_path}")
        return str(output_path)

    def export_videos_excel(self, videos: list[BiliVideo], filename: str = "videos.xlsx"):
        """导出视频列表为Excel"""
        import pandas as pd
        import re

        output_path = Path(self.config.OUTPUT_DIR) / filename
        output_path.parent.mkdir(parents=True, exist_ok=True)

        records = [v.to_dict() for v in videos]
        df = pd.DataFrame(records)

        if not df.empty:
            df["pubtime_str"] = pd.to_datetime(df["pubtime"], unit="s", errors="coerce")
            df["fav_time_str"] = pd.to_datetime(df["fav_time"], unit="s", errors="coerce")
            df["duration_min"] = (df["duration_sec"] / 60).round(1)

            # 按UP主和发布时间排序
            df = df.sort_values(["up_name", "pubtime"], ascending=[True, False])

            # 清理非法字符（openpyxl不支持某些Unicode控制字符）
            illegal_re = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]')
            for col in df.select_dtypes(include=["object"]).columns:
                df[col] = df[col].apply(
                    lambda x: illegal_re.sub('', x) if isinstance(x, str) else x
                )

        df.to_excel(output_path, index=False, engine="openpyxl")
        print(f"  [OK] 已导出 {len(videos)} 条视频到 {output_path}")
        return str(output_path)

    def export_folders_json(self, folders: list[BiliFolder], filename: str = "folders.json"):
        """导出收藏夹列表为JSON"""
        output_path = Path(self.config.OUTPUT_DIR) / filename
        output_path.parent.mkdir(parents=True, exist_ok=True)

        data = [f.to_dict() for f in folders]
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        print(f"  [OK] 已导出 {len(folders)} 个收藏夹到 {output_path}")
        return str(output_path)

    # ─── 完整爬取流程 ───

    async def crawl_folder_concurrent(
        self, folder: BiliFolder, progress_callback=None
    ) -> list[BiliVideo]:
        """并发爬取单个收藏夹（IDs API 优先 + 分页 API 回退）

        1. 先尝试 IDs API 获取所有视频 ID
        2. 如果 IDs API 成功，用列表 API 并发获取每页视频详情
        3. 如果 IDs API 失败，回退到分页 API 并发爬取
        """
        try:
            video_ids = await self.get_folder_video_ids(folder.id)
        except Exception:
            video_ids = None

        if video_ids:
            # IDs API 成功：已知总数，用分页 API 并发获取详情
            return await self.get_folder_videos(
                folder.id, progress_callback=progress_callback
            )
        else:
            # IDs API 失败：回退到分页 API 并发爬取
            return await self.get_folder_videos(
                folder.id, progress_callback=progress_callback
            )

    async def crawl_all_tmp_folders(self, up_mid: int = None) -> list[BiliVideo]:
        """并发爬取所有TMP前缀收藏夹的视频"""
        from rich.console import Console
        from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn

        console = Console()
        all_videos: list[BiliVideo] = []

        # 1. 获取TMP收藏夹列表
        console.print("\n[bold cyan]🔍 正在获取收藏夹列表...[/bold cyan]")
        tmp_folders = await self.get_tmp_folders(up_mid)

        if not tmp_folders:
            console.print("[yellow]⚠ 未找到TMP前缀的收藏夹[/yellow]")
            return all_videos

        console.print(f"[green]✓ 找到 {len(tmp_folders)} 个TMP收藏夹[/green]")
        for f in tmp_folders:
            console.print(f"  • {f.title} ({f.media_count} 个视频)")

        # 2. 并发爬取
        total_videos = sum(f.media_count for f in tmp_folders)
        console.print(f"\n[bold cyan]📥 开始并发爬取，预计共 {total_videos} 条视频...[/bold cyan]")

        semaphore = asyncio.Semaphore(self.config.CONCURRENT_FOLDERS)

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TextColumn("{task.completed}/{task.total}"),
            console=console,
        ) as progress:
            main_task = progress.add_task("总进度", total=total_videos)

            # 为每个收藏夹创建进度任务
            folder_tasks = {}
            for folder in tmp_folders:
                folder_tasks[folder.id] = progress.add_task(
                    f"  {folder.title}", total=folder.media_count
                )

            # 已完成计数（用于更新主进度条）
            completed_count = 0

            async def crawl_single_folder(folder: BiliFolder) -> list[BiliVideo]:
                nonlocal completed_count
                async with semaphore:
                    ft = folder_tasks[folder.id]

                    async def on_progress(done, total, title):
                        progress.update(ft, completed=done)

                    videos = await self.crawl_folder_concurrent(
                        folder, progress_callback=on_progress
                    )

                    folder.videos = videos
                    completed_count += len(videos)
                    progress.update(main_task, completed=completed_count)
                    return videos

            results = await asyncio.gather(
                *[crawl_single_folder(f) for f in tmp_folders]
            )

            for videos in results:
                all_videos.extend(videos)

        # 3. 统计
        valid_count = sum(1 for v in all_videos if v.is_valid)
        invalid_count = len(all_videos) - valid_count
        up_set = set(v.upper.name for v in all_videos if v.is_valid)

        console.print(f"\n[bold green]✓ 爬取完成！[/bold green]")
        console.print(f"  总计: {len(all_videos)} 条视频")
        console.print(f"  有效: {valid_count} 条 | 失效: {invalid_count} 条")
        console.print(f"  涉及UP主: {len(up_set)} 位")

        # 4. 导出
        console.print("\n[bold cyan]💾 导出数据...[/bold cyan]")
        self.export_folders_json(tmp_folders)
        self.export_videos_csv(all_videos)
        self.export_videos_excel(all_videos)

        return all_videos

    # ─── 数据分析辅助 ───

    @staticmethod
    def analyze_videos(videos: list[BiliVideo]) -> dict:
        """对视频数据进行基础分析，帮助确定分类规则"""
        from collections import Counter

        valid_videos = [v for v in videos if v.is_valid]

        # UP主视频数量排行
        up_counter = Counter(v.upper.name for v in valid_videos)

        # 收藏夹分布
        folder_counter = Counter(v.source_folder_title for v in valid_videos)

        # 标题关键词（简单的分词统计，取2-4字的常见词）
        title_words = Counter()
        for v in valid_videos:
            # 简单提取：以常见分隔符拆分
            for sep in ["|", "｜", "—", "–", "·", "【", "】", "「", "」", "#", "＃"]:
                v.title = v.title.replace(sep, " ")
            words = v.title.split()
            for w in words:
                if 2 <= len(w) <= 8:
                    title_words[w] += 1

        return {
            "total": len(valid_videos),
            "invalid": len(videos) - len(valid_videos),
            "up_ranking": up_counter.most_common(50),
            "folder_distribution": folder_counter.most_common(),
            "title_keywords": title_words.most_common(100),
            "unique_ups": len(up_counter),
        }
