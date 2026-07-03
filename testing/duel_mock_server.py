"""双人对战模拟器。

扩展原有 mock_server 为双人模式：
- 红蓝双方各自运行独立的 DecisionEngine
- 每帧同时收集双方动作，按游戏规则结算
- 简化模拟：移动耗时、固定处理、设卡阻挡、窗口争夺、鲜度损耗
- 输出完整对局日志供 analysis/ 分析

用法:
    from testing.duel_mock_server import DuelMockServer, run_duel

    server = DuelMockServer(map_config, red_factory, blue_factory)
    result = server.run()  # -> MatchResult
"""

import copy
import json
import math
import os
import time
from dataclasses import dataclass, field

# 确保 client/ 在 import 路径上
import sys
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_PROJECT_ROOT, "client"))

from core.game_map import GameMap
from core.rules import (
    BASE_MOVE_NONE, BASE_MOVE, ROUTE_TIME_COEF,
    frames_on_edge, to_station_move_amount, per_frame_move_amount,
    FRESHNESS_LOSS_MOVE, FRESHNESS_LOSS_BASE,
    break_guard_attack_value, guard_defense, guard_time_tax,
    crossed_good_to_bad_thresholds,
    OBSTACLE_TIME_TAX,
)
from protocol.enums import Action, PlayerState, Phase, Card


# ── 简化的游戏状态 ──

@dataclass
class ConvoyState:
    """一支车队的简化状态。"""
    player_id: int
    team_id: str
    current_node: str
    state: str = "IDLE"
    # 移动
    moving_to: str = None
    move_progress: float = 0.0       # 已完成的移动量
    move_required: float = 0.0        # 到站所需移动量
    edge_route_type: str = None
    # 处理
    processing_remaining: int = 0      # 处理剩余帧数
    processing_type: str = None       # "fixed" / "task" / "resource" / "clear" / "guard"
    # 资产
    good_fruit: int = 100
    bad_fruit: int = 0
    freshness: float = 100.0
    verified: bool = False
    delivered: bool = False
    task_score: int = 0               # 任务基础分累计
    bounty_score: int = 0
    # 资源
    resources: dict = field(default_factory=dict)
    buffs: list = field(default_factory=list)
    # 战术
    squad_available: int = 8
    guard_action_point: int = 4
    rush_tactic_used: bool = False
    # 统计
    illegal_count: int = 0
    penalty: int = 0


@dataclass
class NodeSimState:
    """节点的运行时状态。"""
    node_id: str
    has_obstacle: bool = False
    guard_owner: str = None           # team_id
    guard_defense: int = 0
    guard_extra_good: int = 0
    scout_marks: dict = field(default_factory=dict)  # team_id -> expire_round
    resource_stock: dict = field(default_factory=dict)


@dataclass
class MatchResult:
    """对局结果。"""
    match_id: str
    winner_id: int = None
    result_type: str = None           # "DELIVERY_WIN" / "TIME_UP" / "RETIRED"
    over_round: int = 0
    players: list = field(default_factory=list)
    log: list = field(default_factory=list)  # 每帧事件


# ── 双人模拟器 ──

