"""B站收藏夹整理工具 - 三层漏斗分类引擎

三层漏斗分流架构:
Layer 1: UP主白名单分流（零误判直投）
Layer 2: B站结构化字段与标签交叉匹配（高效率层）
Layer 3: 批量LLM语义推断（智能兜底层）

设计理念 (2026-07-21 简化版):
  前两层仅保留"十分明显的直接分类判断"，不再尝试通过复杂的硬编码逻辑
  去总结无限多的视频特征。绝大多数视频交给 Layer 3 LLM 进行语义推断。

  已移除的复杂逻辑:
  - AMBIGUOUS_WORDS: 歧义词消歧规则（复杂硬编码，全部交给 LLM）
  - 弱分区回退 (WEAK_TID_BUCKET_MAP): 准确率仅约 50%，全部交给 LLM
  - tag 歧义消歧逻辑：只保留强特征词精确匹配，不再做上下文消歧
"""

import json
import os
import re
import time
from pathlib import Path
from typing import Optional

from config import BiliConfig
from models import BiliVideo


def _safe_int_field(val, default=0):
    """从CSV字段安全转int，处理 NaN/None/空字符串"""
    if val is None:
        return default
    s = str(val).strip()
    if s == "" or s == "nan" or s == "NaN":
        return default
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return default


def enrich_video_from_csv(video: BiliVideo, row: dict):
    """从CSV行数据补充视频的分区和标签信息"""
    # OGV内容跳过tags/tid补充，从CSV补充ogv字段
    if video.is_ogv:
        video.ogv_type_name = str(row.get('ogv_type_name', '') or video.ogv_type_name)
        video.ogv_type_id = _safe_int_field(row.get('ogv_type_id', 0), video.ogv_type_id)
        video.season_id = _safe_int_field(row.get('season_id', 0), video.season_id)
        return
    video._tid = _safe_int_field(row.get('tid', 0))
    video._parent_tid = _safe_int_field(row.get('parent_tid', 0))
    video._tags = str(row.get('tags', '') or '')
    video._tname = str(row.get('tname', '') or '')
    video._tname_v2 = str(row.get('tname_v2', '') or '')
    video._tid_v2 = _safe_int_field(row.get('tid_v2', 0))


class Layer1WhitelistClassifier:
    """Layer 1: UP主单领域白名单分流（零误判直投）"""

    def __init__(self, up_whitelist: dict, mixed_type_ups: set = None):
        """
        Args:
            up_whitelist: {up_mid(int/str): 桶名称} 白名单映射
            mixed_type_ups: 跨领域UP主的mid集合，这些UP主的视频不在此层拦截
        """
        self.up_whitelist = {}
        for k, v in up_whitelist.items():
            self.up_whitelist[str(k)] = v
        self.mixed_type_ups = set()
        for m in (mixed_type_ups or set()):
            self.mixed_type_ups.add(str(m))
            self.mixed_type_ups.add(m)

    def classify(self, video: BiliVideo) -> Optional[str]:
        """对单个视频进行白名单分类，返回桶名称或None"""
        # OGV内容无UP主，跳过Layer 1白名单
        if video.is_ogv:
            return None
        up_mid = str(video.upper.mid)
        # 混合型UP主隔离：不在白名单中拦截
        if up_mid in self.mixed_type_ups or video.upper.mid in self.mixed_type_ups:
            return None
        # 白名单匹配
        if up_mid in self.up_whitelist:
            return self.up_whitelist[up_mid]
        # 也尝试按名称匹配（兼容）
        up_name = video.upper.name
        if up_name in self.up_whitelist:
            return self.up_whitelist[up_name]
        return None

    def classify_all(self, videos: list[BiliVideo]) -> tuple[dict[str, list[BiliVideo]], list[BiliVideo]]:
        """批量分类
        Returns:
            (classified, unclassified)
            classified: {桶名称: [视频列表]}
            unclassified: 未被此层拦截的视频列表
        """
        classified = {}
        unclassified = []
        for video in videos:
            if not video.is_valid:
                continue
            bucket = self.classify(video)
            if bucket:
                video.category = bucket
                if bucket not in classified:
                    classified[bucket] = []
                classified[bucket].append(video)
            else:
                unclassified.append(video)
        return classified, unclassified


