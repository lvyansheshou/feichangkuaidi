"""从 digest JSON 还原 MatchLog，接入分析管线。

Digest 是压缩后的紧凑 JSON（短键名），Restorer 将其重建为
结构化的 MatchLog，可传入分析器生成报告。
"""

import json
import os

from .datatypes import FrameRecord, MatchLog

DIGEST_DIR = "logs/digests"


class DigestRestorer:
    """从 digest JSON 还原 MatchLog。"""

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

        # 关键时刻 → 帧
        for km in digest.get("km", []):
            t = km.get("t", "")
            if t == "n":
                ml.frames.append(FrameRecord(
                    round=km["r"], node=km.get("nd", ""), fresh=km.get("f", 0),
                ))
            elif t == "dv":
                ml.frames.append(FrameRecord(
                    round=km["r"], delivered=True, fresh=km.get("f", 0),
                ))

        # 投影 & ETA
        ml.projections = digest.get("pj", [])
        for e in digest.get("et", []):
            ml.etas.append({
                "round": e["r"], "oppFrom": e.get("op", ""),
                "toGate": e.get("tg", 0), "toFinish": e.get("tf", 0),
                "verified": e.get("v", False), "conf": 0,
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

        digest_files = [f for f in sorted(os.listdir(digest_dir))
                        if f.endswith(".digest.json")]

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
