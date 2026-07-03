"""M5 对抗策略单测：阻塞路由/突破/窗口出牌/终局急策/小分队探路。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.game_map import GameMap  # noqa: E402
from core.world_state import WorldState  # noqa: E402
from strategy.decision import DecisionEngine, GameContext  # noqa: E402

PID = 1001

# 线性地图：S01 - SB - S14(宫门) - S15(终点)。SB 为终段唯一通路（不可绕行）。
LINEAR = {
    "matchId": "c", "durationRound": 600,
    "nodes": [
        {"nodeId": "S01", "type": "START", "x": 0, "y": 0, "start": True},
        {"nodeId": "SB", "type": "STATION", "x": 1, "y": 0},
        {"nodeId": "S14", "type": "GATE", "x": 2, "y": 0},
        {"nodeId": "S15", "type": "FINISH", "x": 3, "y": 0, "terminal": True},
    ],
    "edges": [
        {"fromNodeId": "S01", "toNodeId": "SB", "routeType": "ROAD", "distance": 20, "bidirectional": True},
        {"fromNodeId": "SB", "toNodeId": "S14", "routeType": "ROAD", "distance": 20, "bidirectional": True},
        {"fromNodeId": "S14", "toNodeId": "S15", "routeType": "ROAD", "distance": 20, "bidirectional": True},
    ],
    "map": {"gameplay": {"roles": {"startNodeId": "S01", "gateNodeId": "S14", "terminalNodeIds": ["S15"]}}},
    "processNodes": [],
}

# Y 型可绕行地图：S01 → (SL 或 SR) → SJ → S15。SL/SR 互为备选。
YMAP = {
    "matchId": "y", "durationRound": 600,
    "nodes": [
        {"nodeId": "S01", "type": "START", "x": 0, "y": 0, "start": True},
        {"nodeId": "SL", "type": "STATION", "x": 1, "y": 1},
        {"nodeId": "SR", "type": "STATION", "x": 1, "y": -1},
        {"nodeId": "SJ", "type": "STATION", "x": 2, "y": 0},
        {"nodeId": "S15", "type": "FINISH", "x": 3, "y": 0, "terminal": True},
    ],
    "edges": [
        {"fromNodeId": "S01", "toNodeId": "SL", "routeType": "ROAD", "distance": 20, "bidirectional": True},
        {"fromNodeId": "S01", "toNodeId": "SR", "routeType": "ROAD", "distance": 20, "bidirectional": True},
        {"fromNodeId": "SL", "toNodeId": "SJ", "routeType": "ROAD", "distance": 20, "bidirectional": True},
        {"fromNodeId": "SR", "toNodeId": "SJ", "routeType": "ROAD", "distance": 20, "bidirectional": True},
        {"fromNodeId": "SJ", "toNodeId": "S15", "routeType": "ROAD", "distance": 20, "bidirectional": True},
    ],
    "map": {"gameplay": {"roles": {"startNodeId": "S01", "terminalNodeIds": ["S15"]}}},
    "processNodes": [],
}


def world(start_data, node="S01", state="IDLE", phase="NORMAL", verified=False, delivered=False,
          freshness=100.0, good=100, bad=0, resources=None, buffs=None, rush_used=0,
          nodes=None, tasks=None, contests=None, squad=0, gp=4, gm=None, rnd=20):
    inquire = {
        "round": rnd, "phase": phase,
        "players": [{"playerId": PID, "teamId": "RED", "state": state, "currentNodeId": node,
                     "verified": verified, "delivered": delivered, "goodFruit": good, "badFruit": bad,
                     "freshness": freshness, "resources": resources or {}, "buffs": buffs or [],
                     "rushTacticUsedCount": rush_used, "squadAvailable": squad, "guardActionPoint": gp}],
        "nodes": nodes or [], "tasks": tasks or [], "contests": contests or [], "events": [],
    }
    return WorldState(inquire, PID, gm)


def obstacle_node(nid):
    return {"nodeId": nid, "hasObstacle": True, "obstacleType": "ROCKFALL"}


def guard_node(nid, owner="BLUE", defense=4):
    return {"nodeId": nid, "guard": {"ownerTeamId": owner, "defense": defense, "active": True}}


class TestBlockRouting(unittest.TestCase):
    def test_reroute_around_obstacle(self):
        gm = GameMap(YMAP)
        eng = DecisionEngine(GameContext(PID, "RED", 0, YMAP))
        # 默认最短会经 SL（先定义），障碍放 SL → 应绕行经 SR
        w = world(YMAP, node="S01", nodes=[obstacle_node("SL")], gm=gm)
        a = eng.decide(w)[0]
        self.assertEqual(a, {"action": "MOVE", "targetNodeId": "SR"})

    def test_path_avoids_blocked(self):
        gm = GameMap(YMAP)
        path, _ = gm.time_optimal_path("S01", "S15", blocked={"SL"})
        self.assertNotIn("SL", path)
        self.assertEqual(path[-1], "S15")


class TestBreakthroughObstacle(unittest.TestCase):
    def setUp(self):
        self.gm = GameMap(LINEAR)

    def eng(self):
        return DecisionEngine(GameContext(PID, "RED", 0, LINEAR))

    def test_clear_when_unavoidable(self):
        w = world(LINEAR, node="S01", nodes=[obstacle_node("SB")], gm=self.gm, good=100)
        a = self.eng().decide(w)[0]
        self.assertEqual(a, {"action": "CLEAR", "targetNodeId": "SB"})

    def test_t04_preferred_when_available(self):
        tasks = [{"taskId": "TZ", "taskTemplateId": "T04", "nodeId": "SB", "processRound": 6,
                  "active": True, "completed": False}]
        w = world(LINEAR, node="S01", nodes=[obstacle_node("SB")], tasks=tasks, gm=self.gm)
        a = self.eng().decide(w)[0]
        self.assertEqual(a, {"action": "CLAIM_TASK", "taskId": "TZ"})

    def test_forced_pass_when_low_fruit(self):
        w = world(LINEAR, node="S01", nodes=[obstacle_node("SB")], gm=self.gm, good=1)  # 好果不足以清障
        a = self.eng().decide(w)[0]
        self.assertEqual(a, {"action": "FORCED_PASS", "targetNodeId": "SB"})


class TestBreakthroughGuard(unittest.TestCase):
    def setUp(self):
        self.gm = GameMap(LINEAR)

    def eng(self):
        return DecisionEngine(GameContext(PID, "RED", 0, LINEAR))

    def test_break_guard_with_min_fruit(self):
        w = world(LINEAR, node="S01", nodes=[guard_node("SB", "BLUE", 4)], gm=self.gm, good=100)
        a = self.eng().decide(w)[0]
        self.assertEqual(a["action"], "BREAK_GUARD")
        self.assertEqual(a["targetNodeId"], "SB")
        self.assertGreaterEqual(a.get("goodFruit", 0) * 2 + a.get("badFruit", 0) * 3, 4)

    def test_break_order_bound_in_rush(self):
        w = world(LINEAR, node="S01", phase="RUSH", nodes=[guard_node("SB", "BLUE", 7)], gm=self.gm,
                  good=100, rush_used=0)
        a = self.eng().decide(w)[0]
        self.assertEqual(a["action"], "BREAK_GUARD")
        self.assertEqual(a.get("rushTactic"), "BREAK_ORDER")  # 2好果(4)+破关令(3)=7

    def test_break_guard_max_fruit_cap(self):
        # §6.3.1: 好果各最多 2 篓 → 防4 可用 2 好果(4)攻破
        w = world(LINEAR, node="S01", nodes=[guard_node("SB", "BLUE", 4)], gm=self.gm, good=100, bad=0)
        a = self.eng().decide(w)[0]
        self.assertEqual(a["action"], "BREAK_GUARD")
        self.assertLessEqual(a.get("goodFruit", 0), 2)  # 不超上限

    def test_forced_pass_when_cannot_afford(self):
        # 防5, bad=0, max攻=2×2=4<5 → 强制通行
        w = world(LINEAR, node="S01", nodes=[guard_node("SB", "BLUE", 5)], gm=self.gm, good=100, bad=0)
        a = self.eng().decide(w)[0]
        self.assertEqual(a, {"action": "FORCED_PASS", "targetNodeId": "SB"})

    def test_forced_pass_when_truly_cannot_afford(self):
        # 防守值 7, bad=0, good 仅够保底 → max攻=(2-1)×2=2<7 → 强制通行
        w = world(LINEAR, node="S01", nodes=[guard_node("SB", "BLUE", 7)], gm=self.gm, good=3, bad=0)
        a = self.eng().decide(w)[0]
        self.assertEqual(a, {"action": "FORCED_PASS", "targetNodeId": "SB"})


class TestWindowCard(unittest.TestCase):
    def setUp(self):
        self.gm = GameMap(LINEAR)

    def test_bing_zheng_when_have_guard_points(self):
        c = [{"contestId": "C1", "redPlayerId": PID, "bluePlayerId": 2222, "resolved": False}]
        w = world(LINEAR, node="SB", state="CONTESTING", contests=c, gm=self.gm)
        a = DecisionEngine(GameContext(PID, "RED", 0, LINEAR)).decide(w)[0]
        self.assertEqual(a, {"action": "WINDOW_CARD", "contestId": "C1", "card": "BING_ZHENG"})


class TestEndgameTactic(unittest.TestCase):
    def setUp(self):
        self.gm = GameMap(LINEAR)

    def eng(self):
        return DecisionEngine(GameContext(PID, "RED", 0, LINEAR))

    def test_rush_protect_when_low_fresh(self):
        w = world(LINEAR, node="SB", phase="RUSH", freshness=80.0, gm=self.gm)
        self.assertEqual(self.eng().decide(w)[0], {"action": "RUSH_PROTECT"})

    def test_rush_speed_when_far_and_healthy_no_horse(self):
        # SB→S15 距离 40 > 30 判为"远"，鲜度健康且无马 → 疾行令
        w = world(LINEAR, node="SB", phase="RUSH", freshness=100.0, gm=self.gm)
        self.assertEqual(self.eng().decide(w)[0], {"action": "RUSH_SPEED"})

    def test_no_rush_speed_when_holding_horse(self):
        w = world(LINEAR, node="SB", phase="RUSH", freshness=100.0, resources={"FAST_HORSE": 1}, gm=self.gm)
        self.assertNotEqual(self.eng().decide(w)[0].get("action"), "RUSH_SPEED")


class TestSquadScout(unittest.TestCase):
    def test_scout_gate_when_near_and_have_squad(self):
        gm = GameMap(LINEAR)
        # 在 SB：到宫门 S14 帧数在 [8,40] 窗口内 → 派探路宫门
        w = world(LINEAR, node="SB", squad=8, gm=gm)
        acts = DecisionEngine(GameContext(PID, "RED", 0, LINEAR)).decide(w)
        squad = [a for a in acts if a["action"] == "SQUAD_SCOUT"]
        self.assertEqual(squad, [{"action": "SQUAD_SCOUT", "targetNodeId": "S14"}])

    def test_no_scout_when_no_squad(self):
        gm = GameMap(LINEAR)
        w = world(LINEAR, node="SB", squad=0, gm=gm)
        acts = DecisionEngine(GameContext(PID, "RED", 0, LINEAR)).decide(w)
        self.assertFalse([a for a in acts if a["action"] == "SQUAD_SCOUT"])


if __name__ == "__main__":
    unittest.main()
