"""CLI 入口：python -m log_compressor [options]。

用法:
    python -m log_compressor                          # 批量生成 digest
    python -m log_compressor --input <file.log>       # 单文件
    python -m log_compressor --mode compact           # 精简压缩
    python -m log_compressor --mode archive -o out.tar.gz
    python -m log_compressor --stats                  # 统计
    python -m log_compressor --restore --report out.md # 还原分析
"""

import argparse
import json
import os
import sys
import time

from .compressor import LogCompressor, DIGEST_DIR, COMPACT_DIR
from .restorer import DigestRestorer


def print_stats(log_dir="logs", digest_dir=DIGEST_DIR, compact_dir=COMPACT_DIR):
    """打印压缩效果统计。"""
    print("=" * 60)
    print("  LOG COMPRESSION STATS")
    print("=" * 60)

    files = sorted(os.listdir(log_dir)) if os.path.isdir(log_dir) else []
    log_files = [f for f in files if f.endswith(".log")]
    raw_total = sum(os.path.getsize(os.path.join(log_dir, f)) for f in log_files)
    print(f"\n  Raw .log:     {len(log_files)} files, {raw_total / 1024 / 1024:.1f} MB")

    if os.path.isdir(digest_dir):
        d_files = [f for f in os.listdir(digest_dir) if f.endswith(".digest.json")]
        d_total = sum(os.path.getsize(os.path.join(digest_dir, f)) for f in d_files)
        ratio = raw_total / max(1, d_total)
        print(f"  Digest:       {len(d_files)} files, {d_total / 1024:.1f} KB, {ratio:.0f}:1")

    if os.path.isdir(compact_dir):
        c_files = [f for f in os.listdir(compact_dir) if f.endswith(".compact.jsonl.gz")]
        c_total = sum(os.path.getsize(os.path.join(compact_dir, f)) for f in c_files)
        ratio = raw_total / max(1, c_total)
        print(f"  Compact:      {len(c_files)} files, {c_total / 1024 / 1024:.1f} MB, {ratio:.0f}:1")

    print()


