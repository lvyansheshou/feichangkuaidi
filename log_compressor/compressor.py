"""日志压缩器：digest / compact / archive 三种压缩模式。

- digest:  结构化摘要（~80:1），提取关键指标为紧凑 JSON
- compact: 去冗余帧 + gzip（~50:1），保留完整分析能力
- archive: 全量 tar.gz 归档（~5:1）
"""

import gzip
import json
import os
import sys
import tarfile
from collections import defaultdict

from .parser import LogParser

# 默认输出目录
DIGEST_DIR = "logs/digests"
COMPACT_DIR = "logs/compact"

# 心跳帧窗口
HEARTBEAT_WINDOW = 3

# 投影变化阈值（gap 变化超过此值才保留）
PROJECTION_CHANGE_THRESHOLD = 5


class LogCompressor:
    """日志压缩器：读取 .log 文件，产出 digest / compact / archive。"""

    def __init__(self, log_dir="logs"):
        self.log_dir = log_dir

    # ── digest ────────────────────────────────────────────

    def digest(self, path: str) -> dict:
        """从单个日志文件提取结构化摘要。返回紧凑 dict（短键名），约 1-5KB。"""
        ml = LogParser.parse_file(path)
        if ml is None or not ml.match_id:
            return None

        d = {}

        # 身份
        d["mid"] = ml.match_id
        d["pid"] = ml.player_id
        d["pn"] = ml.player_name
        d["tid"] = ml.team_id

        # 结果
        d["ts"] = ml.total_score
        d["os"] = ml.opp_score
        d["w"] = ml.i_won
        d["dv"] = ml.delivered
        d["dr"] = ml.deliver_round
        d["or"] = ml.over_round
        d["ff"] = ml.final_freshness
        d["gf"] = ml.final_good_fruit
        d["tsk"] = ml.task_score
        d["bty"] = ml.bounty_score
        d["of"] = ml.opp_freshness
        d["odr"] = ml.opp_deliver_round
        d["rt"] = ml.result_type
        d["ore"] = ml.over_reason

        # 路线
        d["pa"] = "→".join(ml.path_nodes)
        d["nc"] = len(ml.path_nodes)

        # 鲜度曲线
        if ml.freshness_curve:
            d["fc"] = [{"r": r, "f": f} for r, f in ml.freshness_curve]

        # 动作分布
        d["ac"] = ml.action_dist
        d["tc"] = sum(ml.action_dist.values())

        # 关键时刻
        key_moments = self._extract_key_moments(ml)
        d["km"] = key_moments

        # 得分投影（去冗余）
        projections = []
        prev_gap = None
        for p in ml.projections:
            gap = p["gap"]
            if prev_gap is None or abs(gap - prev_gap) >= PROJECTION_CHANGE_THRESHOLD:
                projections.append({
                    "r": p["round"], "ms": p["myScore"],
                    "os": p["oppScore"], "g": gap, "md": p["mode"],
                })
                prev_gap = gap
        if projections:
            d["pj"] = projections

        # ETA（去冗余）
        etas = []
        prev_finish, prev_verified = None, None
        for e in ml.etas:
            tf = e["toFinish"]
            v = e["verified"]
            if (prev_finish is None or abs(tf - prev_finish) >= 20 or v != prev_verified):
                etas.append({
                    "r": e["round"], "op": e["oppFrom"],
                    "tg": e["toGate"], "tf": tf, "v": v,
                })
                prev_finish, prev_verified = tf, v
        if etas:
            d["et"] = etas

        # 错误
        errors = []
        for fr in ml.frames:
            for evt in fr.events:
                if "FAILED" in evt.upper() or "ERROR" in evt.upper():
                    errors.append({"r": fr.round, "e": evt})
        if errors:
            d["er"] = errors[:20]

        # 帧统计
        state_counts = defaultdict(int)
        for fr in ml.frames:
            state_counts[fr.state] += 1
        total_frames = len(ml.frames)
        d["tf"] = total_frames
        d["sd"] = dict(state_counts)

        useful = (state_counts.get("MOVING", 0) + state_counts.get("PROCESSING", 0) +
                  state_counts.get("VERIFYING", 0) + state_counts.get("FORCED_PASSING", 0))
        wasted = (state_counts.get("IDLE", 0) + state_counts.get("WAITING", 0) +
                  state_counts.get("RESTING", 0))
        d["up"] = round(useful / max(1, total_frames) * 100, 1)
        d["wp"] = round(wasted / max(1, total_frames) * 100, 1)

        return d

    @staticmethod
    def _extract_key_moments(ml) -> list:
        """从 MatchLog 提取关键时刻：首次动作、节点进入、交付、模式切换。"""
        moments = []

        for a in ml.actions:
            if a["action"] != "NONE":
                moments.append({"r": a["round"], "a": a["action"], "t": a.get("target", "")})
                break

        seen_nodes = set()
        for fr in ml.frames:
            if fr.node and fr.node not in seen_nodes:
                seen_nodes.add(fr.node)
                moments.append({
                    "r": fr.round, "t": "n", "nd": fr.node, "f": round(fr.fresh, 2),
                })

        if ml.delivered and ml.deliver_round > 0:
            moments.append({"r": ml.deliver_round, "t": "dv", "f": round(ml.final_freshness, 2)})

        for mc in ml.mode_changes:
            moments.append({
                "r": mc["round"], "t": "m",
                "frm": mc["from"], "to": mc["to"], "rs": mc["reason"][:3],
            })

        return moments

    def batch_digest(self, output_dir=DIGEST_DIR, verbose=True):
        """批量生成摘要。返回 (成功数, 失败数, 原始总大小, 摘要总大小)。"""
        os.makedirs(output_dir, exist_ok=True)
        log_files = [f for f in sorted(os.listdir(self.log_dir)) if f.endswith(".log")]

        ok, fail, total_raw, total_digest = 0, 0, 0, 0

        for fname in log_files:
            path = os.path.join(self.log_dir, fname)
            raw_size = os.path.getsize(path)
            total_raw += raw_size

            try:
                d = self.digest(path)
                if d is None:
                    fail += 1
                    continue
                out_name = fname.replace(".log", ".digest.json")
                out_path = os.path.join(output_dir, out_name)
                with open(out_path, "w", encoding="utf-8") as f:
                    json.dump(d, f, ensure_ascii=False, separators=(",", ":"))
                total_digest += os.path.getsize(out_path)
                ok += 1
                if verbose and ok % 50 == 0:
                    print(f"  ... {ok} digests generated ...")
            except Exception as e:
                fail += 1
                if verbose:
                    print(f"  FAIL {fname}: {e}", file=sys.stderr)

        if verbose:
            print(f"\nDigest complete: {ok} ok, {fail} fail")
            print(f"  Raw:     {total_raw / 1024 / 1024:.1f} MB")
            print(f"  Digest:  {total_digest / 1024:.1f} KB")
            if total_raw > 0:
                print(f"  Ratio:   {total_raw / max(1, total_digest):.0f}:1")

        return ok, fail, total_raw, total_digest

    # ── compact ───────────────────────────────────────────

    def compact(self, path: str) -> list:
        """精简单个日志：去心跳 Frame/Action、去无变化 Projection/Eta。

        返回精简的 dict 列表（每行一条）。
        """
        records = []
        prev_frame_key = None
        heartbeat_count = 0
        prev_gap, prev_finish, prev_verified = None, None, None

        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                rec = LogParser._parse_line(line)
                if rec is None:
                    continue

                t = rec["_type"]
                rnd = rec.get("round", 0)

                if t == "Frame":
                    frame_key = (
                        rec.get("node"), rec.get("state"), rec.get("phase"),
                        rec.get("fresh"), rec.get("goodFruit"), rec.get("taskScore"),
                        rec.get("verified"), rec.get("delivered"),
                    )
                    if frame_key == prev_frame_key:
                        heartbeat_count += 1
                        if heartbeat_count <= HEARTBEAT_WINDOW:
                            pass
                        else:
                            continue
                    else:
                        heartbeat_count = 0
                        prev_frame_key = frame_key

                    compact_rec = {
                        "k": "F", "r": rnd,
                        "ph": rec.get("phase", ""), "nd": rec.get("node", ""),
                        "st": rec.get("state", ""), "f": rec.get("fresh", ""),
                        "gf": rec.get("goodFruit", ""), "tsk": rec.get("taskScore", ""),
                        "v": "1" if rec.get("verified", "").lower() == "true" else "",
                        "dv": "1" if rec.get("delivered", "").lower() == "true" else "",
                    }
                    evts = rec.get("events", "")
                    if evts:
                        key_evts = [e for e in evts.split("|")
                                    if e not in ("FRESHNESS_DROP", "MOVE_PROGRESS",
                                                  "PROCESS_PROGRESS")]
                        if key_evts:
                            compact_rec["ev"] = "|".join(key_evts)
                    records.append(compact_rec)

                elif t == "Projection":
                    gap = int(rec.get("gap", 0))
                    if prev_gap is not None and abs(gap - prev_gap) < PROJECTION_CHANGE_THRESHOLD:
                        continue
                    prev_gap = gap
                    records.append({
                        "k": "P", "r": rnd,
                        "ms": rec.get("myScore", ""), "os": rec.get("oppScore", ""),
                        "g": str(gap), "md": rec.get("mode", ""),
                    })

                elif t == "Eta":
                    tf = int(rec.get("toFinish", 0))
                    v = rec.get("verified", "").lower() == "true"
                    if (prev_finish is not None and abs(tf - prev_finish) < 20
                            and v == prev_verified):
                        continue
                    prev_finish, prev_verified = tf, v
                    records.append({
                        "k": "E", "r": rnd,
                        "op": rec.get("oppFrom", ""), "tg": rec.get("toGate", ""),
                        "tf": str(tf), "v": "1" if v else "",
                    })

                elif t == "Action":
                    action = rec.get("action", "NONE")
                    if action == "NONE":
                        continue
                    records.append({
                        "k": "A", "r": rnd,
                        "ac": action, "tg": rec.get("target", ""),
                    })

                elif t in ("Startup", "Register", "Start", "Ready"):
                    records.append({
                        "k": t[0], "typ": t, "r": rnd,
                        **{k: v for k, v in rec.items()
                           if k in ("matchId", "playerId", "name", "teamId", "camp",
                                    "durationRound", "nodes", "edges", "host", "port",
                                    "version")},
                    })

                elif t in ("Over", "Score", "Shutdown", "ModeChange"):
                    records.append({
                        "k": t[0], "typ": t, "r": rnd,
                        **{k: v for k, v in rec.items() if k != "_type"},
                    })

        return records

    def compact_to_gz(self, path: str, output_dir=COMPACT_DIR):
        """精简单文件并写入 .compact.jsonl.gz。"""
        os.makedirs(output_dir, exist_ok=True)
        fname = os.path.basename(path).replace(".log", ".compact.jsonl.gz")
        out_path = os.path.join(output_dir, fname)

        records = self.compact(path)
        with gzip.open(out_path, "wt", encoding="utf-8", compresslevel=9) as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")

        return out_path, len(records)

    def batch_compact(self, output_dir=COMPACT_DIR, verbose=True):
        """批量精简压缩。"""
        os.makedirs(output_dir, exist_ok=True)
        log_files = [f for f in sorted(os.listdir(self.log_dir)) if f.endswith(".log")]

        ok, fail, total_raw, total_compact = 0, 0, 0, 0

        for fname in log_files:
            path = os.path.join(self.log_dir, fname)
            total_raw += os.path.getsize(path)
            try:
                out_path, _ = self.compact_to_gz(path, output_dir)
                total_compact += os.path.getsize(out_path)
                ok += 1
                if verbose and ok % 50 == 0:
                    print(f"  ... {ok} compacted ...")
            except Exception as e:
                fail += 1
                if verbose:
                    print(f"  FAIL {fname}: {e}", file=sys.stderr)

        if verbose:
            print(f"\nCompact complete: {ok} ok, {fail} fail")
            print(f"  Raw:       {total_raw / 1024 / 1024:.1f} MB")
            print(f"  Compact:   {total_compact / 1024 / 1024:.1f} MB")
            if total_raw > 0:
                print(f"  Ratio:     {total_raw / max(1, total_compact):.0f}:1")

        return ok, fail, total_raw, total_compact

    # ── archive ───────────────────────────────────────────

    def archive(self, output_path="logs/archive.tar.gz", verbose=True):
        """将 logs/ 下所有 .log 打包为 tar.gz。"""
        log_files = [f for f in sorted(os.listdir(self.log_dir)) if f.endswith(".log")]

        total_size = 0
        with tarfile.open(output_path, "w:gz", compresslevel=9) as tar:
            for fname in log_files:
                fpath = os.path.join(self.log_dir, fname)
                tar.add(fpath, arcname=fname)
                total_size += os.path.getsize(fpath)
                if verbose and len(tar.getnames()) % 50 == 0:
                    print(f"  ... {len(tar.getnames())} files archived ...")

        archive_size = os.path.getsize(output_path)
        if verbose:
            print(f"\nArchive complete: {len(log_files)} files")
            print(f"  Raw:      {total_size / 1024 / 1024:.1f} MB")
            print(f"  Archive:  {archive_size / 1024 / 1024:.1f} MB")
            if total_size > 0:
                print(f"  Ratio:    {total_size / max(1, archive_size):.0f}:1")
            print(f"  Output:   {output_path}")

        return len(log_files), total_size, archive_size