class Layer2StructuralClassifier:
    """Layer 2: B站结构化字段与标签交叉匹配（高效率层）

    简化后匹配逻辑 (2026-07-21):
    1. OGV类型直投 (番剧/电影/纪录片/国创/电视剧等非UP主内容)
    2. 强分区直投 (tid → 桶) -- 仅保留内容单一、误判率极低的强分区
    3. 标题关键词匹配 (TITLE_KEYWORD_DICT, 高权重)
    4. 标签精确匹配 (TAG_KEYWORD_DICT, 仅强特征词)

    已移除的复杂逻辑:
    - 弱分区回退 (WEAK_TID_BUCKET_MAP) -- 准确率仅约 50%
    - 歧义词消歧 (AMBIGUOUS_WORDS) -- 复杂硬编码，全部交给 LLM
    """

    def __init__(self, tid_bucket_map: dict, tag_keyword_dict: dict,
                 title_keyword_dict: dict = None,
                 tag_hit_threshold: int = 2,
                 weak_tid_bucket_map: dict = None):
        """
        Args:
            tid_bucket_map: {tid(int): 桶名称} 分区→桶映射（仅强分区）
            tag_keyword_dict: {桶名称: [关键词列表]} 标签关键词词典（仅强特征词）
            title_keyword_dict: {桶名称: [关键词列表]} 标题关键词词典（高权重）
            tag_hit_threshold: 标签命中计数阈值
            weak_tid_bucket_map: (已弃用) 保留参数仅为向后兼容，传入会被忽略
        """
        self.tid_bucket_map = tid_bucket_map
        self.tag_keyword_dict = tag_keyword_dict
        self.title_keyword_dict = title_keyword_dict or {}
        self.tag_hit_threshold = tag_hit_threshold
        # weak_tid_bucket_map 已弃用，保留参数仅为向后兼容
        self.weak_tid_bucket_map = {}

        # 预处理: 将 tag_keyword_dict 中的关键词转成小写 set
        self._tag_keyword_sets = {}
        for bucket_name, keywords in self.tag_keyword_dict.items():
            self._tag_keyword_sets[bucket_name] = set(k.lower() for k in keywords)

        # 预处理: 将 title_keyword_dict 中的关键词转成小写 set
        self._title_keyword_sets = {}
        for bucket_name, keywords in self.title_keyword_dict.items():
            self._title_keyword_sets[bucket_name] = set(k.lower() for k in keywords)

    def _classify_by_ogv(self, video: BiliVideo) -> Optional[str]:
        """2.0 OGV类型直投（番剧/电影/纪录片/国创/电视剧等非UP主内容）"""
        if not video.is_ogv:
            return None
        if not video.ogv_type_name:
            return None
        from bucket_config import OGV_TYPE_BUCKET_MAP
        return OGV_TYPE_BUCKET_MAP.get(video.ogv_type_name)

    def _classify_by_tid(self, video: BiliVideo) -> Optional[str]:
        """2.1 强分区直投

        简化版: 不再区分强/弱分区，仅当 tid/tid_v2/parent_tid 在 tid_bucket_map 中
        直接命中时返回。所有混杂分区已从 tid_bucket_map 中移除，交给 LLM。
        """
        tid = getattr(video, '_tid', 0)
        parent_tid = getattr(video, '_parent_tid', 0)
        tid_v2 = getattr(video, '_tid_v2', 0)

        if tid and tid in self.tid_bucket_map:
            return self.tid_bucket_map[tid]
        if tid_v2 and tid_v2 in self.tid_bucket_map:
            return self.tid_bucket_map[tid_v2]
        if parent_tid and parent_tid in self.tid_bucket_map:
            return self.tid_bucket_map[parent_tid]
        return None

    def _classify_by_title(self, video: BiliVideo) -> Optional[str]:
        """2.2 标题关键词匹配（高权重层）

        对标题做简单分词后，检查是否命中 TITLE_KEYWORD_DICT 中的关键词。
        只要命中1个标题关键词即可归类（因为标题关键词是精选的高置信度词）。
        """
        title = video.title.lower()
        if not title:
            return None

        # 简单分词：按常见分隔符拆分标题为词片段
        for sep in ['|', '｜', '—', '–', '·', '【', '】', '「', '」', '#', '＃',
                     '！', '？', '!', '?', '(', ')', '（', '）', '[', ']', ' ', '：',
                     ':', '《', '》', '"', '"', '"', ''', ''', ',', '，', '、']:
            title = title.replace(sep, '\x00')

        # 拆分为词片段
        segments = [s.strip() for s in title.split('\x00') if s.strip()]

        # 对每个词片段做关键词子串匹配（限定匹配范围，减少跨片段误匹配）
        for bucket_name, keyword_set in self._title_keyword_sets.items():
            for keyword in keyword_set:
                for segment in segments:
                    if keyword in segment:
                        return bucket_name
        return None

    def _classify_by_tags(self, video: BiliVideo) -> Optional[str]:
        """2.3 标签特征向量匹配（简化版）

        简化后逻辑:
        - tag 按","拆分后精确匹配（小写）
        - 计算每个桶的命中计数
        - 最佳桶命中数 >= tag_hit_threshold 即归类
        - 单次命中也可归类，只要该词是该桶独有的强特征词
        - 不再做歧义词消歧（已移除 AMBIGUOUS_WORDS）
        """
        # 构建特征词集: tags（精确拆分）
        feature_words = set()
        tags_str = getattr(video, '_tags', '')
        if tags_str:
            for tag in tags_str.split(','):
                tag = tag.strip()
                if tag:
                    feature_words.add(tag.lower())

        if not feature_words:
            return None

        # 计算每个桶的命中计数
        bucket_hits = {}  # {桶名称: (hit_count, matched_words)}

        for bucket_name, keyword_set in self._tag_keyword_sets.items():
            if not keyword_set:
                continue
            matched = feature_words & keyword_set
            if matched:
                bucket_hits[bucket_name] = (len(matched), matched)

        if not bucket_hits:
            return None

        # 找出最佳桶
        best_bucket = max(bucket_hits.keys(), key=lambda b: bucket_hits[b][0])
        best_hit_count, best_matched = bucket_hits[best_bucket]

        # 超过阈值才归类
        if best_bucket and best_hit_count >= self.tag_hit_threshold:
            return best_bucket

        # 弱匹配: 1个tag命中但不在歧义词中，降低阈值
        # 检查是否是强特征词（仅出现在一个桶的关键词中）
        if best_hit_count == 1 and best_bucket:
            word = list(best_matched)[0] if best_matched else ""
            is_strong = True
            for bucket_name, keyword_set in self._tag_keyword_sets.items():
                if bucket_name != best_bucket and word in keyword_set:
                    is_strong = False
                    break
            if is_strong:
                return best_bucket

        return None

    def classify(self, video: BiliVideo) -> Optional[str]:
        """对单个视频进行Layer 2分类

        匹配优先级:
        1. OGV类型直投 (番剧/电影/纪录片/国创/电视剧等非UP主内容)
        2. 强分区直投 (tid → 桶) -- 仅强分区
        3. 标题关键词 (TITLE_KEYWORD_DICT) -- 高权重
        4. 标签特征 (TAG_KEYWORD_DICT) -- 仅强特征词精确匹配
        """
        # 先尝试OGV类型直投
        result = self._classify_by_ogv(video)
        if result:
            return result
        # 再尝试强分区直投
        result = self._classify_by_tid(video)
        if result:
            return result
        # 再尝试标题关键词匹配
        result = self._classify_by_title(video)
        if result:
            return result
        # 最后尝试标签特征匹配
        return self._classify_by_tags(video)

    def classify_all(self, videos: list[BiliVideo]) -> tuple[dict[str, list[BiliVideo]], list[BiliVideo]]:
        """批量分类
        Returns:
            (classified, unclassified)
        """
        classified = {}
        unclassified = []
        for video in videos:
            bucket = self.classify(video)
            if bucket:
                video.category = bucket
                if bucket not in classified:
                    classified[bucket] = []
                classified[bucket].append(video)
            else:
                unclassified.append(video)
        return classified, unclassified


