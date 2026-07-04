r"""决策引擎 v4.5 — 移植 demo 对手优势。

核心改进（来自 demo 真机归因）:
  1. 冰鉴≤90使用: 不撞100上限，+10全效存活到交付
  2. 绕路领冰鉴: 净收益≥6才绕，排除山路高损耗绕路
  3. 冰鉴领取免预算: 2帧成本<<1篓好果3.6分
  4. 鲜度λ路由: time_optimal_path内置λ=5.0惩罚（差分式）
  5. 任务追180: 封顶检测+慷慨绕路70帧
  6. 后期前置宫门: r360未验核→直奔S14
  7. 交付投影估算: _deliver_estimate用于预算决策
"""

import math
import config
from core.game_map import GameMap
from core import rules as r
from protocol import actions
from protocol.enums import Action, Card, PlayerState, ResourceType

_IDLE_LIKE = (PlayerState.IDLE, PlayerState.COST_BANKRUPT, PlayerState.CONTESTING, PlayerState.WAITING, None)
_MOVE_BUFF_TYPES = frozenset({ResourceType.FAST_HORSE, ResourceType.SHORT_HORSE, "RUSH_SPEED"})
_MOVE_BLOCK_CODES = frozenset({
    "MOVE_BLOCKED_BY_GUARD", "TARGET_NOT_REACHABLE",
    "MOVE_EDGE_NOT_FOUND", "OBJECT_BUSY",
})
_INF = float("inf")


class GameContext:
    def __init__(self, player_id, team_id=None, camp=None, start_data=None):
        self.player_id = int(player_id)
        self.team_id = team_id
        self.camp = camp
        start_data = start_data or {}
        self.match_id = start_data.get("matchId")
        self.duration_round = start_data.get("durationRound") or 600
        try:
            self.game_map = GameMap(start_data)
        except Exception:
            self.game_map = None


