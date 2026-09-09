# B站收藏夹自动整理工具 — 项目意图 · 技术合理性 · 文档对齐 综合分析报告

> **分析时间：2026-09-01**
> **版本：v2（修订版，取代 2026-07-27 版）**
> **分析范围**：全仓库核心模块 + 9 个 `run_*` 入口 + 周边/遗留脚本 + `output/` 全部产物 + `.gitignore` + `README.md`
> **方法**：逐文件实读源码 → 提取关键算法 → **用真实模块离线复算**（加载 `output/videos.csv` 5564 条，跑 `FunnelClassifier` 的 Layer1/Layer2，不触发网络与 LLM）→ 用实测数据校正旧报告结论

---

## 〇、本次修订说明（相对 2026-07-27 旧报告）

旧报告以源码静态阅读为主，少量结论依赖推断。本次改为**实测驱动**，凡能算的都用真实数据复算。结果有 5 处结论需要修正：

| # | 旧报告结论 | 本次实测 | 判定 |
|---|---|---|---|
| 1 | `parent_tid` 用 `tid_v2` 近似导致"**强分区直投结果剧烈漂移**" | 值层面 5563/5564 条确实全错；但受 `_classify_by_tid` 的 `tid→tid_v2→parent_tid` 查找顺序保护，**端到端分类结果仅 30 条（0.5%）变化** | ⚠️ 旧报告**夸大了影响** |
| 2 | 设计理念"Layer 3 把**绝大多数**视频交给 LLM" | 实测：**Layer 1 白名单命中 51.5%**，Layer 2 命中 14.0%，LLM 仅 **34.6%** | ❌ 与代码实际行为不符 |
| 3 | 未提及 `tname` 全空 | 实测 `tname`/`tname_v2` 非空 **0/5564** | 🆕 新增发现（根因是 B站 API 变更，非代码 bug） |
| 4 | 未提及 Bottom 区块异常 | 实测 Bottom 275 条 **100% 是 `fav_time=0` 的稍后再看视频**，无任何"真·超 6 个月"视频 | 🆕 新增缺陷（真 bug） |
| 5 | "13 分类全部 completed"（暗示整理成功） | 实测：13 分类 `completed` 但 **top/main 区块 pending 且 0 条**，5251 条全挤在 bottom；**430 条 `failed_permanent`** | ❌ 旧报告**漏报了失败规模** |

---

## 一、项目意图

### 1.1 一句话意图

把 B站收藏夹（一批 `tmp` 前缀的"待看"收藏夹 + "稍后再看"）里的视频，**自动归类到 13 个固定主题桶**，并在桶内做精细化排序，最终通过 B站 API 把视频移动/添加到对应收藏夹。

### 1.2 目标场景

- 收藏夹被塞爆（当前 **5564 条**），手动整理不现实。
- 希望按主题分桶（编程/科普/影视/游戏…），且桶内有合理顺序（时效性内容在前、同 UP 主聚合、系列连续、陈年内容沉底）。

### 1.3 两条设计主线（可从代码注释还原演进史）

**主线一：分类 —— 从"复杂硬编码规则"走向"三层漏斗 + LLM 兜底"**

- 早期：`AMBIGUOUS_WORDS` 歧义消歧、`WEAK_TID_BUCKET_MAP` 弱分区回退、大量关键词词典——据注释准确率仅约 50%，维护成本高。
- 现状（2026-07-21 简化版）：Layer 1 UP主白名单直投（零误判）→ Layer 2 仅保留"内容单一、误判率极低"的强分区/强特征词 → Layer 3 LLM 语义推断，中/低置信度一律落入 `tmp_待分类` 人工兜底。

**主线二：整理 —— 从"全量重排"走向"增量整理（容忍排序不完全一致）"**

- B站 POST 写操作（`add`/`move`/`delete`）风控极严：0.5s 间隔即触发 412；全量重排 5000+ 条需 3 小时以上且易中断。
- 现状：增量模式只 move 分类变化的、只 add 新增的，不动的保持原位；接受"新增/变化视频排到桶最前"这一偏差（README 用一整节说明）。

### 1.4 意图评价

意图清晰、定位务实。它不是玩具脚本，而是针对"B站收藏夹爆炸 + API 严格风控"这一真实痛点、经过多轮迭代、认真处理了断点续跑/风控退避/容量分卷/防数据丢失的**生产级个人工具**。对核心约束（B站风控）的工程妥协清醒且诚实——这一点在 README 中被明确记录而非掩盖，是成熟工程态度。

---

## 二、数据现状基线（全部为实测值）

