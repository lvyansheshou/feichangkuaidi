"""对手建模与博弈策略。

追踪对手状态、预测行为模式、评估相对态势，支撑自适应决策：
- 窗口出牌反制（基于对手历史出牌模式）
- 条件设卡（仅在领先且在必经节点时触发）
- 态势感知（领先/落后/持平 → 不同策略姿态）
"""

import config
from protocol.enums import Card, PlayerState

# 窗口牌克制表（任务书 §5.4.4）：card -> 可击败的牌集合
_CARD_COUNTERS = {
    Card.BING_ZHENG:  {Card.YAN_DIE, Card.QIANG_XING},
    Card.XIAN_GONG:   {Card.YAN_DIE, Card.BING_ZHENG},
    Card.QIANG_XING:  {Card.XIAN_GONG},
    Card.YAN_DIE:     {Card.QIANG_XING},
}

# 被克制表（反向）：card -> 克制此牌的牌集合
_CARD_COUNTERED_BY = {
    Card.BING_ZHENG:  {Card.XIAN_GONG},
    Card.XIAN_GONG:   {Card.QIANG_XING, Card.YAN_DIE},
    Card.QIANG_XING:  {Card.BING_ZHENG, Card.YAN_DIE},
    Card.YAN_DIE:     {Card.BING_ZHENG, Card.XIAN_GONG},
}


