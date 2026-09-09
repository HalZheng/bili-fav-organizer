"""B站收藏夹整理工具 - 主程序

交互式CLI，串联 爬取→分析→分类→整理 全流程。
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.prompt import Prompt, Confirm
from rich import print as rprint

from config import BiliConfig
from crawler import BiliCrawler
from classifier import RuleClassifier, LLMClassifier, HybridClassifier
from organizer import BiliOrganizer
from models import BiliVideo
from bucket_config import BUCKETS, BUCKET_NAMES, DEFAULT_BUCKET
from classifier import FunnelClassifier, enrich_video_from_csv
from sorter import FolderSorter

console = Console()


def print_banner():
    """打印启动横幅"""
    banner = """
[bold cyan]╔══════════════════════════════════════════╗
║   📺 B站收藏夹整理工具 v1.0              ║
║   自动分类 · 批量整理 · 智能排序          ║
╚══════════════════════════════════════════╝[/bold cyan]
"""
    console.print(banner)


def get_config() -> BiliConfig:
    """获取配置，优先从环境变量，其次交互输入"""
    config = BiliConfig()

    if not config.SESSDATA:
        console.print("\n[yellow]⚠ 需要B站登录凭证 (SESSDATA)[/yellow]")
        console.print("获取方式: 浏览器登录B站 → F12 → Application → Cookies → SESSDATA")
        config.SESSDATA = Prompt.ask("[cyan]请输入 SESSDATA[/cyan]")

    if not config.DedeUserID:
        config.DedeUserID = Prompt.ask("[cyan]请输入你的B站UID (DedeUserID)[/cyan]")

    if not config.BILI_JCT:
        config.BILI_JCT = Prompt.ask(
            "[cyan]请输入 bili_jct (整理操作需要，仅爬取可留空)[/cyan]", default=""
        )

    return config


def show_analysis(analysis: dict):
    """展示数据分析结果"""
    console.print(Panel.fit(
        f"[bold]📊 数据概览[/bold]\n"
        f"  有效视频: {analysis['total']} 条\n"
        f"  失效视频: {analysis['invalid']} 条\n"
        f"  UP主数量: {analysis['unique_ups']} 位",
        border_style="cyan"
    ))

    # UP主排行
    if analysis["up_ranking"]:
        table = Table(title="🔝 UP主视频数量 TOP 30", show_lines=False)
        table.add_column("排名", style="dim", width=4)
        table.add_column("UP主", style="cyan")
        table.add_column("视频数", justify="right", style="green")
        table.add_column("占比", justify="right", style="yellow")

        total = analysis["total"]
        for i, (up, count) in enumerate(analysis["up_ranking"][:30], 1):
            pct = f"{count / total * 100:.1f}%"
            table.add_row(str(i), up, str(count), pct)

        console.print(table)

    # 收藏夹分布
    if analysis["folder_distribution"]:
        table = Table(title="📁 收藏夹分布", show_lines=False)
        table.add_column("收藏夹", style="cyan")
        table.add_column("视频数", justify="right", style="green")

        for folder, count in analysis["folder_distribution"]:
            table.add_row(folder, str(count))

        console.print(table)

    # 标题关键词
    if analysis["title_keywords"]:
        table = Table(title="🏷️ 标题高频关键词 TOP 30", show_lines=False)
        table.add_column("关键词", style="cyan")
        table.add_column("出现次数", justify="right", style="green")

        for word, count in analysis["title_keywords"][:30]:
            table.add_row(word, str(count))

        console.print(table)


def generate_rules_template(videos: list[BiliVideo], output_dir: str):
    """根据爬取数据生成分类规则模板"""
    from collections import Counter

    valid_videos = [v for v in videos if v.is_valid]
    up_counter = Counter(v.upper.name for v in valid_videos)

    # 生成UP主映射模板（标注高频UP主）
    up_map_template = {}
    for up, count in up_counter.most_common(50):
        up_map_template[up] = "待分类"  # 用户需要手动填入分类

    template = {
        "up_category_map": up_map_template,
        "keyword_rules": [
            {"pattern": "Python|编程|开发|代码|前端|后端|算法", "category": "编程开发"},
            {"pattern": "音乐|翻唱|吉他|钢琴|唱歌|MV", "category": "音乐"},
            {"pattern": "游戏|通关|攻略|实况|直播录像", "category": "游戏"},
            {"pattern": "美食|做菜|烹饪|食谱|吃播", "category": "美食"},
            {"pattern": "旅行|旅游|景点|打卡|vlog", "category": "旅行"},
            {"pattern": "学习|课程|教程|考试|知识", "category": "学习"},
            {"pattern": "健身|运动|跑步|瑜伽|减肥", "category": "运动健身"},
            {"pattern": "科技|数码|手机|电脑|测评", "category": "科技数码"},
        ],
        "default_category": "未分类",
    }

    path = Path(output_dir) / "classification_rules_template.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(template, f, ensure_ascii=False, indent=2)

    console.print(f"\n[green]✓ 已生成分类规则模板: {path}[/green]")
    console.print("[yellow]  请编辑此文件，将UP主映射和关键词规则修改为你的分类方案[/yellow]")
    return str(path)


def show_resume_summary(summary: dict):
    """展示恢复摘要表格"""
    console.print(Panel.fit(
        f"[bold yellow]⚠ 检测到未完成的移动任务[/bold yellow]\n"
        f"  会话 ID: {summary.get('session_id', 'unknown')}\n"
        f"  创建时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(summary.get('created_at', 0)))}\n"
        f"  操作模式: {summary.get('mode', 'auto')}\n"
        f"  总分类: {summary.get('total_categories', 0)} | "
        f"已完成: {summary.get('completed_categories', 0)} | "
        f"未完成: {summary.get('incomplete_categories', 0)}\n"
        f"  总视频: {summary.get('total_videos', 0)} | "
        f"已完成: {summary.get('completed_videos', 0)} | "
        f"待处理: {summary.get('pending_videos', 0)} | "
        f"失败: {summary.get('failed_videos', 0)}",
        border_style="yellow"
    ))

    if summary.get("source_csv_changed"):
        console.print("[yellow]⚠ 源数据文件已变化，恢复可能导致分类结果不一致[/yellow]")

    # 各分类进度表格
    categories = summary.get("categories", [])
    if categories:
        table = Table(title="未完成分类进度", show_lines=False)
        table.add_column("分类", style="cyan")
        table.add_column("状态", style="yellow")
        table.add_column("总数", justify="right", style="white")
        table.add_column("已完成", justify="right", style="green")
        table.add_column("待处理", justify="right", style="blue")
        table.add_column("失败", justify="right", style="red")

        for cat in categories:
            table.add_row(
                cat.get("name", ""),
                cat.get("status", ""),
                str(cat.get("total", 0)),
                str(cat.get("completed", 0)),
                str(cat.get("pending", 0)),
                str(cat.get("failed", 0)),
            )
        console.print(table)

    console.print("\n[bold]选择操作:[/bold]")
    console.print("  [1] 恢复未完成的任务（继续处理 pending/failed 视频）")
    console.print("  [2] 重新开始（归档旧状态，创建新任务）")
    console.print("  [3] 退出（不做任何操作）")


async def _rebuild_folder_map(organizer: BiliOrganizer, classified_videos: dict) -> dict[str, int]:
    """从已创建的收藏夹列表重建 folder_map"""
    folders = await organizer.crawler.get_created_folders()
    folder_map = {}
    for cat in classified_videos.keys():
        # 精确匹配分类名
        for f in folders:
            if f.title == cat:
                folder_map[cat] = f.id
                break
        # 如果精确匹配失败，尝试前缀匹配（处理分卷桶情况）
        if cat not in folder_map:
            for f in folders:
                if f.title == cat or f.title.startswith(f"{cat}_"):
                    folder_map[cat] = f.id
                    break
    return folder_map


async def cmd_crawl(config: BiliConfig):
    """步骤1: 爬取所有TMP收藏夹数据"""
    crawler = BiliCrawler(config)
    try:
        videos = await crawler.crawl_all_tmp_folders()

        if videos:
            # 数据分析
            analysis = BiliCrawler.analyze_videos(videos)
            show_analysis(analysis)

            # 生成分类规则模板
            generate_rules_template(videos, config.OUTPUT_DIR)

            console.print("\n[bold green]✓ 爬取完成！接下来请:[/bold green]")
            console.print("  1. 查看 output/videos.xlsx 分析数据")
            console.print("  2. 编辑 output/classification_rules_template.json 设定分类规则")
            console.print("  3. 运行步骤2进行分类")
    finally:
        await crawler.close()


async def cmd_classify(config: BiliConfig):
    """步骤2: 根据规则分类视频"""
    # 检查数据文件
    csv_path = Path(config.OUTPUT_DIR) / "videos.csv"
    rules_path = Path(config.OUTPUT_DIR) / "classification_rules_template.json"

    if not csv_path.exists():
        console.print("[red]✗ 未找到视频数据，请先执行步骤1爬取数据[/red]")
        return

    if not rules_path.exists():
        console.print("[red]✗ 未找到分类规则文件，请先执行步骤1生成规则模板[/red]")
        return

    # 读取视频数据
    import pandas as pd
    df = pd.read_csv(csv_path, dtype={"up_mid": "Int64"})
    console.print(f"[cyan]加载 {len(df)} 条视频数据[/cyan]")

    # 重建BiliVideo对象
    videos = []
    for _, row in df.iterrows():
        v = BiliVideo(
            bvid=row.get("bvid", ""),
            id=row.get("avid", 0),
            title=row.get("title", ""),
            intro=row.get("intro", ""),
            attr=row.get("attr", 0),
            pubtime=int(row.get("pubtime", 0)),
            fav_time=int(row.get("fav_time", 0)),
            upper=BiliUP(
                mid=int(row.get("up_mid", 0)),
                name=str(row.get("up_name", "")),
            ),
            source_folder_id=int(row.get("source_folder_id", 0)),
            source_folder_title=str(row.get("source_folder_title", "")),
        )
        videos.append(v)

    # 使用规则分类器
    classifier = RuleClassifier(config)
    classifier.load_rules(str(rules_path))
    result = classifier.classify_all(videos)

    # 展示分类结果
    table = Table(title="📋 分类结果预览", show_lines=False)
    table.add_column("分类", style="cyan")
    table.add_column("视频数", justify="right", style="green")
    table.add_column("UP主数", justify="right", style="yellow")

    for category, cat_videos in sorted(result.items(), key=lambda x: -len(x[1])):
        up_count = len(set(v.upper.name for v in cat_videos))
        table.add_row(category, str(len(cat_videos)), str(up_count))

    console.print(table)

    # 导出分类结果
    for v in videos:
        pass  # category已经设置

    output_path = Path(config.OUTPUT_DIR) / "classified_videos.csv"
    records = [v.to_dict() for v in videos]
    pd.DataFrame(records).to_csv(output_path, index=False, encoding="utf-8-sig")
    console.print(f"\n[green]✓ 分类结果已导出到: {output_path}[/green]")

    # 询问是否继续整理
    if Confirm.ask("\n是否继续执行步骤3（整理视频到收藏夹）？"):
        await cmd_organize(config, result)


async def cmd_organize(config: BiliConfig, classified_videos: dict = None):
    """步骤3: 按分类整理视频到收藏夹"""
    if not classified_videos:
        # 从文件加载
        csv_path = Path(config.OUTPUT_DIR) / "classified_videos.csv"
        if not csv_path.exists():
            console.print("[red]✗ 未找到分类数据，请先执行步骤2[/red]")
            return

        import pandas as pd
        df = pd.read_csv(csv_path)
        videos = []
        for _, row in df.iterrows():
            v = BiliVideo(
                bvid=row.get("bvid", ""),
                id=row.get("avid", 0),
                title=row.get("title", ""),
                attr=row.get("attr", 0),
                category=str(row.get("category", "未分类")),
                pubtime=int(row.get("pubtime", 0)),
                upper=BiliUP(
                    mid=int(row.get("up_mid", 0)),
                    name=str(row.get("up_name", "")),
                ),
                source_folder_id=int(row.get("source_folder_id", 0)),
            )
            videos.append(v)

        classified_videos = {}
        for v in videos:
            if v.category not in classified_videos:
                classified_videos[v.category] = []
            classified_videos[v.category].append(v)

    if not config.BILI_JCT:
        console.print("[red]✗ 整理操作需要 bili_jct (CSRF Token)，请在配置中填入[/red]")
        return

    organizer = BiliOrganizer(config)
    try:
        # 预览
        console.print("\n[bold cyan]📋 整理预览[/bold cyan]")
        plan = await organizer.full_organize(
            classified_videos,
            folder_prefix="",
            mode="auto",
            dry_run=True,
        )

        # 展示计划
        for cat, info in plan["categories"].items():
            console.print(f"  [cyan]{cat}[/cyan]: {info['video_count']} 条视频 → 收藏夹 id={info['target_folder']}")

        # 确认执行
        console.print("\n[dim]操作模式: 自动(有源收藏夹则move，无则add)[/dim]")
        confirm = Confirm.ask(
            "确认执行整理操作？",
            default=False,
        )

        if confirm:
            result = await organizer.full_organize(
                classified_videos,
                folder_prefix="",
                mode="auto",
                dry_run=False,
            )

            # 检查是否检测到未完成任务
            if result.get("interrupted"):
                summary = result.get("resume_summary", {})
                show_resume_summary(summary)
                choice = Prompt.ask(
                    "选择操作",
                    choices=["1", "2", "3"],
                    default="1",
                )
                if choice == "1":  # 恢复
                    try:
                        # 需要重新构建 folder_map（从已创建的收藏夹）
                        folder_map = await _rebuild_folder_map(organizer, classified_videos)
                        resume_result = await organizer.resume_organize(classified_videos, folder_map)
                        console.print(f"\n[bold green]✓ 恢复完成！[/bold green]")
                        console.print(f"  恢复分类数: {len(resume_result.get('resumed_categories', []))}")
                        console.print(f"  处理视频数: {resume_result.get('total_processed', 0)}")
                        if resume_result.get("reorder_candidates"):
                            console.print(f"  [yellow]⚠ 以下分类顺序可能需要检查: {resume_result['reorder_candidates']}[/yellow]")
                    except Exception as e:
                        console.print(f"[red]✗ 恢复失败: {e}[/red]")
                elif choice == "2":  # 重新开始
                    try:
                        # 归档旧状态，重新执行
                        await organizer.state_manager.archive_state()
                    except Exception as e:
                        console.print(f"[yellow]⚠ 归档旧状态失败: {e}，继续重新执行[/yellow]")
                    result = await organizer.full_organize(
                        classified_videos,
                        folder_prefix="",
                        mode="auto",
                        dry_run=False,
                    )
                    if result.get("interrupted"):
                        console.print("[yellow]⚠ 重新执行时仍检测到中断，请检查状态文件[/yellow]")
                    else:
                        console.print("\n[bold green]✓ 整理完成！[/bold green]")
                else:  # 3 = 退出
                    console.print("[yellow]已取消[/yellow]")
            else:
                console.print("\n[bold green]✓ 整理完成！[/bold green]")
        else:
            console.print("[yellow]已取消[/yellow]")
    finally:
        await organizer.close()


async def cmd_auto_organize(config: BiliConfig):
    """一键自动化整理: 爬取→三层分流→桶内排序→移动执行"""

    # 显示并发配置
    console.print(
        f"[dim][并发配置] 请求并发={config.MAX_CONCURRENT_REQUESTS}, "
        f"RPS={config.MAX_REQUESTS_PER_SECOND}, "
        f"收藏夹并发={config.CONCURRENT_FOLDERS}, "
        f"排序线程={config.CONCURRENT_SORT_WORKERS}[/dim]"
    )

    # ── Step 1: 爬取数据 ──
    console.print("\n[bold cyan]━━━ Step 1: 爬取收藏夹数据 ━━━[/bold cyan]")
    crawler = BiliCrawler(config)
    try:
        videos = await crawler.crawl_all_tmp_folders()
    finally:
        await crawler.close()

    if not videos:
        console.print("[red]✗ 未爬取到任何视频数据[/red]")
        return

    # ── Step 1.5: 补充分区/标签信息 ──
    console.print("\n[bold cyan]━━━ Step 1.5: 从CSV补充分区/标签信息 ━━━[/bold cyan]")
    csv_path = Path(config.OUTPUT_DIR) / "videos.csv"
    if csv_path.exists():
        import pandas as pd
        df = pd.read_csv(csv_path, dtype={"up_mid": "Int64"})
        # 构建 bvid → row 映射
        bvid_row_map = {}
        for _, row in df.iterrows():
            bvid_row_map[row.get("bvid", "")] = row

        enriched = 0
        for v in videos:
            row = bvid_row_map.get(v.bvid)
            if row is not None:
                if v.is_ogv:
                    # OGV内容从CSV补充ogv字段，跳过tags/tid补充
                    v.ogv_type_name = str(row.get('ogv_type_name', '') or v.ogv_type_name)
                    v.ogv_type_id = int(row.get('ogv_type_id', 0) or v.ogv_type_id)
                    v.season_id = int(row.get('season_id', 0) or v.season_id)
                    enriched += 1
                else:
                    enrich_video_from_csv(v, row.to_dict())
                    enriched += 1
        console.print(f"  [green]✓ 已补充 {enriched}/{len(videos)} 条视频的分区/标签信息[/green]")
    else:
        console.print("  [yellow]⚠ 未找到CSV文件，跳过分区/标签补充[/yellow]")

    # ── Step 2: 三层漏斗分流 ──
    console.print("\n[bold cyan]━━━ Step 2: 三层漏斗分流 ━━━[/bold cyan]")
    funnel = FunnelClassifier(config)
    classified = funnel.classify_all(videos)

    # 展示分流结果
    table = Table(title="📋 分流结果统计", show_lines=False)
    table.add_column("桶名称", style="cyan")
    table.add_column("视频数", justify="right", style="green")
    table.add_column("UP主数", justify="right", style="yellow")

    for bucket_name in BUCKET_NAMES:
        if bucket_name in classified:
            vids = classified[bucket_name]
            up_count = len(set(v.upper.name for v in vids))
            table.add_row(bucket_name, str(len(vids)), str(up_count))

    console.print(table)

    # ── Step 3: 桶内精细化排序 ──
    console.print("\n[bold cyan]━━━ Step 3: 桶内精细化排序 ━━━[/bold cyan]")
    sorter = FolderSorter(archive_months=config.ARCHIVE_MONTHS)

    for bucket_name, bucket_videos in classified.items():
        sorted_videos = sorter.sort_folder(bucket_videos)
        classified[bucket_name] = sorted_videos
        summary = sorter.get_sort_summary(sorted_videos)
        console.print(
            f"  {bucket_name}: Top={summary['top']} | Main={summary['main']} | "
            f"Bottom={summary['bottom']} | 高频UP主={len(summary['frequent_ups'])}"
        )

    # ── Step 4: dry-run 预览 ──
    console.print("\n[bold cyan]━━━ Step 4: 整理预览 (dry-run) ━━━[/bold cyan]")
    if not config.BILI_JCT:
        console.print("[yellow]⚠ 未配置 bili_jct，仅展示预览[/yellow]")

    organizer = BiliOrganizer(config)
    try:
        plan = await organizer.full_organize(
            classified, folder_prefix="", mode="auto", dry_run=True
        )

        # 展示计划
        for cat, info in plan["categories"].items():
            console.print(
                f"  [cyan]{cat}[/cyan]: {info['video_count']} 条视频 → 收藏夹 id={info['target_folder']}"
            )
    finally:
        await organizer.close()

    # ── Step 5: 确认执行 ──
    if not config.BILI_JCT:
        console.print("\n[yellow]⚠ 整理操作需要 bili_jct，请在配置中填入后重试[/yellow]")
        return

    console.print("\n[dim]操作模式: 自动(有源收藏夹则move，无则add)[/dim]")
    confirm = Confirm.ask(
        "确认执行整理操作？",
        default=False,
    )

    if confirm:
        console.print("\n[bold cyan]━━━ Step 5: 执行整理 ━━━[/bold cyan]")
        organizer = BiliOrganizer(config)
        try:
            result = await organizer.full_organize(
                classified, folder_prefix="", mode="auto", dry_run=False
            )

            # 检查是否检测到未完成任务
            if result.get("interrupted"):
                summary = result.get("resume_summary", {})
                show_resume_summary(summary)
                choice = Prompt.ask(
                    "选择操作",
                    choices=["1", "2", "3"],
                    default="1",
                )
                if choice == "1":  # 恢复
                    try:
                        # 需要重新构建 folder_map（从已创建的收藏夹）
                        folder_map = await _rebuild_folder_map(organizer, classified)
                        resume_result = await organizer.resume_organize(classified, folder_map)
                        console.print(f"\n[bold green]✓ 恢复完成！[/bold green]")
                        console.print(f"  恢复分类数: {len(resume_result.get('resumed_categories', []))}")
                        console.print(f"  处理视频数: {resume_result.get('total_processed', 0)}")
                        if resume_result.get("reorder_candidates"):
                            console.print(f"  [yellow]⚠ 以下分类顺序可能需要检查: {resume_result['reorder_candidates']}[/yellow]")
                    except Exception as e:
                        console.print(f"[red]✗ 恢复失败: {e}[/red]")
                elif choice == "2":  # 重新开始
                    try:
                        # 归档旧状态，重新执行
                        await organizer.state_manager.archive_state()
                    except Exception as e:
                        console.print(f"[yellow]⚠ 归档旧状态失败: {e}，继续重新执行[/yellow]")
                    result = await organizer.full_organize(
                        classified, folder_prefix="", mode="auto", dry_run=False
                    )
                    if result.get("interrupted"):
                        console.print("[yellow]⚠ 重新执行时仍检测到中断，请检查状态文件[/yellow]")
                    else:
                        console.print("\n[bold green]✓ 整理完成！[/bold green]")
                else:  # 3 = 退出
                    console.print("[yellow]已取消[/yellow]")
            else:
                console.print("\n[bold green]✓ 整理完成！[/bold green]")
        finally:
            await organizer.close()
    else:
        console.print("[yellow]已取消执行[/yellow]")


def cmd_config_menu(config: BiliConfig):
    """配置管理菜单"""
    while True:
        console.print("\n[bold]━━━ 配置管理 ━━━[/bold]")
        console.print("  [1] 修改登录凭证 (SESSDATA / UID / bili_jct)")
        console.print("  [2] 查看并修改并发配置")
        console.print("  [0] 返回主菜单")

        sub = Prompt.ask("请选择", choices=["0", "1", "2"], default="0")

        if sub == "1":
            # 原有的凭证配置流程
            config.SESSDATA = Prompt.ask("[cyan]SESSDATA[/cyan]", default=config.SESSDATA)
            config.DedeUserID = Prompt.ask("[cyan]DedeUserID[/cyan]", default=config.DedeUserID)
            config.BILI_JCT = Prompt.ask("[cyan]bili_jct[/cyan]", default=config.BILI_JCT)
            console.print("[green]✓ 凭证配置已更新[/green]")

        elif sub == "2":
            # 显示当前并发配置
            table = Table(title="⚡ 并发配置", show_lines=False)
            table.add_column("配置项", style="cyan")
            table.add_column("当前值", justify="right", style="green")
            table.add_column("说明", style="dim")

            table.add_row("MAX_CONCURRENT_REQUESTS", str(config.MAX_CONCURRENT_REQUESTS), "最大并发请求数")
            table.add_row("MAX_REQUESTS_PER_SECOND", str(config.MAX_REQUESTS_PER_SECOND), "每秒最大请求数 (RPS)")
            table.add_row("CONCURRENT_FOLDERS", str(config.CONCURRENT_FOLDERS), "收藏夹级最大并发数")
            table.add_row("CONCURRENT_SORT_WORKERS", str(config.CONCURRENT_SORT_WORKERS), "排序线程池大小")
            table.add_row("RATE_LIMIT_COOLDOWN", str(config.RATE_LIMIT_COOLDOWN), "412 退避等待秒数")
            table.add_row("CAPACITY_CACHE_TTL", str(config.CAPACITY_CACHE_TTL), "容量检查缓存秒数")

            console.print(table)

            if Confirm.ask("\n是否修改并发配置？", default=False):
                config.MAX_CONCURRENT_REQUESTS = int(Prompt.ask(
                    "MAX_CONCURRENT_REQUESTS", default=str(config.MAX_CONCURRENT_REQUESTS)
                ))
                config.MAX_REQUESTS_PER_SECOND = int(Prompt.ask(
                    "MAX_REQUESTS_PER_SECOND", default=str(config.MAX_REQUESTS_PER_SECOND)
                ))
                config.CONCURRENT_FOLDERS = int(Prompt.ask(
                    "CONCURRENT_FOLDERS", default=str(config.CONCURRENT_FOLDERS)
                ))
                config.CONCURRENT_SORT_WORKERS = int(Prompt.ask(
                    "CONCURRENT_SORT_WORKERS", default=str(config.CONCURRENT_SORT_WORKERS)
                ))
                config.RATE_LIMIT_COOLDOWN = float(Prompt.ask(
                    "RATE_LIMIT_COOLDOWN", default=str(config.RATE_LIMIT_COOLDOWN)
                ))
                config.CAPACITY_CACHE_TTL = int(Prompt.ask(
                    "CAPACITY_CACHE_TTL", default=str(config.CAPACITY_CACHE_TTL)
                ))
                console.print("[green]✓ 并发配置已更新[/green]")

        elif sub == "0":
            break


def main():
    print_banner()

    config = get_config()

    while True:
        console.print("\n[bold]━━━ 操作菜单 ━━━[/bold]")
        console.print("  [1] 爬取TMP收藏夹数据（步骤1）")
        console.print("  [2] 分类视频（步骤2，需先完成步骤1和编辑规则）")
        console.print("  [3] 整理视频到收藏夹（步骤3）")
        console.print("  [4] 修改配置")
        console.print("  [5] 一键自动化整理（爬取→分流→排序→移动）")
        console.print("  [0] 退出")

        choice = Prompt.ask("\n请选择", choices=["0", "1", "2", "3", "4", "5"], default="5")

        if choice == "1":
            asyncio.run(cmd_crawl(config))
        elif choice == "2":
            asyncio.run(cmd_classify(config))
        elif choice == "3":
            asyncio.run(cmd_organize(config))
        elif choice == "4":
            cmd_config_menu(config)
        elif choice == "5":
            asyncio.run(cmd_auto_organize(config))
        elif choice == "0":
            console.print("[cyan]再见！[/cyan]")
            break


if __name__ == "__main__":
    main()