| 项目 | 数值 |
|---|---|
| `output/videos.csv` | **5564 行 / 25 列**；有效 5563，失效 1 |
| `output/classified_sorted_videos.csv` | **5563 行 / 26 列** |
| 来源分布 | `tmp_知识科普` 827、`tmp_生活娱乐` 708、`tmp_数码科技` 707、`tmp_影视解说` 547、`tmp_待分类` 471、`tmp_游戏资讯` 457、`tmp_社会观察` 404、`tmp_编程开发` 315、`tmp_汽车运动` 303、**`稍后再看` 275**、`tmp_美食探店` 210、`tmp_游戏实况` 207、`tmp_投资财经` 109、`tmp_播客访谈` 24 |
| `tags` 非空 | 5561 / 5564（99.9%，数据源 `/x/tag/archive/tags` 正常） |
| `tname` / `tname_v2` 非空 | **0 / 5564**（B站 API 已不返回，见 §4.3） |
| `parent_tid == tid_v2` | **5564 / 5564**（100% 为近似值，见 §4.2） |
| B站 13 桶实际条数合计（folders.json 2026-07-22 快照） | 5294 |
| 分类结果合计 | 5563（与线上差 **269**） |

---

## 三、架构与数据流

### 3.1 模块职责

| 模块 | 职责 | 评价 |
|---|---|---|
| `config.py` | 配置（凭证、并发、LLM、排序参数），`__post_init__` 从 `bucket_config` 加载规则 | 关键但含硬编码凭证 |
| `models.py` | `BiliVideo` / `BiliUP` / `BiliFolder` | 干净，但 `to_dict()` 缺 7 列 |
| `crawler.py` | B站 API 封装（WBI 签名、收藏夹列表/内容、创建/移动/复制/删除、412 退避） | 核心，健壮，注释准确 |
| `bucket_config.py` | 13 桶、分区映射、标签/标题词典、时效词、系列正则、UP 白名单/混合型集合 | 数据驱动，体量大 |
| `classifier.py` | 三层漏斗分类引擎 | 设计清晰 |
| `sorter.py` | 桶内 Top/Main/Bottom 三区块排序 | 合理，但归档判定有 bug（§4.4） |
| `organizer.py` | 创建桶、move/add/copy、容量分卷、断点续移、增量重排 | 最复杂、最健壮 |
| `rate_limiter.py` | 令牌桶 + 412 自适应退避 | 工程亮点 |
| `move_state.py` / `reorder_state.py` | 断点续移 / 续重排状态机（原子写、恢复、源/目标双向验证） | 工程亮点，但对"上轮已移动"误判为永久失败（§4.5） |
| `run_*.py` | 9 个独立入口（直接 `python xxx.py`，彼此不 import） | 入口碎片化 |
| 周边脚本 | `supplement_*` / `crawl_*` / `analyze_*` / `enrich_tags` / `reorder_folders` / `debug_api` / `check_sort` | 大量遗留 |

### 3.2 活跃主干管线（README 应主推这一条）

```
run_crawl.py      → output/videos.csv        （基础字段；tags 有值；tname 空；parent_tid 用 tid_v2 近似）
      ↓
crawl_tags.py     → 回填 tags/tname/tname_v2/parent_tid/parent_name   （★当前 CSV 尚未回填）
      ↓
run_classify.py   → FunnelClassifier + FolderSorter
      ↓           → output/classified_sorted_videos.csv  （★丢失 tags/tname/parent_tid 等 7 列）
      ↓           → output/bucket_stats.json
run_organize_from_classified.py → organizer.full_organize() → B站收藏夹
```

**旁支**：`run_organize.py` / `run_organize_incremental.py` / `run_organize_rebuild.py` / `run_dryrun.py` / `run_crawl_incremental.py` 为不同阶段的实验入口；`main.py` 交互菜单走的是**已废弃的旧路径**（§4.6）。

---

## 四、技术合理性评估

### 4.1 做得好的地方（工程亮点）

1. **风控应对工程化**：`rate_limiter.py` 令牌桶 + `crawler.py` 对 412 的专门退避冷却（30→60→120→240→300s 阶梯，成功后衰减）+ `MAX_CONCURRENT_REQUESTS=1`。这是对 B站实战约束的严肃应对，不是拍脑袋的 `sleep`。
2. **双状态机断点续跑**：`move_state.py` / `reorder_state.py` 做了原子写、terminal 状态、源/目标双向验证、412 中断保持 `in_progress` 可恢复。远超一般脚本水准。
3. **排序感知移动**（`organizer.py:387-393`）：利用 B站按 `mtime` 倒序的特性，按 **Bottom → Main → Top** 顺序操作（后操作的排最前），纯靠 API 行为实现期望顺序。构思巧妙且注释清楚。
4. **防数据丢失**：`reorder_folder_full` 删临时桶前检查是否非空、非空先回迁；`cleanup_temp_folders` 对孤儿桶先回迁再删。
5. **数据驱动分类**：UP 白名单 / 分区映射 / 关键词全部集中在 `bucket_config.py`，并附"为何移除此映射"的注释，可追溯性强。
6. **代码注释质量高**：`crawler.py:410-411` 明确记录"B站 API 变更后不再返回 tname/tname_v2 和 tag"，说明作者踩过坑并留了档。
7. **`.gitignore` 已忽略** `config.py` / `output/` / `*.bak`，避免凭证与大数据入库。

