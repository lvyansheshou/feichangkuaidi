"""对手策略：AggressiveGuard — 激进设卡型。

在必经节点（chokepoint）设最大防守值设卡，积极守窗。
用于测试我方突破能力和窗口反制能力。
"""

import os
import sys
_PROJ = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_PROJ, "client"))

from protocol.enums import Action, PlayerState, Card, ResourceType
from protocol import actions as act
from strategy.opponent_model import _has_move_buff


_IDLE_LIKE = (PlayerState.IDLE, PlayerState.COST_BANKRUPT, None)


class AggressiveGuard:
    """激进设卡：必经节点设最大防守值设卡 + 窗口最优出牌 + 最短路推进。"""

    name = "AggressiveGuard"

    def __init__(self, player_id=1001, team_id="RED"):
        self.player_id = player_id
        self.team_id = team_id
        self._processed = set()
        self._guards_set = 0            # 已设卡数（上限 2）
        self._window_beat = 0           # 当前窗口拍数

    def decide(self, world):
        me = world.me
        gm = world.game_map
        if me is None or gm is None or me.delivered:
            return []

        node = me.current_node_id
        terminal = gm.terminal_nodes[0] if gm.terminal_nodes else None
        gate = gm.gate_node

        # ── 窗口出牌（积极） ──
        contests = world.my_contests()
        if contests:
            cid = contests[0].get("contestId")
            if cid:
                card = self._best_card(me)
                return [act.window_card(cid, card)]

        if me.state not in _IDLE_LIKE:
            return []

        # ── 终点 ──
        if terminal and node == terminal:
            if me.verified and me.good_fruit > 0 and me.freshness > 0:
                return [act.deliver()]
            return []

        # ── 宫门 ──
        if gate and node == gate:
            if not me.verified and world.is_rush:
                return [act.verify_gate()]
            return []

        # ── 固定处理 ──
        if node in gm.process_nodes and node not in self._processed:
            self._processed.add(node)
            return [act.process()]

        # ── ★ 核心：在必经节点设卡 ──
        if self._guards_set < 2 and gm.is_chokepoint(node) and node != gate:
            ns = world.node(node)
            if not (ns and ns.guard and (ns.guard.get("defense", 0) or 0) > 0):
                extra = 2 if (gm.node(node) and gm.node(node).type == "KEY_PASS" and me.good_fruit >= 22) else 1
                self._guards_set += 1
                return [act.set_guard(node, extra_good_fruit=extra)]

        # ── 最短路推进 ──
        if terminal:
            blocked = set()
            for nid, ns in world.node_states.items():
                if ns.has_obstacle or ns.active_guard_owner():
                    blocked.add(nid)
            path, _ = gm.time_optimal_path(node, terminal, blocked=blocked)
            if not path:
                # 无法绕行 → 突破
                path, _ = gm.time_optimal_path(node, terminal)
                if path and len(path) > 1:
                    nxt = path[1]
                    ns = world.node(nxt)
                    if ns and ns.has_obstacle:
                        return [act.clear(nxt)]
                    if ns and ns.active_guard_owner():
                        defense = (ns.guard or {}).get("defense", 0)
                        if me.good_fruit >= 3 and me.bad_fruit >= 1:
                            return [act.break_guard(nxt, good_fruit=2, bad_fruit=1)]
                        return [act.forced_pass(nxt)]
            if path and len(path) > 1:
                return [act.move(path[1])]

        return []

    def _best_card(self, me):
        """最优出牌：BING_ZHENG > XIAN_GONG > YAN_DIE > QIANG_XING > ABSTAIN"""
        if (me.guard_action_point or 0) > 0:
            return Card.BING_ZHENG
        if me.freshness >= 80 and me.good_fruit > 2:
            return Card.XIAN_GONG
        if (me.resource_count(ResourceType.PASS_TOKEN) > 0
                or me.resource_count(ResourceType.OFFICIAL_PERMIT) > 0):
            return Card.YAN_DIE
        if _has_move_buff(me):
            return Card.QIANG_XING
        return Card.ABSTAIN