class DuelMockServer:
    """双人对战模拟器。

    red_factory / blue_factory: () -> DecisionEngine 的工厂函数。
    """

    def __init__(self, map_data, red_factory, blue_factory, duration=600,
                 enable_weather=False, obstacle_nodes=None):
        self.gm = GameMap(map_data)
        self.duration = duration

        # 双方车队
        self.red_convoy = ConvoyState(player_id=1001, team_id="RED",
                                      current_node=self.gm.start_node)
        self.blue_convoy = ConvoyState(player_id=1002, team_id="BLUE",
                                       current_node=self.gm.start_node)

        # 节点状态
        self.node_states = {}
        for nid in self.gm.nodes:
            ns = NodeSimState(node_id=nid)
            # 从 map_data 初始化资源库存
            for res in map_data.get("visibleResources", []):
                if res.get("nodeId") == nid:
                    ns.resource_stock[res["resourceType"]] = 1
            self.node_states[nid] = ns

        # 障碍
        self.obstacle_nodes = set(obstacle_nodes or [])

        # 引擎
        from strategy.decision import DecisionEngine, GameContext
        self.red_engine = red_factory() if red_factory else None
        self.blue_engine = blue_factory() if blue_factory else None

        # 对局状态
        self.round = 0
        self.phase = "NORMAL"
        self.weather = None            # 简化：None 或 {"type": "HOT", ...}
        self.contests = []             # 当前活跃窗口
        self.active_tasks = []         # 当前活跃任务
        self.events = []               # 本帧事件
        self.action_results = []       # 本帧动作结果

        # 日志
        self.log = []

    # ── 主循环 ──

    def run(self) -> MatchResult:
        """运行到对局结束。"""
        while self.round < self.duration:
            self._step()
            if self._is_over():
                break
        return self._build_result()

    def _step(self):
        """推进一帧。"""
        self.round += 1
        self.events = []
        self.action_results = []

        # 检查 RUSH 触发
        self._check_rush_trigger()

        # 收双方动作
        red_actions = self._get_actions(self.red_convoy, self.red_engine, "RED")
        blue_actions = self._get_actions(self.blue_convoy, self.blue_engine, "BLUE")

        # 结算移动
        self._resolve_movement(self.red_convoy)
        self._resolve_movement(self.blue_convoy)

        # 结算处理
        self._resolve_processing(self.red_convoy)
        self._resolve_processing(self.blue_convoy)

        # 结算动作
        self._apply_actions(self.red_convoy, red_actions, "RED")
        self._apply_actions(self.blue_convoy, blue_actions, "BLUE")

        # 结算鲜度
        self._apply_freshness(self.red_convoy)
        self._apply_freshness(self.blue_convoy)

        # 记录日志
        self._log_frame()

    def _get_actions(self, convoy, engine, side):
        """获取一方的本帧动作。"""
        if convoy.delivered or engine is None:
            return []
        try:
            world = self._build_world(convoy)
            return engine.decide(world)
        except Exception:
            return []

    def _build_world(self, convoy):
        """为一方构建简化的 WorldState。"""
        from core.world_state import WorldState, PlayerView, NodeState as NS

        opp = self.red_convoy if convoy.team_id == "BLUE" else self.blue_convoy

        me_view = {
            "playerId": convoy.player_id, "teamId": convoy.team_id,
            "state": convoy.state,
            "currentNodeId": convoy.current_node,
            "freshness": convoy.freshness, "goodFruit": convoy.good_fruit,
            "badFruit": convoy.bad_fruit, "frozenGoodFruit": 0,
            "verified": convoy.verified, "delivered": convoy.delivered,
            "retired": False, "missingActionRounds": 0, "illegalActionCount": convoy.illegal_count,
            "penaltyScore": convoy.penalty, "rushTacticUsedCount": 1 if convoy.rush_tactic_used else 0,
            "resources": convoy.resources, "buffs": convoy.buffs,
            "squadAvailable": convoy.squad_available,
            "guardActionPoint": convoy.guard_action_point,
            "taskScore": convoy.task_score, "bountyScore": convoy.bounty_score,
            "totalScore": self._preview_score(convoy),
        }
        opp_view = {
            "playerId": opp.player_id, "teamId": opp.team_id,
            "state": opp.state,
            "currentNodeId": opp.current_node,
            "freshness": opp.freshness, "goodFruit": opp.good_fruit,
            "badFruit": opp.bad_fruit, "frozenGoodFruit": 0,
            "verified": opp.verified, "delivered": opp.delivered,
            "retired": False, "missingActionRounds": 0, "illegalActionCount": opp.illegal_count,
            "penaltyScore": opp.penalty, "rushTacticUsedCount": 1 if opp.rush_tactic_used else 0,
            "resources": opp.resources, "buffs": opp.buffs,
            "squadAvailable": opp.squad_available,
            "guardActionPoint": opp.guard_action_point,
            "taskScore": opp.task_score, "bountyScore": opp.bounty_score,
            "totalScore": self._preview_score(opp),
        }

        nodes_data = []
        for nid, ns in self.node_states.items():
            guard_data = None
            if ns.guard_owner and ns.guard_defense > 0:
                guard_data = {"ownerTeamId": ns.guard_owner, "defense": ns.guard_defense, "active": True}
            nodes_data.append({
                "nodeId": nid,
                "hasObstacle": nid in self.obstacle_nodes,
                "guard": guard_data,
                "resourceStock": ns.resource_stock,
                "scouted": [{"teamId": t, "expireRound": e} for t, e in ns.scout_marks.items()],
            })

        data = {
            "matchId": "duel", "round": self.round, "phase": self.phase,
            "players": [me_view, opp_view],
            "nodes": nodes_data,
            "tasks": self.active_tasks,
            "contests": self.contests,
            "weather": self.weather or {},
            "events": self.events,
            "actionResults": self.action_results,
        }
        return WorldState(data, convoy.player_id, self.gm)

    # ── 移动结算 ──

    def _resolve_movement(self, c: ConvoyState):
        if c.state != "MOVING" or not c.moving_to:
            return

        base = BASE_MOVE_NONE
        for buff in c.buffs:
            if buff.get("type") in ("FAST_HORSE", "SHORT_HORSE", "RUSH_SPEED"):
                if (buff.get("remainingRound", 0) or 0) > 0:
                    base = BASE_MOVE.get(buff["type"], BASE_MOVE_NONE)
                    buff["remainingRound"] = (buff.get("remainingRound", 0) or 0) - 1
                    break

        weather_mult = 1000
        if self.weather:
            wt = self.weather.get("type")
            if wt == "MOUNTAIN_FOG" and c.edge_route_type == "MOUNTAIN":
                weather_mult = 1100
            elif wt == "HEAVY_RAIN" and c.edge_route_type == "WATER":
                weather_mult = 1350

        per_frame = per_frame_move_amount(base, weather_mult)
        c.move_progress += per_frame

        if c.move_progress >= c.move_required:
            # 到达目标节点
            c.current_node = c.moving_to
            c.state = "IDLE"
            c.moving_to = None
            c.move_progress = 0.0
            c.move_required = 0.0
            c.edge_route_type = None

    # ── 处理结算 ──

    def _resolve_processing(self, c: ConvoyState):
        if c.state != "PROCESSING" or c.processing_remaining <= 0:
            return
        c.processing_remaining -= 1
        if c.processing_remaining <= 0:
            c.state = "IDLE"
            ptype = c.processing_type
            c.processing_type = None
            # 处理完成效果
            if ptype == "fixed":
                pass  # 固定处理完成
            elif ptype == "verify":
                c.verified = True  # 验核完成（修复：在 PROCESSING 结束时标记）
            elif ptype == "guard":
                # 设卡完成
                ns = self.node_states.get(c.current_node)
                if ns:
                    max_def = 6
                    node_obj = self.gm.node(c.current_node)
                    if node_obj and node_obj.type == "KEY_PASS":
                        max_def = 7
                    elif node_obj and node_obj.type == "GATE":
                        max_def = 4
                    ns.guard_owner = c.team_id
                    ns.guard_defense = guard_defense(ns.guard_extra_good, max_def)
            elif ptype == "clear":
                # 清障完成
                if c.current_node in self.obstacle_nodes:
                    self.obstacle_nodes.discard(c.current_node)
                # 好果扣除
                c.good_fruit -= 1
            elif ptype == "resource":
                pass  # 资源在 _apply_actions 中已处理
            elif ptype == "task":
                pass  # 任务在 _apply_actions 中已处理

    # ── 动作应用 ──

    def _apply_actions(self, c: ConvoyState, actions, side):
        for a in (actions or []):
            act = a.get("action", "")
            tgt = a.get("targetNodeId")

            if act == Action.MOVE and tgt:
                if c.state == "IDLE" and tgt in self.gm.neighbors(c.current_node):
                    # 检查是否被阻挡
                    tgt_ns = self.node_states.get(tgt)
                    blocked = False
                    if tgt in self.obstacle_nodes:
                        blocked = True
                    if tgt_ns and tgt_ns.guard_owner and tgt_ns.guard_owner != c.team_id:
                        blocked = True
                    if blocked:
                        self.action_results.append({
                            "playerId": c.player_id, "round": self.round,
                            "accepted": False, "errorCode": "MOVE_BLOCKED_BY_GUARD"
                        })
                        continue

                    edge = self.gm.edge_between(c.current_node, tgt)
                    if edge:
                        c.state = "MOVING"
                        c.moving_to = tgt
                        c.move_required = to_station_move_amount(edge.distance, edge.route_type)
                        c.move_progress = 0.0
                        c.edge_route_type = edge.route_type

            elif act == Action.PROCESS:
                if c.state == "IDLE" and c.current_node in self.gm.process_nodes:
                    pn = self.gm.process_nodes[c.current_node]
                    c.state = "PROCESSING"
                    c.processing_remaining = pn.get("processRound", 4)
                    c.processing_type = "fixed"

            elif act == Action.VERIFY_GATE:
                if c.state == "IDLE" and c.current_node == self.gm.gate_node and self.phase == "RUSH":
                    rush_tactic = a.get("rushTactic")
                    proc_round = 6
                    if rush_tactic == Action.BREAK_ORDER and not c.rush_tactic_used:
                        c.rush_tactic_used = True
                        proc_round = 3
                        if c.bad_fruit >= 2:
                            c.bad_fruit -= 2
                        else:
                            c.good_fruit -= 1
                    # 检查探路标记
                    tgt_ns = self.node_states.get(c.current_node)
                    if tgt_ns and c.team_id in tgt_ns.scout_marks:
                        proc_round = max(2, proc_round - 3)
                    c.state = "PROCESSING"
                    c.processing_remaining = proc_round
                    c.processing_type = "verify"  # 特殊类型，完成后标记 verified

            elif act == Action.DELIVER:
                if (c.current_node in self.gm.terminal_nodes and c.verified
                        and c.good_fruit > 0 and c.freshness > 0
                        and c.state == "IDLE"):
                    c.delivered = True
                    c.state = "DELIVERED"

            elif act == Action.CLEAR and tgt:
                if c.state == "IDLE":
                    c.state = "PROCESSING"
                    c.processing_remaining = 6
                    c.processing_type = "clear"

            elif act == Action.SET_GUARD and tgt:
                if c.state == "IDLE" and tgt == c.current_node:
                    extra = a.get("extraGoodFruit", 0)
                    c.state = "PROCESSING"
                    c.processing_remaining = 4
                    c.processing_type = "guard"
                    ns = self.node_states.get(tgt)
                    if ns:
                        ns.guard_extra_good = extra
                    # 设卡基础成本 1 好果 + extra（任务书 §6.2.1）
                    base_cost = 1 if (self.gm.node(tgt) and
                                      self.gm.node(tgt).type in ("KEY_PASS", "GATE")) else 0
                    c.good_fruit -= (base_cost + extra)

            elif act == Action.CLAIM_RESOURCE and tgt:
                if c.state == "IDLE" and tgt == c.current_node:
                    restype = a.get("resourceType")
                    ns = self.node_states.get(tgt)
                    if ns and ns.resource_stock.get(restype, 0) > 0:
                        ns.resource_stock[restype] -= 1
                        c.resources[restype] = c.resources.get(restype, 0) + 1
                        c.state = "PROCESSING"
                        c.processing_remaining = 2
                        c.processing_type = "resource"

            elif act == Action.USE_RESOURCE:
                restype = a.get("resourceType")
                if restype == "ICE_BOX" and c.resources.get("ICE_BOX", 0) > 0:
                    c.resources["ICE_BOX"] -= 1
                    c.freshness = min(100, c.freshness + 10)
                elif restype in ("FAST_HORSE", "SHORT_HORSE") and c.resources.get(restype, 0) > 0:
                    c.resources[restype] -= 1
                    duration = 20 if restype == "FAST_HORSE" else 14
                    c.buffs.append({"type": restype, "remainingRound": duration})

            elif act == Action.CLAIM_TASK:
                task_id = a.get("taskId")
                matching = [t for t in self.active_tasks if t.get("taskId") == task_id]
                if matching and c.state == "IDLE":
                    task = matching[0]
                    c.state = "PROCESSING"
                    c.processing_remaining = task.get("processRound", 4)
                    c.processing_type = "task"
                    base_score = task.get("baseScore", 0)
                    c.task_score += base_score
                    self.active_tasks.remove(task)

            elif act == Action.BREAK_GUARD and tgt:
                ns = self.node_states.get(tgt)
                if ns and ns.guard_owner and ns.guard_owner != c.team_id:
                    g = a.get("goodFruit", 0) or 0
                    b = a.get("badFruit", 0) or 0
                    bo = a.get("rushTactic") == Action.BREAK_ORDER
                    atk = break_guard_attack_value(g, b, bo)
                    if atk >= ns.guard_defense:
                        ns.guard_defense = 0
                        ns.guard_owner = None
                        c.bounty_score += 10  # 简化悬赏
                    else:
                        ns.guard_defense -= atk
                        c.state = "RESTING"
                        c.processing_remaining = 5
                    c.good_fruit -= g
                    c.bad_fruit -= b
                    if bo:
                        c.rush_tactic_used = True

            elif act == Action.FORCED_PASS and tgt:
                if c.state == "IDLE":
                    # 简化：直接 START forced pass
                    ns = self.node_states.get(tgt)
                    time_tax = OBSTACLE_TIME_TAX  # 默认 8
                    if ns and ns.guard_owner and ns.guard_owner != c.team_id:
                        time_tax = guard_time_tax(
                            "key_pass" if (self.gm.node(tgt) and self.gm.node(tgt).type == "KEY_PASS") else "normal",
                            ns.guard_defense
                        )
                    c.state = "FORCED_PASSING"
                    c.moving_to = tgt
                    c.move_required = to_station_move_amount(
                        self.gm.edge_between(c.current_node, tgt).distance,
                        self.gm.edge_between(c.current_node, tgt).route_type
                    ) + time_tax
                    c.move_progress = 0.0

            elif act == Action.SQUAD_SCOUT and tgt:
                if c.squad_available >= 1:
                    c.squad_available -= 1
                    ns = self.node_states.get(tgt)
                    if ns:
                        ns.scout_marks[c.team_id] = self.round + 45

            elif act == Action.SQUAD_CLEAR and tgt:
                if c.squad_available >= 2:
                    c.squad_available -= 2
                    self.obstacle_nodes.discard(tgt)

            elif act == Action.SQUAD_WEAKEN and tgt:
                if c.squad_available >= 2:
                    c.squad_available -= 2
                    ns = self.node_states.get(tgt)
                    if ns and ns.guard_defense > 0:
                        ns.guard_defense = max(0, ns.guard_defense - 2)

            elif act == Action.RUSH_SPEED:
                # 疾行令成本 2 好果（任务书 §6.5）
                if not c.rush_tactic_used and c.good_fruit >= 2:
                    c.rush_tactic_used = True
                    c.good_fruit -= 2
                    c.buffs.append({"type": "RUSH_SPEED", "remainingRound": 15})

            elif act == Action.RUSH_PROTECT:
                if not c.rush_tactic_used:
                    c.rush_tactic_used = True
                    c.buffs.append({"type": "RUSH_PROTECT", "remainingRound": 30})

    # ── 鲜度结算 ──

    def _apply_freshness(self, c: ConvoyState):
        if c.delivered or c.state == "DELIVERED":
            return

        # 基础鲜度损耗
        if c.state == "MOVING" and c.edge_route_type:
            base_loss = FRESHNESS_LOSS_MOVE.get(c.edge_route_type, 0.065)
        elif c.state == "FORCED_PASSING" and c.edge_route_type:
            base_loss = FRESHNESS_LOSS_MOVE.get(c.edge_route_type, 0.065)
        else:
            base_loss = FRESHNESS_LOSS_BASE

        # 天气系数
        weather_coef = 1.0
        if self.weather:
            wt = self.weather.get("type")
            if wt == "HOT":
                weather_coef = 1.5
            elif wt == "HEAVY_RAIN" and c.edge_route_type == "WATER":
                weather_coef = 1.3

        # 急策系数
        rush_coef = 1.0
        for buff in c.buffs:
            if buff.get("type") == "RUSH_PROTECT" and (buff.get("remainingRound", 0) or 0) > 0:
                rush_coef = 0.2
                buff["remainingRound"] = (buff.get("remainingRound", 0) or 0) - 1
                break
            if buff.get("type") == "RUSH_SPEED":
                rush_coef = 1.25

        loss = base_loss * weather_coef * rush_coef
        before = c.freshness
        c.freshness = max(0, c.freshness - loss)

        # 好果转坏
        crossed = crossed_good_to_bad_thresholds(before, c.freshness)
        for _ in crossed:
            if c.good_fruit > 0:
                c.good_fruit -= 1
                c.bad_fruit += 1

    # ── RUSH 触发 ──

    def _check_rush_trigger(self):
        if self.phase != "NORMAL":
            return
        if self.round < 390:
            return
        # 简化：第 390 帧触发，或任一车队到达 S14
        if self.round >= 390:
            self.phase = "RUSH"
        for c in [self.red_convoy, self.blue_convoy]:
            if c.current_node == self.gm.gate_node:
                self.phase = "RUSH"
                return

    # ── 对局结束判断 ──

    def _is_over(self):
        if self.red_convoy.delivered and self.blue_convoy.delivered:
            return True
        if self.round >= self.duration:
            return True
        return False

    # ── 结果 ──

    def _preview_score(self, c: ConvoyState):
        """简化的分数预览。"""
        if not c.delivered:
            task_base = c.task_score
            return min(80, task_base)
        task_base = c.task_score
        delivery_base = min(240, 120 + (task_base * 4) // 3)
        good_fruit_score = math.floor(c.good_fruit / 100 * 180)
        freshness_score = math.floor(c.freshness / 100 * 180)
        raw_time = math.floor((600 - self.round) / 600 * 70)
        time_score = math.floor(raw_time * min(task_base, 90) / 90)
        milestone = 0
        if task_base >= 110:
            milestone = 50
        elif task_base >= 90:
            milestone = 35
        elif task_base >= 60:
            milestone = 15
        task_score = min(180, task_base + milestone)
        return delivery_base + good_fruit_score + freshness_score + time_score + task_score + min(c.bounty_score, 100)

    def _build_result(self) -> MatchResult:
        red_score = self._preview_score(self.red_convoy)
        blue_score = self._preview_score(self.blue_convoy)
        if red_score > blue_score:
            winner = self.red_convoy.player_id
        elif blue_score > red_score:
            winner = self.blue_convoy.player_id
        else:
            winner = None

        return MatchResult(
            match_id="duel",
            winner_id=winner,
            result_type="DELIVERY_WIN" if (self.red_convoy.delivered or self.blue_convoy.delivered) else "TIME_UP",
            over_round=self.round,
            players=[
                {"playerId": self.red_convoy.player_id, "teamId": "RED",
                 "totalScore": red_score, "delivered": self.red_convoy.delivered,
                 "freshness": self.red_convoy.freshness, "goodFruit": self.red_convoy.good_fruit,
                 "taskScore": self.red_convoy.task_score, "bountyScore": self.red_convoy.bounty_score},
                {"playerId": self.blue_convoy.player_id, "teamId": "BLUE",
                 "totalScore": blue_score, "delivered": self.blue_convoy.delivered,
                 "freshness": self.blue_convoy.freshness, "goodFruit": self.blue_convoy.good_fruit,
                 "taskScore": self.blue_convoy.task_score, "bountyScore": self.blue_convoy.bounty_score},
            ],
            log=self.log,
        )

    def _log_frame(self):
        self.log.append({
            "round": self.round,
            "phase": self.phase,
            "red": {"node": self.red_convoy.current_node, "state": self.red_convoy.state,
                    "good": self.red_convoy.good_fruit, "fresh": round(self.red_convoy.freshness, 2)},
            "blue": {"node": self.blue_convoy.current_node, "state": self.blue_convoy.state,
                     "good": self.blue_convoy.good_fruit, "fresh": round(self.blue_convoy.freshness, 2)},
        })


# ── 便捷入口 ──

def run_duel(map_data, red_factory, blue_factory, **kwargs) -> MatchResult:
    """运行一场对决。"""
    server = DuelMockServer(map_data, red_factory, blue_factory, **kwargs)
    return server.run()