### 4.2 三层漏斗的真实命中分布（实测）

用真实 `FunnelClassifier` 对 5563 条有效视频离线复算（跳过 LLM 层，统计"本该交给 LLM 的量"）：

| 层级 | 命中数 | 占有效视频 |
|---|---|---|
| **Layer 1** UP主白名单 | **2863** | **51.5%** |
| Layer 2 强分区直投（`_classify_by_tid`） | 321 | 5.8% |
| Layer 2 标签匹配（`_classify_by_tags`） | 267 | 4.8% |
| Layer 2 标题关键词（`_classify_by_title`） | 189 | 3.4% |
| Layer 2 OGV 直投 | **0** | 0% |
| **Layer 3** LLM | **1923** | **34.6%** |
| 失效视频 | 1 | 0.0% |

**结论**：

- **Layer 1 白名单才是主力（51.5%）**，而非旧报告与代码注释宣称的"绝大多数交给 LLM"。LLM 实际处理约 1/3。
- 这个结构其实**很健康**：一半以上由零误判的规则直投，仅 1/3 需要 LLM，成本可控。
- 但**代码注释（`classifier.py:21-22`）与旧报告的"绝大多数交给 LLM"表述是错的**，会误导后续维护者对 LLM 成本与调优优先级的判断——真正该持续投入的是 **UP主白名单的覆盖面**。
- **OGV 直投分支 0 命中**：`OGV_TYPE_BUCKET_MAP` 相关代码从未生效过（当前数据 `is_ogv` 全 False）。

### 4.3 数据质量：`tname` / `tname_v2` 全空（新增发现）

- 实测：`videos.csv` 中 `tname` 非空 **0/5564**，`tname_v2` 同样全空。
- **根因不是 bug**：`crawler.py:410-411` 已注释说明 B站 `/x/web-interface/view` 接口变更后不再返回 `tname`/`tname_v2`。代码取到空串就写空串。
- **缓解手段已存在但未执行**：`crawl_tags.py:136,142-145` 用 `TID_MAP`（121 条）回填 `tname`。但当前 `videos.csv` 明显未回填（或其结果被后续 `run_crawl.py` 覆盖）。
- **实际影响**：**低**。分类器用的是数字 `tid` 而非名称，`tname` 只影响人工查看 CSV 与调试。
- **定位**：数据质量瑕疵 + 流程执行遗漏（`crawl_tags.py` 未跑或未生效），不是代码缺陷。

### 4.4 🔴 真 Bug：Bottom 区块 100% 是 `fav_time=0` 造成的误判（新增发现）

**判定逻辑**（`sorter.py:53-57`）：

```python
def identify_archived(self, video, now=None) -> bool:
    if now is None: now = time.time()
    return (now - video.fav_time) >= self.archive_seconds   # ARCHIVE_MONTHS=6
```

当 `fav_time == 0` 时，`now - 0 ≈ 1.78e9 秒`，远超 6 个月阈值 → **无条件判定为归档**。

**实测三重完全重合**：

| 指标 | 条数 |
|---|---|
| `block_type == 'bottom'` | 275 |
| `fav_time == 0` | 275 |
| `source_folder_title == '稍后再看'` | 275 |

- Bottom 区块 **275 条全部**是稍后再看视频，且这 275 条**就是全部** `fav_time=0` 的记录。
- 稍后再看视频共 275 条（`source_folder_id=0`，`fav_time` 全为 0）。
- 也就是说：**Bottom 区块当前不包含任何一条"真正收藏超 6 个月"的视频**，它完全是 `fav_time` 缺失的产物。

**附带影响**：`is_timely=True` 共 533 条，但 Top 区块只有 510 条——差额 23 条因 `fav_time=0` 时归档判定优先于时效判定，被错误压入 Bottom。

**根因**：稍后再看走的是独立接口（`/x/v2/history/toview` 系），不返回收藏时间，抓取代码未赋默认值 → `fav_time` 留 0。

**建议**：
- 在 `identify_archived` 中显式处理 `fav_time <= 0`：视为"时间未知"，**归入 Main 而非 Bottom**；
- 或为稍后再看视频补一个合理的时间代理（如用 `pubtime` 或抓取时刻）。

### 4.5 🔴 `move_state.json` 暴露的整理质量问题（旧报告漏报）

`move_state.json` 记录的是 **2026-07-21 22:47** 的一次 `mode=auto` 全量整理（`dry_run=False`）。实测内容：

**区块分布异常**：