def analyze_from_digests(digest_dir=DIGEST_DIR, output_report=None):
    """从 digest 还原并生成分析报告。"""
    matches = DigestRestorer.batch_restore(digest_dir)

    if not matches:
        print("No digests found to analyze.")
        return

    print(f"\nAnalyzing {len(matches)} matches from digests...")
    print("=" * 60)

    wins = sum(1 for m in matches if m.i_won)
    losses = sum(1 for m in matches if not m.i_won and m.over_round > 0)
    delivered = sum(1 for m in matches if m.delivered)
    total_scores = [m.total_score for m in matches if m.total_score > 0]
    opp_scores = [m.opp_score for m in matches if m.opp_score > 0]
    fresh_vals = [m.final_freshness for m in matches if m.final_freshness > 0]
    deliver_rounds = [m.deliver_round for m in matches if m.deliver_round > 0]

    print(f"\n  Matches:   {len(matches)}")
    print(f"  Wins:      {wins} ({wins / max(1, len(matches)) * 100:.1f}%)")
    print(f"  Losses:    {losses}")
    print(f"  Delivered: {delivered} ({delivered / max(1, len(matches)) * 100:.1f}%)")

    if total_scores:
        print(f"\n  Avg Score:      {sum(total_scores) / len(total_scores):.1f}")
        print(f"  Max Score:      {max(total_scores)}")
        print(f"  Min Score:      {min(total_scores)}")
        print(f"  Avg Opp Score:  {sum(opp_scores) / max(1, len(opp_scores)):.1f}")

    if fresh_vals:
        print(f"\n  Avg Freshness:      {sum(fresh_vals) / len(fresh_vals):.1f}")
        print(f"  Avg Deliver Round:  {sum(deliver_rounds) / max(1, len(deliver_rounds)):.0f}")

    if output_report:
        lines = [
            "# 日志分析报告（从 Digest 还原）",
            "",
            f"**分析时间**: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"**对局数量**: {len(matches)}",
            "",
            "## 汇总",
            "",
            "| 指标 | 值 |",
            "|------|----|",
            f"| 总场次 | {len(matches)} |",
            f"| 胜场 | {wins} ({wins / max(1, len(matches)) * 100:.1f}%) |",
            f"| 负场 | {losses} |",
            f"| 交付率 | {delivered}/{len(matches)} ({delivered / max(1, len(matches)) * 100:.1f}%) |",
        ]
        if total_scores:
            lines.append(f"| 平均得分 | {sum(total_scores) / len(total_scores):.1f} |")
        lines += [
            "",
            "## 每场详情",
            "",
            "| # | Match ID | 得分 | 对手分 | 胜负 | 交付 | 鲜度 | 路线节点 |",
            "|---|----------|------|--------|------|------|------|----------|",
        ]
        for i, m in enumerate(matches):
            win_str = "WIN" if m.i_won else "LOSS"
            dv_str = f"r{m.deliver_round}" if m.delivered else "NO"
            nodes = "→".join(m.path_nodes[:8])
            if len(m.path_nodes) > 8:
                nodes += "→..."
            mid_short = m.match_id[:30] + ("..." if len(m.match_id) > 30 else "")
            lines.append(
                f"| {i + 1} | {mid_short} | {m.total_score} | {m.opp_score} | "
                f"{win_str} | {dv_str} | {m.final_freshness:.1f} | {nodes} |"
            )

        with open(output_report, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        print(f"\n  Report saved to: {output_report}")


def main():
    parser = argparse.ArgumentParser(
        description="日志压缩与关键信息提取系统",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m log_compressor                        # 批量生成 digest
  python -m log_compressor --input match.log      # 单文件 digest
  python -m log_compressor --mode compact         # 批量精简压缩
  python -m log_compressor --mode archive -o out.tar.gz
  python -m log_compressor --stats                # 统计压缩效果
  python -m log_compressor --restore --report report.md
        """,
    )
    parser.add_argument("--log-dir", default="logs", help="日志目录 (默认: logs)")
    parser.add_argument("--mode", choices=["digest", "compact", "archive"],
                        default="digest", help="压缩模式 (默认: digest)")
    parser.add_argument("--input", help="单文件输入路径")
    parser.add_argument("-o", "--output", help="输出路径")
    parser.add_argument("--digest-dir", default=DIGEST_DIR, help="digest 输出目录")
    parser.add_argument("--compact-dir", default=COMPACT_DIR, help="compact 输出目录")
    parser.add_argument("--restore", action="store_true", help="从 digest 还原并分析")
    parser.add_argument("--report", help="还原后输出分析报告路径")
    parser.add_argument("--stats", action="store_true", help="打印压缩统计")
    parser.add_argument("--verbose", "-v", action="store_true", help="详细输出")
    args = parser.parse_args()

    compressor = LogCompressor(args.log_dir)

    if args.stats:
        print_stats(args.log_dir, args.digest_dir, args.compact_dir)
        return

    if args.restore:
        analyze_from_digests(args.digest_dir, args.report or args.output)
        return

    if args.input:
        path = args.input
        if not os.path.exists(path):
            print(f"File not found: {path}")
            sys.exit(1)

        if args.mode == "digest":
            d = compressor.digest(path)
            if d is None:
                print("Failed to parse log.")
                sys.exit(1)
            if args.output:
                os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
                with open(args.output, "w", encoding="utf-8") as f:
                    json.dump(d, f, ensure_ascii=False, indent=2)
                print(f"Digest saved to: {args.output}")
            else:
                text = json.dumps(d, ensure_ascii=False, indent=2)
                lines = text.split("\n")
                for line in lines[:60]:
                    print(line)
                if len(lines) > 60:
                    print(f"... ({len(lines)} total lines)")
        elif args.mode == "compact":
            out_path, n = compressor.compact_to_gz(path, args.compact_dir)
            print(f"Compacted: {path}")
            print(f"  Records: {n}")
            print(f"  Output:  {out_path}")
            print(f"  Size:    {os.path.getsize(out_path) / 1024:.1f} KB")
        return

    if not os.path.isdir(args.log_dir):
        print(f"Log directory not found: {args.log_dir}")
        print("Specify a directory with --log-dir or a single file with --input")
        sys.exit(1)

    print(f"Mode: {args.mode}")
    print(f"Log dir: {args.log_dir}")
    print()

    if args.mode == "digest":
        compressor.batch_digest(args.digest_dir, verbose=args.verbose or True)
    elif args.mode == "compact":
        compressor.batch_compact(args.compact_dir, verbose=args.verbose or True)
    elif args.mode == "archive":
        out = args.output or "logs/archive.tar.gz"
        compressor.archive(out, verbose=args.verbose or True)


if __name__ == "__main__":
    main()
