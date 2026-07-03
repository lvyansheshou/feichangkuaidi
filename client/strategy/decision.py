"""决策引擎。

分层（每层在前一层之上，"稳定交付"始终是硬约束）：
- M3 基线：最短路推进 → 固定处理 → 宫门 RUSH 验核 → 交付。
- M4 收益：时间感知路由；机会式皇榜任务/资源领取；冰鉴保鲜；马加速；护果令。
- M5 对抗：阻塞感知路由(绕行)；突破(障碍 T04/CLEAR/强制通行；敌卡攻坚含破关令/强制通行)；窗口出牌；
  疾行令/护果令二选一；小分队探路宫门。
- M7 能力补全：
  * 拒绝反馈：读上一帧 actionResults/events，PROCESS_REQUIRED 强制处理、移动阻塞类临时拉黑目标（防循环）。
  * 情报(INTEL)：探路前方处理点/宫门（射程 15）减处理帧；并机会式领取情报。
  * 绕行 vs 清障权衡：绕行远超就地清障成本时改为清障。
  * 绕路做任务：任务分<90 且时间预算允许时，向近处任务节点绕行以拉高任务分。
  * 防御性小分队：预清路线前方障碍(SQUAD_CLEAR)/削弱前方敌卡(SQUAD_WEAKEN)。
  * 进攻干扰(默认关闭，config.ENABLE_OFFENSIVE)：关键关隘主动设卡。
- M8 博弈对抗（对手建模 + 自适应）：
  * 对手轨迹追踪与路径预测
  * 战略态势判定（leading/racing/trailing/contested/sprinting）
  * 窗口出牌反制（基于历史模式）
  * 条件设卡（仅在领先且在必经节点时）
  * FORCED_PASS 退避（防止无限循环）
  * 态势感知任务优先级

策略与通信解耦：只依赖 core.WorldState / GameMap，不 import socket。
"""

import config
from core.game_map import GameMap
from protocol import actions
from protocol.enums import Action, Card, PlayerState, ResourceType
from strategy.opponent_model import OpponentModel

_IDLE_LIKE = (PlayerState.IDLE, PlayerState.COST_BANKRUPT, None)
_MOVE_BUFF_TYPES = frozenset({ResourceType.FAST_HORSE, ResourceType.SHORT_HORSE, "RUSH_SPEED"})
_MOVE_BLOCK_CODES = frozenset({"MOVE_BLOCKED_BY_GUARD", "TARGET_NOT_REACHABLE",
                               "MOVE_EDGE_NOT_FOUND", "OBJECT_BUSY"})
_INF = float("inf")


