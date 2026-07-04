#!/usr/bin/env python3
r"""日志压缩与关键信息提取系统。

解析 client 的文本格式运行日志（key=value 格式），提取关键信息，
压缩为适合 git 上传的紧凑格式。支持三种模式：

  digest   — 结构化摘要（~100:1 压缩），适合 git 追踪
  compact  — 去冗余帧 + gzip（~10:1），保留完整分析能力
  archive  — 全量 tar.gz 归档（~5:1）

用法:
  python log_compressor.py                           # 批量生成 digest
  python log_compressor.py --input <file.log>        # 单文件 digest
  python log_compressor.py --mode compact            # 批量精简压缩
  python log_compressor.py --mode archive -o out.tar.gz
  python log_compressor.py --restore --report out.md  # 从 digest 还原并分析
  python log_compressor.py --stats                   # 统计压缩效果

日志格式（每行一条）:
  HH:MM:SS.ms Type key1=val1, key2=val2, ...
  Type ∈ {Startup, Register, Start, Ready, Frame, Projection, Eta,
           Action, ModeChange, Over, Score, Shutdown}
"""

import gzip
import json
import os
import re
import sys
import tarfile
import time
from collections import defaultdict
from dataclasses import dataclass, field

# ============================================================
#  常量
# ============================================================

DIGEST_DIR = "logs/digests"       # 摘要输出目录
COMPACT_DIR = "logs/compact"      # 精简压缩输出目录

# 心跳帧窗口：连续 N 帧 state/node/phase 无变化视为心跳，compact 模式下跳过
HEARTBEAT_WINDOW = 3

# 鲜度采样间隔（帧）
FRESHNESS_SAMPLE_INTERVAL = 20

# 投影片段阈值：score/gap 变化超过此值才保留
PROJECTION_CHANGE_THRESHOLD = 5


# ============================================================
#  数据结构
# ============================================================

@dataclass
class FrameRecord:
    """单帧快照。"""
    round: int = 0
    phase: str = ""
    node: str = ""
    state: str = ""
    fresh: float = 0.0
    good_fruit: int = 0
    task_score: int = 0
    verified: bool = False
    delivered: bool = False
    events: list = field(default_factory=list)


@dataclass
class MatchLog:
    """一场对局的完整结构化日志。"""
    match_id: str = ""
    player_id: int = 0
    player_name: str = ""
    team_id: str = ""
    # 帧序列
    frames: list = field(default_factory=list)          # list[FrameRecord]
    # 动作序列
    actions: list = field(default_factory=list)          # [{round, action, target, note}]
    # 投影序列（得分预估）
    projections: list = field(default_factory=list)      # [{round, myScore, oppScore, gap, mode, ...}]
    # ETA 序列
    etas: list = field(default_factory=list)             # [{round, oppFrom, toGate, toFinish, verified, conf}]
    # 模式切换
    mode_changes: list = field(default_factory=list)
    # 结算
    winner_id: int = None
    i_won: bool = False
    over_round: int = 0
    over_reason: str = ""
    result_type: str = ""
    # 最终分数
    total_score: int = 0
    delivered: bool = False
    deliver_round: int = 0
    final_freshness: float = 0.0
    final_good_fruit: int = 0
    task_score: int = 0
    bounty_score: int = 0
    retired: bool = False
    # 对手分数
    opp_score: int = 0
    opp_deliver_round: int = 0
    opp_freshness: float = 0.0
    # 衍生数据
    path_nodes: list = field(default_factory=list)
    freshness_curve: list = field(default_factory=list)
    action_dist: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)


# ============================================================
#  文本日志解析器
# ============================================================

