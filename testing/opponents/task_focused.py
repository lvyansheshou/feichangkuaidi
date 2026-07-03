"""对手策略：TaskFocused — 任务优先型。

积极做任务追求 90 分阈值，顺路收集资源，不设卡。
用于测试我方控场对任务型对手的压制效果。
"""

import os
import sys
_PROJ = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_PROJ, "client"))

from protocol.enums import Action, PlayerState, Card, ResourceType
from protocol import actions as act

_IDLE_LIKE = (PlayerState.IDLE, PlayerState.COST_BANKRUPT, None)
_INF = float("inf")


class TaskFocused:
    """任务优先：追求 90 任务分阈值 + 资源收集 + 不设卡。"""

    name = "TaskFocused"

    def __init__(self, player_id=1001, team_id="RED"):
        self.player_id = player_id
        self.team_id = team_id
        self._processed = set()
        self._gate_scout_sent = False

    def decide(self, world):
        me = world.me
        gm = world.game_map
        if me is None or gm is None or me.delivered:
            return []

        node = me.current_node_id
        terminal = gm.terminal_nodes[0] if gm.terminal_nodes else None
        gate = gm.gate_node

        # 窗口：弃权（任务型不争窗口）
        contests = world.my_contests()
        if contests:
            cid = contests[0].get("contestId")
            if cid:
                return [act.window_card(cid, Card.ABSTAIN)]

        if me.state not in _IDLE_LIKE:
            return []

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

        # ★ 核心：优先做当前节点的任务
        task = self._best_task_at_node(world, me, node)
        if task:
            return [act.claim_task(task["taskId"])]

        # 冰鉴保鲜
        if me.resource_count(ResourceType.ICE_BOX) > 0 and me.freshness < 80:
            return [act.use_resource(ResourceType.ICE_BOX)]

        # 收集资源
        claim = self._claim_best_resource(world, me, node)
        if claim:
            return [claim]

        # 小分队探路宫门
        squad = self._maybe_scout_gate(world, me, gm, node)
        if squad:
            return [squad]

        # 绕路做任务（任务分 < 90）
        if (me.task_score or 0) < 90:
            detour = self._task_detour(world, me, gm, node, terminal)
            if detour:
                return self._advance(world, me, gm, node, detour, terminal)

        # 最短路推进
        return self._advance(world, me, gm, node, terminal, terminal)

    def _best_task_at_node(self, world, me, node):
        """当前节点可做的最优任务（高分优先）。"""
        best = None
        best_score = 0
        for t in world.active_tasks():
            if t.get("nodeId") != node:
                continue
            if t.get("taskTemplateId") in ("T04", "T06"):
                continue
            score = t.get("baseScore", 0)
            if score > best_score:
                best = t
                best_score = score
        return best

    def _claim_best_resource(self, world, me, node):
        """收集最有用的资源。"""
        ns = world.node(node)
        if ns is None:
            return None
        # 优先级：快马 > 冰鉴 > 短程马 > 过所 > 官凭 > 情报
        priority = ["FAST_HORSE", "ICE_BOX", "SHORT_HORSE",
                    "PASS_TOKEN", "OFFICIAL_PERMIT", "INTEL"]
        for rt in priority:
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

    def _task_detour(self, world, me, gm, node, terminal):
        """寻找值得绕路的任务节点。"""
        if not terminal:
            return None
        _, direct = gm.time_optimal_path(node, terminal)
        if direct == _INF:
            return None
        pid = self.player_id
        best, best_extra = None, _INF
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
            extra = (c1 + t.get("processRound", 4) + c2) - direct
            if 0 <= extra <= 80 and extra < best_extra:
                best, best_extra = tn, extra
        return best

    def _advance(self, world, me, gm, src, dst, terminal):
        """推进移动。"""
        blocked = set()
        for nid, ns in world.node_states.items():
            if ns.has_obstacle or ns.active_guard_owner():
                blocked.add(nid)
        path, _ = gm.time_optimal_path(src, dst, blocked=blocked)
        if not path:
            path, _ = gm.time_optimal_path(src, dst)
        if path and len(path) > 1:
            nxt = path[1]
            ns = world.node(nxt)
            if ns and ns.has_obstacle:
                if me.good_fruit > 1:
                    return [act.clear(nxt)]
                return [act.forced_pass(nxt)]
            if ns and ns.active_guard_owner():
                return [act.forced_pass(nxt)]
            return [act.move(nxt)]
        return []
