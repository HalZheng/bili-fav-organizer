"""验证整理结果"""
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def main():
    # 检查 move_state.json
    print("=== move_state.json 状态 ===")
    with open('output/move_state.json', encoding='utf-8') as f:
        data = json.load(f)

    session_id = data.get('session_id', 'unknown')
    mode = data.get('mode', 'unknown')
    cats = data.get('categories', {})

    print(f"session: {session_id}")
    print(f"mode: {mode}")
    print(f"categories: {len(cats)}")

    total_completed = 0
    total_videos = 0
    total_failed = 0

    print("\n各桶状态:")
    print(f"  {'桶名':<20} {'状态':<12} {'目标ID':<12} {'完成/总数':<12}")
    print(f"  {'-'*20} {'-'*12} {'-'*12} {'-'*12}")

    for name, info in sorted(cats.items()):
        status = info.get('status', 'unknown')
        target = info.get('target_folder_id', 0)
        blocks = info.get('blocks', {})

        cat_total = 0
        cat_completed = 0
        cat_failed = 0

        for block_name, block_info in blocks.items():
            videos = block_info.get('videos', [])
            cat_total += len(videos)
            for v in videos:
                v_status = v.get('status', '')
                if v_status in ('moved', 'added', 'skipped'):
                    cat_completed += 1
                elif v_status in ('failed_permanent', 'failed'):
                    cat_failed += 1

        total_completed += cat_completed
        total_videos += cat_total
        total_failed += cat_failed

        print(f"  {name:<20} {status:<12} {target:<12} {cat_completed}/{cat_total}")

    print(f"\n  合计: {total_completed}/{total_videos} (失败: {total_failed})")

    # 检查 bucket_stats.json
    print("\n=== bucket_stats.json ===")
    with open('output/bucket_stats.json', encoding='utf-8') as f:
        stats = json.load(f)

    print(f"{'桶名':<20} {'总数':>6} {'Top':>6} {'Main':>6} {'Bottom':>6}")
    print(f"{'-'*20} {'-'*6} {'-'*6} {'-'*6} {'-'*6}")
    for name, info in sorted(stats.items()):
        total = info.get('total', 0)
        top = info.get('top_block', 0)
        main = info.get('main_block', 0)
        bottom = info.get('bottom_block', 0)
        print(f"{name:<20} {total:>6} {top:>6} {main:>6} {bottom:>6}")


if __name__ == "__main__":
    main()