| 区块 | 状态 | 记录数 |
|---|---|---|
| bottom | completed | **5251** |
| main | pending | 0 |
| top | pending | 0 |

13 个分类的 `status` 都是 `completed`，但 **top/main 区块从未执行**，全部 5251 条记录集中在 bottom。这说明该次运行时 sorter 把所有视频判为了归档。结合 §4.4 的机制，可推断**当时所用的 `videos.csv` 中 `fav_time` 大面积为 0**；`source_csv_mtime = 2026-07-05 03:49`，而运行在 07-21，源数据已陈旧 16 天。之后 07-22 10:26 重新爬取，fav_time 才修复（当前 CSV 中仅 275 条为 0）。

**视频级状态分布**：

| 状态 | 条数 | 占比 | 说明 |
|---|---|---|---|
| `skipped` | 3839 | 73.1% | 目标收藏夹已存在，幂等跳过（正常） |
| **`failed_permanent`** | **430** | **8.2%** | **异常** |
| `moved` | 897 | 17.1% | 成功移动 |
| `added` | 85 | 1.6% | 成功添加 |

**430 条永久失败全部是同一个原因**：

> `视频不在源收藏夹且不在目标收藏夹`

**根因**：源 CSV 陈旧（07-05 快照 vs 07-21 运行）。这些视频**已被更早的轮次移出了源收藏夹**，源侧验证必然失败；而目标侧校验也未命中（可能实际在别的桶里、或目标列表缓存过期）。状态机把这个"上轮已移动"的正常情况误判成了 `failed_permanent`，并**永久遗弃**——不会再重试。

**交叉验证**：`folders.json`（07-22 10:26）13 桶合计 **5294** 条，分类结果 **5563** 条，差 **269** 条。与 430 条永久失败同量级，佐证确有视频未落到桶里（部分可能已通过增量流程补回）。

### 4.5.1 【2026-09-07 线上实测复核】430 条的真实去向

上文是基于状态文件的推断。**本次直接拉取 B站线上全量数据做了复核**（44 个自建收藏夹 + 稍后再看），结论有实质性修正：

| 核查项 | 结果 |
|---|---|
| 430 条 `failed_permanent` 现在**确实在 13 桶中** | **411 条（95.6%）** |
| 其中落在"原计划桶"的 | **0 条** —— 全部在**别的**桶里 |
| 现在仍不在任何桶中 | 19 条（其中 17 条已不在当前 `videos.csv`） |

**结论：`failed_permanent` 有 95.6% 是误报。** 判定逻辑（`organizer.py:550-565`）只看"源收藏夹"和"当前目标桶"，**从不去其他桶里找**。而后续增量整理已按新分类把这些视频移到了正确的新桶，于是旧状态文件里的计划就永远"对不上"了。

> 典型链路：07-21 计划把它放进 `tmp_待分类` → 源侧验证失败 → 判永久失败；07-22 增量整理按新分类把它放进了 `tmp_生活娱乐` → 视频其实好好的。

**线上全量数据快照**（2026-09-07）：

| 指标 | 数值 |
|---|---|
| 13 个 tmp 桶合计（含重复） | 5509 |
| 去重后 | **5465** |
| **同一视频存在于两个桶（重复）** | **44 条**（全部为 `tmp_待分类` + 另一桶） |
| 分类结果中，全部 44 个收藏夹都找不到、也不在稍后再看 | **41 条（真实无归属）** |
| 落桶与当前分类期望不符（错位） | **234 条（4.3%）** |
| 分类 CSV 重复 avid | 60 个（5563 行 / 5503 唯一） |

**41 条真实无归属**中，26 条来源是"稍后再看"（已被 `watchlater_deleted_bvids` 记录为删除），15 条来源是 tmp_* 桶。视频本身在 B站上仍存在，只是不在任何收藏夹里 —— 可按 bvid 找回。

**修正后的建议**：
1. **不是"430 条丢失"，而是"41 条无归属 + 44 条重复 + 411 条误报"**。严重性大幅下降，但 41 条仍需找回。
2. 判 `failed_permanent` 前，先批量拉取全部 tmp_* 桶的 ID 列表定位视频实际位置（成本极低，13 次请求）。
3. 写 `recover_orphans.py`：比对分类结果与线上差集，把这 41 条 add 回对应桶。
4. 加 `--reconcile` 模式纠正 234 条错位。
5. 整理前强校验 `source_csv_mtime` 与线上状态的时间差，超过阈值拒绝执行。

### 4.6 🟠 `parent_tid` 算法不一致（实测校正：影响被旧报告夸大）