class DecisionEngine:
    def __init__(self, context):
        self.ctx = context
        self._stay_node = None
        self._processed_here = False
        self._prev_state = None
        self._cooldown = {}          # nodeId -> 拉黑截止回合
        self._squad_sent = set()     # (nodeId, kind)
        self._gate_scout_sent = False
        self._last_main_action = None
        self._fp_failures = {}
        self._window_played = {}     # v4.5: contestId -> {roundIndex} 已出牌拍次
        self._triggered = set()      # v4.5: 已触发的好果转坏阈值
        self._prev_freshness = None  # v4.5: 上一帧鲜度
        self._task_base = 0
        self._completed_task_ids = set()
        self._task_attempted = set()    # v4.3: 已尝试过的任务（防重试风暴）
        self._task_fail_count = {}      # v4.3: 任务失败计数
        self._obstacle_detour_budget = 25  # v4.2: 绕行≤25帧就绕，省好果优先

    # ================================================================
    #  主入口
    # ================================================================

    def decide(self, world):
        me = world.me
        gm = self.ctx.game_map
        if me is None or gm is None:
            return []

        node = me.current_node_id
        self._apply_rejection_feedback(world)
        self._update_process_memory(world, me, node)

        terminal = gm.terminal_nodes[0] if gm.terminal_nodes else None
        gate = gm.gate_node
        result = []

        try:
            if me.delivered or me.state == PlayerState.DELIVERED:
                return result

            card = self._window_card(world, me)
            if card:
                result = [card]
                return result

            if me.state in (PlayerState.MOVING, PlayerState.WAITING):
                horse = self._maybe_horse(me, gm, terminal)
                if horse:
                    result = [horse]
                    return result
                # MOVING: nothing to do but wait for arrival
                if me.state == PlayerState.MOVING:
                    return result
                # WAITING: fall through to _plan() — don't stay idle

            if me.state not in _IDLE_LIKE:
                return result

            main = self._plan(world, me, gm, node, terminal, gate)
            squad = self._maybe_squad_v3(world, me, gm, node, terminal)
            result = main + ([squad] if squad else [])
            return result
        finally:
            self._prev_state = me.state
            self._last_main_action = self._extract_main(result)

    # ================================================================
    #  L1 基线：状态机推进
    # ================================================================

    def _plan(self, world, me, gm, node, terminal, gate):
        # v4.5: 冰鉴 ≤90 使用
        rescue = self._freshness_rescue(world, me)
        if rescue:
            return [rescue]

        # S15 终点
        if terminal and node == terminal:
            if me.verified and me.good_fruit > 0 and me.freshness > 0:
                return [actions.deliver()]
            if not me.verified and gate and gate in gm.neighbors(node):
                return [actions.move(gate)]
            return []

        # S14 宫门
        if gate and node == gate:
            if not me.verified:
                if world.is_rush:
                    bo = (Action.BREAK_ORDER
                          if (me.rush_tactic_used_count or 0) == 0
                          and (me.bad_fruit >= 2 or me.good_fruit > config.KEEP_GOOD_FRUIT_MIN)
                          else None)
                    return [actions.verify_gate(rush_tactic=bo)]
                rp = self._maybe_rush_protect(world, me)
                if rp:
                    return [rp]
                return self._opportunistic(world, me, gm, node, terminal) or []
            return self._advance(world, me, gm, node, terminal, terminal)

        # 固定处理站点
        if node in gm.process_nodes and not self._processed_here:
            return [actions.process()]

        # 情报
        intel = self._maybe_intel(world, me, gm, node, terminal)
        if intel:
            return [intel]

        # 急策
        rp = self._maybe_rush_protect(world, me)
        if rp:
            return [rp]
        speed = self._rush_speed_warranted(world, me, gm, node, terminal)
        if speed:
            return [speed]

        # v3: T04 任务（障碍节点）优先于普通任务
        t04 = self._maybe_t04_task(world, me, gm, node, terminal)
        if t04:
            return [t04]

        opp = self._opportunistic(world, me, gm, node, terminal)
        if opp:
            return opp

        # v4.5: 后期前置宫门（demo RUSH_PREPOSITION_ROUND）
        route_dst = self._late_route_target(world, me, gate, terminal)
        if route_dst == gate:
            return self._advance(world, me, gm, node, gate, terminal)

        # v4.5: 绕路领冰鉴（鲜度优先）→ 绕路做任务
        dst = (self._ice_box_detour_target(world, me, gm, node, terminal)
               or self._task_detour_target(world, me, gm, node, terminal)
               or terminal)
        if dst:
            return self._advance(world, me, gm, node, dst, terminal)
        return []

    # ================================================================
    #  v3: T04 障碍任务
    # ================================================================

    def _maybe_t04_task(self, world, me, gm, node, terminal):
        """v3: 如果前方路径上有障碍且存在 T04 任务，优先做 T04（清障+30分）。"""
        pid = self.ctx.player_id
        for t in world.active_tasks():
            tid = t.get("taskTemplateId")
            if tid != "T04":
                continue
            tn = t.get("nodeId")
            if not tn:
                continue
            # T04 可在目标节点或相邻节点完成
            if node != tn and tn not in gm.neighbors(node):
                continue
            prot = t.get("protectionPlayerId") or 0
            if prot and prot != pid:
                continue
            owner = t.get("ownerPlayerId") or 0
            if owner and owner != pid:
                continue
            # 确认目标节点确实有障碍
            ns = world.node(tn)
            if not ns or not ns.has_obstacle:
                continue
            pr = t.get("processRound", 0) or 0
            if not self._can_afford(world, gm, node, pr, terminal):
                continue
            return actions.claim_task(t.get("taskId"))
        return None

    # ================================================================
    #  L2 收益: 冰鉴/马/急策/任务/资源
    # ================================================================

    def _freshness_rescue(self, world, me):
        """v4.5: 冰鉴 ≤90 使用（demo ICE_BOX_CAP_AVOID）。

        demo 洞察: ≤90 用不撞 100 上限，+10 全效存活到交付。
        线性损耗下冰鉴为永久偏移，2-3个叠20-30可把80阈值延后到交付后。
        """
        if me.resource_count(ResourceType.ICE_BOX) <= 0:
            return None
        f = me.freshness
        if 0 < f <= config.ICE_BOX_CAP_AVOID:
            return actions.use_resource(ResourceType.ICE_BOX)
        return None

    def _maybe_horse(self, me, gm, terminal):
        if self._has_move_buff(me):
            return None
        horse = None
        if me.resource_count(ResourceType.FAST_HORSE) > 0:
            horse = ResourceType.FAST_HORSE
        elif me.resource_count(ResourceType.SHORT_HORSE) > 0:
            horse = ResourceType.SHORT_HORSE
        if not horse or not terminal or not me.current_node_id:
            return None
        dist = gm.route_distance(me.current_node_id, terminal)
        if dist == _INF or dist < 60:
            return None
        return actions.use_resource(horse)

    def _maybe_rush_protect(self, world, me):
        """v4.2: RUSH 立即护果。鲜度 < 98 且 > 30 即用。"""
        if not world.is_rush or me.delivered or (me.rush_tactic_used_count or 0) > 0:
            return None
        if me.freshness < config.RUSH_PROTECT_FRESHNESS_BELOW and me.freshness > 30:
            return actions.rush_protect()
        return None

    def _rush_speed_warranted(self, world, me, gm, node, terminal):
        if not world.is_rush or me.delivered or (me.rush_tactic_used_count or 0) > 0:
            return None
        if me.good_fruit < config.KEEP_GOOD_FRUIT_MIN + 2:
            return None
        if me.freshness < config.RUSH_PROTECT_FRESHNESS_BELOW:
            return None
        if self._has_any_horse(me) or not self._far_from_terminal(gm, node, terminal):
            return None
        return actions.rush_speed()

    def _opportunistic(self, world, me, gm, node, terminal):
        task = self._maybe_task(world, me, gm, node, terminal)
        if task:
            return [task]
        claim = self._maybe_claim_v2(world, me, gm, node, terminal)
        if claim:
            return [claim]
        return None

    def _maybe_task(self, world, me, gm, node, terminal):
        """v4.5: 任务领取 + 封顶检测（demo _task_score_capped）。"""
        if self._task_score_capped(me):
            return None
        pid = self.ctx.player_id
        MAX_TASK_FAILURES = 3
        for t in world.active_tasks():
            if t.get("nodeId") != node:
                continue
            tid = t.get("taskTemplateId")
            if tid in config.SKIP_TASK_TEMPLATES:
                continue
            task_id = t.get("taskId")
            if self._task_fail_count.get(task_id, 0) >= MAX_TASK_FAILURES:
                continue
            if task_id in self._task_attempted:
                continue
            prot = t.get("protectionPlayerId") or 0
            if prot and prot != pid:
                continue
            owner = t.get("ownerPlayerId") or 0
            if owner and owner != pid:
                continue
            pr = t.get("processRound", 0) or 0
            if not self._can_afford(world, gm, node, pr, terminal):
                continue
            self._task_attempted.add(task_id)
            return actions.claim_task(task_id)
        return None

    def _maybe_claim_v2(self, world, me, gm, node, terminal):
        """v4.3: 资源领取 + 前方冰鉴探测。"""
        ns = world.node(node)
        if ns is None:
            return None
        # 冰鉴优先 — 当前节点（v4.5: 豁免时间预算，demo做法）
        if (me.resource_count(ResourceType.ICE_BOX) < config.CLAIM_ICE_BOX_KEEP
                and ns.resource_available(ResourceType.ICE_BOX)):
            return actions.claim_resource(node, ResourceType.ICE_BOX)
        # 马——剩余距离>100
        if not self._has_any_horse(me) and terminal and node:
            dist = gm.route_distance(node, terminal)
            if dist != _INF and dist > 100:
                if ns.resource_available(ResourceType.FAST_HORSE):
                    if self._can_afford(world, gm, node, 2, terminal):
                        return actions.claim_resource(node, ResourceType.FAST_HORSE)
                if ns.resource_available(ResourceType.SHORT_HORSE):
                    if self._can_afford(world, gm, node, 2, terminal):
                        return actions.claim_resource(node, ResourceType.SHORT_HORSE)
        # 情报
        if (me.resource_count(ResourceType.INTEL) < 1
                and ns.resource_available(ResourceType.INTEL)
                and self._intel_usable_ahead(world, me, gm, node, terminal)):
            if self._can_afford(world, gm, node, 2, terminal):
                return actions.claim_resource(node, ResourceType.INTEL)
        return None

    def _find_ice_on_route(self, world, me, gm, node, terminal):
        """v4.4: 激进冰鉴探测。沿路径搜索 config.ICE_BOX_DETOUR_KEEP 跳。

        对方用了 2+ 次冰鉴才保 88 鲜度。我方必须确保充足冰鉴。
        """
        if me.resource_count(ResourceType.ICE_BOX) >= config.CLAIM_ICE_BOX_KEEP:
            return None
        if not terminal:
            return None
        path, _ = gm.time_optimal_path(node, terminal)
        if not path or len(path) < 2:
            return None
        max_range = min(config.ICE_BOX_DETOUR_KEEP, len(path))
        for i in range(1, max_range):
            nid = path[i]
            ns = world.node(nid)
            if ns and ns.resource_available(ResourceType.ICE_BOX):
                if nid == path[1]:
                    return None  # 下一跳到达时自然领
                if self._can_afford(world, gm, node, 4, terminal):
                    return actions.claim_resource(nid, ResourceType.ICE_BOX)
        return None

    # ================================================================
    #  情报 / 绕路做任务（不变）
    # ================================================================

    def _maybe_intel(self, world, me, gm, node, terminal):
        if me.resource_count(ResourceType.INTEL) <= 0:
            return None
        for nxt in self._reduce_targets_on_route(world, me, gm, node, terminal):
            d = gm.route_distance(node, nxt)
            if d == _INF or d > config.INTEL_RANGE:
                continue
            return actions.use_resource(ResourceType.INTEL, nxt)
        return None

    def _reduce_targets_on_route(self, world, me, gm, node, terminal):
        if not terminal:
            return []
        path, _ = gm.time_optimal_path(node, terminal, blocked=self._blocked_nodes(world, me))
        if not path or len(path) < 2:
            path, _ = gm.time_optimal_path(node, terminal)
        out = []
        for nxt in (path[1:] if path else []):
            if nxt == gm.gate_node or nxt in gm.process_nodes:
                ns = world.node(nxt)
                if ns and ns.my_scout_marks(me.team_id):
                    continue
                out.append(nxt)
        return out

    def _intel_usable_ahead(self, world, me, gm, node, terminal):
        path, _ = gm.time_optimal_path(node, terminal)
        if not path:
            return False
        for i in range(1, len(path)):
            p = path[i]
            if p == gm.gate_node or p in gm.process_nodes:
                ns = world.node(p)
                if ns and ns.my_scout_marks(me.team_id):
                    continue
                if gm.route_distance(path[i - 1], p) <= config.INTEL_RANGE:
                    return True
        return False

    def _task_detour_target(self, world, me, gm, node, terminal):
        """v4.5: 任务绕路 — 封顶检测 + 鲜度地板 + 慷慨预算70帧（demo参数）。"""
        self._track_task_completion(world)
        if self._task_score_capped(me) or not terminal:
            return None
        base = self._task_base or me.task_score or 0
        if base >= config.TASK_SEEK_TARGET:
            return None
        pid = self.ctx.player_id
        _, direct = self._time_path(world, node, terminal)
        if direct == _INF:
            return None
        budget = config.TASK_DETOUR_MAX_EXTRA_FRAMES
        best, best_extra = None, _INF
        for t in world.active_tasks():
            tn = t.get("nodeId")
            if not tn or tn == node:
                continue
            tid = t.get("taskTemplateId")
            if tid in config.SKIP_TASK_TEMPLATES:
                continue
            prot = t.get("protectionPlayerId") or 0
            if prot and prot != pid:
                continue
            owner = t.get("ownerPlayerId") or 0
            if owner and owner != pid:
                continue
            _, c1 = self._time_path(world, node, tn)
            _, c2 = self._time_path(world, tn, terminal)
            if c1 == _INF or c2 == _INF:
                continue
            pr = t.get("processRound", 0) or 0
            extra = (c1 + pr + c2) - direct
            # 鲜度地板：预估鲜度不能跌破
            projected = me.freshness - extra * config.FRESHNESS_LOSS_ASSUME
            if projected < config.FRESHNESS_DETOUR_FLOOR:
                continue
            if 0 <= extra <= budget and extra < best_extra \
                    and self._can_afford(world, gm, node, extra, terminal,
                                         safety_margin=config.TASK_DETOUR_SAFETY_MARGIN):
                best, best_extra = tn, extra
        return best

    def _track_task_completion(self, world):
        pid = self.ctx.player_id
        for e in (world.events or []):
            if e.get("type") == "TASK_COMPLETE":
                payload = e.get("payload") or {}
                if payload.get("playerId") == pid:
                    tid = payload.get("taskId")
                    if tid and tid not in self._completed_task_ids:
                        self._completed_task_ids.add(tid)
                        template_id = payload.get("taskTemplateId", "")
                        score = 30 if template_id in ("T01", "T02", "T04", "T06", "T08", "T11") else 15
                        self._task_base += score

    # ================================================================
    #  L3 v4.5: 鲜度λ路由 + 交付估算 + 冰鉴绕路
    # ================================================================

    def _time_path(self, world, src, dst, blocked=None, enter_cost_fn=None):
        """v4.5: time_optimal_path 的天气+鲜度λ封装（demo _time_path）。"""
        return self.ctx.game_map.time_optimal_path(
            src, dst, weather_type=world.active_weather_type(),
            blocked=blocked, enter_cost_fn=enter_cost_fn,
            freshness_weight=config.FRESHNESS_ROUTE_LAMBDA)

    def _deliver_estimate(self, world, me, gm, node, terminal):
        """v4.5: 从当前节点完成交付的估计帧数。"""
        if not terminal:
            return _INF
        _, travel = self._time_path(world, node, terminal)
        if travel == _INF:
            return _INF
        est = travel + 2
        if not me.verified and gm.gate_node:
            info = gm.process_nodes.get(gm.gate_node)
            verify_frames = (info.get("processRound") if info else 6) or 6
            est += verify_frames
        return est

    def _path_freshness_loss(self, world, path):
        """v4.5: 估算路径总鲜度损耗。"""
        if not path or len(path) < 2:
            return 0.0
        gm = self.ctx.game_map
        wtype = world.active_weather_type()
        wcoef = r.FRESHNESS_WEATHER_COEF.get(wtype, 1.0) if wtype else 1.0
        total = 0.0
        for i in range(len(path) - 1):
            e = gm.edge_between(path[i], path[i + 1])
            if e is None:
                continue
            wmult = r.weather_move_multiplier(e.route_type, wtype)
            frames = r.frames_on_edge(e.distance, e.route_type, weather_mult=wmult)
            total += frames * r.route_freshness_loss(e.route_type) * wcoef
        return total

    def _task_score_capped(self, me):
        """v4.5: 任务分是否已达 180 封顶。"""
        base = me.task_score or 0
        return base + r.milestone_bonus(base) >= 180

    def _late_route_target(self, world, me, gate, terminal):
        """v4.5: r360后未验核→直奔宫门（demo RUSH_PREPOSITION_ROUND）。"""
        if (gate and terminal and gate != terminal
                and (world.round or 0) >= config.RUSH_PREPOSITION_ROUND
                and not me.verified):
            return gate
        return terminal

    def _ice_box_detour_target(self, world, me, gm, node, terminal):
        """v4.5: 绕路领冰鉴（demo _ice_box_detour_target）。

        净收益过滤: 冰鉴+10 − 绕路额外损耗 ≥ ICE_BOX_DETOUR_NET_MIN(6)。
        排除山路等高损耗绕路，保留官道绕路。
        """
        if not terminal:
            return None
        have = me.resource_count(ResourceType.ICE_BOX)
        if have >= config.ICE_BOX_DETOUR_KEEP:
            return None
        remaining = self._deliver_estimate(world, me, gm, node, terminal)
        if remaining >= _INF:
            return None
        projected = me.freshness + have * 10 - remaining * config.FRESHNESS_LOSS_ASSUME
        if projected >= config.ICE_BOX_DETOUR_PROJECTED_BELOW:
            return None
        direct_path, direct_cost = self._time_path(world, node, terminal)
        if direct_cost == _INF or not direct_path:
            return None
        direct_loss = self._path_freshness_loss(world, direct_path)
        best, best_extra = None, _INF
        for nid, ns in world.node_states.items():
            if nid == node or not ns.resource_available(ResourceType.ICE_BOX):
                continue
            p1, c1 = self._time_path(world, node, nid)
            p2, c2 = self._time_path(world, nid, terminal)
            if c1 == _INF or c2 == _INF or not p1 or not p2:
                continue
            extra = c1 + config.RESOURCE_CLAIM_ROUND + c2 - direct_cost
            if extra <= 0 or extra > config.ICE_BOX_DETOUR_MAX_EXTRA_FRAMES:
                continue
            via_loss = self._path_freshness_loss(world, p1[:-1] + p2)
            net = 10 - (via_loss - direct_loss)
            if net < config.ICE_BOX_DETOUR_NET_MIN:
                continue
            if extra < best_extra and self._can_afford(
                    world, gm, node, extra, terminal,
                    safety_margin=config.DELIVER_TIME_SAFETY_MARGIN):
                best, best_extra = nid, extra
        return best

    def _advance(self, world, me, gm, src, dst, terminal):
        """v4.3: 动态路由 — 鲜度 + 对手位置 + 时间预算综合决策。

        1. 计算多条候选路径（时间最优 / 鲜度最优 / 天气调整）
        2. 根据对手位置和路径动态调整 blocked 节点
        3. 综合评分选最优路径：帧数 + 鲜度损耗 + 对抗风险
        """
        blocked = self._blocked_nodes(world, me)
        active_wt = world.active_weather_type()
        upcoming = world.upcoming_weather(within_frames=30)
        current_round = world.round or 0
        duration = self.ctx.duration_round or 600

        # ── 对手感知阻塞 ──
        # 对手在前方路径节点上 → 提前避开
        opp_blocked = self._opponent_blocked_nodes(world, gm, src, terminal)
        all_blocked = blocked | opp_blocked

        # ── 候选路径 ──
        # 路径1: 时间最优（阻塞感知）
        path_time, cost_time = gm.time_optimal_path(src, dst, blocked=all_blocked)
        # 路径2: 时间最优（无阻塞基准）
        path_base, cost_base = gm.time_optimal_path(src, dst)
        # 路径3: 时间最优（仅障碍/守卫阻塞）
        path_safe, cost_safe = gm.time_optimal_path(src, dst, blocked=blocked)

        # ── 动态鲜度权重（v4.4: 对齐对方策略，目标鲜度 88%）──
        remaining_budget = duration - current_round - config.DELIVER_TIME_SAFETY_MARGIN
        # 鲜度距目标越远 → 越急迫地选低损耗路线
        freshness_urgency = max(1.5, (100 - me.freshness) / 15.0)
        # 时间越充裕 → 权重越大（对方 r561 到达，我们接受 r560）
        if cost_base > 0 and remaining_budget > cost_base:
            time_slack = min(4.0, (remaining_budget - cost_base) / max(1, cost_base) * 3)
        else:
            time_slack = 0
        # 天气加剧 → 更保守的路线
        weather_bonus = 2.0 if active_wt in ("HOT", "MOUNTAIN_FOG") else 0
        # v4.4: 基础权重提高到 2.0，范围 2.0 ~ 8.0
        fw = min(8.0, max(2.0, freshness_urgency + time_slack + weather_bonus))

        # ── 路径4: 鲜度+时间平衡 ──
        path_fresh = None
        cost_fresh = _INF
        if path_safe and len(path_safe) > 1:
            try:
                path_fresh, _ = gm.freshness_optimal_path(
                    src, dst, blocked=all_blocked, freshness_weight=fw)
                if path_fresh and len(path_fresh) > 1:
                    _, cost_fresh = gm.time_optimal_path(
                        src, dst, blocked=all_blocked)
            except Exception:
                path_fresh = None

        # ── 天气调整路径 ──
        path_weather = None
        cost_weather = _INF
        if active_wt and self._weather_hurts_path(active_wt, gm, path_safe or path_base):
            path_weather, cost_weather = gm.weather_adjusted_path(
                src, dst, weather_type=active_wt, blocked=all_blocked)

        # ── 路径评分与选择 ──
        FRESH_RATE = {"ROAD": 0.055, "WATER": 0.045, "MOUNTAIN": 0.07, "BRANCH": 0.065}
        candidates = []

        for label, path, cost in [
            ("time", path_time, cost_time),
            ("safe", path_safe, cost_safe),
            ("fresh", path_fresh, cost_fresh),
            ("weather", path_weather, cost_weather),
        ]:
            if not path or len(path) < 2 or cost == _INF:
                continue
            if cost > remaining_budget:
                continue  # 不能在预算内到达 → 排除

            # 计算鲜度损耗
            types = self._path_route_types(gm, path)
            avg_loss = sum(FRESH_RATE.get(t, 0.06) for t in types) / max(1, len(types))
            est_freshness_loss = cost * avg_loss

            # 对手风险：路径与对手路径重叠的节点数
            opp_risk = self._opponent_path_overlap(gm, path, world)

            # 综合评分（越低越好）
            score = cost + est_freshness_loss * 10 + opp_risk * 30
            candidates.append((score, path, cost, label))

        # 选评分最低的路径
        if candidates:
            candidates.sort(key=lambda x: x[0])
            _, best_path, best_cost, best_label = candidates[0]
            path_b = best_path
            cost_b = best_cost
            path_u = path_base  # 用于后续判断
            cost_u = cost_base
        else:
            # 所有候选都超预算 → 退回时间最优
            path_b = path_time or path_safe
            cost_b = cost_time if cost_time != _INF else (cost_safe if cost_safe != _INF else 0)
            path_u = path_base
            cost_u = cost_base

        # ── 酷暑/山雾预告 → 倾向官道 ──
        if upcoming and upcoming.get("type") in ("HOT", "MOUNTAIN_FOG") and path_u:
            u_types = self._path_route_types(gm, path_u)
            u_mtn = sum(1 for t in u_types if t == "MOUNTAIN") / max(1, len(u_types))
            if u_mtn > 0.3:
                alt_path, alt_cost = gm.weather_adjusted_path(
                    src, dst, weather_type=upcoming.get("type"), blocked=all_blocked)
                if alt_path and len(alt_path) > 1 and alt_cost - cost_u < 40:
                    a_types = self._path_route_types(gm, alt_path)
                    a_mtn = sum(1 for t in a_types if t == "MOUNTAIN") / max(1, len(a_types))
                    if a_mtn < u_mtn - 0.1:
                        path_b, cost_b = alt_path, alt_cost

        # ── 执行移动 ──
        if path_b and len(path_b) > 1:
            nxt = path_b[1]
            ns = world.node(nxt)

            # 障碍处理
            if ns and ns.has_obstacle and not self._is_cooldown(world, nxt):
                alt_path, alt_cost = gm.time_optimal_path(
                    src, dst, blocked=all_blocked | {nxt})
                detour_extra = (alt_cost - cost_b) if alt_cost != _INF else _INF
                if detour_extra <= self._obstacle_detour_budget:
                    return [actions.move(alt_path[1])]
                if me.good_fruit > config.KEEP_GOOD_FRUIT_MIN:
                    t04 = self._find_t04(world, nxt)
                    if t04:
                        return [actions.claim_task(t04.get("taskId"))]
                    return [actions.clear_obstacle(nxt)]
                return [actions.move(alt_path[1] if alt_path else path_b[1])]

            speed = self._rush_speed_warranted(world, me, gm, src, terminal)
            if speed:
                return [speed]
            return [actions.move(nxt)]

        if not path_u or len(path_u) < 2:
            return []
        return self._breakthrough(world, me, gm, path_u[1], terminal)

    def _opponent_blocked_nodes(self, world, gm, src, terminal):
        """v4.3: 对手感知阻塞。对手前方路径上的节点标记为需避开。"""
        blocked = set()
        opp = world.opponent
        if opp is None or opp.current_node_id is None:
            return blocked
        opp_node = opp.current_node_id
        # 对手到终点的路径
        opp_path, _ = gm.time_optimal_path(opp_node, terminal)
        if not opp_path:
            return blocked
        # 对手前方 0-2 跳标记（对手可能在这些节点设卡或到达）
        opp_idx = -1
        for i, nid in enumerate(opp_path):
            if nid == opp_node:
                opp_idx = i
                break
        if opp_idx >= 0:
            for j in range(opp_idx, min(opp_idx + 3, len(opp_path))):
                nid = opp_path[j]
                ns = world.node(nid)
                # 对手在此有守卫 → 避开
                if ns and ns.active_guard_owner() and ns.active_guard_owner() != world.me.team_id:
                    blocked.add(nid)
        return blocked

    def _opponent_path_overlap(self, gm, my_path, world):
        """v4.3: 计算路径与对手路径的重叠节点数（越高越不利）。"""
        opp = world.opponent
        if opp is None or opp.current_node_id is None:
            return 0
        terminal = (gm.terminal_nodes or [None])[0]
        if not terminal:
            return 0
        opp_path, _ = gm.time_optimal_path(opp.current_node_id, terminal)
        if not opp_path or not my_path:
            return 0
        my_set = set(my_path)
        opp_set = set(opp_path)
        return len(my_set & opp_set)

    def _freshness_optimal_path(self, gm, src, dst):
        """v4-fix: 返回预估总鲜度损耗最低的路径。

        总鲜度损耗 = (移动帧数 + 途经节点处理开销) × 加权每帧损耗率。
        途经节点越多处理开销越大，避免绕远路多停站。
        """
        if not src or not dst:
            return None
        try:
            candidates = gm.enumerate_paths(src, dst, top_k=4)
        except Exception:
            return None
        if not candidates:
            return None

        FRESH_PER_FRAME = {"ROAD": 0.055, "WATER": 0.045, "MOUNTAIN": 0.07, "BRANCH": 0.065}
        # 每个途经节点额外处理开销（帧）：PROCESS + 可能的窗口博弈
        NODE_OVERHEAD = 50

        best_path, best_total_loss = None, _INF
        for path, _dist in candidates:
            types = self._path_route_types(gm, path)
            if not types:
                continue
            est_move_frames = self._estimate_path_frames(gm, path)
            if est_move_frames <= 0 or est_move_frames == _INF:
                continue
            # 途经节点处理开销（不包括起点和终点）
            intermediate_nodes = max(0, len(path) - 2)
            total_frames = est_move_frames + intermediate_nodes * NODE_OVERHEAD
            # 路径加权每帧损耗率
            avg_loss_rate = sum(FRESH_PER_FRAME.get(t, 0.06) for t in types) / len(types)
            total_loss = total_frames * avg_loss_rate
            if total_loss < best_total_loss:
                best_total_loss = total_loss
                best_path = path
        return best_path

    def _estimate_path_frames(self, gm, path):
        """估算给定路径的总帧数。使用路线类型对应的每帧移动量。"""
        if not path or len(path) < 2:
            return _INF
        # 每帧移动量（基础 1000 + 路线系数调整）
        ROUTE_COST = {"ROAD": 1380, "WATER": 1250, "MOUNTAIN": 1780, "BRANCH": 1550}
        total_frames = 0
        for i in range(len(path) - 1):
            edge = gm.edge_between(path[i], path[i + 1])
            if edge:
                dist = edge.distance or 0
                rt = getattr(edge, 'route_type', 'ROAD') or 'ROAD'
                move_amount = dist * ROUTE_COST.get(rt, 1380)
                # 每帧移动 1000，ceil 计算帧数
                total_frames += int(move_amount / 1000) + (1 if move_amount % 1000 > 0 else 0)
        return max(total_frames, 1)

    def _weather_hurts_path(self, weather_type, gm, path):
        """检查天气是否对给定路径有害。"""
        if not weather_type or not path:
            return False
        types = self._path_route_types(gm, path)
        if weather_type == "HEAVY_RAIN" and "WATER" in types:
            return True  # 暴雨 → 水路减速
        if weather_type == "MOUNTAIN_FOG" and "MOUNTAIN" in types:
            return True  # 山雾 → 山路减速
        if weather_type == "HOT" and "MOUNTAIN" in types:
            return True  # 酷暑 + 山路 = 鲜度损耗 ×1.5
        return False

    def _path_route_types(self, gm, path):
        types = []
        for i in range(len(path) - 1):
            e = gm.edge_between(path[i], path[i + 1])
            if e:
                types.append(e.route_type)
        return types

    def _blocked_nodes(self, world, me):
        blocked = set()
        for nid, ns in world.node_states.items():
            if ns.has_obstacle:
                blocked.add(nid)
            owner = ns.active_guard_owner()
            if owner and owner != me.team_id:
                blocked.add(nid)
        rnd = world.round or 0
        for nid, exp in self._cooldown.items():
            if exp > rnd:
                blocked.add(nid)
        return blocked

    def _is_cooldown(self, world, nid):
        return self._cooldown.get(nid, 0) > (world.round or 0)

    def _breakthrough(self, world, me, gm, nxt, terminal):
        """v4.5: 突破障碍/敌卡。有好果→清障/攻坚，无好果→强制通行。"""
        ns = world.node(nxt)
        if ns and ns.has_obstacle:
            t04 = self._find_t04(world, nxt)
            if t04:
                self._fp_failures.pop(nxt, None)
                return [actions.claim_task(t04.get("taskId"))]
            if me.good_fruit > config.KEEP_GOOD_FRUIT_MIN:
                self._fp_failures.pop(nxt, None)
                return [actions.clear_obstacle(nxt)]
            return [actions.forced_pass(nxt)]

        owner = ns.active_guard_owner() if ns else None
        if owner and owner != me.team_id:
            plan = self._plan_attack(me, ns)
            if plan is not None:
                self._fp_failures.pop(nxt, None)
                g, b, bo = plan
                return [actions.break_guard(nxt, good_fruit=g, bad_fruit=b,
                                           rush_tactic=(Action.BREAK_ORDER if bo else None))]
            fails = self._fp_failures.get(nxt, 0)
            if fails >= config.FP_RETRY_LIMIT:
                if fails >= config.FP_RETRY_LIMIT + config.FP_RETRY_COOLDOWN:
                    self._fp_failures.pop(nxt, None)
                    return [actions.forced_pass(nxt)]
                self._fp_failures[nxt] = fails + 1
                return []
            self._fp_failures[nxt] = fails + 1
            return [actions.forced_pass(nxt)]

        return [actions.move(nxt)]

    def _find_t04(self, world, node):
        for t in world.active_tasks():
            if t.get("taskTemplateId") == "T04" and t.get("nodeId") == node:
                return t
        return None

    def _plan_attack(self, me, ns):
        defense = (ns.guard or {}).get("defense", 0) or 0
        if defense <= 0:
            return None
        bo = (me.rush_tactic_used_count or 0) == 0
        bonus = 3 if bo else 0
        best = None
        max_g = min(2, me.good_fruit - config.KEEP_GOOD_FRUIT_MIN)
        max_b = min(2, me.bad_fruit)
        for g in range(0, max_g + 1):
            if g > me.good_fruit:
                continue
            for b in range(0, max_b + 1):
                if b > me.bad_fruit:
                    continue
                if g * 2 + b * 3 + bonus >= defense:
                    if best is None or (g, b) < (best[0], best[1]):
                        best = (g, b, bo)
        return best

    # ================================================================
    #  窗口出牌 v4.5: 反应式 3 拍（移植 demo 对手策略）
    # ================================================================

    # 牌克制矩阵（§5.4.4）：BEATS[对手牌] = 克制它的牌（按成本从低到高）
    _BEATS = {
        Card.YAN_DIE: (Card.XIAN_GONG, Card.BING_ZHENG),
        Card.QIANG_XING: (Card.YAN_DIE, Card.BING_ZHENG),
        Card.XIAN_GONG: (Card.QIANG_XING,),
        Card.BING_ZHENG: (Card.XIAN_GONG,),
    }
    _STAKES_RANK = {"GATE": 3, "PASS": 3, "TASK": 2, "OBSTACLE": 2, "DOCK": 1, "RESOURCE": 1}

    def _window_card(self, world, me):
        """v4.5: 反应式 3 拍出牌（demo 对手策略）。

        读对手上一拍牌，按筹码分级、克制矩阵反制。
        胜负已定则弃权省成本。同帧多窗口选最高筹码者。
        """
        contests = self._my_active_contests(world)
        if not contests:
            return None
        c = contests[0]
        cid = c.get("contestId")
        if not cid:
            return None
        ri = c.get("roundIndex") or 1
        played = self._window_played.get(cid)
        if played and ri in played:
            return None

        my_color = self._my_color(c)
        my_pt, opp_pt = self._points(c, my_color)
        stakes = self._stakes(c)
        allow_bing = stakes >= 3 and (me.guard_action_point or 0) > 0
        allow_xian = stakes >= 2 and me.freshness >= 80 \
            and me.good_fruit >= config.KEEP_GOOD_FRUIT_MIN + 1
        avail = self._available_cards(me, allow_bing, allow_xian)

        if my_pt >= 2 or opp_pt >= 2:
            card = Card.ABSTAIN
        else:
            opp_card = self._opp_last_card(world, c, my_color)
            if ri >= 2 and opp_card and opp_card != Card.ABSTAIN:
                card = self._pick_counter(opp_card, avail)
                if card is None and opp_card in avail:
                    card = opp_card
                elif card is None:
                    card = Card.ABSTAIN
            else:
                card = self._lead_card(me, avail, stakes >= 3)

        self._window_played.setdefault(cid, set()).add(ri)
        return actions.window_card(cid, card)

    def _my_active_contests(self, world):
        contests = world.my_contests()
        if not contests:
            return []
        active_ids = {c.get("contestId") for c in contests}
        for cid in list(self._window_played):
            if cid not in active_ids:
                del self._window_played[cid]
        return sorted(contests, key=self._stakes, reverse=True)

    def _stakes(self, c):
        return self._STAKES_RANK.get(c.get("contestType"), 1)

    def _my_color(self, c):
        return "RED" if c.get("redPlayerId") == self.ctx.player_id else "BLUE"

    def _points(self, c, my_color):
        if my_color == "RED":
            return (c.get("redPoint") or 0, c.get("bluePoint") or 0)
        return (c.get("bluePoint") or 0, c.get("redPoint") or 0)

    def _opp_last_card(self, world, c, my_color):
        opp_color = "BLUE" if my_color == "RED" else "RED"
        cid = c.get("contestId")
        best_ri, best_card = -1, None
        for e in world.events:
            if e.get("type") != "WINDOW_CARD_REVEAL":
                continue
            p = e.get("payload") or {}
            if p.get("contestId") != cid:
                continue
            eri = p.get("roundIndex")
            if eri is not None and eri > best_ri:
                best_ri = eri
                best_card = p.get("redCard") if opp_color == "RED" else p.get("blueCard")
        if best_card:
            return best_card
        cards = c.get("cards") or {}
        return cards.get(opp_color)

    def _available_cards(self, me, allow_bing, allow_xian):
        avail = []
        if allow_bing:
            avail.append(Card.BING_ZHENG)
        if allow_xian:
            avail.append(Card.XIAN_GONG)
        if me.resource_count(ResourceType.PASS_TOKEN) > 0 \
                or me.resource_count(ResourceType.OFFICIAL_PERMIT) > 0:
            avail.append(Card.YAN_DIE)
        if self._has_move_buff(me) or self._has_any_horse(me):
            avail.append(Card.QIANG_XING)
        return avail

    def _pick_counter(self, opp_card, avail):
        for card in self._BEATS.get(opp_card, ()):
            if card in avail:
                return card
        return None

    def _lead_card(self, me, avail, stakes_high):
        if stakes_high:
            for card in (Card.BING_ZHENG, Card.XIAN_GONG, Card.QIANG_XING, Card.YAN_DIE):
                if card in avail:
                    return card
        else:
            if self._has_move_buff(me) and Card.QIANG_XING in avail:
                return Card.QIANG_XING
            if Card.YAN_DIE in avail:
                return Card.YAN_DIE
        return avail[0] if avail else Card.ABSTAIN

    # ================================================================
    #  v3: 小分队（主动清障 + 探路宫门）
    # ================================================================

    def _maybe_squad_v3(self, world, me, gm, node, terminal):
        """v3 小分队：优先清障、其次探路宫门。"""
        if world.is_rush:
            return None
        avail = me.squad_available or 0
        if avail <= 0:
            return None

        # v3: 优先 SQUAD_CLEAR 前方障碍
        if avail >= 2:
            obstacle_ahead = self._find_obstacle_ahead(world, gm, node, terminal)
            if obstacle_ahead:
                key = (obstacle_ahead, "clear")
                if key not in self._squad_sent:
                    self._squad_sent.add(key)
                    return actions.squad_clear(obstacle_ahead)

        # 探路宫门
        if avail >= 1:
            scout = self._maybe_scout_gate(world, me, gm, node)
            if scout:
                return scout

        return None

    def _find_obstacle_ahead(self, world, gm, node, terminal):
        """v3: 查找路径前方第一个障碍节点（跳过前2跳以留延迟落地余量）。"""
        if not terminal:
            return None
        path, _ = gm.time_optimal_path(node, terminal)
        if not path:
            return None
        for i, nid in enumerate(path):
            if i < config.SQUAD_AHEAD_MIN_HOPS:
                continue
            ns = world.node(nid)
            if ns and ns.has_obstacle:
                return nid
        return None

    def _maybe_scout_gate(self, world, me, gm, node):
        if self._gate_scout_sent or (me.squad_available or 0) < 1:
            return None
        gate = gm.gate_node
        if not gate or not node:
            return None
        ns = world.node(gate)
        if ns and ns.my_scout_marks(me.team_id):
            self._gate_scout_sent = True
            return None
        _, frames = gm.time_optimal_path(node, gate)
        if frames == _INF or not (config.GATE_SCOUT_MIN_FRAMES <= frames <= config.GATE_SCOUT_MAX_FRAMES):
            return None
        self._gate_scout_sent = True
        return actions.squad_scout(gate)

    # ================================================================
    #  拒绝反馈
    # ================================================================

    def _apply_rejection_feedback(self, world):
        la = self._last_main_action
        if la is None:
            return
        self._detect_fp_result(world, la)
        # v4.3: 跟踪任务完成事件
        self._detect_task_complete(world)
        code = self._my_reject_code(world)
        if not code:
            return
        if code == "PROCESS_REQUIRED":
            self._processed_here = False
            return
        if la.get("action") == Action.MOVE and code in _MOVE_BLOCK_CODES:
            tgt = la.get("targetNodeId")
            if tgt:
                self._cooldown[tgt] = (world.round or 0) + config.REJECT_BLOCK_ROUNDS
        # v4.3: 任务领取被拒 → 记录失败，避免死循环重试
        if la.get("action") == "CLAIM_TASK":
            tid = la.get("taskId")
            if tid:
                self._task_fail_count[tid] = self._task_fail_count.get(tid, 0) + 1
                self._task_attempted.discard(tid)

    def _detect_task_complete(self, world):
        """v4.3: 检测任务完成 → 清除任务跟踪状态。"""
        pid = self.ctx.player_id
        for e in (world.events or []):
            if e.get("type") == "TASK_COMPLETE":
                payload = e.get("payload") or {}
                if payload.get("playerId") == pid:
                    tid = payload.get("taskId")
                    if tid:
                        self._task_attempted.discard(tid)
                        self._task_fail_count.pop(tid, None)

    def _detect_fp_result(self, world, last_action):
        if last_action.get("action") != Action.FORCED_PASS:
            return
        tgt = last_action.get("targetNodeId")
        if not tgt:
            return
        if world.me and world.me.current_node_id == tgt:
            self._fp_failures.pop(tgt, None)

    def _my_reject_code(self, world):
        pid = self.ctx.player_id
        prev = (world.round or 0) - 1
        for r in world.action_results:
            if r.get("playerId") == pid and r.get("round") == prev \
                    and r.get("accepted") is False:
                return r.get("errorCode")
        for e in world.events:
            if e.get("type") in ("ACTION_REJECTED", "INVALID_ACTION"):
                p = e.get("payload") or {}
                if p.get("playerId") == pid:
                    return p.get("errorCode")
        return None

    # ================================================================
    #  辅助
    # ================================================================

    def _extract_main(self, result):
        for a in result:
            act = a.get("action", "")
            if act.startswith("SQUAD_") or act == Action.WINDOW_CARD:
                continue
            return a
        return None

    def _has_move_buff(self, me):
        for b in me.buffs:
            if b.get("type") in _MOVE_BUFF_TYPES and (b.get("remainingRound", 0) or 0) > 0:
                return True
        return False

    def _has_any_horse(self, me):
        return (me.resource_count(ResourceType.FAST_HORSE) > 0
                or me.resource_count(ResourceType.SHORT_HORSE) > 0
                or self._has_move_buff(me))

    def _far_from_terminal(self, gm, node, terminal):
        if not node or not terminal:
            return False
        return gm.route_distance(node, terminal) > config.HORSE_MIN_REMAINING_DISTANCE

    def _can_afford(self, world, gm, node, extra_frames, terminal, safety_margin=None):
        if terminal is None:
            return True
        margin = safety_margin if safety_margin is not None else config.DELIVER_TIME_SAFETY_MARGIN
        _, travel = self._time_path(world, node, terminal)
        if travel == _INF:
            return False
        end = (world.round or 0) + extra_frames + travel + margin
        return end <= (self.ctx.duration_round or 600)

    def _update_process_memory(self, world, me, node):
        if node != self._stay_node:
            self._stay_node = node
            self._processed_here = False
        gm = self.ctx.game_map
        is_proc_node = gm is not None and node in gm.process_nodes
        transition_done = (is_proc_node
                           and self._prev_state == PlayerState.PROCESSING
                           and me.state != PlayerState.PROCESSING)
        if self._saw_process_complete(world) or transition_done:
            self._processed_here = True

    def _saw_process_complete(self, world):
        pid = self.ctx.player_id
        for e in world.events:
            if e.get("type") == "PROCESS_COMPLETE":
                if (e.get("payload") or {}).get("playerId") == pid:
                    return True
        return False
