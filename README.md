# B站收藏夹自动化整理工具

将B站"待看"收藏夹中的视频，通过三层漏斗分流自动归类到 13 个主题桶，并在桶内按时效性、UP主聚合、系列识别、发布时序进行精细化排序。

> 当前数据规模：**5564 条视频**（13 个 `tmp_*` 收藏夹 + 稍后再看 275 条）。
> 详细的项目意图分析、技术合理性评估与实测缺陷清单见 [`ANALYSIS_REPORT.md`](ANALYSIS_REPORT.md)。

---

## 快速开始（活跃管线）

### 0. 安装依赖

```bash
pip install -r requirements.txt
```

> 当前管线依赖 `pandas`（CSV 读写）与 `aiohttp`。

### 1. 配置B站登录凭证

方式一：复制凭证模板并填入你的凭证（推荐）：

```bash
cp bili_secrets.py.example bili_secrets.py
# 然后编辑 bili_secrets.py 填入你的凭证（该文件已被 .gitignore 忽略，不会被提交）
```

方式二：环境变量：

```bash
set BILI_SESSDATA=你的SESSDATA
set BILI_DEDEUSERID=你的UID
set BILI_BILI_JCT=你的bili_jct
```

| 配置项 | 获取方式 |
|--------|----------|
| `SESSDATA` | 浏览器登录B站 → F12 → Application → Cookies → SESSDATA |
| `DedeUserID` | 你的B站UID |
| `BILI_JCT` | Cookies 中的 bili_jct（整理操作需要，仅爬取可留空） |

> ⚠️ `bili_secrets.py` 含登录凭证，已被 `.gitignore` 忽略，切勿提交或外传。

### 2. 按顺序执行四步

```bash
python run_crawl.py                        # ① 爬取 → output/videos.csv
python crawl_tags.py                       # ② 回填 tags / tname / parent_tid（推荐，详见下方说明）
python run_classify.py                     # ③ 三层分流 + 桶内排序 → output/classified_sorted_videos.csv
python run_organize_from_classified.py     # ④ 实际整理（先 dry-run 确认）
```

| 步骤 | 脚本 | 产物 |
|---|---|---|
| ① 爬取 | `run_crawl.py` | `output/videos.csv`（25 列，含 tags；`tname` 为空） |
| ② 回填 | `crawl_tags.py` | 回填 `tags` / `tname` / `tname_v2` / `parent_tid` / `parent_name` |
| ③ 分类排序 | `run_classify.py` | `output/classified_sorted_videos.csv`（26 列）、`output/bucket_stats.json` |
| ④ 整理 | `run_organize_from_classified.py` | 写入 B站收藏夹，状态落 `output/move_state.json` |

**关于步骤 ②**：README 旧版把它标为"可选"。实际上**建议每次分类前都跑**，原因：

- B站 `/x/web-interface/view` 接口已不再返回 `tname` / `tname_v2`（见 `crawler.py:410-411` 注释），只有 `crawl_tags.py` 会用 `TID_MAP` 回填出可读的分区名；
- 步骤 ① 产出的 `parent_tid` 是用 `tid_v2` 近似的值，只有 `crawl_tags.py` 会按 `PARENT_MAP` 算出正确的父分区。

> ⚠️ 注意：`run_classify.py` 导出时用 `BiliVideo.to_dict()`，会丢掉 `tags` / `tname` / `parent_tid` 等 7 列。因此步骤 ③ 的产物**不适合直接拿去重新分类**——重新分类请用步骤 ①② 的 `videos.csv`。

### 3. 其他入口（按需）

| 脚本 | 用途 |
|---|---|
| `run_dryrun.py` | 只预览整理方案，不写 B站 |
| `run_crawl_incremental.py` | 增量爬取（只补缺失视频） |
| `run_organize_incremental.py` | 增量整理（对比上次状态，只处理变化/新增） |
| `run_organize.py` | 从 `videos.csv` 现分类现整理（含 `--incremental`） |
| `run_organize_rebuild.py` | 重建式整理（清空重排，风控风险高，慎用） |

> ⚠️ `main.py` 的交互式菜单走的是**已废弃的旧路径**（依赖 `classification_rules_template.json`、产出 `classified_videos.csv`，两者当前均不存在，且模板类别与 13 个 `tmp_*` 桶不匹配）。**请勿使用**，以本节的命令式管线为准。

---

## 功能概览

### 三层漏斗分流

| 层级 | 机制 | 说明 |
|------|------|------|
| Layer 1 | UP主白名单直投 | 单领域UP主零误判路由，混合型UP主隔离到 `MIXED_TYPE_UPS` |
| Layer 2 | OGV直投 + 强分区直投 + 标题/标签关键词 | B站 tid 分区映射 + 强特征词命中 |
| Layer 3 | 批量LLM语义推断 | 15-20 条一批，输出置信度，中/低置信度归入 `tmp_待分类` |

**实测命中分布**（5563 条有效视频，2026-09-01）：

