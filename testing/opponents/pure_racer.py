"""对手策略：PureRacer — 纯竞速基线。

只做最短路 + 固定处理 + 交付，不做任务、不设卡、不收集资源。
用于测试我方策略相对于"简单竞速"的胜率提升。
"""

import os
import sys
_PROJ = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_PROJ, "client"))

from protocol.enums import Action, PlayerState
from protocol import actions as act


_IDLE_LIKE = (PlayerState.IDLE, PlayerState.COST_BANKRUPT, None)


class PureRacer:
    """纯竞速：最短路 → 固定处理 → 宫门验核 → 交付。"""

    name = "PureRacer"

    def __init__(self, player_id=1001, team_id="RED"):
        self.player_id = player_id
        self.team_id = team_id
        self._processed = set()

    def decide(self, world):
        me = world.me
        gm = world.game_map
        if me is None or gm is None or me.delivered:
            return []

        node = me.current_node_id
        terminal = gm.terminal_nodes[0] if gm.terminal_nodes else None
        gate = gm.gate_node

        # 窗口出牌：只弃权
        contests = world.my_contests()
        if contests:
            cid = contests[0].get("contestId")
            if cid:
                return [act.window_card(cid, "ABSTAIN")]

        if me.state not in _IDLE_LIKE:
            return []

        # 终点
        if terminal and node == terminal:
            if me.verified and me.good_fruit > 0 and me.freshness > 0:
                return [act.deliver()]
            if not me.verified and gate:
                path, _ = gm.time_optimal_path(node, gate)
                if path and len(path) > 1:
                    return [act.move(path[1])]
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

        # 最短路推进
        if terminal:
            blocked = set()
            for nid, ns in world.node_states.items():
                if ns.has_obstacle or ns.active_guard_owner():
                    blocked.add(nid)
            path, _ = gm.time_optimal_path(node, terminal, blocked=blocked)
            if path and len(path) > 1:
                return [act.move(path[1])]

        return []