- **权威算法**：`crawl_tags.py:139` → `parent_tid = PARENT_MAP.get(tid, tid)`，`PARENT_MAP` 101 条（键为老分区号 `tid` 空间，值为父分区号）。
- **错误近似**：`run_crawl.py:83`、`run_crawl_incremental.py:73`、`run_organize.py:245`、`crawl_watchlater.py:57` 均写 `"parent_tid": str(detail.get("tid_v2", ""))`。
- **实测值层面影响**：当前 `videos.csv` 中 `parent_tid == tid_v2` 为 **5564/5564**；用 `PARENT_MAP` 重算，**5563 条值不同**。
- **实测端到端影响（关键）**：`_classify_by_tid`（`classifier.py:167-183`）的查找顺序是 `tid → tid_v2 → parent_tid`，而 `TID_BUCKET_MAP` 的键全部是**老分区号**（`211/181/245/223/...`）。由于第一步 `tid` 通常就已命中或落空，`parent_tid` 的错误被掩盖。

  实际复算结果：修正 `parent_tid` 后，**仅 30 条视频的分类结果发生变化**（29 条 → `tmp_影视解说`，1 条 → `tmp_生活娱乐`）。

  | 变化 | 条数 | 样例 |
  |---|---|---|
  | `None` → `tmp_影视解说` | 29 | tid=168/85，tid_v2=2187/2186/2154/2002/2006，正确 parent=167/181 |
  | `None` → `tmp_生活娱乐` | 1 | — |

**结论**：旧报告称"导致强分区直投结果剧烈漂移"**不成立**。真实影响是 0.5%（30/5563）。

**但它仍是必须修的债**，理由有三：
1. **脆弱性**：一旦有人调整 `_classify_by_tid` 的查找顺序，或往 `TID_BUCKET_MAP` 里加 `tid_v2` 空间（4 位数）的键，影响面会立刻放大。当前"没事"是巧合，不是设计。
2. **`parent_name` 恒为空**：`run_crawl.py:84` 直接写 `"parent_name": ""`，分区名这一维度完全缺失，调试与人工核对时无从下手。
3. **跨入口不一致**：同一份数据经不同入口（`run_crawl` vs `crawl_tags`）会得到不同的 `parent_tid`，结果不可复现。

### 4.7 其他问题（按严重度）

#### 🔴 4.7.1 [安全] `config.py` 硬编码真实登录凭证

`config.py:13-16` 把真实可用的 `SESSDATA`、`bili_jct`、UID 设为默认值（`os.getenv(..., "真实值")`）。即使不设环境变量，程序也直接可用。

- 风险：工作区文件本体含明文凭证，一旦被拷贝/上传/截屏即泄露；SESSDATA 泄露可冒用账号。
- 建议：① 去掉默认值，纯环境变量，缺失即报错退出；② 若怀疑曾外泄，到 B站"退出其他设备"轮换登录态；③ 工作区 `config.py` 替换为占位模板。

#### 🟠 4.7.2 [数据保真] `to_dict()` 丢失 7 列

`models.BiliVideo.to_dict()`（`models.py:76-105`）不含 `tags / tid / tname / tid_v2 / tname_v2 / parent_tid / parent_name`。

实测列数对比：

| 文件 | 列数 | 是否含上述 7 列 |
|---|---|---|
| `videos.csv` | 25 | ✅ 含 |
| `classified_sorted_videos.csv` | 26 | ❌ **不含**（改为 `category/series_name/is_timely/block_type/ogv_*/is_ogv`） |

- 后果：① 中间产物丢失标签/分区信息；② 任何人基于 `classified_sorted_videos.csv` 重新分类，Layer 2 的 tag/tid 信号全空，质量骤降；③ 产物不可复现、不可审计。
- 缓解：`run_organize_from_classified.py` 直接信任已存的 `category`，**整理动作本身不受影响**。
- 建议：`to_dict()` 补齐这 7 列（或导出时合并 `enrich_video_from_csv` 注入的私有属性）。

#### 🟠 4.7.3 [架构] 三套并行入口 + 已废弃的分类路径

**路径 A（`main.py` 菜单）——已废弃，实测证据**：

| 事实 | 证据 |
|---|---|
| 依赖 `classification_rules_template.json` | `main.py:239`；该文件 **不存在** |
| 产出 `classified_videos.csv` | `main.py:295`；该文件 **不存在** |
| `cmd_organize` 读 `classified_videos.csv` | `main.py:309` |
| 模板类别与 13 桶完全不符 | `main.py:126-132` 生成 `音乐/游戏/美食/旅行/学习/运动健身/科技数码` |

即便用户按模板填完，生成的类别也**匹配不上**任何 `tmp_*` 桶。

**路径 B（`run_classify.py` → `run_organize_from_classified.py`）——实际活跃路径**。

**路径 C（`run_organize.py` 非增量分支）**——从 `videos.csv` 现分类现整理。

**额外的自相矛盾**：`run_classify.py:189` 在成功产出 `classified_sorted_videos.csv` 后，提示用户"运行 `main.py` 选项 5 执行实际整理操作"——而 `main.py` 选项 5（`cmd_auto_organize`）走的是废弃的 Path A。活跃管线的最后一步把用户推回了死路。