class LogParser:
    """解析文本格式的 key=value 日志。"""

    @staticmethod
    def _parse_line(line: str) -> dict:
        """解析一行日志为 {type, round, ...fields}。

        格式: HH:MM:SS.ms TypeName key1=val1, key2=val2, ...
        """
        line = line.strip()
        if not line:
            return None

        # 去掉时间戳前缀 (HH:MM:SS.ms )
        m = re.match(r'^\S+\s+(\S+)\s+(.*)', line)
        if not m:
            return None

        entry_type = m.group(1)
        rest = m.group(2)

        rec = {"_type": entry_type}

        # 解析 key=value 对，按 ", " 分割
        pairs = rest.split(", ")
        for pair in pairs:
            eq = pair.find('=')
            if eq < 0:
                continue
            key = pair[:eq].strip()
            value = pair[eq + 1:].strip()
            rec[key] = value

        # 类型转换常用字段
        if 'round' in rec:
            try:
                rec['round'] = int(rec['round'])
            except (ValueError, TypeError):
                pass
        if 'playerId' in rec:
            try:
                rec['playerId'] = int(rec['playerId'])
            except (ValueError, TypeError):
                pass

        return rec

    @staticmethod
    def parse_file(path: str) -> MatchLog:
        """解析整个日志文件为 MatchLog。"""
        ml = MatchLog()
        frame_map = {}   # round -> dict (raw frame data)
        action_list = []
        prev_node = None

        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                rec = LogParser._parse_line(line)
                if rec is None:
                    continue

                t = rec["_type"]

                if t == "Startup":
                    try:
                        ml.player_id = int(rec.get("playerId", 0))
                    except (ValueError, TypeError):
                        pass

                elif t == "Register":
                    ml.player_name = rec.get("name", "")

                elif t == "Start":
                    ml.match_id = rec.get("matchId", "")
                    ml.team_id = rec.get("teamId", "")

                elif t == "Frame":
                    rnd = rec.get("round", 0)
                    frame_map[rnd] = {
                        "round": rnd,
                        "phase": rec.get("phase", ""),
                        "node": rec.get("node", ""),
                        "state": rec.get("state", ""),
                        "fresh": float(rec.get("fresh", 0)),
                        "good_fruit": int(rec.get("goodFruit", 0)),
                        "task_score": int(rec.get("taskScore", 0)),
                        "verified": rec.get("verified", "").lower() == "true",
                        "delivered": rec.get("delivered", "").lower() == "true",
                        "events": rec.get("events", "").split("|") if rec.get("events") else [],
                    }

                elif t == "Projection":
                    rnd = rec.get("round", 0)
                    ml.projections.append({
                        "round": rnd,
                        "myScore": int(rec.get("myScore", 0)),
                        "oppScore": int(rec.get("oppScore", 0)),
                        "gap": int(rec.get("gap", 0)),
                        "mode": rec.get("mode", ""),
                        "myDeliver": int(rec.get("myDeliver", 0)),
                        "oppDeliver": int(rec.get("oppDeliver", 0)),
                        "confidence": float(rec.get("confidence", 0)),
                    })

                elif t == "Eta":
                    rnd = rec.get("round", 0)
                    ml.etas.append({
                        "round": rnd,
                        "oppFrom": rec.get("oppFrom", ""),
                        "toGate": int(rec.get("toGate", 0)),
                        "toFinish": int(rec.get("toFinish", 0)),
                        "verified": rec.get("verified", "").lower() == "true",
                        "conf": float(rec.get("conf", 0)),
                    })

                elif t == "Action":
                    rnd = rec.get("round", 0)
                    action_rec = {
                        "round": rnd,
                        "action": rec.get("action", "NONE"),
                        "target": rec.get("target", ""),
                        "note": rec.get("note", ""),
                    }
                    action_list.append(action_rec)

                    # 非心跳动作绑定到帧（简化：绑定到当前 round）
                    if action_rec["action"] != "NONE":
                        if rnd in frame_map:
                            pass  # 帧数据已存在

                elif t == "ModeChange":
                    ml.mode_changes.append({
                        "round": rec.get("round", 0),
                        "reason": rec.get("reason", ""),
                        "from": rec.get("from", ""),
                        "to": rec.get("to", ""),
                    })

                elif t == "Over":
                    ml.result_type = rec.get("resultType", "")
                    ml.over_reason = rec.get("reason", "")
                    ml.over_round = int(rec.get("overRound", 0))
                    ml.winner_id = int(rec.get("winner", 0))
                    ml.i_won = rec.get("iWon", "").lower() == "true"

                elif t == "Score":
                    is_me = rec.get("me", "").lower() == "true"
                    if is_me:
                        ml.total_score = int(rec.get("total", 0))
                        ml.delivered = rec.get("delivered", "").lower() == "true"
                        ml.deliver_round = int(rec.get("deliverRound", 0))
                        ml.final_freshness = float(rec.get("fresh", 0))
                        ml.final_good_fruit = int(rec.get("goodFruit", 0))
                        ml.task_score = int(rec.get("taskScore", 0))
                        ml.bounty_score = int(rec.get("bountyScore", 0))
                        ml.retired = rec.get("retired", "").lower() == "true"
                    else:
                        ml.opp_score = int(rec.get("total", 0))
                        ml.opp_deliver_round = int(rec.get("deliverRound", 0))
                        ml.opp_freshness = float(rec.get("fresh", 0))

        # 组装帧序列
        ml.frames = []
        for rnd in sorted(frame_map.keys()):
            fr_data = frame_map[rnd]
            fr = FrameRecord(
                round=fr_data["round"],
                phase=fr_data["phase"],
                node=fr_data["node"],
                state=fr_data["state"],
                fresh=fr_data["fresh"],
                good_fruit=fr_data["good_fruit"],
                task_score=fr_data["task_score"],
                verified=fr_data["verified"],
                delivered=fr_data["delivered"],
                events=fr_data["events"],
            )
            ml.frames.append(fr)

            # 构建路径节点
            if fr.node and fr.node != prev_node:
                ml.path_nodes.append(fr.node)
                prev_node = fr.node

            # 采样鲜度曲线
            if fr.round % FRESHNESS_SAMPLE_INTERVAL == 0:
                ml.freshness_curve.append((fr.round, round(fr.fresh, 2)))

        ml.actions = action_list

        # 计算动作分布
        action_counts = defaultdict(int)
        for a in action_list:
            action_counts[a["action"]] += 1
        ml.action_dist = dict(action_counts)

        return ml