| 层级 | 命中数 | 占比 |
|---|---|---|
| Layer 1 UP主白名单 | 2863 | **51.5%** |
| Layer 2 强分区直投 | 321 | 5.8% |
| Layer 2 标签匹配 | 267 | 4.8% |
| Layer 2 标题关键词 | 189 | 3.4% |
| Layer 2 OGV 直投 | 0 | 0% |
| Layer 3 LLM | 1923 | 34.6% |

> Layer 1 白名单才是主力。想提升分类质量，优先扩充 `bucket_config.py` 的 `UP_WHITELIST`，收益远大于调 LLM。

### 13 个固定目标桶

`tmp_生活娱乐` `tmp_知识科普` `tmp_数码科技` `tmp_游戏资讯` `tmp_游戏实况` `tmp_影视解说` `tmp_播客访谈` `tmp_社会观察` `tmp_汽车运动` `tmp_编程开发` `tmp_投资财经` `tmp_美食探店` `tmp_待分类`

### 桶内三区块排序

```
[Top 区块]    时效性内容（预告/资讯/行情等）→ 收藏时间倒序
[Main 区块]   常规待看池
  ├─ 高频UP主 A 块 → 系列识别保持连续 → 发布时间升序
  ├─ 高频UP主 B 块 → ...
  └─ 零散UP主池
[Bottom 区块] 收藏超6个月归档 → 收藏时间倒序
```

> ⚠️ 已知缺陷：稍后再看的 275 条视频 `fav_time` 为 0，会被无条件判定为归档并全部落入 Bottom（当前 Bottom 区块 275 条全部来自这里，没有一条是真正"超 6 个月"的）。详见 ANALYSIS_REPORT §4.4。

### 容量自动分卷

目标收藏夹达 950 条时自动路由至 `_2` 后缀桶（如 `tmp_编程开发_2`），分卷桶不存在时自动创建。

---

## 项目结构

```
bili-fav-organizer/
├── 核心模块
│   ├── config.py          # 配置（认证、并发、LLM、排序参数），从 bucket_config 加载规则
│   ├── models.py          # 数据模型（BiliVideo / BiliUP / BiliFolder）
│   ├── crawler.py         # B站API封装（WBI签名、收藏夹读写、412退避）
│   ├── bucket_config.py   # 13桶定义、分区映射、标签/标题词典、时效词、系列正则、UP白名单
│   ├── classifier.py      # 三层漏斗分流引擎
│   │   ├── Layer1WhitelistClassifier    # UP主白名单
│   │   ├── Layer2StructuralClassifier   # OGV直投 + 强分区 + 标题/标签关键词
│   │   ├── LLMClassifier                # 批量LLM语义推断（含置信度路由）
│   │   └── FunnelClassifier             # 三层串联入口
│   ├── sorter.py          # 桶内 Top/Main/Bottom 三区块排序
│   ├── organizer.py       # 整理执行（创建桶、移动、容量分卷、断点续移、增量重排）
│   ├── rate_limiter.py    # 令牌桶限流 + 412 自适应退避
│   ├── move_state.py      # 断点续移状态机
│   └── reorder_state.py   # 续重排状态机
├── 入口脚本（活跃管线）
│   ├── run_crawl.py                     # ① 爬取
│   ├── crawl_tags.py                    # ② 回填 tags / tname / parent_tid
│   ├── run_classify.py                  # ③ 分流 + 排序
│   └── run_organize_from_classified.py  # ④ 整理
├── 入口脚本（其他）
│   ├── run_dryrun.py / run_crawl_incremental.py
│   ├── run_organize.py / run_organize_incremental.py / run_organize_rebuild.py
│   └── main.py            # 交互式菜单 —— 已废弃，请勿使用
├── 周边/诊断/遗留脚本
│   ├── supplement_crawl.py / supplement_v2.py / supplement_v3.py / supplement_v4.py
│   ├── crawl_toview.py / crawl_watchlater.py / enrich_tags.py / reorder_folders.py
│   ├── analyze_state.py / analyze_pending.py / check_sort.py / debug_api.py
│   └── test_llm_classifier.py / test_resume.py / test_watchlater_cleanup.py
├── output/                # 数据输出（已被 .gitignore 忽略）
│   ├── videos.csv                        # 爬取原始结果
│   ├── classified_sorted_videos.csv      # 分类+排序结果
│   ├── bucket_stats.json / folders.json
│   └── move_state.json / incremental_state.json / rebuild_state.json
├── ANALYSIS_REPORT.md     # 项目分析：意图 / 技术合理性 / 文档对齐
└── requirements.txt
```

---

## 自定义配置

### 修改UP主白名单（收益最高）

编辑 `bucket_config.py` 的 `UP_WHITELIST`，添加 `mid → 桶名称` 映射。跨领域UP主应加入 `MIXED_TYPE_UPS` 而非白名单。

### 修改分区映射

编辑 `bucket_config.py` 的 `TID_BUCKET_MAP`，调整B站分区ID到目标桶的映射（键为老分区号，如 `181` / `245` / `211`）。