#### 🟡 4.7.4 [维护] `OGV_TYPE_BUCKET_MAP` 游离 + 0 命中

定义在 `bucket_config.py:135-141`，但 `config.__post_init__` 的 import 列表未包含它，仅 `classifier._classify_by_ogv`（`classifier.py:164`）局部 `import` 使用。功能可用，但：

- 与同文件其他 10 个常量的加载方式不一致；
- 实测 **0 命中**（当前数据 `is_ogv` 全 False），该分支从未被验证。

建议：纳入 `__post_init__` 统一加载；并确认 B站 OGV 内容是否真的会走这个收藏夹流程，否则可考虑移除。

#### 🟡 4.7.5 [维护] 死代码 `WEAK_TID_BUCKET_MAP`

`bucket_config.py:128` 为空字典，仍被 `config.__post_init__` 加载、被 `Layer2StructuralClassifier.__init__` 接收后强制置空（`classifier.py:146`）。无害但属认知负担，建议删除传递链。

#### 🟡 4.7.6 [维护] 大量遗留/重复脚本

| 脚本 | 状态 |
|---|---|
| `supplement_crawl.py` / `supplement_v2/v3/v4.py` | 补缺失视频的四代实验，已被 `run_crawl_incremental.py` 取代 |
| `crawl_toview.py` | 已被 `crawl_watchlater.py` 取代 |
| `crawl_watchlater.py` | 与 `run_organize.py` / `run_crawl_incremental.py` 内联逻辑重复 |
| `enrich_tags.py` | 与 `crawl_tags.py` 重叠，且不动 `parent_tid` |
| `reorder_folders.py` | 与核心 `reorder_state` / `organizer.reorder_folder_full` 重复 |
| `analyze_state.py` / `analyze_pending.py` / `check_sort.py` / `debug_api.py` | 诊断脚本，仅认旧 `move_state.json` |
| `test_llm_classifier.py` / `test_resume.py` / `test_watchlater_cleanup.py` | 测试，可保留 |

以上脚本实测**均 0 处被其他模块引用**。

#### 🟡 4.7.7 [设计] "稍后再看"逻辑 7 处重复

`crawler.py`、`organizer.py`、`run_organize.py`、`run_crawl_incremental.py`、`crawl_toview.py`、`crawl_watchlater.py`、`test_watchlater_cleanup.py` 各自实现一遍；`source_folder_id` 有 `-1` / `0` 两种写法（当前产物统一为 `0`），`parent_tid` 取法也不一致。建议收敛到 `crawler` 单一方法。

#### 🟢 4.7.8 [设计] LLM 置信度阈值偏激进（可商榷）

`medium`/`low` 置信度一律归入 `tmp_待分类`（`classifier.py:427-428`），相当于"只在高置信时采纳 LLM"。对"分类正确性优先"的目标合理，但实测 `tmp_待分类` 有 **598 条（10.8%）**，人工兜底负担不轻。可考虑中置信度回退到 Layer 2 弱信号或二次 prompt，而非直接丢弃。

---

## 五、文档与代码对齐（差异清单）

| # | README 现状描述 | 代码实际 | 判定 |
|---|---|---|---|
| 1 | "运行 `python main.py`，菜单步骤 2/3" 为主推用法 | `main.py` 路径已废弃（`classification_rules_template.json` 与 `classified_videos.csv` 均不存在） | ❌ **描述的是死路** |
| 2 | 步骤 2 需编辑 `output/classification_rules_template.json` | 该文件不存在；`generate_rules_template` 生成的类别（音乐/游戏/美食/旅行/学习/运动健身/科技数码）与 13 个 `tmp_*` 桶完全不符 | ❌ 模板已过时 |
| 3 | 项目结构仅列约 11 个文件 | 实际 30+ 个 `.py`（9 个 `run_*` + 十余个周边脚本 + 测试） | ⚠️ 严重不全 |
| 4 | "13 个固定目标桶" | `bucket_config.BUCKETS` 确为 13 个，含 `tmp_待分类` 兜底 | ✅ 一致 |
| 5 | "Layer 3：15-20 条一批，中/低置信度归入待分类" | `LLM_BATCH_SIZE=20`，`medium/low → 待分类` | ✅ 一致 |
| 6 | 桶内三区块 Top / Main / Bottom | `sorter.py` 实现一致，但 Bottom 实测被 `fav_time=0` 污染（§4.4） | ⚠️ 描述正确，实现有 bug |
| 7 | "Bottom：收藏超 6 个月归档" | 实测 Bottom 275 条全为 `fav_time=0` 的稍后再看视频，无一条真·超期 | ❌ 实际行为与描述不符 |
| 8 | 完全重排：用 `organizer.incremental_organize`（带 `--incremental`）对"某个桶"全量重排 | `incremental_organize` 对所有桶串行处理；单桶全量重排是 `reorder_folder_full`（在 incremental 内按需调用） | ⚠️ 描述模糊/不精确 |
| 9 | `crawl_tags.py` 标注"可选但推荐" | 实测当前 `videos.csv` 未回填 `tname`/`parent_name`；且 `parent_tid` 在 4 处入口被 `tid_v2` 近似 | ⚠️ "可选"定位与其实际重要性不符 |
| 10 | 未提及 OGV 直投 | `OGV_TYPE_BUCKET_MAP` 存在但实测 0 命中 | ⚠️ 功能存在未说明，且未生效 |
| 11 | 未提及 430 条永久失败 / 269 条桶内外差异 | `move_state.json` 实测 `failed_permanent` 430 条 | ❌ 遗漏重要质量信息 |
| 12 | 未提及 `.gitignore` 已忽略 `config.py`/`output/` | 实际已忽略 | ✅ 隐藏良好（但本体仍含明文凭证） |

