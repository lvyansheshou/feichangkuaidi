"""M7 能力补全单测：拒绝反馈/情报/绕行-清障权衡/绕路做任务/防御性小分队/主动设卡(flag)。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from core.game_map import GameMap  # noqa: E402
from core.world_state import WorldState  # noqa: E402
from strategy.decision import DecisionEngine, GameContext  # noqa: E402

PID = 1001


def _map(nodes, edges, roles=None, process=None):
    return {
        "matchId": "a", "durationRound": 600,
        "nodes": nodes, "edges": edges,
        "map": {"gameplay": {"roles": roles or {}}},
        "processNodes": process or [],
    }


def _node(nid, typ="STATION", start=False, terminal=False):
    return {"nodeId": nid, "type": typ, "x": 0, "y": 0, "start": start, "terminal": terminal}


def _edge(a, b, dist=10, rt="ROAD"):
    return {"fromNodeId": a, "toNodeId": b, "routeType": rt, "distance": dist, "bidirectional": True}


def world(sd, gm, node, state="IDLE", phase="NORMAL", verified=False, delivered=False,
          freshness=100.0, good=100, bad=0, resources=None, buffs=None, rush_used=0,
          nodes=None, tasks=None, contests=None, squad=0, gp=4, rnd=20):
    inquire = {
        "round": rnd, "phase": phase,
        "players": [{"playerId": PID, "teamId": "RED", "state": state, "currentNodeId": node,
                     "verified": verified, "delivered": delivered, "goodFruit": good, "badFruit": bad,
                     "freshness": freshness, "resources": resources or {}, "buffs": buffs or [],
                     "rushTacticUsedCount": rush_used, "squadAvailable": squad, "guardActionPoint": gp}],
        "nodes": nodes or [], "tasks": tasks or [], "contests": contests or [], "events": [],
    }
    return WorldState(inquire, PID, gm)


# 线性 S01-SA(处理点)-S14-S15
LINEAR = _map(
    [_node("S01", "START", start=True), _node("SA"), _node("S14", "GATE"), _node("S15", "FINISH", terminal=True)],
    [_edge("S01", "SA"), _edge("SA", "S14"), _edge("S14", "S15")],
    {"startNodeId": "S01", "gateNodeId": "S14", "terminalNodeIds": ["S15"]},
    [{"nodeId": "SA", "processType": "TRANSFER", "processRound": 4}],
)

# Y 型：S01→(SL|SR)→SJ→S15
YMAP = _map(
    [_node("S01", "START", start=True), _node("SL"), _node("SR"), _node("SJ"), _node("S15", "FINISH", terminal=True)],
    [_edge("S01", "SL"), _edge("S01", "SR"), _edge("SL", "SJ"), _edge("SR", "SJ"), _edge("SJ", "S15")],
    {"startNodeId": "S01", "terminalNodeIds": ["S15"]},
)


class TestRejectionFeedback(unittest.TestCase):
    def test_process_required_forces_process(self):
        gm = GameMap(LINEAR)
        eng = DecisionEngine(GameContext(PID, "RED", 0, LINEAR))
        eng._last_main_action = {"action": "MOVE", "targetNodeId": "S14"}
        eng._processed_here = True  # 误以为已处理
        w = world(LINEAR, gm, "SA", rnd=21, nodes=[{"nodeId": "SA"}])
        w.action_results = [{"playerId": PID, "round": 20, "action": "MOVE",
                             "accepted": False, "errorCode": "PROCESS_REQUIRED"}]
        a = eng.decide(w)[0]
        self.assertEqual(a, {"action": "PROCESS"})

    def test_move_block_cooldowns_target_and_reroutes(self):
        gm = GameMap(YMAP)
        eng = DecisionEngine(GameContext(PID, "RED", 0, YMAP))
        eng._last_main_action = {"action": "MOVE", "targetNodeId": "SL"}
        w = world(YMAP, gm, "S01", rnd=21)
        w.action_results = [{"playerId": PID, "round": 20, "action": "MOVE",
                             "accepted": False, "errorCode": "MOVE_BLOCKED_BY_GUARD"}]
        a = eng.decide(w)[0]
        self.assertEqual(a, {"action": "MOVE", "targetNodeId": "SR"})  # 绕开被拉黑的 SL


class TestIntel(unittest.TestCase):
    def test_use_intel_on_gate_within_range(self):
        gm = GameMap(LINEAR)  # SA→S14 距离 10 ≤15
        eng = DecisionEngine(GameContext(PID, "RED", 0, LINEAR))
        eng._stay_node = "SA"
        eng._processed_here = True  # SA 已处理，进入后续
        w = world(LINEAR, gm, "SA", resources={"INTEL": 1}, nodes=[{"nodeId": "SA"}])
        a = eng.decide(w)[0]
        self.assertEqual(a, {"action": "USE_RESOURCE", "resourceType": "INTEL", "targetNodeId": "S14"})

    def test_no_intel_when_out_of_range(self):
        far = _map(
            [_node("S01", "START", start=True), _node("S14", "GATE"), _node("S15", "FINISH", terminal=True)],
            [_edge("S01", "S14", dist=20), _edge("S14", "S15")],  # S01→S14 距离 20 >15
            {"startNodeId": "S01", "gateNodeId": "S14", "terminalNodeIds": ["S15"]},
        )
        gm = GameMap(far)
        eng = DecisionEngine(GameContext(PID, "RED", 0, far))
        w = world(far, gm, "S01", resources={"INTEL": 1})
        a = eng.decide(w)[0]
        self.assertNotEqual(a.get("action"), "USE_RESOURCE")


class TestRerouteVsClear(unittest.TestCase):
    def test_clear_when_reroute_far_more_costly(self):
        # 经 SL 很短(各10)，经 SR 很长(各60)；SL 有障碍 → 就地清障 SL 比绕远 SR 便宜
        m = _map(
            [_node("S01", "START", start=True), _node("SL"), _node("SR"), _node("SJ"),
             _node("S15", "FINISH", terminal=True)],
            [_edge("S01", "SL", 10), _edge("SL", "SJ", 10),
             _edge("S01", "SR", 60), _edge("SR", "SJ", 60), _edge("SJ", "S15", 10)],
            {"startNodeId": "S01", "terminalNodeIds": ["S15"]},
        )
        gm = GameMap(m)
        eng = DecisionEngine(GameContext(PID, "RED", 0, m))
        w = world(m, gm, "S01", good=100, nodes=[{"nodeId": "SL", "hasObstacle": True}])
        a = eng.decide(w)[0]
        self.assertEqual(a, {"action": "CLEAR", "targetNodeId": "SL"})


class TestTaskDetour(unittest.TestCase):
    def test_detour_to_offroute_task(self):
        # 菱形：直达经 SA(先定义→默认路径)，任务在 ST(等距旁路) → 绕去 ST 只多 processRound
        m = _map(
            [_node("S01", "START", start=True), _node("SA"), _node("ST"), _node("SJ"),
             _node("S15", "FINISH", terminal=True)],
            [_edge("S01", "SA"), _edge("SA", "SJ"), _edge("S01", "ST"), _edge("ST", "SJ"), _edge("SJ", "S15")],
            {"startNodeId": "S01", "terminalNodeIds": ["S15"]},
        )
        gm = GameMap(m)
        eng = DecisionEngine(GameContext(PID, "RED", 0, m))
        tasks = [{"taskId": "TK", "taskTemplateId": "T01", "nodeId": "ST", "processRound": 3,
                  "active": True, "completed": False}]
        w = world(m, gm, "S01", tasks=tasks)
        a = eng.decide(w)[0]
        self.assertEqual(a, {"action": "MOVE", "targetNodeId": "ST"})  # 绕去任务节点

    def test_no_detour_when_task_score_enough(self):
        # 与上同图，但任务分已≥90：不绕路，直达（走默认 SA）
        m = _map(
            [_node("S01", "START", start=True), _node("SA"), _node("ST"), _node("SJ"),
             _node("S15", "FINISH", terminal=True)],
            [_edge("S01", "SA"), _edge("SA", "SJ"), _edge("S01", "ST"), _edge("ST", "SJ"), _edge("SJ", "S15")],
            {"startNodeId": "S01", "terminalNodeIds": ["S15"]},
        )
        gm = GameMap(m)
        eng = DecisionEngine(GameContext(PID, "RED", 0, m))
        tasks = [{"taskId": "TK", "taskTemplateId": "T01", "nodeId": "ST", "processRound": 3,
                  "active": True, "completed": False}]
        w = world(m, gm, "S01", tasks=tasks)
        w.me.task_score = 90
        a = eng.decide(w)[0]
        self.assertEqual(a["targetNodeId"], "SA")


class TestDefensiveSquad(unittest.TestCase):
    def _chain(self):
        return _map(
            [_node("S01", "START", start=True), _node("SB"), _node("SC"), _node("S15", "FINISH", terminal=True)],
            [_edge("S01", "SB"), _edge("SB", "SC"), _edge("SC", "S15")],
            {"startNodeId": "S01", "terminalNodeIds": ["S15"]},
        )

    def test_squad_clear_obstacle_ahead(self):
        m = self._chain()
        gm = GameMap(m)
        eng = DecisionEngine(GameContext(PID, "RED", 0, m))
        w = world(m, gm, "S01", squad=8, nodes=[{"nodeId": "SC", "hasObstacle": True}])
        acts = eng.decide(w)
        self.assertIn({"action": "SQUAD_CLEAR", "targetNodeId": "SC"}, acts)

    def test_squad_weaken_guard_ahead(self):
        m = self._chain()
        gm = GameMap(m)
        eng = DecisionEngine(GameContext(PID, "RED", 0, m))
        w = world(m, gm, "S01", squad=8,
                  nodes=[{"nodeId": "SC", "guard": {"ownerTeamId": "BLUE", "defense": 4, "active": True}}])
        acts = eng.decide(w)
        self.assertIn({"action": "SQUAD_WEAKEN", "targetNodeId": "SC"}, acts)


class TestOffensiveGuardFlag(unittest.TestCase):
    def _keypass_map(self):
        return _map(
            [_node("S01", "START", start=True), _node("SK", "KEY_PASS"),
             _node("S14", "GATE"), _node("S15", "FINISH", terminal=True)],
            [_edge("S01", "SK"), _edge("SK", "S14"), _edge("S14", "S15")],
            {"startNodeId": "S01", "gateNodeId": "S14", "terminalNodeIds": ["S15"]},
        )

    def test_set_guard_only_when_enabled(self):
        m = self._keypass_map()
        gm = GameMap(m)
        eng = DecisionEngine(GameContext(PID, "RED", 0, m))
        w = world(m, gm, "SK", good=100)
        # 默认关闭：不设卡
        self.assertNotEqual(eng.decide(w)[0].get("action"), "SET_GUARD")
        # 开启后：在关键关隘设卡
        old = config.ENABLE_OFFENSIVE
        config.ENABLE_OFFENSIVE = True
        try:
            eng2 = DecisionEngine(GameContext(PID, "RED", 0, m))
            # M8: 关键关隘投入 2 好果（防守值=6，风化延迟45帧）
            self.assertEqual(eng2.decide(world(m, gm, "SK", good=100))[0],
                             {"action": "SET_GUARD", "targetNodeId": "SK", "extraGoodFruit": 2})
        finally:
            config.ENABLE_OFFENSIVE = old


if __name__ == "__main__":
    unittest.main()