### 修改标签/标题关键词词典

编辑 `TAG_KEYWORD_DICT`（标签精确匹配，需命中 2 个）与 `TITLE_KEYWORD_DICT`（标题关键词，命中 1 个即归类）。

### 配置LLM分类

在 `config.py` 中设置：

- `LLM_API_URL`：LLM API 地址（兼容 OpenAI 格式）
- `LLM_API_KEY`：API 密钥
- `LLM_MODEL`：模型名称
- `LLM_BATCH_SIZE`：批处理大小（默认 20）
- `LLM_CONFIDENCE_THRESHOLD`：置信度阈值（默认 medium，medium/low 归入 `tmp_待分类`）

### 调整排序参数

- `ARCHIVE_MONTHS`：归档月数阈值（默认 6）
- `FOLDER_CAPACITY_LIMIT`：收藏夹容量上限（默认 950）

---

## 已知限制

### 1. 增量整理下桶内排序的不完全一致性

桶内三区块排序依赖 `sorter.py` 对全量视频计算排序键，再通过 B站 API 按 `mtime` 倒序写入收藏夹实现最终顺序。**理想的完全排序要求每次整理都对目标桶内全部视频重新写入一遍**（清空桶 → 按序逐个 add）。

**为什么不这样做**——B站对 POST 写操作（`add`/`move`/`delete`）的风控比 GET 严格得多：

- GET 读操作 0.5s 间隔连续 5564 次无 412（已实测）
- POST 写操作 0.5s 间隔会触发 412（已实测触发）
- POST 写操作 2s 间隔可安全执行，但 5563 条全量重排需 3 小时以上，且中断恢复成本高

因此整理采用**增量模式**：只对分类变化的视频 `move`，对新增视频 `add`，不动的保持原位。

**实际效果**（B站收藏夹按 `mtime` 倒序，后添加的排最前）：

```
[今天 add 的新视频]              ← 排最前
[今天 move 的分类变化视频]       ← 次之
[上次整理时按三区块排序的旧视频] ← 保持原有相对顺序
```

- 未变视频（约 83%）：三区块相对排序完整保留
- 新增/变化视频（约 17%）：排到桶最前面，与三区块预期位置可能不一致

**为什么接受**：新内容优先展示符合直觉；分类正确性优先于排序精度；412 风控代价过高；个别视频可在 B站网页端手动拖动微调。

### 2. 历史整理遗留的数据问题（2026-09-07 线上实测）

`output/move_state.json` 记录（2026-07-21 运行，源 CSV 为 07-05 快照，陈旧 16 天）：

| 状态 | 条数 | 占比 |
|---|---|---|
| skipped（目标已存在，幂等跳过） | 3839 | 73.1% |
| moved | 897 | 17.1% |
| added | 85 | 1.6% |
| **failed_permanent** | **430** | **8.2%** |

已直接拉取 B站线上全量数据（44 个自建收藏夹 + 稍后再看）复核，结论：

| 核查项 | 实测 |
|---|---|
| 430 条 `failed_permanent` 其实**在 13 桶里** | **411 条（95.6%）** —— 只是不在原计划桶 |
| 落在"原计划桶"的 | 0 条 |
| **真正无归属（任何收藏夹都找不到）** | **41 条** |
| 同一视频重复存在于两个桶 | 44 条（均为 `tmp_待分类` + 另一桶） |
| 落桶与当前分类期望不符（错位） | 234 条（4.3%） |

即：**`failed_permanent` 95.6% 是误报**（判定逻辑只看"源"和"目标"，从不去别的桶找），真正需要处理的是 41 条无归属 + 44 条重复 + 234 条错位。

> **建议**：① 找回那 41 条（视频在 B站仍存在，按 bvid 重新 add）；② 判永久失败前先全桶定位；③ 加 `--reconcile` 纠正 234 条错位。详见 ANALYSIS_REPORT §4.5.1。

### 3. `tname` / `tname_v2` 为空

B站 `/x/web-interface/view` 接口变更后不再返回分区名（`crawler.py:410-411` 已记录）。需用 `crawl_tags.py` 的 `TID_MAP` 回填。分类器只依赖数字 `tid`，**不影响分类正确性**，只影响 CSV 可读性。

### 4. 其他

- 收藏夹标题限制 20 字符，分卷桶名称过长会自动截断
- `bili_jct` 是执行移动/创建操作的必要凭证，仅爬取数据时可以不填
- OGV 直投分支（`OGV_TYPE_BUCKET_MAP`）当前数据下 0 命中，未经实际验证
- 建议每次整理前先 dry-run 预览，确认无误再执行

---

## 注意事项

- B站 API 有频率限制，脚本内置了请求间隔与 412 风控自动退避（30→60→120→240→300s 阶梯，成功后衰减）
- `config.py` 与 `output/` 已被 `.gitignore` 忽略，不会入库
- 整理前建议确认 `videos.csv` 是最新爬取结果——用陈旧的 CSV 整理会导致大量源侧验证失败（见"已知限制 2"）