class LLMClassifier:
    """Layer 3: 批量 LLM 语义推断（智能兜底层）"""

    def __init__(self, config: BiliConfig):
        self.config = config
        self.api_url = config.LLM_API_URL
        self.api_key = config.LLM_API_KEY
        self.model = config.LLM_MODEL
        self.bucket_names = config.BUCKET_NAMES or config.LLM_CATEGORIES
        self.batch_size = config.LLM_BATCH_SIZE
        self.confidence_threshold = config.LLM_CONFIDENCE_THRESHOLD
        self.default_bucket = config.DEFAULT_BUCKET

    def _build_prompt(self, videos: list[dict], bucket_names: list[str]) -> str:
        """构建分类prompt，要求输出桶名称和置信度"""
        bucket_desc = "\n".join(f"  - {name}" for name in bucket_names)
        video_list = "\n".join(
            f"  {i+1}. [UP: {v.get('up_name', '')}] {v.get('title', '')} | 标签: {v.get('tags', '无')}"
            for i, v in enumerate(videos)
        )

        return f"""请对以下B站视频进行分类，每条视频归入最合适的一个类别，并给出置信度。

可选类别（只能从以下选择）:
{bucket_desc}

视频列表:
{video_list}

请直接输出JSON格式，不要其他内容:
{{
  "1": {{"bucket": "类别名", "confidence": "high/medium/low"}},
  "2": {{"bucket": "类别名", "confidence": "high/medium/low"}},
  ...
}}

注意：
- 类别必须从上面的可选类别中选择
- confidence只能填 high、medium 或 low
- 对分类不确定的视频，confidence填medium或low
"""

    async def _make_api_call(self, prompt: str) -> str:
        """调用LLM API，返回响应内容"""
        import httpx

        async with httpx.AsyncClient(timeout=180.0) as client:
            resp = await client.post(
                self.api_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "user", "content": prompt}
                    ],
                    "temperature": 0.1,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]

    async def classify_batch(
        self,
        videos: list[BiliVideo],
        batch_size: int = None,
    ) -> dict[str, str]:
        """使用LLM批量分类

        Returns: {bvid: bucket_name}
        """
        if batch_size is None:
            batch_size = self.batch_size

        if not self.api_url or not self.api_key:
            print("[ERROR] LLM API未配置，请设置 LLM_API_URL 和 LLM_API_KEY")
            return {}

        # 断点续分类：加载历史 checkpoint（{bvid: bucket}）
        checkpoint_path = os.path.join(self.config.OUTPUT_DIR, "llm_checkpoint.json")
        checkpoint = {}
        if os.path.exists(checkpoint_path):
            try:
                with open(checkpoint_path, "r", encoding="utf-8") as f:
                    checkpoint = json.load(f)
                print(f"  [LLM] 加载checkpoint: {len(checkpoint)} 条已分类")
            except Exception:
                checkpoint = {}

        result = {}
        bucket_names = self.bucket_names

        # 命中 checkpoint 的直接采用，剩余的走 LLM
        pending = []
        for v in videos:
            cached = checkpoint.get(v.bvid)
            if cached and cached in bucket_names:
                result[v.bvid] = cached
                v.category = cached
            else:
                pending.append(v)

        if not pending:
            print(f"  [LLM] 全部命中checkpoint，无需请求")
            return result

        def _save_checkpoint():
            try:
                merged = dict(checkpoint)
                merged.update(result)
                os.makedirs(self.config.OUTPUT_DIR, exist_ok=True)
                with open(checkpoint_path, "w", encoding="utf-8") as f:
                    json.dump(merged, f, ensure_ascii=False)
            except Exception as e:
                print(f"  [WARN] checkpoint保存失败: {e}")

        print(f"  [LLM] checkpoint命中 {len(result)} 条，需请求 {len(pending)} 条")

        import asyncio

        async def _process_batch(batch, batch_no):
            """处理单个批次，返回是否成功"""
            video_dicts = [v.to_dict() for v in batch]
            prompt = self._build_prompt(video_dicts, bucket_names)
            try:
                content = await self._make_api_call(prompt)

                content_clean = content.strip()
                if content_clean.startswith("```"):
                    content_clean = re.sub(r'^```\w*\n?', '', content_clean)
                    content_clean = re.sub(r'\n?```$', '', content_clean)
                json_match = re.search(r'\{[\s\S]*\}', content_clean)
                if json_match:
                    mapping = json.loads(json_match.group())
                    for idx_str, value in mapping.items():
                        idx = int(idx_str) - 1
                        if 0 <= idx < len(batch):
                            bvid = batch[idx].bvid
                            if isinstance(value, dict):
                                bucket = value.get("bucket", self.default_bucket)
                                confidence = value.get("confidence", "low")
                            else:
                                bucket = str(value)
                                confidence = "high"

                            if bucket not in bucket_names:
                                bucket = self.default_bucket
                                confidence = "low"

                            if confidence in ("medium", "low"):
                                bucket = self.default_bucket

                            result[bvid] = bucket
                            batch[idx].category = bucket

                print(f"  [LLM] 批次 {batch_no}: 分类 {len(batch)} 条")
                return True

            except Exception as e:
                print(f"  [ERROR] LLM分类失败 (batch {batch_no}): {e!r}")
                return False

        # 并发波次：每波 CONCURRENT_BATCHES 个批次同时请求，提升吞吐
        concurrent_batches = int(os.getenv("LLM_CONCURRENT_BATCHES", "3"))
        total_batches = (len(pending) + batch_size - 1) // batch_size
        wave_start = 0
        while wave_start < len(pending):
            wave = pending[wave_start:wave_start + batch_size * concurrent_batches]
            tasks = []
            for j in range(0, len(wave), batch_size):
                batch_no = (wave_start + j) // batch_size + 1
                tasks.append(_process_batch(wave[j:j + batch_size], batch_no))
            await asyncio.gather(*tasks)
            _save_checkpoint()
            wave_start += batch_size * concurrent_batches
        print(f"  [LLM] 全部 {total_batches} 批次处理完成，checkpoint 共 {len(checkpoint) + len(result)} 条")

        return result

    def classify_all_sync(self, videos: list[BiliVideo]) -> tuple[dict[str, list[BiliVideo]], list[BiliVideo]]:
        """同步版本的批量分类（兼容已有事件循环）
        Returns: (classified, unclassified)
        """
        import asyncio
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            # 已在事件循环中，用 nest_asyncio 或新建线程
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                llm_result = pool.submit(
                    asyncio.run, self.classify_batch(videos)
                ).result()
        else:
            llm_result = asyncio.run(self.classify_batch(videos))

        classified = {}
        unclassified = []
        for video in videos:
            if not video.is_valid:
                continue
            if video.category and video.category in self.bucket_names:
                if video.category not in classified:
                    classified[video.category] = []
                classified[video.category].append(video)
            else:
                video.category = self.default_bucket
                unclassified.append(video)
        return classified, unclassified