---

## 六、改进建议（按优先级）

### P0 — 安全与正确性（建议立即处理）

> 以下 1-4 项已于 2026-09-07 用线上真实数据复核，数字为实测值。

1. **找回 41 条无归属视频**（§4.5.1）：不在 44 个收藏夹、也不在稍后再看。视频在 B站上仍存在，写 `recover_orphans.py` 按 bvid 重新 add 回对应桶即可。其中 26 条源自"稍后再看被删除但入桶失败"，属**清理与入桶未原子化**的后果。
2. **修 `identify_archived` 的 `fav_time=0` 误判**（§4.4）：时间未知时应归入 Main，而非 Bottom。这直接决定稍后再看全部视频的落位。
3. **修 `failed_permanent` 误判**（§4.5.1）：判永久失败前先在全部 tmp_* 桶中定位视频（13 次请求）。当前 430 条里有 411 条是误报，噪音会掩盖真实问题。
4. **消除 44 条重复**（§4.5.1）：`auto` 模式 move 失败回退 `add`（`organizer.py:573-586`）后未清理源副本。add 成功后应删除源收藏夹中的残留。
5. **移除 `config.py` 凭证默认值**，改为纯环境变量；评估是否需轮换 SESSDATA。
6. **修复稍后再看的清理时序**：必须"add 成功后才 del 稍后再看"，且状态持久化，避免中断导致视频两边都没有。

### P1 — 数据保真与架构收敛

1. **加 `--reconcile` 错位纠正模式**：实测 **234 条（4.3%）**视频所在桶与当前分类期望不符。当前增量逻辑认为"已在某个 tmp 桶 = 已处理"，因此历史错位会**永久累积**，分类规则每改一次就多一批。改法：整理前拉取 13 桶实际内容，与最新分类结果比对，只 move 错位的（当前 234 条，成本可控）。
2. **源数据去重**：`videos.csv` 5564 行 / **5504 唯一 avid**，60 条重复（同一视频被多个 tmp 收藏夹收录）。导致重复操作、容量计算偏差、`classified_sorted_videos.csv` 出现同一视频两行。改法：爬取后按 avid 去重，或在整理层按 avid 聚合。
3. **统一 `parent_tid` 算法**：删除 4 处 `tid_v2` 近似，统一用 `crawl_tags.py` 的 `PARENT_MAP`；并把"分类前必跑 crawl_tags"变成强制步骤（或在 `run_crawl` 内直接按 PARENT_MAP 计算）。
4. **补齐 `models.BiliVideo.to_dict()` 的 7 列**，修复中间产物丢列。
5. **收敛入口 + 修正文档主线**：README 改为以 `run_crawl → crawl_tags → run_classify → run_organize_from_classified` 为唯一主线；`main.py` 菜单要么复用真实产物与 13 桶名，要么明确标注废弃；**务必修掉 `run_classify.py:189` 把用户导向 `main.py` 选项 5 的提示**。
6. **补跑 `crawl_tags.py`** 回填 `tname`/`parent_name`，让 CSV 可读、可核对。
7. **整理前校验数据新鲜度**：当前 `videos.csv` 是 2026-07-22 的快照，已陈旧 **47 天**；稍后再看已从 275 条涨到 **542 条**（+267 条未整理）。建议跑管线前校验 `videos.csv` mtime 与线上收藏夹计数，差异超阈值则告警/拒绝。

### P2 — 维护与整洁

8. 把 `OGV_TYPE_BUCKET_MAP` 纳入 `config.__post_init__`，并确认该分支是否值得保留（当前 0 命中）。
9. 清理遗留脚本（§4.7.6），收敛"稍后再看"逻辑到 `crawler` 单一方法。
10. 删除 `WEAK_TID_BUCKET_MAP` 死代码传递链。
11. 补全 README 项目结构与已知限制章节；修正"完全重排"描述。

