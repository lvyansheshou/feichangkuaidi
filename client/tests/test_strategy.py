"""M3 基线策略单测：在小地图上逐场景断言 decide 输出。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.world_state import WorldState  # noqa: E402
from strategy.decision import DecisionEngine, GameContext  # noqa: E402

PID = 1001

# 线性地图：S01(起点) - S02(处理点) - S14(宫门) - S15(终点)
START_DATA = {
    "matchId": "t",
    "durationRound": 600,
    "nodes": [
        {"nodeId": "S01", "type": "START", "x": 0, "y": 0, "start": True},
        {"nodeId": "S02", "type": "STATION", "x": 1, "y": 0},
        {"nodeId": "S14", "type": "GATE", "x": 2, "y": 0},
        {"nodeId": "S15", "type": "FINISH", "x": 3, "y": 0, "terminal": True},
    ],
    "edges": [
        {"edgeId": "E1", "fromNodeId": "S01", "toNodeId": "S02", "routeType": "ROAD", "distance": 10, "bidirectional": True},
        {"edgeId": "E2", "fromNodeId": "S02", "toNodeId": "S14", "routeType": "ROAD", "distance": 10, "bidirectional": True},
        {"edgeId": "E3", "fromNodeId": "S14", "toNodeId": "S15", "routeType": "ROAD", "distance": 10, "bidirectional": True},
    ],
    "map": {"gameplay": {"roles": {"startNodeId": "S01", "gateNodeId": "S14",
                                   "terminalNodeIds": ["S15"], "safeZoneNodeIds": ["S15"]}}},
    "processNodes": [{"nodeId": "S02", "processType": "TRANSFER", "processRound": 4}],
}


def make_world(node, state="IDLE", phase="NORMAL", verified=False, delivered=False,
               good=100, fresh=100.0, events=None, game_map=None):
    inquire = {
        "round": 10, "phase": phase,
        "players": [{
            "playerId": PID, "teamId": "RED", "state": state, "currentNodeId": node,
            "verified": verified, "delivered": delivered, "goodFruit": good, "freshness": fresh,
        }],
        "events": events or [],
    }
    return WorldState(inquire, PID, game_map)


class TestBaselineStrategy(unittest.TestCase):
    def setUp(self):
        self.ctx = GameContext(PID, "RED", 0, START_DATA)
        self.engine = DecisionEngine(self.ctx)
        self.gm = self.ctx.game_map

    def act(self, world):
        acts = self.engine.decide(world)
        return acts[0] if acts else None

    def test_advance_from_start(self):
        a = self.act(make_world("S01", game_map=self.gm))
        self.assertEqual(a, {"action": "MOVE", "targetNodeId": "S02"})

    def test_process_at_process_node(self):
        a = self.act(make_world("S02", game_map=self.gm))
        self.assertEqual(a, {"action": "PROCESS"})

    def test_advance_after_process_complete(self):
        # 先在 S02 未处理 → PROCESS
        self.assertEqual(self.act(make_world("S02", game_map=self.gm)), {"action": "PROCESS"})
        # 收到 PROCESS_COMPLETE 后 → 前进到 S14
        w = make_world("S02", game_map=self.gm,
                       events=[{"type": "PROCESS_COMPLETE", "payload": {"playerId": PID, "nodeId": "S02"}}])
        self.assertEqual(self.act(w), {"action": "MOVE", "targetNodeId": "S14"})

    def test_gate_wait_when_not_rush(self):
        a = self.act(make_world("S14", phase="NORMAL", game_map=self.gm))
        self.assertIsNone(a)  # 普通阶段停在宫门等待

    def test_gate_verify_when_rush(self):
        a = self.act(make_world("S14", phase="RUSH", verified=False, game_map=self.gm))
        # M8: RUSH 阶段验核自动绑定破关令（加速 6→3 帧）
        self.assertEqual(a, {"action": "VERIFY_GATE", "rushTactic": "BREAK_ORDER"})

    def test_gate_advance_after_verified(self):
        a = self.act(make_world("S14", phase="RUSH", verified=True, game_map=self.gm))
        self.assertEqual(a, {"action": "MOVE", "targetNodeId": "S15"})

    def test_deliver_at_terminal(self):
        a = self.act(make_world("S15", verified=True, good=50, fresh=80.0, game_map=self.gm))
        self.assertEqual(a, {"action": "DELIVER"})

    def test_terminal_unverified_returns_to_gate(self):
        a = self.act(make_world("S15", verified=False, game_map=self.gm))
        self.assertEqual(a, {"action": "MOVE", "targetNodeId": "S14"})

    def test_delivered_only_heartbeat(self):
        self.assertIsNone(self.act(make_world("S15", delivered=True, verified=True, game_map=self.gm)))

    def test_moving_state_heartbeat(self):
        self.assertIsNone(self.act(make_world("S01", state="MOVING", game_map=self.gm)))


if __name__ == "__main__":
    unittest.main()