class FunnelClassifier:
    """三层漏斗分流分类器

    Layer 1: UP主白名单（零误判直投）
    Layer 2: 分区直投 + 标题关键词 + 标签特征向量匹配（仅强特征词）
    Layer 3: 批量LLM语义推断
    """

    def __init__(self, config: BiliConfig):
        self.config = config
        self.layer1 = Layer1WhitelistClassifier(
            config.UP_WHITELIST,
            config.MIXED_TYPE_UPS
        )
        # Layer 2 仅使用强分区直投 + 标题/标签强特征词匹配
        # 注: WEAK_TID_BUCKET_MAP 已弃用，不再传递给 Layer2
        self.layer2 = Layer2StructuralClassifier(
            config.TID_BUCKET_MAP,
            config.TAG_KEYWORD_DICT,
            getattr(config, 'TITLE_KEYWORD_DICT', {}),
            config.TAG_HIT_THRESHOLD,
        )
        self.layer3 = LLMClassifier(config)
        self.default_bucket = config.DEFAULT_BUCKET

    def classify_all(self, videos: list[BiliVideo]) -> dict[str, list[BiliVideo]]:
        """三层漏斗分流，返回 {桶名称: [视频列表]}"""
        valid_videos = [v for v in videos if v.is_valid]
        print(f"\n[分流] 开始三层漏斗分流，共 {len(valid_videos)} 条有效视频")

        # Layer 1: UP主白名单
        l1_classified, l1_unclassified = self.layer1.classify_all(valid_videos)
        l1_count = sum(len(v) for v in l1_classified.values())
        print(f"  [Layer 1] UP主白名单拦截: {l1_count} 条")
        for bucket, vids in l1_classified.items():
            print(f"    → {bucket}: {len(vids)} 条")

        # Layer 2: 分区/标签匹配
        l2_classified, l2_unclassified = self.layer2.classify_all(l1_unclassified)
        l2_count = sum(len(v) for v in l2_classified.values())
        print(f"  [Layer 2] 分区/标签匹配拦截: {l2_count} 条")
        for bucket, vids in l2_classified.items():
            print(f"    → {bucket}: {len(vids)} 条")

        # Layer 3: LLM语义推断
        if l2_unclassified and self.config.LLM_API_URL:
            print(f"  [Layer 3] LLM语义推断: {len(l2_unclassified)} 条")
            l3_classified, l3_unclassified = self.layer3.classify_all_sync(l2_unclassified)
            l3_count = sum(len(v) for v in l3_classified.values())
            print(f"  [Layer 3] LLM分类完成: {l3_count} 条")
            for bucket, vids in l3_classified.items():
                print(f"    → {bucket}: {len(vids)} 条")
            # 未被LLM分类的归入待分类
            for video in l3_unclassified:
                video.category = self.default_bucket
                if self.default_bucket not in l3_classified:
                    l3_classified[self.default_bucket] = []
                l3_classified[self.default_bucket].append(video)
        else:
            l3_classified = {}
            if l2_unclassified:
                print(f"  [Layer 3] LLM未配置，{len(l2_unclassified)} 条归入待分类")
                for video in l2_unclassified:
                    video.category = self.default_bucket
                    if self.default_bucket not in l3_classified:
                        l3_classified[self.default_bucket] = []
                    l3_classified[self.default_bucket].append(video)

        # 合并三层结果
        final_result = {}
        for classified in [l1_classified, l2_classified, l3_classified]:
            for bucket, vids in classified.items():
                if bucket not in final_result:
                    final_result[bucket] = []
                final_result[bucket].extend(vids)

        # 统计
        total_classified = sum(len(v) for v in final_result.values())
        print(f"\n[分流完成] 总计分类: {total_classified} 条")
        for bucket in sorted(final_result.keys()):
            print(f"  {bucket}: {len(final_result[bucket])} 条")

        return final_result