### 可选 — 设计优化

12. 修正代码注释中"绝大多数视频交给 LLM"的表述（实测 Layer 1 才是 51.5% 主力），避免误导调优方向——真正该持续扩充的是 UP主白名单。
13. LLM 中/低置信度回退到弱信号或二次 prompt，而非直接丢进 `tmp_待分类`（当前 598 条 / 10.8%）。

---

## 七、总体结论

- **意图**：清晰、务实。针对"B站收藏夹爆炸 + API 严格风控"这一真实痛点，做了清醒的工程妥协并诚实记录。

- **技术合理性**：核心管线（crawler / 三层漏斗 / sorter / organizer + 双状态机）设计合理、健壮，**风控应对、断点续跑、排序感知移动、防数据丢失**四处是真正的工程亮点，超出一般个人脚本水准。

- **本次实测暴露的核心问题**（按危害排序）：
  1. **430 条视频被永久标记为失败**（8.2%），根因是陈旧源 CSV 导致源侧验证失败 + 状态机误判定性 —— 可能伴随真实数据丢失，**需立即核对**；
  2. **Bottom 区块 100% 是 `fav_time=0` 误判**，归档功能实际未生效；
  3. **凭证硬编码**未处理的安全债；
  4. **入口碎片化 + 文档指向废弃路径**，活跃管线最后一步还把用户推回死路；
  5. `parent_tid` 近似（影响虽仅 30 条，但属脆弱设计）、`to_dict` 丢 7 列、`tname` 全空等数据保真问题。

- **文档对齐**：README 对架构、13 桶、三区块、容量分卷的描述与代码基本一致，但**核心操作流程描述的是已废弃的菜单路径**，项目结构严重不全，且**完全未提及 430 条永久失败这一重要质量信息**。文档需要一次"对齐到 `run_*` 真实管线 + 补充实测已知限制"的修订。

**一句话**：骨架优秀、细节有债。风控与状态机部分值得保留和借鉴；当前最该做的是**先核对那 430 条视频是否真的丢了**，再修 Bottom 误判与凭证安全债，最后收敛入口与文档——这样这个项目才能从"能跑通的个人脚本"过渡到"可放心复跑、可交接"的状态。

---

## 八、修复记录（2026-09-07，自动执行）

按用户授权"按需修复"，本次实际落地以下代码修复（均为离线可验证、不触发 B站 写操作）：

| # | 文件 | 修复内容 | 验证 |
|---|------|---------|------|
| 1 | `sorter.py` | `identify_archived` 增加 `fav_time<=0`（时间未知）守卫：不再把稍后再看等未知时间的视频误判为"归档"。这是 Bottom 区块 100% 误判的根因 | 真实数据复算：Bottom 275→0，Top 510→533，Main 4778→5030 |
| 2 | `sorter.py` | 删除与 `_sort_top_block` 完全重复的 `_sort_bottom_block`（改为直接复用）；`_sort_main_block` 中 `frequent_ups`/`solitary_ups` 由 list 改为 set，消除 `in` 的 O(n²) | py_compile + 功能复算通过 |
| 3 | `config.py` + 新建 `bili_secrets.py` | 移除硬编码凭证默认值（改为仅读环境变量 `BILI_SESSDATA/BILI_BILI_JCT/BILI_BUVID3/BILI_DEDEUSERID`），未设置时回退读取 gitignored 的 `bili_secrets.py`；`.gitignore` 同步忽略 | `BiliConfig()` 仍能正确载入凭证 |
| 4 | `models.py` | `BiliVideo.to_dict()` 补回 `tid/parent_tid/tid_v2/tname/tname_v2/tags` 六列，修复中间产物不可重新分类的问题 | to_dict 输出含 6 列且值正确 |
| 5 | `organizer.py` | 新增 `_load_all_bucket_ids()`，并在"源/目标都无此视频"判定 `failed_permanent` 前，先批量核查 13 个桶——若视频已在其它桶（旧轮次已移走）则标记为 `skipped` 而非永久失败，消除 95%+ 的误报 | 语法/导入通过；逻辑仅新增只读校验，不阻断流程 |
| 6 | `run_classify.py` | 修正末尾提示：把用户从废弃的"main.py 选项5"导向真实活跃管线 `run_organize_from_classified.py` | — |

**未做（需用户确认/谨慎操作，涉及 B站 真实写操作，不擅自执行）：**
- 线上找回 41 条无归属视频、234 条错位纠正、44 条重复清理——这些需要实际 move/add/delete 操作，已在 #5 修好判定逻辑的前提下，交由带 `--dry-run` 的整理流程由你确认后执行。
- `to_dict` 第 7 列 `parent_name` 未补：模型中本就未持久化该字段（由 `parent_tid` 经 `crawl_tags.PARENT_MAP` 派生），补列无意义。