# ============================================================
#  LogCompressor — 三种压缩模式
# ============================================================

class LogCompressor:
    """日志压缩器。"""

    def __init__(self, log_dir="logs"):
        self.log_dir = log_dir

    # ── 模式 1: digest ────────────────────────────────────

    def digest(self, path: str) -> dict:
        """从单个日志文件提取结构化摘要。

        返回紧凑 dict（短键名），约 1-3KB。
        """
        ml = LogParser.parse_file(path)
        if ml is None or not ml.match_id:
            return None

        d = {}

        # ── 身份 ──
        d["mid"] = ml.match_id
        d["pid"] = ml.player_id
        d["pn"] = ml.player_name
        d["tid"] = ml.team_id

        # ── 结果 ──
        d["ts"] = ml.total_score          # total score
        d["os"] = ml.opp_score            # opponent score
        d["w"] = ml.i_won                 # won?
        d["dv"] = ml.delivered            # delivered?
        d["dr"] = ml.deliver_round        # deliver round
        d["or"] = ml.over_round           # over round
        d["ff"] = ml.final_freshness      # final freshness
        d["gf"] = ml.final_good_fruit     # good fruit
        d["tsk"] = ml.task_score          # task score
        d["bty"] = ml.bounty_score        # bounty score
        d["of"] = ml.opp_freshness        # opponent freshness
        d["odr"] = ml.opp_deliver_round   # opp deliver round
        d["rt"] = ml.result_type          # result type
        d["ore"] = ml.over_reason         # over reason

        # ── 路线 ──
        d["pa"] = "→".join(ml.path_nodes)      # path
        d["nc"] = len(ml.path_nodes)            # node count

        # ── 鲜度曲线（采样） ──
        if ml.freshness_curve:
            d["fc"] = [{"r": r, "f": f} for r, f in ml.freshness_curve]

        # ── 动作分布 ──
        d["ac"] = ml.action_dist          # action counts
        d["tc"] = sum(ml.action_dist.values())  # total actions

        # ── 关键时刻 ──
        key_moments = []

        # 首次有意义的动作
        first_real_action = None
        for a in ml.actions:
            if a["action"] != "NONE":
                first_real_action = {"r": a["round"], "a": a["action"], "t": a.get("target", "")}
                break
        if first_real_action:
            key_moments.append(first_real_action)

        # 节点进入（首次出现）
        seen_nodes = set()
        for fr in ml.frames:
            if fr.node and fr.node not in seen_nodes:
                seen_nodes.add(fr.node)
                key_moments.append({
                    "r": fr.round, "t": "n", "nd": fr.node,
                    "f": round(fr.fresh, 2),
                })

        # 交付时刻
        if ml.delivered and ml.deliver_round > 0:
            key_moments.append({"r": ml.deliver_round, "t": "dv", "f": round(ml.final_freshness, 2)})

        # 模式切换
        for mc in ml.mode_changes:
            key_moments.append({
                "r": mc["round"], "t": "m",
                "frm": mc["from"], "to": mc["to"], "rs": mc["reason"][:3],
            })

        d["km"] = key_moments

        # ── 得分投影（关键帧，去冗余） ──
        projections = []
        prev_gap = None
        for p in ml.projections:
            gap = p["gap"]
            if prev_gap is None or abs(gap - prev_gap) >= PROJECTION_CHANGE_THRESHOLD:
                projections.append({
                    "r": p["round"],
                    "ms": p["myScore"],
                    "os": p["oppScore"],
                    "g": gap,
                    "md": p["mode"],
                })
                prev_gap = gap
        if projections:
            d["pj"] = projections

        # ── ETA（关键帧） ──
        etas = []
        prev_finish = None
        prev_verified = None
        for e in ml.etas:
            to_finish = e["toFinish"]
            verified = e["verified"]
            if (prev_finish is None or abs(to_finish - prev_finish) >= 20
                    or verified != prev_verified):
                etas.append({
                    "r": e["round"],
                    "op": e["oppFrom"],
                    "tg": e["toGate"],
                    "tf": to_finish,
                    "v": verified,
                })
                prev_finish = to_finish
                prev_verified = verified
        if etas:
            d["et"] = etas

        # ── 错误/异常检测 ──
        errors = []
        for fr in ml.frames:
            for evt in fr.events:
                if "FAILED" in evt.upper() or "ERROR" in evt.upper():
                    errors.append({"r": fr.round, "e": evt})
        if errors:
            d["er"] = errors[:20]  # 最多 20 条

        # ── 帧统计 ──
        state_counts = defaultdict(int)
        for fr in ml.frames:
            state_counts[fr.state] += 1
        total_frames = len(ml.frames)
        d["tf"] = total_frames
        d["sd"] = dict(state_counts)

        # 有用帧率
        useful = state_counts.get("MOVING", 0) + state_counts.get("PROCESSING", 0) + \
                 state_counts.get("VERIFYING", 0) + state_counts.get("FORCED_PASSING", 0)
        wasted = state_counts.get("IDLE", 0) + state_counts.get("WAITING", 0) + \
                 state_counts.get("RESTING", 0)
        d["up"] = round(useful / max(1, total_frames) * 100, 1)
        d["wp"] = round(wasted / max(1, total_frames) * 100, 1)

        return d

    def batch_digest(self, output_dir=DIGEST_DIR, verbose=True):
        """批量生成摘要。"""
        os.makedirs(output_dir, exist_ok=True)
        files = sorted(os.listdir(self.log_dir))
        log_files = [f for f in files if f.endswith(".log")]

        ok, fail = 0, 0
        total_raw, total_digest = 0, 0

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

    # ── 模式 2: compact ───────────────────────────────────

    def compact(self, path: str) -> list:
        """精简单个日志文件：去冗余 Frame/Projection/Eta + 去心跳 Action。

        返回精简的 dict 列表（每行一条）。
        """
        records = []
        prev_frame_key = None
        heartbeat_count = 0
        prev_gap = None
        prev_finish = None
        prev_verified = None

        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                rec = LogParser._parse_line(line)
                if rec is None:
                    continue

                t = rec["_type"]
                rnd = rec.get("round", 0)

                if t == "Frame":
                    # 心跳检测
                    frame_key = (
                        rec.get("node"),
                        rec.get("state"),
                        rec.get("phase"),
                        rec.get("fresh"),
                        rec.get("goodFruit"),
                        rec.get("taskScore"),
                        rec.get("verified"),
                        rec.get("delivered"),
                    )
                    if frame_key == prev_frame_key:
                        heartbeat_count += 1
                        if heartbeat_count <= HEARTBEAT_WINDOW:
                            pass
                        else:
                            continue  # 跳过纯心跳帧
                    else:
                        heartbeat_count = 0
                        prev_frame_key = frame_key

                    compact_rec = {
                        "k": "F",
                        "r": rnd,
                        "ph": rec.get("phase", ""),
                        "nd": rec.get("node", ""),
                        "st": rec.get("state", ""),
                        "f": rec.get("fresh", ""),
                        "gf": rec.get("goodFruit", ""),
                        "tsk": rec.get("taskScore", ""),
                        "v": "1" if rec.get("verified", "").lower() == "true" else "",
                        "dv": "1" if rec.get("delivered", "").lower() == "true" else "",
                    }
                    evts = rec.get("events", "")
                    if evts:
                        # 只保留非心跳事件
                        key_evts = [e for e in evts.split("|")
                                    if e not in ("FRESHNESS_DROP", "MOVE_PROGRESS", "PROCESS_PROGRESS")]
                        if key_evts:
                            compact_rec["ev"] = "|".join(key_evts)
                    records.append(compact_rec)

                elif t == "Projection":
                    gap = int(rec.get("gap", 0))
                    if prev_gap is not None and abs(gap - prev_gap) < PROJECTION_CHANGE_THRESHOLD:
                        continue  # 跳过无显著变化的投影
                    prev_gap = gap
                    records.append({
                        "k": "P",
                        "r": rnd,
                        "ms": rec.get("myScore", ""),
                        "os": rec.get("oppScore", ""),
                        "g": str(gap),
                        "md": rec.get("mode", ""),
                    })

                elif t == "Eta":
                    to_finish = int(rec.get("toFinish", 0))
                    verified = rec.get("verified", "").lower() == "true"
                    if (prev_finish is not None and abs(to_finish - prev_finish) < 20
                            and verified == prev_verified):
                        continue
                    prev_finish = to_finish
                    prev_verified = verified
                    records.append({
                        "k": "E",
                        "r": rnd,
                        "op": rec.get("oppFrom", ""),
                        "tg": rec.get("toGate", ""),
                        "tf": str(to_finish),
                        "v": "1" if verified else "",
                    })

                elif t == "Action":
                    action = rec.get("action", "NONE")
                    if action == "NONE":
                        continue  # 跳过所有心跳动作
                    records.append({
                        "k": "A",
                        "r": rnd,
                        "ac": action,
                        "tg": rec.get("target", ""),
                    })

                elif t in ("Startup", "Register", "Start", "Ready"):
                    records.append({
                        "k": t[0],  # S, R, S, R
                        "typ": t,
                        "r": rnd,
                        **({k: v for k, v in rec.items()
                            if k in ("matchId", "playerId", "name", "teamId", "camp",
                                     "durationRound", "nodes", "edges", "host", "port",
                                     "version")}),
                    })

                elif t in ("Over", "Score", "Shutdown", "ModeChange"):
                    # 这些关键行全部保留
                    records.append({
                        "k": t[0],
                        "typ": t,
                        "r": rnd,
                        **{k: v for k, v in rec.items() if k not in ("_type",)},
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
        files = sorted(os.listdir(self.log_dir))
        log_files = [f for f in files if f.endswith(".log")]

        ok, fail = 0, 0
        total_raw, total_compact = 0, 0

        for fname in log_files:
            path = os.path.join(self.log_dir, fname)
            raw_size = os.path.getsize(path)
            total_raw += raw_size

            try:
                out_path, n_records = self.compact_to_gz(path, output_dir)
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

    # ── 模式 3: archive ───────────────────────────────────

    def archive(self, output_path="logs/archive.tar.gz", verbose=True):
        """将 logs/ 目录下所有 .log 文件打包为 tar.gz。"""
        files = sorted(os.listdir(self.log_dir))
        log_files = [f for f in files if f.endswith(".log")]

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


# ============================================================
#  DigestRestorer — 从摘要还原 MatchLog
# ============================================================

class DigestRestorer:
    """从 digest JSON 还原 MatchLog，可接入分析管线。"""

    @staticmethod
    def restore(digest: dict) -> MatchLog:
        """从单个 digest dict 重建 MatchLog。"""
        ml = MatchLog(
            match_id=digest.get("mid", ""),
            player_id=digest.get("pid", 0),
            player_name=digest.get("pn", ""),
            team_id=digest.get("tid", ""),
        )

        # 结果
        ml.total_score = digest.get("ts", 0)
        ml.opp_score = digest.get("os", 0)
        ml.i_won = digest.get("w", False)
        ml.delivered = digest.get("dv", False)
        ml.deliver_round = digest.get("dr", 0)
        ml.over_round = digest.get("or", 0)
        ml.final_freshness = digest.get("ff", 0)
        ml.final_good_fruit = digest.get("gf", 0)
        ml.task_score = digest.get("tsk", 0)
        ml.bounty_score = digest.get("bty", 0)
        ml.opp_freshness = digest.get("of", 0)
        ml.opp_deliver_round = digest.get("odr", 0)
        ml.result_type = digest.get("rt", "")
        ml.over_reason = digest.get("ore", "")

        # 路线
        path_str = digest.get("pa", "")
        if path_str:
            ml.path_nodes = [n.strip() for n in path_str.split("→") if n.strip()]

        # 鲜度曲线
        fc = digest.get("fc", [])
        if fc:
            ml.freshness_curve = [(p["r"], p["f"]) for p in fc]

        # 动作分布
        ml.action_dist = digest.get("ac", {})

        # 关键时刻 → 事件/帧
        for km in digest.get("km", []):
            t = km.get("t", "")
            if t == "n":  # node enter
                ml.frames.append(FrameRecord(
                    round=km["r"], node=km.get("nd", ""),
                    fresh=km.get("f", 0),
                ))
            elif t == "dv":  # deliver
                ml.frames.append(FrameRecord(
                    round=km["r"], delivered=True,
                    fresh=km.get("f", 0),
                ))

        # 投影
        ml.projections = digest.get("pj", [])

        # ETA
        for e in digest.get("et", []):
            ml.etas.append({
                "round": e["r"],
                "oppFrom": e.get("op", ""),
                "toGate": e.get("tg", 0),
                "toFinish": e.get("tf", 0),
                "verified": e.get("v", False),
                "conf": 0,
            })

        # 错误
        for ed in digest.get("er", []):
            ml.errors.append(ed)

        return ml

    @staticmethod
    def batch_restore(digest_dir=DIGEST_DIR) -> list:
        """从 digest 目录还原所有 MatchLog。"""
        if not os.path.isdir(digest_dir):
            print(f"Digest directory not found: {digest_dir}")
            return []

        files = sorted(os.listdir(digest_dir))
        digest_files = [f for f in files if f.endswith(".digest.json")]

        results = []
        for fname in digest_files:
            path = os.path.join(digest_dir, fname)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    d = json.load(f)
            except (json.JSONDecodeError, IOError):
                continue

            ml = DigestRestorer.restore(d)
            if ml and ml.match_id:
                results.append(ml)

        return results


# ============================================================
#  统计与分析
# ============================================================

def print_stats(log_dir="logs", digest_dir=DIGEST_DIR, compact_dir=COMPACT_DIR):
    """打印压缩效果统计。"""
    print("=" * 60)
    print("  LOG COMPRESSION STATS")
    print("=" * 60)

    # 原始日志
    files = sorted(os.listdir(log_dir)) if os.path.isdir(log_dir) else []
    log_files = [f for f in files if f.endswith(".log")]
    raw_total = sum(os.path.getsize(os.path.join(log_dir, f)) for f in log_files)
    print(f"\n  Raw .log:     {len(log_files)} files, {raw_total / 1024 / 1024:.1f} MB")

    # Digest
    if os.path.isdir(digest_dir):
        d_files = [f for f in os.listdir(digest_dir) if f.endswith(".digest.json")]
        d_total = sum(os.path.getsize(os.path.join(digest_dir, f)) for f in d_files)
        ratio = raw_total / max(1, d_total)
        print(f"  Digest:       {len(d_files)} files, {d_total / 1024:.1f} KB, {ratio:.0f}:1")

    # Compact
    if os.path.isdir(compact_dir):
        c_files = [f for f in os.listdir(compact_dir) if f.endswith(".compact.jsonl.gz")]
        c_total = sum(os.path.getsize(os.path.join(compact_dir, f)) for f in c_files)
        ratio = raw_total / max(1, c_total)
        print(f"  Compact:      {len(c_files)} files, {c_total / 1024 / 1024:.1f} MB, {ratio:.0f}:1")

    print()


def analyze_from_digests(digest_dir=DIGEST_DIR, output_report=None):
    """从 digest 还原并生成对比分析报告。"""
    matches = DigestRestorer.batch_restore(digest_dir)

    if not matches:
        print("No digests found to analyze.")
        return

    print(f"\nAnalyzing {len(matches)} matches from digests...")
    print("=" * 60)

    # 汇总统计
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
        print(f"\n  Avg Score:  {sum(total_scores) / len(total_scores):.1f}")
        print(f"  Max Score:  {max(total_scores)}")
        print(f"  Min Score:  {min(total_scores)}")
        print(f"  Avg Opp Score: {sum(opp_scores) / max(1, len(opp_scores)):.1f}")

    if fresh_vals:
        print(f"\n  Avg Freshness: {sum(fresh_vals) / len(fresh_vals):.1f}")
        print(f"  Avg Deliver Round: {sum(deliver_rounds) / max(1, len(deliver_rounds)):.0f}")

    # 生成报告
    if output_report:
        lines = []
        lines.append("# 日志分析报告（从 Digest 还原）")
        lines.append(f"")
        lines.append(f"**分析时间**: {time.strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"**对局数量**: {len(matches)}")
        lines.append(f"")
        lines.append("## 汇总")
        lines.append("")
        lines.append(f"| 指标 | 值 |")
        lines.append(f"|------|----|")
        lines.append(f"| 总场次 | {len(matches)} |")
        lines.append(f"| 胜场 | {wins} ({wins / max(1, len(matches)) * 100:.1f}%) |")
        lines.append(f"| 负场 | {losses} |")
        lines.append(f"| 交付率 | {delivered}/{len(matches)} ({delivered / max(1, len(matches)) * 100:.1f}%) |")
        if total_scores:
            lines.append(f"| 平均得分 | {sum(total_scores) / len(total_scores):.1f} |")
        lines.append("")
        lines.append("## 每场详情")
        lines.append("")
        lines.append("| # | Match ID | 得分 | 对手分 | 胜负 | 交付 | 鲜度 | 路线节点 |")
        lines.append("|---|----------|------|--------|------|------|------|----------|")
        for i, m in enumerate(matches):
            win_str = "WIN" if m.i_won else "LOSS"
            dv_str = f"r{m.deliver_round}" if m.delivered else "NO"
            nodes = "→".join(m.path_nodes[:8])
            if len(m.path_nodes) > 8:
                nodes += "→..."
            lines.append(f"| {i + 1} | {m.match_id[:30]}... | {m.total_score} | {m.opp_score} | "
                         f"{win_str} | {dv_str} | {m.final_freshness:.1f} | {nodes} |")

        with open(output_report, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        print(f"\n  Report saved to: {output_report}")


# ============================================================
#  主入口
# ============================================================

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="日志压缩与关键信息提取系统",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python log_compressor.py                          # 批量生成 digest
  python log_compressor.py --input match.log        # 单文件 digest
  python log_compressor.py --mode compact           # 批量精简压缩
  python log_compressor.py --mode archive -o out.tar.gz
  python log_compressor.py --stats                  # 统计压缩效果
  python log_compressor.py --restore --report report.md  # 从 digest 还原分析
        """,
    )
    parser.add_argument("--log-dir", default="logs", help="日志目录 (默认: logs)")
    parser.add_argument("--mode", choices=["digest", "compact", "archive"],
                        default="digest", help="压缩模式 (默认: digest)")
    parser.add_argument("--input", help="单文件输入路径")
    parser.add_argument("-o", "--output", help="输出路径（archive / report）")
    parser.add_argument("--digest-dir", default=DIGEST_DIR, help="digest 输出目录")
    parser.add_argument("--compact-dir", default=COMPACT_DIR, help="compact 输出目录")
    parser.add_argument("--restore", action="store_true", help="从 digest 还原并分析")
    parser.add_argument("--report", help="还原后输出分析报告路径")
    parser.add_argument("--stats", action="store_true", help="打印压缩统计")
    parser.add_argument("--verbose", "-v", action="store_true", help="详细输出")
    args = parser.parse_args()

    compressor = LogCompressor(args.log_dir)

    # --stats
    if args.stats:
        print_stats(args.log_dir, args.digest_dir, args.compact_dir)
        return

    # --restore
    if args.restore:
        analyze_from_digests(args.digest_dir, args.report or args.output)
        return

    # --input: 单文件模式
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
                # 打印前 60 行预览
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

    # 批量模式
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