class RuleClassifier:
    """规则式分类器（兼容旧接口，内部委托给 FunnelClassifier）"""

    def __init__(self, config: BiliConfig):
        self.config = config
        self._funnel = FunnelClassifier(config)
        self.up_category_map = config.UP_WHITELIST
        self.keyword_rules = []
        self.default_category = config.DEFAULT_BUCKET

    def load_rules(self, rules_path: str):
        """从JSON文件加载分类规则（兼容旧接口）"""
        with open(rules_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # 合并到白名单
        if "up_category_map" in data:
            self.config.UP_WHITELIST.update(data["up_category_map"])
        if "default_category" in data:
            self.default_category = data["default_category"]
        # 重建funnel
        self._funnel = FunnelClassifier(self.config)

    def set_up_map(self, up_map: dict[str, str]):
        """直接设置UP主映射"""
        self.config.UP_WHITELIST.update(up_map)
        self._funnel = FunnelClassifier(self.config)

    def classify(self, video: BiliVideo) -> str:
        """对单个视频进行分类"""
        result = self._funnel.layer1.classify(video)
        if result:
            return result
        result = self._funnel.layer2.classify(video)
        if result:
            return result
        return self.default_category

    def classify_all(self, videos: list[BiliVideo]) -> dict[str, list[BiliVideo]]:
        """批量分类"""
        return self._funnel.classify_all(videos)


class HybridClassifier:
    """混合分类器（兼容旧接口，内部委托给 FunnelClassifier）"""

    def __init__(self, config: BiliConfig):
        self.config = config
        self.funnel = FunnelClassifier(config)
        # Keep references for backward compatibility
        self.rule_classifier = RuleClassifier(config)
        self.llm_classifier = self.funnel.layer3

    def classify_all(self, videos: list[BiliVideo]) -> dict[str, list[BiliVideo]]:
        """完整的三层漏斗分流"""
        return self.funnel.classify_all(videos)
