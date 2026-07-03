"""对手策略：Balanced — 平衡型。

根据态势自适应：领先时设卡，落后时追任务，胶着时竞速。
模拟一个"有基本博弈意识"的对手。
"""

import os
import sys
_PROJ = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_PROJ, "client"))

from protocol.enums import Action, PlayerState, Card, ResourceType
from protocol import actions as act

_IDLE_LIKE = (PlayerState.IDLE, PlayerState.COST_BANKRUPT, None)
_INF = float("inf")


class Balanced:
    """平衡型：态势感知 + 设卡 + 任务 + 竞速。"""

    name = "Balanced"

    def __init__(self, player_id=1001, team_id="RED"):
        self.player_id = player_id
        self.team_id = team_id
        self._processed = set()
        self._guards_set = 0
        self._gate_scout_sent = False
        self._opp_visited = set()
        self._last_opp_node = None

    def decide(self, world):
        me = world.me
        gm = world.game_map
        if me is None or gm is None or me.delivered:
            return []

        # 追踪对手
        opp = world.opponent
        if opp and opp.current_node_id and opp.current_node_id != self._last_opp_node:
            self._opp_visited.add(opp.current_node_id)
            self._last_opp_node = opp.current_node_id

        node = me.current_node_id
        terminal = gm.terminal_nodes[0] if gm.terminal_nodes else None
        gate = gm.gate_node

        # 窗口出牌
        contests = world.my_contests()
        if contests:
            cid = contests[0].get("contestId")
            if cid:
                return [act.window_card(cid, self._pick_card(me))]

        if me.state not in _IDLE_LIKE:
            return []

        # 态势评估
        posture = self._assess(me, gm, terminal)

        # 终点
        if terminal and node == terminal:
            if me.verified and me.good_fruit > 0 and me.freshness > 0:
                return [act.deliver()]
            return []

        # 宫门
        if gate and node == gate:
            if not me.verified and world.is_rush:
                return [act.verify_gate()]
            return []

        # 固定处理
        if node in gm.process_nodes and node not in self._processed:
            self._processed.add(node)
            return [act.process()]

        # 冰鉴
        if me.resource_count(ResourceType.ICE_BOX) > 0 and me.freshness < 78:
            return [act.use_resource(ResourceType.ICE_BOX)]

        # ★ 领先时：在必经节点设卡
        if posture == "leading" and self._guards_set < 2:
            if gm.is_chokepoint(node) and node != gate and node not in gm.terminal_nodes:
                ns = world.node(node)
                if not (ns and ns.guard and (ns.guard.get("defense", 0) or 0) > 0):
                    extra = 2 if (gm.node(node) and gm.node(node).type == "KEY_PASS"
                                  and me.good_fruit >= 22) else 1
                    self._guards_set += 1
                    return [act.set_guard(node, extra_good_fruit=extra)]

        # ★ 落后时：激进做任务追分
        if posture == "trailing" and (me.task_score or 0) < 90:
            task = self._best_task_anywhere(world, me, gm, node, terminal)
            if task:
                return [act.claim_task(task["taskId"])]

        # 当前节点任务
        task = self._task_at_node(world, node)
        if task:
            return [act.claim_task(task["taskId"])]

        # 资源收集
        claim = self._claim_resource(world, me, node)
        if claim:
            return [claim]

        # 小分队探路
        squad = self._maybe_scout_gate(world, me, gm, node)
        if squad:
            return [squad]

        # 推进
        return self._advance(world, me, gm, node, terminal)

    def _assess(self, me, gm, terminal):
        """简化态势评估。"""
        if not terminal or not self._last_opp_node:
            return "racing"
        _, my_eta = gm.time_optimal_path(me.current_node_id, terminal)
        _, opp_eta = gm.time_optimal_path(self._last_opp_node, terminal)
        if my_eta == _INF or opp_eta == _INF:
            return "racing"
        lead = opp_eta - my_eta
        if lead > 40:
            return "leading"
        if lead < -30:
            return "trailing"
        return "racing"

    def _pick_card(self, me):
        if (me.guard_action_point or 0) > 0:
            return Card.BING_ZHENG
        if me.freshness >= 80 and me.good_fruit > 2:
            return Card.XIAN_GONG
        if (me.resource_count(ResourceType.PASS_TOKEN) > 0
                or me.resource_count(ResourceType.OFFICIAL_PERMIT) > 0):
            return Card.YAN_DIE
        return Card.ABSTAIN

    def _task_at_node(self, world, node):
        for t in world.active_tasks():
            if t.get("nodeId") == node and t.get("taskTemplateId") not in ("T04", "T06"):
                return t
        return None

    def _best_task_anywhere(self, world, me, gm, node, terminal):
        if not terminal:
            return None
        _, direct = gm.time_optimal_path(node, terminal)
        best, best_val = None, _INF
        for t in world.active_tasks():
            tn = t.get("nodeId")
            if not tn or tn == node:
                continue
            if t.get("taskTemplateId") in ("T04", "T06"):
                continue
            _, c1 = gm.time_optimal_path(node, tn)
            _, c2 = gm.time_optimal_path(tn, terminal)
            if c1 == _INF or c2 == _INF:
                continue
            extra = c1 + t.get("processRound", 4) + c2 - direct
            val = extra - t.get("baseScore", 0) * 2  # 分数价值 vs 时间成本
            if val < best_val and extra <= 100:
                best, best_val = t, val
        return best

    def _claim_resource(self, world, me, node):
        ns = world.node(node)
        if ns is None:
            return None
        for rt in ["FAST_HORSE", "ICE_BOX", "SHORT_HORSE", "INTEL"]:
            if ns.resource_available(rt):
                return act.claim_resource(node, rt)
        return None

    def _maybe_scout_gate(self, world, me, gm, node):
        if self._gate_scout_sent or (me.squad_available or 0) < 1:
            return None
        gate = gm.gate_node
        if not gate:
            return None
        _, frames = gm.time_optimal_path(node, gate)
        if frames == _INF or frames < 8 or frames > 40:
            return None
        self._gate_scout_sent = True
        return act.squad_scout(gate)

    def _advance(self, world, me, gm, node, terminal):
        if not terminal:
            return []
        blocked = set()
        for nid, ns in world.node_states.items():
            if ns.has_obstacle or ns.active_guard_owner():
                blocked.add(nid)
        path, _ = gm.time_optimal_path(node, terminal, blocked=blocked)
        if not path:
            path, _ = gm.time_optimal_path(node, terminal)
        if path and len(path) > 1:
            nxt = path[1]
            ns = world.node(nxt)
            if ns and ns.has_obstacle and me.good_fruit > 1:
                return [act.clear(nxt)]
            if ns and ns.active_guard_owner() and ns.active_guard_owner() != me.team_id:
                return [act.forced_pass(nxt)]
            return [act.move(nxt)]
        return []