class OpponentModel:
    """对手状态追踪与行为预测。

    每帧由 DecisionEngine 调用 update()，然后各子策略读取模型做决策。
    """

    def __init__(self):
        # 基础轨迹
        self.visited_nodes = []          # [(nodeId, round), ...] 按时间排序
        self.last_node = None
        self.last_state = None
        self.last_round_seen = 0

        # 窗口出牌历史：[(contestType, round, their_card, beat), ...]
        self.window_history = []

        # 设卡记录：对手在哪些节点设了卡
        self.guard_nodes = set()

        # 资产快照（最近一次 observe）
        self.good_fruit = 100
        self.bad_fruit = 0
        self.freshness = 100.0
        self.task_score = 0
        self.squad_available = 8
        self.guard_action_point = 4
        self.delivered = False
        self.rush_tactic_used = False

        # 估算
        self.estimated_path = None       # 预测对手去终点的路径 (list of nodeId)
        self.estimated_eta_s10 = None    # 预计到达 S10 的帧数

        # 态势
        self.lead_frames = 0             # 正=我方领先帧数，负=落后
        self.posture = "racing"          # racing / leading / trailing / contested / sprinting

        # 资源消耗追踪
        self.guard_points_used = 0       # 已用护卫行动点
        self.squad_used = 0              # 已用小分队人手
        self.rush_tactic_used_flag = False
        self.ice_boxes_used = 0          # 估算已用冰鉴数

    # ── 每帧更新 ──

    def update(self, world):
        """从 WorldState 更新对手模型。"""
        opp = world.opponent
        if opp is None:
            return

        rnd = world.round or 0

        # 轨迹
        if opp.current_node_id and opp.current_node_id != self.last_node:
            self.visited_nodes.append((opp.current_node_id, rnd))
            self.last_node = opp.current_node_id
        self.last_state = opp.state
        self.last_round_seen = rnd

        # 资产
        self.good_fruit = opp.good_fruit
        self.bad_fruit = opp.bad_fruit
        self.freshness = opp.freshness
        self.task_score = opp.task_score or 0
        self.squad_available = opp.squad_available or 0
        self.guard_action_point = opp.guard_action_point or 0
        self.delivered = opp.delivered
        self.rush_tactic_used = (opp.rush_tactic_used_count or 0) > 0

        # 资源消耗追踪（累计使用量）
        self.guard_points_used = max(self.guard_points_used,
                                     4 - (opp.guard_action_point or 0))
        self.squad_used = max(self.squad_used,
                              8 - (opp.squad_available or 0))
        # 冰鉴使用：鲜度突然 +10 → 使用了一次
        if hasattr(self, '_last_opp_freshness') and self._last_opp_freshness is not None:
            if opp.freshness - self._last_opp_freshness > 5:
                self.ice_boxes_used += 1
        self._last_opp_freshness = opp.freshness

        # 检测对手设卡
        for nid, ns in world.node_states.items():
            owner = ns.active_guard_owner()
            if owner and owner != world.me.team_id:
                self.guard_nodes.add(nid)

        # 窗口出牌记录
        for c in (world.contests or []):
            if c.get("resolved"):
                self._record_window_result(c, world)

    def _record_window_result(self, contest, world):
        """从已结算窗口中提取对手出牌信息。

        协议字段: contest.cards = {teamId: card, ...}, contest.winnerTeamId
        """
        pid = world.player_id
        my_team = world.me.team_id if world.me else None
        if not my_team:
            return

        # 找出对手 teamId
        red_team = contest.get("redTeamId") or (
            "RED" if contest.get("redPlayerId") == pid else None)
        blue_team = contest.get("blueTeamId") or (
            "BLUE" if contest.get("bluePlayerId") == pid else None)
        if not red_team:
            red_team = "RED"
        if not blue_team:
            blue_team = "BLUE"
        opp_team = red_team if red_team != my_team else blue_team

        # 从 cards dict 提取对手出牌
        cards = contest.get("cards") or {}
        their_card = cards.get(opp_team)
        if their_card:
            winner = contest.get("winnerTeamId")
            self.window_history.append({
                "contestType": contest.get("contestType"),
                "round": world.round,
                "card": their_card,
                "won": winner == opp_team,
            })

    # ── 路径预测 ──

    def estimate_opponent_path(self, gm):
        """预测对手到终点的路径。

        优先假设对手也是 time-optimal 的（和我们一样走山路），
        但如果对手已走过官道节点，则按官道路径预测。
        """
        if not self.last_node or not gm.terminal_nodes:
            return None

        terminal = gm.terminal_nodes[0]

        # 如果对手已走过 S02/S03/S04 等官道节点 → 官道
        road_nodes = {"S02", "S03", "S04", "S05", "S07", "S09"}
        mountain_nodes = {"S06", "S08"}

        visited_set = {n for n, _ in self.visited_nodes}
        if visited_set & road_nodes:
            # 对手走官道或水路
            path, _ = gm.shortest_path(self.last_node, terminal, metric="move")
        elif visited_set & mountain_nodes:
            # 对手走山路
            path, _ = gm.shortest_path(self.last_node, terminal, metric="move")
        else:
            # 未知，用 time_optimal（和我们一样的逻辑）
            path, _ = gm.time_optimal_path(self.last_node, terminal)

        self.estimated_path = path
        return path

    # ── ETA 估算 ──

    def estimate_eta_to(self, gm, target_node):
        """估算对手到达目标节点的剩余帧数。"""
        if not self.last_node:
            return float("inf")
        if self.last_node == target_node:
            return 0
        path, frames = gm.time_optimal_path(self.last_node, target_node)
        return frames if frames != float("inf") else 9999

    # ── 态势判断 ──

    def assess_posture(self, world, me, gm):
        """评估当前战略态势：racing / leading / trailing / contested / sprinting。

        调用时机：每帧 decide() 开始时。
        """
        if world.is_rush:
            self.posture = "sprinting"
            return self.posture

        if me.delivered or self.delivered:
            self.posture = "sprinting"
            return self.posture

        # 估算双方到终端或第一个必经节点的帧数
        terminal = gm.terminal_nodes[0] if gm.terminal_nodes else None
        if not terminal:
            self.posture = "racing"
            return self.posture

        # 使用第一个必经节点作为参考点（动态检测，不硬编码）
        ref_node = None
        chokes = gm.chokepoints - {terminal} if gm.chokepoints else set()
        if chokes:
            # 取距离起点最近的必经节点（= 第一个必经节点）
            best_dist = float("inf")
            for cn in chokes:
                _, d = gm.time_optimal_path(gm.start_node or my_node, cn)
                if d < best_dist:
                    best_dist = d
                    ref_node = cn
        if ref_node is None:
            ref_node = terminal

        my_node = me.current_node_id or gm.start_node
        opp_node = self.last_node or gm.start_node

        _, my_eta = gm.time_optimal_path(my_node, ref_node)
        _, opp_eta = gm.time_optimal_path(opp_node, ref_node)
        if my_eta == float("inf"):
            my_eta = 9999
        if opp_eta == float("inf"):
            opp_eta = 9999

        _, my_to_end = gm.time_optimal_path(my_node, terminal)
        _, opp_to_end = gm.time_optimal_path(opp_node, terminal)
        if my_to_end == float("inf"):
            my_to_end = 9999
        if opp_to_end == float("inf"):
            opp_to_end = 9999

        self.lead_frames = opp_to_end - my_to_end  # 正=我方领先

        # 判定态势
        if self.lead_frames > 50:
            self.posture = "leading"       # 大幅领先
        elif self.lead_frames > 10:
            self.posture = "racing"        # 小幅领先，继续竞速
        elif self.lead_frames > -20:
            self.posture = "contested"     # 胶着
        else:
            self.posture = "trailing"      # 落后

        return self.posture

    def has_passed(self, node_id):
        """对手是否已经过某节点。"""
        return node_id in {n for n, _ in self.visited_nodes}

    def is_ahead(self):
        """我方是否领先。"""
        return self.lead_frames > 0

    # ── 窗口牌自适应 ──

    def predict_opponent_card(self, contest_type):
        """预测对手在当前窗口类型下最可能出的牌。

        策略：取同类型窗口历史中对手最常用的那张牌。
        """
        relevant = [h for h in self.window_history
                    if h["contestType"] == contest_type]
        if not relevant:
            return None

        from collections import Counter
        card_counts = Counter(h["card"] for h in relevant)
        return card_counts.most_common(1)[0][0]

    def counter_card(self, predicted_card):
        """给定对手预测牌，返回克制它的牌。

        克制优先级（资源消耗从低到高）：
        1. BING_ZHENG（护卫行动点，不耗好果）
        2. YAN_DIE（文书资源，不耗好果）
        3. XIAN_GONG（1 好果 + 鲜度≥80）
        4. QIANG_XING（马资源）
        """
        if not predicted_card or predicted_card == Card.ABSTAIN:
            return None  # 用默认策略即可

        counters = _CARD_COUNTERED_BY.get(predicted_card, set())

        # 按资源代价排序：优先用不耗好果的
        if Card.BING_ZHENG in counters:
            return Card.BING_ZHENG
        if Card.YAN_DIE in counters:
            return Card.YAN_DIE
        if Card.QIANG_XING in counters:
            return Card.QIANG_XING
        if Card.XIAN_GONG in counters:
            return Card.XIAN_GONG
        return None  # 无克制牌，回退默认

    def adaptive_window_card(self, world, me, contest):
        """自适应窗口出牌：基于对手历史 + 资源可用性。

        返回 (card, contestId) 或 None（表示用默认策略）。
        """
        cid = contest.get("contestId")
        ctype = contest.get("contestType")
        if not cid:
            return None

        # 1. 预测对手出牌
        predicted = self.predict_opponent_card(ctype)

        # 2. 选择克制牌
        counter = self.counter_card(predicted) if predicted else None

        # 3. 验证资源可用
        if counter == Card.BING_ZHENG and (me.guard_action_point or 0) > 0:
            return (cid, Card.BING_ZHENG)
        if counter == Card.YAN_DIE and (
            me.resource_count("PASS_TOKEN") > 0 or
            me.resource_count("OFFICIAL_PERMIT") > 0
        ):
            return (cid, Card.YAN_DIE)
        if counter == Card.XIAN_GONG and me.freshness >= 80 and me.good_fruit > config.KEEP_GOOD_FRUIT_MIN:
            return (cid, Card.XIAN_GONG)
        if counter == Card.QIANG_XING and (
            me.resource_count("FAST_HORSE") > 0 or
            me.resource_count("SHORT_HORSE") > 0 or
            _has_move_buff(me)
        ):
            return (cid, Card.QIANG_XING)

        # 4. 若克制牌不可用，使用预测牌的反反制
        #    如果对手预测我们会出 counter，对手可能出 counter 的克制牌
        #    此时我们出对手预测牌的克制的克制的克制... 简化：出安全牌
        return None  # 回退默认策略

    # ── 设卡条件判断 ──

    def should_set_guard(self, world, me, gm, node_id):
        """判断是否应该在 node_id 设卡。

        条件：
        1. 我方领先（lead_frames > 设卡处理帧数 + 安全余量）
        2. node 是必经节点（chokepoint）
        3. 对手尚未通过
        4. 好果充足
        5. 该节点无敌方设卡
        """
        if not config.ENABLE_OFFENSIVE:
            return False

        # 态势检查：只要领先（racing 或 leading）就可设卡
        if self.posture in ("trailing", "contested", "sprinting"):
            return False

        # ★ 设卡时机优化：对手 ETA ≤ 设卡处理帧数 → 不设卡
        # （设卡需 4 帧处理，对手在 4 帧内到达则可以 MOVE 通过，设卡白费）
        opp_eta = self.estimate_eta_to(gm, node_id)
        if opp_eta is not None and opp_eta <= 5:
            return False

        # 必经节点检查
        if not gm.is_chokepoint(node_id):
            return False

        # 对手未通过
        if self.has_passed(node_id):
            return False

        # 无敌方设卡
        ns = world.node(node_id)
        if ns and ns.active_guard_owner():
            return False

        # 好果充足：至少留 20 好果交付（≈36分） + 设卡成本
        guard_cost = 1 if gm.node(node_id) and gm.node(node_id).type == "KEY_PASS" else 0
        if me.good_fruit < 20 + guard_cost + config.KEEP_GOOD_FRUIT_MIN:
            return False

        # 我方已过此节点（设卡只能在身后）
        if not self._we_have_passed(me, node_id, gm):
            return False

        return True

    def _we_have_passed(self, me, node_id, gm):
        """检查我方是否在 node_id 或已经过了 node_id。

        SET_GUARD 只能在当前所在节点提交（任务书 §6.2.1），
        所以"在节点上"也算满足条件。
        """
        if me.current_node_id == node_id:
            return True
        if not me.current_node_id or not gm.terminal_nodes:
            return False
        terminal = gm.terminal_nodes[0]
        _, dist_node_to_end = gm.time_optimal_path(node_id, terminal)
        _, dist_me_to_end = gm.time_optimal_path(me.current_node_id, terminal)
        if dist_me_to_end == float("inf") or dist_node_to_end == float("inf"):
            return False
        return dist_me_to_end < dist_node_to_end


def _has_move_buff(me):
    """检查是否有移动加速 buff（马或疾行令）。"""
    for b in (me.buffs or []):
        if b.get("type") in ("FAST_HORSE", "SHORT_HORSE", "RUSH_SPEED"):
            if (b.get("remainingRound", 0) or 0) > 0:
                return True
    return False
