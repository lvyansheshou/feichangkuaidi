r"""决策引擎 v4.2 — 鲜度 + 好果最高优先。

1 鲜度 ≈ 1.8 分，1 好果 ≈ 1.8 分。两者是得分核心，
优先级高于快速交付、任务绕路、窗口博弈。

v4.1→v4.2:
  1. 冰鉴阈值 94: 鲜度刚跌就用冰鉴，几乎不等待
  2. 好果保护 3: 清障/攻坚至少保留 3 好果，宁愿绕行
  3. RUSH 立即护果: 鲜度 < 98 即用护果令
  4. 任务绕路降到 20 帧: 鲜度损失 > 任务收益
  5. 障碍优先绕行: 省好果，宁多花几帧
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
        # 精确冰鉴
        rescue = self._freshness_rescue_v2(me, world)
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

        dst = self._task_detour_target(world, me, gm, node, terminal) or terminal
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

    def _freshness_rescue_v2(self, me, world=None):
        """v4.2: 冰鉴最高优先。鲜度 < 94 即用，几乎不等待。

        冰鉴 +10 鲜度 ≈ 18 分。早用 = 早保住鲜度分。
        """
        ice = me.resource_count(ResourceType.ICE_BOX)
        if ice <= 0 or me.freshness <= 0:
            return None

        # 直接阈值触发（94，比 v4 的 88 再高 6 点）
        if me.freshness < config.ICE_BOX_USE_BELOW:
            return actions.use_resource(ResourceType.ICE_BOX)

        # 预判转坏：距下一阈值 ≤ 8 帧时提前用
        est_loss = 0.065
        wt = world.active_weather_type() if world else None
        if wt == "HOT":
            est_loss = 0.065 * 1.5
        elif wt == "MOUNTAIN_FOG":
            est_loss = 0.07
        for threshold in (95, 90, 85, 80, 75, 70, 60, 50, 40):
            if me.freshness < threshold:
                continue
            frames_to = (me.freshness - threshold) / max(est_loss, 0.001)
            if 1 <= frames_to <= 8:
                return actions.use_resource(ResourceType.ICE_BOX)

        # 酷暑预告 → 立即冰鉴
        if world is not None:
            upcoming = world.upcoming_weather(within_frames=25)
            if upcoming and upcoming.get("type") == "HOT":
                if me.freshness < 95:
                    return actions.use_resource(ResourceType.ICE_BOX)

        # v4.3: 有多余冰鉴 → 不攒，直接用
        if ice >= 2 and me.freshness < 97:
            return actions.use_resource(ResourceType.ICE_BOX)
        # v4.3: 低于目标鲜度且还有冰鉴 → 立即用
        if me.freshness < config.TARGET_FRESHNESS and ice >= 1:
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
        if me.freshness < config.RUSH_PROTECT_BELOW and me.freshness > 30:
            return actions.rush_protect()
        return None

    def _rush_speed_warranted(self, world, me, gm, node, terminal):
        if not world.is_rush or me.delivered or (me.rush_tactic_used_count or 0) > 0:
            return None
        if me.good_fruit < config.KEEP_GOOD_FRUIT_MIN + 2:
            return None
        if me.freshness < config.RUSH_PROTECT_BELOW:
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
        """v4.3: 任务领取 + 防重试风暴。失败≥3次的任务不再尝试。"""
        pid = self.ctx.player_id
        MAX_TASK_FAILURES = 3
        for t in world.active_tasks():
            if t.get("nodeId") != node:
                continue
            tid = t.get("taskTemplateId")
            if tid in config.SKIP_TASK_TEMPLATES:
                continue
            task_id = t.get("taskId")
            # 防重试：已失败≥3次的任务跳过
            if self._task_fail_count.get(task_id, 0) >= MAX_TASK_FAILURES:
                continue
            # 已尝试过但不确定结果的任务，标记为尝试中
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
        # 冰鉴优先 — 当前节点
        if (me.resource_count(ResourceType.ICE_BOX) < config.CLAIM_ICE_BOX_KEEP
                and ns.resource_available(ResourceType.ICE_BOX)):
            if self._can_afford(world, gm, node, 2, terminal):
                return actions.claim_resource(node, ResourceType.ICE_BOX)
        # 冰鉴 — 前方路径节点探测
        ice_detour = self._find_ice_on_route(world, me, gm, node, terminal)
        if ice_detour:
            return ice_detour
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
        """v4.3: 在前方路径上找冰鉴资源。冰鉴不足时主动探测。

        沿时间最优路径向前看 3 跳，如果有节点提供冰鉴且距路径不远，
        返回 CLAIM_RESOURCE 动作（稍微绕路去领）。
        """
        if me.resource_count(ResourceType.ICE_BOX) >= config.CLAIM_ICE_BOX_KEEP:
            return None
        if not terminal:
            return None
        path, _ = gm.time_optimal_path(node, terminal)
        if not path or len(path) < 2:
            return None
        # 沿路径向前看
        for i in range(1, min(4, len(path))):
            nid = path[i]
            ns = world.node(nid)
            if ns and ns.resource_available(ResourceType.ICE_BOX):
                # 该节点在路径上且可领冰鉴
                if nid == path[1]:
                    # 下一跳就是 → 到达时自然会领
                    return None
                # 需要稍微绕路 → 领了再回来
                if self._can_afford(world, gm, node, 3, terminal):
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
        """v4: 大幅降低任务绕路热情。逻辑：
        - 任务分 > 90 → 不再绕路（上限 180，剩余收益递减）
        - 绕路额外帧需 < 配置阈值
        - 鲜度代价需可接受（绕路帧 × 鲜度损耗系数 < 预期任务分增益）
        """
        self._track_task_completion(world)
        base = self._task_base or me.task_score or 0
        # v4: 任务分 ≥ 80 即停止绕路（v3 是 90）
        if base >= 80 or not terminal:
            return None
        pid = self.ctx.player_id
        _, direct = gm.time_optimal_path(node, terminal)
        if direct == _INF:
            return None
        budget = config.TASK_DETOUR_MAX_EXTRA
        # v4: 鲜度代价估算：每帧 ≈ 0.06 鲜度 × 1.8 分/鲜度 ≈ 0.11 分/帧
        # 30分任务需要 ≤ 270 帧额外才值得，但我们更保守
        freshness_budget = (me.freshness - 70) / 0.06  # 最多损失到 70 鲜度
        effective_budget = min(budget, int(freshness_budget * 0.5))
        if effective_budget <= 0:
            return None

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
            _, c1 = gm.time_optimal_path(node, tn)
            _, c2 = gm.time_optimal_path(tn, terminal)
            if c1 == _INF or c2 == _INF:
                continue
            pr = t.get("processRound", 0) or 0
            extra = (c1 + pr + c2) - direct
            if 0 <= extra <= effective_budget and extra < best_extra \
                    and self._can_afford(world, gm, node, extra, terminal):
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
    #  L3 v3: 智能天气路由 + 障碍绕行权衡 + 交付守卫
    # ================================================================

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

        # ── 动态鲜度权重（目标 85%）──
        remaining_budget = duration - current_round - config.DELIVER_TIME_MARGIN
        # 距目标鲜度越远 → 越急迫
        freshness_gap = max(0, me.freshness - config.TARGET_FRESHNESS)
        freshness_urgency = max(0.5, (100 - me.freshness) / 20.0)
        # 时间越充裕 → 权重越大
        if cost_base > 0 and remaining_budget > cost_base:
            time_slack = min(3.0, (remaining_budget - cost_base) / max(1, cost_base) * 2)
        else:
            time_slack = 0
        # 天气加剧鲜度损耗 → 权重加大
        weather_bonus = 1.5 if active_wt in ("HOT", "MOUNTAIN_FOG") else 0
        fw = min(5.0, max(1.0, freshness_urgency + time_slack + weather_bonus))

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
        """v4.2: 优先保护好果。能绕就绕，能冲就冲，不清障/不攻坚。"""
        ns = world.node(nxt)
        if ns and ns.has_obstacle:
            t04 = self._find_t04(world, nxt)
            if t04:
                self._fp_failures.pop(nxt, None)
                return [actions.claim_task(t04.get("taskId"))]
            # v4.2: 只有好果充裕(>4)才清障，否则强制通行
            if me.good_fruit > config.KEEP_GOOD_FRUIT_MIN + 1:
                self._fp_failures.pop(nxt, None)
                return [actions.clear_obstacle(nxt)]
            return [actions.forced_pass(nxt)]

        owner = ns.active_guard_owner() if ns else None
        if owner and owner != me.team_id:
            plan = self._plan_attack(me, ns)
            if plan is not None:
                # v4.2: 只有消耗低(≤1好果)才攻坚
                g_used = plan[0]
                if g_used <= 1:
                    self._fp_failures.pop(nxt, None)
                    g, b, bo = plan
                    return [actions.break_guard(nxt, good_fruit=g, bad_fruit=b,
                                               rush_tactic=(Action.BREAK_ORDER if bo else None))]
            # 攻击不可行或代价太高 → 强制通行
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
    #  窗口出牌
    # ================================================================

    def _window_card(self, world, me):
        contests = world.my_contests()
        if not contests:
            return None
        c = contests[0]
        cid = c.get("contestId")
        if not cid:
            return None
        available = []
        if (me.guard_action_point or 0) > 0:
            available.append(Card.BING_ZHENG)
        # v4.2: XIAN_GONG 仅在好果充裕(>5)时使用
        if me.freshness >= 80 and me.good_fruit > config.KEEP_GOOD_FRUIT_MIN + 2:
            available.append(Card.XIAN_GONG)
        if me.resource_count(ResourceType.PASS_TOKEN) > 0 \
                or me.resource_count(ResourceType.OFFICIAL_PERMIT) > 0:
            available.append(Card.YAN_DIE)
        if self._has_any_horse(me):
            available.append(Card.QIANG_XING)
        if not available:
            return actions.window_card(cid, Card.ABSTAIN)
        for card in [Card.BING_ZHENG, Card.XIAN_GONG, Card.YAN_DIE, Card.QIANG_XING]:
            if card in available:
                return actions.window_card(cid, card)
        return actions.window_card(cid, Card.ABSTAIN)

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

    def _can_afford(self, world, gm, node, extra_frames, terminal):
        if terminal is None:
            return True
        _, travel = gm.time_optimal_path(node, terminal)
        if travel == _INF:
            return False
        end = (world.round or 0) + extra_frames + travel + config.DELIVER_TIME_MARGIN
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