class GameContext:
    """跨帧静态/半静态上下文（开局缓存）。承载地图镜像 GameMap 与本方身份。"""

    def __init__(self, player_id, team_id=None, camp=None, start_data=None):
        self.player_id = int(player_id)
        self.team_id = team_id
        self.camp = camp
        start_data = start_data or {}
        self.match_id = start_data.get("matchId")
        self.duration_round = start_data.get("durationRound") or 600
        self.task_templates = start_data.get("taskTemplates", [])
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
        self._gate_scout_sent = False
        self._cooldown = {}          # nodeId -> 拉黑截止回合（拒绝反馈）
        self._last_main_action = None
        self._squad_sent = set()     # (nodeId, kind) 已派出的小分队目标，避免重复
        # M8 博弈对抗
        self.opponent = OpponentModel()
        self._fp_failures = {}       # nodeId -> FORCED_PASS 连续失败次数（退避用）
        self._fp_last_node = None    # 上次尝试 FORCED_PASS 的目标节点
        # 窗口争夺优化
        self._window_rounds = 0      # 连续参与窗口的帧数
        self._last_contested_node = None  # 上次触发窗口的节点
        self._window_node_skip = {}  # nodeId -> 跳过资源/任务直到此回合

    def decide(self, world):
        me = world.me
        gm = self.ctx.game_map
        if me is None or gm is None:
            return []

        node = me.current_node_id
        self._apply_rejection_feedback(world)
        self._update_process_memory(world, me, node)

        # M8: 更新对手模型 + 评估战略态势
        self.opponent.update(world)
        self.opponent.assess_posture(world, me, gm)

        terminal = gm.terminal_nodes[0] if gm.terminal_nodes else None
        gate = gm.gate_node

        result = []
        try:
            if me.delivered or me.state == PlayerState.DELIVERED:
                result = []
                return result

            # M8: 自适应窗口出牌（基于对手模型）
            card = self._window_card_adaptive(world, me)
            if card:
                result = [card]
                return result

            if me.state in (PlayerState.MOVING, PlayerState.WAITING):
                # P1-5: 先加速再检查窗口（MOVING 态优先移动效率）
                horse = self._maybe_horse(me, gm, terminal)
                if horse:
                    result = [horse]
                    return result
                # 无加速需求时才处理窗口
                card = self._window_card_adaptive(world, me)
                if card:
                    result = [card]
                    return result
                result = []
                return result
            if me.state not in _IDLE_LIKE:
                result = []
                return result
            main = self._plan(world, me, gm, node, terminal, gate)
            squad = self._maybe_squad(world, me, gm, node, terminal)
            result = main + ([squad] if squad else [])
            return result
        finally:
            self._prev_state = me.state
            self._last_main_action = self._extract_main(result)

    # ---- 主计划（空闲态，在节点）----

    def _plan(self, world, me, gm, node, terminal, gate):
        rescue = self._freshness_rescue(me)
        if rescue:
            return [rescue]

        if terminal and node == terminal:
            if me.verified and me.good_fruit > 0 and me.freshness > 0:
                return [actions.deliver()]
            if not me.verified and gate:
                # S15→S14 特殊返回：无视设卡和障碍（任务书 §2.3.1）
                if gate in gm.neighbors(node):
                    return [actions.move(gate)]
                return self._advance(world, me, gm, node, gate, terminal)
            # 已验核但不满足交付条件 → WAIT 或返回 S14
            if me.verified and gate and gate in gm.neighbors(node):
                return [actions.move(gate)]
            return []

        if gate and node == gate:
            if not me.verified:
                if world.is_rush:
                    # M8: 破关令加速验核 6→3 帧（成本：坏果≥2 优先，否则 1 好果）
                    if (me.rush_tactic_used_count or 0) == 0:
                        if me.bad_fruit >= 2 or me.good_fruit > config.KEEP_GOOD_FRUIT_MIN:
                            return [actions.verify_gate(rush_tactic=Action.BREAK_ORDER)]
                    rp = self._maybe_rush_protect(world, me)
                    if rp:
                        return [rp]
                    return [actions.verify_gate()]
                return self._opportunistic(world, me, gm, node, terminal) or []
            return self._advance(world, me, gm, node, terminal, terminal)

        if node in gm.process_nodes and not self._processed_here:
            # 窗口冷却检测：若 PROCESS 被窗口冷却拒绝，不盲重试
            if self._is_window_cooldown(world, node):
                return []  # WAIT 等冷却结束
            return [actions.process()]

        intel = self._maybe_intel(world, me, gm, node, terminal)
        if intel:
            return [intel]

        rp = self._maybe_rush_protect(world, me)
        if rp:
            return [rp]

        # ★ 控场区感知：进入 S08-S10 区域 → 评估抢先策略
        in_control_zone = self._in_control_zone(gm, node)
        if in_control_zone and self.opponent.posture == "contested":
            # 胶着态 + 控场区 → 抢先冲刺，放弃任务/资源
            dst = terminal
            return self._advance(world, me, gm, node, dst, terminal)

        # M8: 态势感知任务优先级
        opp = self._opportunistic(world, me, gm, node, terminal)
        if opp:
            return opp

        # ★ 主动攻击：顺路狩猎敌方设卡悬赏
        bounty_attack = self._active_bounty_hunt(world, me, gm, node, terminal)
        if bounty_attack:
            return [bounty_attack]

        # M8: 条件设卡（仅在领先且在必经节点时）
        guard = self._maybe_set_guard(world, me, gm, node)
        if guard:
            return [guard]

        # 绕路做任务：落后时更激进，领先时保守
        if self.opponent.posture in ("trailing",):
            # 落后 → 扩大任务绕路预算追分
            dst = self._task_detour_target(world, me, gm, node, terminal,
                                           extra_budget=40) or terminal
        elif self.opponent.posture in ("leading",):
            # 领先 → 收紧预算，优先保持领先
            dst = self._task_detour_target(world, me, gm, node, terminal,
                                           extra_budget=-20) or terminal
        else:
            dst = self._task_detour_target(world, me, gm, node, terminal) or terminal

        if dst:
            return self._advance(world, me, gm, node, dst, terminal)
        return []

    def _opportunistic(self, world, me, gm, node, terminal):
        # ★ 任务天花板：≥90 后只有顺路(≤10帧)才做任务
        if (me.task_score or 0) < 90 or self._task_is_on_path(world, me, gm, node, terminal):
            task = self._maybe_task(world, me, gm, node, terminal)
            if task:
                return [task]
        claim = self._maybe_claim(world, me, gm, node, terminal)
        if claim:
            return [claim]
        return None

    def _task_is_on_path(self, world, me, gm, node, terminal):
        """当前节点的任务是否在去路上（几乎不绕路）。"""
        if not terminal or not node:
            return False
        for t in world.active_tasks():
            if t.get("nodeId") != node:
                continue
            if t.get("taskTemplateId") in config.SKIP_TASK_TEMPLATES:
                continue
            # 从当前节点做完任务后到终点的路径
            _, direct = gm.time_optimal_path(node, terminal)
            _, via_task = gm.time_optimal_path(node, terminal)  # 做完任务后还是从 node 出发
            if direct != _INF:
                return True  # 当前节点就在去路上
        return False

    # ---- 阻塞感知推进 + 突破 + 绕行/清障权衡 ----

    def _advance(self, world, me, gm, src, dst, terminal):
        blocked = self._blocked_nodes(world, me)

        # ★ 态势感知多路径选择：在起点/岔路口，根据对手位置选策略
        if ((src == gm.start_node or len(gm.neighbors(src)) >= 3)
                and not blocked):
            best_path = self._select_strategic_path(world, me, gm, src, dst, blocked)
            if best_path and len(best_path) > 1:
                nxt = best_path[1]
                nxt_ns = world.node(nxt)
                if not (nxt_ns and nxt_ns.has_obstacle
                        and not self._is_cooldown(world, nxt)):
                    if nxt not in blocked:
                        return [actions.move(nxt)]

        # ★ 天气感知路由：当前天气 + 预告天气影响边权
        active_wt = world.active_weather_type() if world else None
        upcoming = world.upcoming_weather(within_frames=30) if world else None

        # 当前有天气 → 用天气感知路径
        if active_wt in ("HOT", "HEAVY_RAIN", "MOUNTAIN_FOG"):
            path_b, cost_b = gm.weather_adjusted_path(
                src, dst, weather_type=active_wt, blocked=blocked)
            path_u, cost_u = gm.weather_adjusted_path(
                src, dst, weather_type=active_wt)
        else:
            path_b, cost_b = gm.time_optimal_path(src, dst, blocked=blocked)
            path_u, cost_u = gm.time_optimal_path(src, dst)

        # 天气预告感知 — 酷暑/山雾将至时倾向官道
        prefer_road = (upcoming and upcoming["type"] in ("HOT", "MOUNTAIN_FOG"))

        if prefer_road and path_b and len(path_b) > 1:
            # 检查 path_u（直路）是否大量走山路 → 如果是，倾向 path_b（绕行/官道）
            u_types = self._path_route_types(gm, path_u)
            b_types = self._path_route_types(gm, path_b)
            u_mountain_ratio = sum(1 for t in u_types if t == "MOUNTAIN") / max(1, len(u_types))
            b_mountain_ratio = sum(1 for t in b_types if t == "MOUNTAIN") / max(1, len(b_types))
            # 直路多山路且绕行少山路 → 考虑绕行
            if u_mountain_ratio > 0.3 and b_mountain_ratio < u_mountain_ratio - 0.1:
                if cost_b - cost_u < 40:  # 绕行代价可接受
                    return [actions.move(path_b[1])]

        if path_b and len(path_b) > 1 and path_u and len(path_u) > 1:
            nxt_u = path_u[1]
            ns = world.node(nxt_u)

            # P0-1: 下一跳有障碍 → 比较"清障直行" vs "绕行"的预估得分
            if ns and ns.has_obstacle and not self._is_cooldown(world, nxt_u):
                choice = self._compare_clear_vs_detour(
                    world, me, gm, nxt_u, path_u, cost_u, path_b, cost_b)
                if choice == "detour":
                    speed = self._rush_speed_warranted(world, me, gm, src, terminal)
                    if speed:
                        return [speed]
                    return [actions.move(path_b[1])]
                if choice == "clear":
                    return self._breakthrough(world, me, gm, nxt_u, terminal)

            # 绕行 vs 清障权衡
            if (cost_b - cost_u > config.REROUTE_VS_CLEAR_EXTRA
                    and me.good_fruit > config.KEEP_GOOD_FRUIT_MIN):
                return self._breakthrough(world, me, gm, nxt_u, terminal)

            speed = self._rush_speed_warranted(world, me, gm, src, terminal)
            if speed:
                return [speed]
            return [actions.move(path_b[1])]

        # 无法绕行：沿忽略阻塞的最短路，突破下一个阻塞节点
        if not path_u or len(path_u) < 2:
            return []
        return self._breakthrough(world, me, gm, path_u[1], terminal)

    def _compare_clear_vs_detour(self, world, me, gm, obstacle_node,
                                  path_direct, cost_direct, path_detour, cost_detour):
        """比较清障直行 vs 绕行：返回 'clear' / 'detour' / 'continue'。

        综合帧数 + 鲜度损耗 + 好果成本，选预估得分更高的方案。
        """
        # 清障成本：6 帧 + 1 好果
        clear_frames = 6
        clear_good_cost = 1

        # 直行路径总帧 = 清障帧 + 直行旅行帧
        direct_total = clear_frames + cost_direct

        # 绕行路径总帧
        detour_total = cost_detour

        # 鲜度差异：估算直行路径 vs 绕行路径的鲜度损耗
        # 直行路径的路线类型（取第一条边）
        direct_route = self._path_route_types(gm, path_direct)
        detour_route = self._path_route_types(gm, path_detour)

        direct_freshness = self._estimate_freshness_cost(direct_total, direct_route)
        detour_freshness = self._estimate_freshness_cost(detour_total, detour_route)

        # 折算为近似分: 1帧≈0.12分, 1好果≈1.8分, 1鲜度≈1.8分
        direct_score_cost = (direct_total * 0.12 + clear_good_cost * 1.8
                             + direct_freshness * 1.8)
        detour_score_cost = detour_total * 0.12 + detour_freshness * 1.8

        # 1.5 分容差（避免在极接近时反复横跳）
        if detour_score_cost + 1.5 < direct_score_cost:
            return "detour"
        if direct_score_cost + 1.5 < detour_score_cost:
            return "clear"
        return "continue"  # 打平，走原逻辑

    def _select_strategic_path(self, world, me, gm, src, dst, blocked):
        """态势感知路径选择。

        核心原则：比赛在 S10 决胜负。路径选择服务于"抢先到达 S10"。
        - leading(领先>50帧): 走官道 — 资源+鲜度优势，稳扎稳打
        - racing(领先10-50帧): 走官道 — 保持领先，不冒险
        - contested(±20帧): 走山路 — 速度优先，抢先控场
        - trailing(落后>20帧): 走山路 — 最大速度追分
        """
        paths = gm.enumerate_paths(src, dst, max_paths=3, blocked=blocked)
        if not paths:
            return None
        if len(paths) < 2:
            return paths[0][0]

        frames_list = [c for _, c in paths]
        # 帧数差太大 → 直接选最短（没有真正选择）
        if max(frames_list) - min(frames_list) > 80:
            return paths[0][0]

        posture = self.opponent.posture
        task_done = (me.task_score or 0) >= 90

        # 收集资源/任务节点（任务已满时不奖励任务）
        resource_nodes = {nid for nid, ns in world.node_states.items() if ns.resource_stock}
        task_nodes = set()
        if not task_done:
            for t in (world.active_tasks() if hasattr(world, 'active_tasks') else []):
                task_nodes.add(t.get("nodeId"))

        # ★ 态势权重
        if posture in ("contested", "trailing"):
            # 速度优先：帧数权重 ↑，鲜度权重 ↓，不关心资源/任务
            FRAME_WEIGHT = 1.5
            FRESH_WEIGHT = 15.0
            RESOURCE_BONUS = 0
            TASK_BONUS = 0
        else:
            # leading/racing: 鲜度保护优先
            FRAME_WEIGHT = 1.0
            FRESH_WEIGHT = 35.0
            RESOURCE_BONUS = -2
            TASK_BONUS = 0 if task_done else -3

        best_path, best_score = None, float("inf")
        for path, frames in paths:
            # 帧数
            score = frames * FRAME_WEIGHT
            # 鲜度
            freshness_loss = self._estimate_freshness_cost(
                frames, self._path_route_types(gm, path))
            score += freshness_loss * FRESH_WEIGHT
            # 资源
            for n in path:
                if n in resource_nodes:
                    score += RESOURCE_BONUS
                if n in task_nodes:
                    score += TASK_BONUS
                ns = world.node(n)
                if ns and ns.has_obstacle:
                    score += 15  # 障碍风险
            if score < best_score:
                best_score = score
                best_path = path

        return best_path

    def _path_route_types(self, gm, path):
        """返回路径各边的路线类型列表。"""
        types = []
        for i in range(len(path) - 1):
            e = gm.edge_between(path[i], path[i + 1])
            if e:
                types.append(e.route_type)
        return types

    def _estimate_freshness_cost(self, frames, route_types):
        """估算路径的鲜度损耗。"""
        if not route_types:
            return frames * 0.06  # 默认均值
        # 按路线类型加权
        from core.rules import FRESHNESS_LOSS_MOVE, FRESHNESS_LOSS_BASE
        total_loss = 0.0
        frames_per_edge = frames / len(route_types) if route_types else frames
        for rt in route_types:
            total_loss += frames_per_edge * FRESHNESS_LOSS_MOVE.get(rt, 0.065)
        # 加上固定处理帧（估算 10%）
        total_loss += frames * 0.1 * FRESHNESS_LOSS_BASE
        return total_loss

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
        ns = world.node(nxt)
        if ns and ns.has_obstacle:
            t04 = self._find_t04(world, nxt)
            if t04 and self._can_afford(world, gm, me.current_node_id, t04.get("processRound", 6) or 6, terminal):
                self._fp_failures.pop(nxt, None)  # T04 不是 FORCED_PASS，重置计数
                return [actions.claim_task(t04.get("taskId"))]
            if me.good_fruit > config.KEEP_GOOD_FRUIT_MIN:
                self._fp_failures.pop(nxt, None)
                return [actions.clear(nxt)]
            # 障碍 FORCED_PASS 不受 retry 限制（无窗口争夺，必然成功）
            return [actions.forced_pass(nxt)]

        owner = ns.active_guard_owner() if ns else None
        if owner and owner != me.team_id:
            # ★ 悬赏狩猎：必经节点的敌方设卡可能有悬赏
            bounty_value = self._check_bounty(world, nxt)
            plan = self._plan_attack(world, me, ns)
            if plan is not None:
                self._fp_failures.pop(nxt, None)  # 攻坚重置 FORCED_PASS 计数
                g, b, bo = plan
                # 有悬赏时优先攻坚（即使代价稍高）
                return [actions.break_guard(nxt, good_fruit=g, bad_fruit=b,
                                            rush_tactic=(Action.BREAK_ORDER if bo else None))]

            # M8: FORCED_PASS 退避机制
            fails = self._fp_failures.get(nxt, 0)
            if fails >= config.FP_RETRY_LIMIT:
                if fails >= config.FP_RETRY_LIMIT + config.FP_RETRY_COOLDOWN:
                    self._fp_failures.pop(nxt, None)
                    self._fp_last_node = None
                    return [actions.forced_pass(nxt)]
                self._fp_failures[nxt] = fails + 1
                return []

            self._fp_failures[nxt] = fails + 1
            self._fp_last_node = nxt
            return [actions.forced_pass(nxt)]

        return [actions.move(nxt)]

    def _find_t04(self, world, node):
        for t in world.active_tasks():
            if t.get("taskTemplateId") == "T04" and t.get("nodeId") == node:
                return t
        return None

    def _check_bounty(self, world, node_id):
        """检查节点是否有可结算的破关悬赏。

        优先读取协议字段 rewardScore（精确值），回退到类型推断。
        """
        for b in (world.bounties or []):
            if b.get("nodeId") != node_id:
                continue
            if b.get("completed") or not b.get("active"):
                continue
            # 冷却中 → 不可结算
            cooldown = b.get("cooldownUntilRound", 0) or 0
            if cooldown > (world.round or 0):
                continue
            # 优先用协议精确值
            reward = b.get("rewardScore", 0) or 0
            if reward > 0:
                return reward
            # 回退类型推断
            btype = b.get("bountyType", "")
            return 18 if btype == "KEY_BOUNTY" else 10
        return 0

    def _active_bounty_hunt(self, world, me, gm, node, terminal):
        """主动狩猎：如果去路上有带悬赏的敌方设卡，评估是否值得主动攻击。

        条件：
        - 敌方设卡在必经路径上（顺路，不绕路）
        - 有可结算悬赏（10-18 分基础 + 20 分悬赏完成 = 30-38 分）
        - 我方好果/坏果足够攻坚
        - 态势不是 trailing（落后时不恋战）
        """
        if self.opponent.posture == "trailing":
            return None  # 落后时不狩猎，专注追分
        if not terminal:
            return None

        # 找去路上第一个有悬赏的敌方设卡
        path, _ = gm.time_optimal_path(node, terminal)
        if not path:
            return None

        for nid in path[1:]:  # 跳过当前节点
            ns = world.node(nid)
            if not ns:
                continue
            owner = ns.active_guard_owner()
            if not owner or owner == me.team_id:
                continue
            bounty = self._check_bounty(world, nid)
            if bounty <= 0:
                continue

            # 必须在相邻节点才能攻坚（任务书 §6.3.1）
            if nid not in gm.neighbors(node):
                continue

            # 能攻得动吗？
            plan = self._plan_attack(world, me, ns)
            if plan is None:
                continue

            # ★ 成本收益分析
            g, b, bo = plan
            attack_cost = g * 1.8 + b * 0.5  # 好果≈1.8分, 坏果≈0.5分
            bounty_value = bounty + 20       # 悬赏基础 + 完成奖励
            if bounty_value > attack_cost:
                return actions.break_guard(nid, good_fruit=g, bad_fruit=b,
                                           rush_tactic=(Action.BREAK_ORDER if bo else None))
        return None

    def _plan_attack(self, world, me, ns):
        defense = (ns.guard or {}).get("defense", 0) or 0
        if defense <= 0:
            return None
        bo = world.is_rush and (me.rush_tactic_used_count or 0) == 0
        bonus = 3 if bo else 0
        best = None
        # §6.3.1: 好果/坏果各最多 2 篓
        max_g = min(2, me.good_fruit - config.KEEP_GOOD_FRUIT_MIN)
        max_b = min(2, me.bad_fruit)
        for g in range(0, max_g + 1):
            if g > me.good_fruit or (me.good_fruit - g) < config.KEEP_GOOD_FRUIT_MIN:
                continue
            for b in range(0, max_b + 1):
                if b > me.bad_fruit:
                    continue
                if g * 2 + b * 3 + bonus >= defense:
                    if best is None or (g, b) < (best[0], best[1]):
                        best = (g, b, bo)
        return best

    # ---- 拒绝反馈（M7）----

    def _apply_rejection_feedback(self, world):
        la = self._last_main_action
        if la is None:
            return

        # M8: 检测 FORCED_PASS 结果
        self._detect_fp_result(world, la)

        # 检测窗口退出：如果上帧在窗口争夺，本帧不在 → 记录冷却
        if self._prev_state == PlayerState.CONTESTING and world.me.state != PlayerState.CONTESTING:
            self._mark_window_cooldown(world, world.me)

        code = self._my_reject_code(world)
        if not code:
            return
        if code == "PROCESS_REQUIRED":
            self._processed_here = False  # 强制在当前节点先完成固定处理
            return
        if la.get("action") == Action.MOVE and code in _MOVE_BLOCK_CODES:
            tgt = la.get("targetNodeId")
            if tgt:
                self._cooldown[tgt] = (world.round or 0) + config.REJECT_BLOCK_ROUNDS

        # 窗口冷却拒绝检测：PROCESS/CLAIM_TASK/CLAIM_RESOURCE 被拒绝 → 进入冷却等待
        _WINDOW_REJECT_ACTIONS = {Action.PROCESS, Action.CLAIM_TASK, Action.CLAIM_RESOURCE,
                                  Action.VERIFY_GATE}
        _WINDOW_REJECT_CODES = {"OBJECT_BUSY", "CONTEST_COOLDOWN", "ACTION_REJECTED",
                                "TASK_LOCKED", "RESOURCE_LOCKED"}
        if la.get("action") in _WINDOW_REJECT_ACTIONS and code in _WINDOW_REJECT_CODES:
            node = world.me.current_node_id if world.me else None
            if node:
                # 进入冷却：18 帧不重试（对齐 DOCK/OBSTACLE 冷却上限）
                self._window_node_skip[node] = (world.round or 0) + 18
                self._last_contested_node = node

    def _detect_fp_result(self, world, last_action):
        """检测 FORCED_PASS 成败，维护退避计数器。

        成功：到达目标节点（位置变化到目标）
        失败：进入 RESTING 状态（PASS 窗口失利）
        """
        if last_action.get("action") != Action.FORCED_PASS:
            return
        tgt = last_action.get("targetNodeId")
        if not tgt:
            return

        me = world.me
        if me is None:
            return

        # 成功：当前位置已到达目标
        if me.current_node_id == tgt:
            self._fp_failures.pop(tgt, None)
            self._fp_last_node = None
            return

        # 失败：进入 RESTING
        if me.state == PlayerState.RESTING:
            # 计数已在 _breakthrough 中增加，此处不重复
            return

        # 仍在 FORCED_PASSING：正常进行中

    def _my_reject_code(self, world):
        pid = self.ctx.player_id
        prev = (world.round or 0) - 1
        for r in world.action_results:
            if r.get("playerId") == pid and r.get("round") == prev and r.get("accepted") is False:
                return r.get("errorCode")
        for e in world.events:
            if e.get("type") in ("ACTION_REJECTED", "INVALID_ACTION"):
                p = e.get("payload") or {}
                if p.get("playerId") == pid:
                    return p.get("errorCode")
        return None

    # ---- 收益子策略（M4）----

    def _freshness_rescue(self, me, world=None):
        """阈值感知 + 天气预告冰鉴使用。

        1. 酷暑预告 → 提前囤鲜度
        2. 即将跌破 90/80/70/... 阈值 → 提前 3 帧预警
        3. 兜底固定阈值
        """
        ice = me.resource_count(ResourceType.ICE_BOX)
        if ice <= 0 or me.freshness <= 0:
            return None

        # P1-7: 酷暑预告即将生效（15 帧内）→ 现在用冰鉴，鲜度撑过酷暑
        if world is not None:
            upcoming = world.upcoming_weather(within_frames=15)
            if upcoming and upcoming["type"] == "HOT" and me.freshness < 88:
                return actions.use_resource(ResourceType.ICE_BOX)

        # 估算单帧鲜度损耗（考虑当前天气）
        est_loss = 0.07
        if world is not None:
            wt = world.active_weather_type()
            if wt == "HOT":
                est_loss = 0.105  # 0.07 × 1.5
            elif wt == "HEAVY_RAIN":
                est_loss = 0.09  # ~0.07 × 1.3

        # 检查是否将在 3 帧内跌破任一好果转坏阈值
        for threshold in (90, 80, 70, 60, 50, 40, 30, 20, 10):
            if me.freshness >= threshold and me.freshness - est_loss * 3 < threshold:
                return actions.use_resource(ResourceType.ICE_BOX)

        # 兜底：固定阈值
        if me.freshness < config.ICE_BOX_USE_BELOW:
            return actions.use_resource(ResourceType.ICE_BOX)
        return None

    def _maybe_task(self, world, me, gm, node, terminal):
        # ★ 对手忙碌中（处理/验核）→ 不会触发 TASK 窗口 → 可以安全做任务
        opponent_here = self._opponent_at_same_node(world, node)
        opponent_busy = self.opponent.is_busy()
        # 对手在同节点且不忙碌 → 避免触发窗口
        if opponent_here and not opponent_busy:
            return None
        # 窗口冷却：刚从此节点窗口出来，不重试
        if self._is_window_cooldown(world, node):
            return None

        pid = self.ctx.player_id
        for t in world.active_tasks():
            if t.get("nodeId") != node:
                continue
            if t.get("taskTemplateId") in config.SKIP_TASK_TEMPLATES:
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
            return actions.claim_task(t.get("taskId"))
        return None

    def _maybe_claim(self, world, me, gm, node, terminal):
        # 窗口回避：如果对手在同节点，只拿高价值资源（冰鉴/快马），其他跳过
        opponent_here = self._opponent_at_same_node(world, node)
        if self._is_window_cooldown(world, node):
            return None

        # ★ 对手忙碌中（处理/验核/休整）→ 不会触发窗口 → 可以放心领
        opponent_busy = self.opponent.is_busy()

        ns = world.node(node)
        if ns is None:
            return None
        wants = []

        # 冰鉴：鲜度保护刚需，即使对手也在也值得争
        if me.resource_count(ResourceType.ICE_BOX) < config.CLAIM_ICE_BOX_KEEP \
                and ns.resource_available(ResourceType.ICE_BOX):
            wants.append(ResourceType.ICE_BOX)
        # 情报：对手不在或对手忙碌时领取
        if (not opponent_here or opponent_busy) and me.resource_count(ResourceType.INTEL) < 1 \
                and ns.resource_available(ResourceType.INTEL) \
                and self._intel_usable_ahead(world, me, gm, node, terminal):
            wants.append(ResourceType.INTEL)
        # 马：对手忙碌/不在/我们领先时才争
        if not self._has_any_horse(me) and self._far_from_terminal(gm, node, terminal):
            if ns.resource_available(ResourceType.FAST_HORSE):
                if not opponent_here or opponent_busy or self.opponent.posture == "leading":
                    wants.append(ResourceType.FAST_HORSE)
            elif ns.resource_available(ResourceType.SHORT_HORSE):
                if not opponent_here or opponent_busy or self.opponent.posture == "leading":
                    wants.append(ResourceType.SHORT_HORSE)
        for r in wants:
            if self._can_afford(world, gm, node, config.RESOURCE_CLAIM_ROUND, terminal):
                return actions.claim_resource(node, r)
        return None

    def _opponent_at_same_node(self, world, node):
        """对手是否在同一节点。"""
        opp = world.opponent
        return opp is not None and opp.current_node_id == node

    def _in_control_zone(self, gm, node):
        """是否进入控场区（S08-S10 区域，即第一个必经节点附近）。

        进入此区域后，抢先到达 S10 比做任务/收集资源更重要。
        """
        chokes = gm.chokepoints
        if not chokes:
            return False
        # 找到第一个必经节点
        terminal = gm.terminal_nodes[0] if gm.terminal_nodes else None
        first_choke = None
        best_dist = float("inf")
        for cn in chokes:
            if cn == terminal:
                continue
            _, d = gm.time_optimal_path(gm.start_node, cn)
            if d < best_dist:
                best_dist = d
                first_choke = cn
        if not first_choke:
            return False
        # 当前节点距离第一个必经节点 ≤ 2 跳 → 在控场区
        _, dist = gm.time_optimal_path(node, first_choke)
        return dist != float("inf") and dist <= 150  # ~2 条边的帧数

    def _is_window_cooldown(self, world, node):
        """当前节点是否处于窗口冷却期（刚从此节点窗口出来）。"""
        rnd = world.round or 0
        return self._window_node_skip.get(node, 0) > rnd

    def _maybe_horse(self, me, gm, terminal):
        if self._has_move_buff(me):
            return None
        horse = None
        if me.resource_count(ResourceType.FAST_HORSE) > 0:
            horse = ResourceType.FAST_HORSE
        elif me.resource_count(ResourceType.SHORT_HORSE) > 0:
            horse = ResourceType.SHORT_HORSE
        if not horse or not self._far_from_terminal(gm, me.current_node_id, terminal):
            return None
        return actions.use_resource(horse)

    def _maybe_rush_protect(self, world, me):
        """护果令：鲜度低时保鲜 30 帧 (×0.2)。鲜度高时不浪费急策。"""
        if not world.is_rush or me.delivered or (me.rush_tactic_used_count or 0) > 0:
            return None
        # 鲜度 < 阈值且不是特别高（>95 时不值得用）
        if config.RUSH_PROTECT_FRESHNESS_BELOW > me.freshness > 50:
            return actions.rush_protect()
        return None

    def _rush_speed_warranted(self, world, me, gm, node, terminal):
        """疾行令：无马、远离终点、鲜度尚可时加速。成本 2 好果（任务书 §6.5）。"""
        if not world.is_rush or me.delivered or (me.rush_tactic_used_count or 0) > 0:
            return None
        # 好果不足（需保留 KEEP_GOOD_FRUIT_MIN 用于交付）→ 不浪费
        if me.good_fruit < config.KEEP_GOOD_FRUIT_MIN + 2:
            return None
        # 鲜度低于护果阈值 → 优先留给护果令
        if me.freshness < config.RUSH_PROTECT_FRESHNESS_BELOW:
            return None
        # 已有马或离终点近 → 不浪费
        if self._has_any_horse(me) or not self._far_from_terminal(gm, node, terminal):
            return None
        return actions.rush_speed()

    # ---- 情报 INTEL（M7）----

    def _reduce_targets_on_route(self, world, me, gm, node, terminal):
        """本方去路上的固定处理点/宫门（可被探路减时且尚无己方标记），按路径顺序。"""
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
        """去路上是否存在"可在其前一节点用情报"的处理点/宫门（前驱到它的路线距离≤射程）。

        用于领取情报的守卫：避免在长边地图（每边>射程）上领取无法使用的情报。
        """
        if not terminal:
            return False
        path, _ = gm.time_optimal_path(node, terminal, blocked=self._blocked_nodes(world, me))
        if not path or len(path) < 2:
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

    def _maybe_intel(self, world, me, gm, node, terminal):
        if me.resource_count(ResourceType.INTEL) <= 0:
            return None
        for nxt in self._reduce_targets_on_route(world, me, gm, node, terminal):
            d = gm.route_distance(node, nxt)
            if d == _INF:
                continue
            if d > config.INTEL_RANGE:
                break  # 路径距离递增，更远的也超射程
            return actions.use_resource(ResourceType.INTEL, nxt)
        return None

    # ---- 绕路做任务（M7）----

    def _task_detour_target(self, world, me, gm, node, terminal, extra_budget=0):
        # ★ 任务天花板：90 分解锁满额送达(240)+用时系数(1.0)+里程碑(+35)
        #   90→110 只多 15 分里程碑，不值得绕路。≥90 后不做非当前节点任务。
        current = me.task_score or 0
        if current >= 90:
            return None  # 已满 90：只做当前节点任务(_maybe_task)，不绕路
        if not terminal:
            return None

        pid = self.ctx.player_id
        _, direct = gm.time_optimal_path(node, terminal)
        if direct == _INF:
            return None

        # M8: 里程碑感知动态预算
        if current < 60:
            base_budget = config.TASK_DETOUR_MAX_EXTRA_FRAMES       # 70
        else:  # 60-89
            base_budget = config.TASK_DETOUR_MAX_EXTRA_FRAMES + 40  # 110（逼近90阈值）

        budget = base_budget + extra_budget
        if budget < 0:
            budget = 0

        best, best_extra = None, _INF
        for t in world.active_tasks():
            tn = t.get("nodeId")
            if not tn or tn == node:
                continue
            if t.get("taskTemplateId") in config.SKIP_TASK_TEMPLATES:
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
            if 0 <= extra <= budget and extra < best_extra \
                    and self._can_afford(world, gm, node, extra, terminal):
                best, best_extra = tn, extra
        return best

    def _best_on_path_task(self, world, me, gm, node, terminal, max_extra=10):
        """找去路上几乎不绕路(≤max_extra帧)的任务节点。

        用于任务分≥90后：只做顺路任务，不专门绕路。
        """
        if not terminal:
            return None
        pid = self.ctx.player_id
        _, direct = gm.time_optimal_path(node, terminal)
        if direct == _INF:
            return None

        best, best_extra = None, _INF
        for t in world.active_tasks():
            tn = t.get("nodeId")
            if not tn or tn == node:
                continue
            if t.get("taskTemplateId") in config.SKIP_TASK_TEMPLATES:
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
            if 0 <= extra <= max_extra and extra < best_extra \
                    and self._can_afford(world, gm, node, extra, terminal):
                best, best_extra = tn, extra
        return best

    # ---- 小分队（M7：防御性预清障/削弱 + 探路宫门）----

    def _maybe_squad(self, world, me, gm, node, terminal):
        if world.is_rush:
            return None  # RUSH 禁止新派小分队
        avail = me.squad_available or 0

        # ★ 增援己方必经节点设卡（防守值<4 且对手尚未通过）
        if avail >= 2:
            reinforce = self._maybe_reinforce_own_guard(world, me, gm)
            if reinforce:
                return reinforce

        if avail >= 2:
            blk = self._first_block_ahead(world, me, gm, node, terminal)
            if blk:
                nid, kind = blk
                key = (nid, kind)
                if key not in self._squad_sent:
                    self._squad_sent.add(key)
                    if kind == "obstacle":
                        return actions.squad_clear(nid)
                    if kind == "guard":
                        return actions.squad_weaken(nid)
        return self._maybe_scout_gate(world, me, gm, node)

    def _maybe_reinforce_own_guard(self, world, me, gm):
        """增援我方在必经节点的设卡（防守薄弱时）。

        条件：设卡防守值 ≤ 4，对手尚未通过该节点。
        效果：防守值 +2，延长风化寿命 30+ 帧。
        """
        for nid, ns in world.node_states.items():
            if not gm.is_chokepoint(nid):
                continue
            owner = ns.active_guard_owner()
            if owner != me.team_id:
                continue
            defense = (ns.guard or {}).get("defense", 0) or 0
            if defense <= 0 or defense > 4:
                continue
            # 对手尚未通过
            if self.opponent.has_passed(nid):
                continue
            key = (nid, "reinforce")
            if key in self._squad_sent:
                continue
            self._squad_sent.add(key)
            return actions.squad_reinforce(nid)
        return None

    def _first_block_ahead(self, world, me, gm, node, terminal):
        if not terminal:
            return None
        path, _ = gm.time_optimal_path(node, terminal)
        if not path:
            return None
        for i, nid in enumerate(path):
            # P1-6: 必经节点放宽到第 1 跳（小分队延迟落地 3-6 帧，
            #       主车队走第一边需 50-100 帧，时间充足）
            min_hops = 1 if gm.is_chokepoint(nid) else config.SQUAD_AHEAD_MIN_HOPS
            if i < min_hops:
                continue
            ns = world.node(nid)
            if ns and ns.has_obstacle:
                return (nid, "obstacle")
            owner = ns.active_guard_owner() if ns else None
            if owner and owner != me.team_id:
                return (nid, "guard")
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
        if frames == _INF or frames < config.GATE_SCOUT_MIN_FRAMES or frames > config.GATE_SCOUT_MAX_FRAMES:
            return None
        self._gate_scout_sent = True
        return actions.squad_scout(gate)

    # ---- M8: 条件设卡（基于对手模型）----

    def _maybe_set_guard(self, world, me, gm, node):
        """设卡决策：委托给 OpponentModel 判断条件。

        仅当 ALL 条件满足时才设卡：
        - 我方领先且态势为 leading
        - 当前节点是必经节点（chokepoint）
        - 对手尚未通过
        - 好果充足
        - ENABLE_OFFENSIVE 开启
        """
        if not self.opponent.should_set_guard(world, me, gm, node):
            return None

        n = gm.node(node)
        if n is None:
            return None

        # 计算最优额外好果投入
        extra = 0
        if n.type == "KEY_PASS" and me.good_fruit >= 22:
            extra = 2  # 防守值=6，风化延迟45帧
        elif me.good_fruit >= 21:
            extra = 1  # 防守值=4

        return actions.set_guard(node, extra_good_fruit=extra)

    # ---- 旧进攻设卡（保留兼容，M7 的 _maybe_set_guard 逻辑被覆盖）----

    def _maybe_set_guard_legacy(self, world, me, gm, node):
        """M7 原始设卡逻辑（ENABLE_OFFENSIVE=False 时不会被调用）。"""
        if not config.ENABLE_OFFENSIVE or world.is_rush:
            return None
        n = gm.node(node)
        if not n or n.type != "KEY_PASS":
            return None
        ns = world.node(node)
        if ns and ns.guard and (ns.guard.get("defense", 0) or 0) > 0:
            return None
        if me.good_fruit < 20:
            return None
        return actions.set_guard(node, extra_good_fruit=1)

    # ---- M8: 自适应窗口出牌 + 窗口回避 ----

    def _window_card_adaptive(self, world, me):
        """自适应窗口出牌 + 快速弃权判断。

        如果窗口不值得争（资源价值低、我们领先不需要冒险），
        直接弃权缩短窗口耗时，避免无谓的 3 帧消耗。
        """
        contests = world.my_contests()
        if not contests:
            self._window_rounds = 0
            return None

        c = contests[0]
        cid = c.get("contestId")
        ctype = c.get("contestType")
        if not cid:
            return None

        # 追踪窗口参与
        self._window_rounds += 1

        # ★ 快速弃权判断：不值得争的窗口直接放弃
        if self._should_concede_window(world, me, c):
            self._mark_window_cooldown(world, me)
            return actions.window_card(cid, Card.ABSTAIN)

        # 尝试自适应反制
        result = self.opponent.adaptive_window_card(world, me, c)
        if result is not None:
            cid, card = result
            return actions.window_card(cid, card)

        # 回退到固定优先级
        return self._window_card_fallback(world, me)

    def _should_concede_window(self, world, me, contest):
        """判断是否应该快速弃权。

        弃权条件（任一满足）：
        1. 对手忙碌中（处理/验核）→ 对手被动弃权，我们不用出牌也能赢
        2. 我们在竞速/领先态，不值得为资源/任务耗 3+ 帧
        3. 同一节点已经争过（连续窗口）
        """
        ctype = contest.get("contestType")

        # PASS/GATE 不能弃权
        if ctype in ("PASS", "GATE"):
            return False

        # ★ 对手忙碌中（处理/验核/休整）→ 对手被动 ABSTAIN → 我们出 ABSTAIN 也能赢当拍
        if self.opponent.is_busy():
            return False  # 必赢，不弃权，但出 ABSTAIN 即可（见 _window_card_fallback）

        # 已经在这个节点争了超过 1 轮 → 放弃
        if self._window_rounds >= 3:
            return True

        # 领先/竞速态：资源不值得消耗 3+ 帧
        if self.opponent.posture in ("leading", "racing"):
            if ctype == "RESOURCE":
                return True

        # 落后态：RESOURCE/DOCK 让给对手
        if self.opponent.posture == "trailing":
            if ctype in ("RESOURCE", "DOCK"):
                return True

        # ★ 分数领先时：非关键窗口弃权
        score_gap = self.opponent.score_gap(world)
        if score_gap > 30 and ctype in ("RESOURCE", "DOCK"):
            return True

        return False

    def _mark_window_cooldown(self, world, me):
        """记录窗口冷却：从此节点退出后，短期内不再触发同类型窗口。"""
        node = me.current_node_id
        if node:
            # 冷却 12 帧（覆盖 RESTING + 重试周期）
            self._window_node_skip[node] = (world.round or 0) + 12
            self._last_contested_node = node
        self._window_rounds = 0

    def _window_card_fallback(self, world, me):
        """窗口出牌固定优先级（对手模型无数据时的回退策略）。"""
        contests = world.my_contests()
        if not contests:
            return None
        cid = contests[0].get("contestId")
        if not cid:
            return None
        if (me.guard_action_point or 0) > 0:
            return actions.window_card(cid, Card.BING_ZHENG)
        if me.freshness >= 80 and me.good_fruit > config.KEEP_GOOD_FRUIT_MIN:
            return actions.window_card(cid, Card.XIAN_GONG)
        if me.resource_count(ResourceType.PASS_TOKEN) > 0 or me.resource_count(ResourceType.OFFICIAL_PERMIT) > 0:
            return actions.window_card(cid, Card.YAN_DIE)
        if self._has_any_horse(me):
            return actions.window_card(cid, Card.QIANG_XING)
        return actions.window_card(cid, Card.ABSTAIN)

    # Keep old _window_card as fallback alias
    _window_card = _window_card_fallback

    # ---- 辅助 ----

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
        end = (world.round or 0) + extra_frames + travel + config.DELIVER_TIME_SAFETY_MARGIN
        return end <= (self.ctx.duration_round or 600)

    # ---- 固定处理完成跟踪 ----

    def _update_process_memory(self, world, me, node):
        if node != self._stay_node:
            self._stay_node = node
            self._processed_here = False
            # 切换节点时清除窗口冷却（新节点新环境）
            self._window_rounds = 0

        gm = self.ctx.game_map
        is_proc_node = gm is not None and node in gm.process_nodes
        transition_done = (is_proc_node and self._prev_state == PlayerState.PROCESSING
                           and me.state != PlayerState.PROCESSING)
        if self._saw_process_complete(world) or transition_done:
            self._processed_here = True

    def _saw_process_complete(self, world):
        pid = self.ctx.player_id
        for e in world.events:
            if e.get("type") == "PROCESS_COMPLETE":
                payload = e.get("payload") or {}
                if payload.get("playerId") == pid:
                    return True
        return False
