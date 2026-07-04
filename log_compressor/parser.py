"""文本日志解析器。

解析 client 的 key=value 格式运行日志，每行格式：
  HH:MM:SS.ms TypeName key1=val1, key2=val2, ...

支持 12 种日志条目类型，输出结构化 MatchLog。
"""

import re
from collections import defaultdict

from .datatypes import FrameRecord, MatchLog

# 鲜度曲线采样间隔
FRESHNESS_SAMPLE_INTERVAL = 20


class LogParser:
    """解析文本格式的 key=value 日志。"""

    @staticmethod
    def _parse_line(line: str) -> dict:
        """解析一行日志为 {_type, key, value, ...}。

        格式: HH:MM:SS.ms TypeName key1=val1, key2=val2, ...
        """
        line = line.strip()
        if not line:
            return None

        # 去掉时间戳前缀
        m = re.match(r'^\S+\s+(\S+)\s+(.*)', line)
        if not m:
            return None

        entry_type = m.group(1)
        rest = m.group(2)

        rec = {"_type": entry_type}

        # 按 ", " 分割 key=value 对
        pairs = rest.split(", ")
        for pair in pairs:
            eq = pair.find('=')
            if eq < 0:
                continue
            key = pair[:eq].strip()
            value = pair[eq + 1:].strip()
            rec[key] = value

        # 类型转换
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
        frame_map = {}    # round -> raw dict
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
                    ml.projections.append({
                        "round": rec.get("round", 0),
                        "myScore": int(rec.get("myScore", 0)),
                        "oppScore": int(rec.get("oppScore", 0)),
                        "gap": int(rec.get("gap", 0)),
                        "mode": rec.get("mode", ""),
                        "myDeliver": int(rec.get("myDeliver", 0)),
                        "oppDeliver": int(rec.get("oppDeliver", 0)),
                        "confidence": float(rec.get("confidence", 0)),
                    })

                elif t == "Eta":
                    ml.etas.append({
                        "round": rec.get("round", 0),
                        "oppFrom": rec.get("oppFrom", ""),
                        "toGate": int(rec.get("toGate", 0)),
                        "toFinish": int(rec.get("toFinish", 0)),
                        "verified": rec.get("verified", "").lower() == "true",
                        "conf": float(rec.get("conf", 0)),
                    })

                elif t == "Action":
                    action_rec = {
                        "round": rec.get("round", 0),
                        "action": rec.get("action", "NONE"),
                        "target": rec.get("target", ""),
                        "note": rec.get("note", ""),
                    }
                    action_list.append(action_rec)

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

        # 组装帧序列 + 衍生数据
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

            if fr.node and fr.node != prev_node:
                ml.path_nodes.append(fr.node)
                prev_node = fr.node

            if fr.round % FRESHNESS_SAMPLE_INTERVAL == 0:
                ml.freshness_curve.append((fr.round, round(fr.fresh, 2)))

        ml.actions = action_list

        action_counts = defaultdict(int)
        for a in action_list:
            action_counts[a["action"]] += 1
        ml.action_dist = dict(action_counts)

        return ml
