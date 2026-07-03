"""每帧公开状态镜像 WorldState（由 inquire 消息构建）。

把 inquire.msg_data 解析为强类型视图，供 strategy 决策。常用字段解析为属性，
少用字段保留在 .raw 中随取随用。可选携带 GameMap 引用以提供距离/寻路便捷方法。
字段口径见协议 §7 与附录 B/C/D/F。
"""

from dataclasses import dataclass, field

from protocol.enums import Phase, PlayerState


@dataclass
class PlayerView:
    raw: dict = field(default_factory=dict)
    player_id: int = 0
    team_id: str = None
    state: str = None
    current_node_id: str = None
    next_node_id: str = None
    route_edge_id: str = None
    move_progress: float = 0.0
    freshness: float = 0.0
    good_fruit: int = 0
    frozen_good_fruit: int = 0
    bad_fruit: int = 0
    squad_available: int = 0
    guard_action_point: int = 0
    verified: bool = False
    delivered: bool = False
    retired: bool = False
    missing_action_rounds: int = 0
    illegal_action_count: int = 0
    penalty_score: int = 0
    rush_tactic_used_count: int = 0
    resources: dict = field(default_factory=dict)
    buffs: list = field(default_factory=list)
    current_process: dict = None
    task_score: int = 0
    bounty_score: int = 0
    total_score: int = 0

    @classmethod
    def from_dict(cls, d):
        return cls(
            raw=d,
            player_id=d.get("playerId", 0),
            team_id=d.get("teamId"),
            state=d.get("state"),
            current_node_id=d.get("currentNodeId"),
            next_node_id=d.get("nextNodeId"),
            route_edge_id=d.get("routeEdgeId"),
            move_progress=d.get("moveProgress", 0.0) or 0.0,
            freshness=d.get("freshness", 0.0) or 0.0,
            good_fruit=d.get("goodFruit", 0) or 0,
            frozen_good_fruit=d.get("frozenGoodFruit", 0) or 0,
            bad_fruit=d.get("badFruit", 0) or 0,
            squad_available=d.get("squadAvailable", 0) or 0,
            guard_action_point=d.get("guardActionPoint", 0) or 0,
            verified=bool(d.get("verified")),
            delivered=bool(d.get("delivered")),
            retired=bool(d.get("retired")),
            missing_action_rounds=d.get("missingActionRounds", 0) or 0,
            illegal_action_count=d.get("illegalActionCount", 0) or 0,
            penalty_score=d.get("penaltyScore", 0) or 0,
            rush_tactic_used_count=d.get("rushTacticUsedCount", 0) or 0,
            resources=dict(d.get("resources") or {}),
            buffs=list(d.get("buffs") or []),
            current_process=d.get("currentProcess"),
            task_score=d.get("taskScore", 0) or 0,
            bounty_score=d.get("bountyScore", 0) or 0,
            total_score=d.get("totalScore", 0) or 0,
        )

    @property
    def is_idle(self):
        return self.state == PlayerState.IDLE

    def resource_count(self, resource_type):
        return self.resources.get(resource_type, 0)


@dataclass
class NodeState:
    raw: dict = field(default_factory=dict)
    node_id: str = None
    node_type: str = None
    process_type: str = None
    process_round: int = 0
    guard: dict = None
    resource_stock: dict = field(default_factory=dict)
    scouted: list = field(default_factory=list)
    has_obstacle: bool = False
    obstacle_type: str = None
    obstacle_residue: dict = None
    can_window: bool = False

    @classmethod
    def from_dict(cls, d):
        return cls(
            raw=d,
            node_id=d.get("nodeId"),
            node_type=d.get("nodeType"),
            process_type=d.get("processType"),
            process_round=d.get("processRound", 0) or 0,
            guard=d.get("guard"),
            resource_stock=dict(d.get("resourceStock") or {}),
            scouted=list(d.get("scouted") or []),
            has_obstacle=bool(d.get("hasObstacle")),
            obstacle_type=d.get("obstacleType"),
            obstacle_residue=d.get("obstacleResidue"),
            can_window=bool(d.get("canWindow")),
        )

    def active_guard_owner(self):
        """有效设卡归属队伍（defense>0 且 active）；无则 None。"""
        g = self.guard
        if g and g.get("active") and (g.get("defense", 0) or 0) > 0:
            return g.get("ownerTeamId")
        return None

    def resource_available(self, resource_type):
        return (self.resource_stock.get(resource_type, 0) or 0) > 0

    def my_scout_marks(self, team_id):
        return [m for m in self.scouted if m.get("teamId") == team_id]


class WorldState:
    def __init__(self, inquire_data, player_id, game_map=None):
        self.raw = inquire_data
        self.game_map = game_map
        self.player_id = int(player_id)
        self.match_id = inquire_data.get("matchId")
        self.round = inquire_data.get("round")
        self.phase = inquire_data.get("phase")

        self.me = None
        self.opponent = None
        for p in inquire_data.get("players", []) or []:
            view = PlayerView.from_dict(p)
            if view.player_id == self.player_id:
                self.me = view
            else:
                self.opponent = view

        self.node_states = {}
        for n in inquire_data.get("nodes", []) or []:
            ns = NodeState.from_dict(n)
            if ns.node_id:
                self.node_states[ns.node_id] = ns

        self.tasks = inquire_data.get("tasks", []) or []
        self.contests = inquire_data.get("contests", []) or []
        self.bounties = inquire_data.get("bounties", []) or []
        self.weather = inquire_data.get("weather", {}) or {}
        self.events = inquire_data.get("events", []) or []
        self.action_results = inquire_data.get("actionResults", []) or []

    # ---- 阶段 ----

    @property
    def is_rush(self):
        return self.phase == Phase.RUSH

    @property
    def is_ended(self):
        return self.phase == Phase.ENDED

    # ---- 访问 ----

    def node(self, node_id):
        return self.node_states.get(node_id)

    def active_tasks(self):
        """当前可参与、未完成、未失败的任务实例。"""
        out = []
        for t in self.tasks:
            if t.get("active") and not t.get("completed") and not t.get("failed"):
                out.append(t)
        return out

    def my_contests(self):
        """本方正在参与、且未结算、未被抑制的窗口。"""
        out = []
        pid = self.player_id
        for c in self.contests:
            if c.get("resolved") or c.get("status") == "SUPPRESSED":
                continue
            if c.get("redPlayerId") == pid or c.get("bluePlayerId") == pid:
                out.append(c)
        return out

    # ---- 天气 ----

    def active_weather(self):
        return self.weather.get("active", []) or []

    def active_weather_type(self):
        """当前生效天气类型（同一帧最多 1 个）；无则 None。"""
        act = self.active_weather()
        return act[0].get("type") if act else None

    def forecast_weather(self):
        """已预告但尚未生效的天气列表。

        天气提前 30 帧预告（任务书 §2.5），用于提前调整路线和冰鉴时机。
        返回: [{'type': str, 'startRound': int, 'duration': int}, ...]
        """
        forecasts = self.weather.get("forecast", []) or []
        result = []
        for f in forecasts:
            result.append({
                "type": f.get("type"),
                "startRound": f.get("startRound", 0),
                "duration": f.get("duration", 0),
            })
        return result

    def upcoming_weather(self, within_frames=30):
        """返回将在 within_frames 帧内生效的最近天气预告，无则 None。"""
        current = self.round or 0
        forecasts = self.forecast_weather()
        upcoming = [f for f in forecasts
                    if 0 < f["startRound"] - current <= within_frames]
        upcoming.sort(key=lambda f: f["startRound"])
        return upcoming[0] if upcoming else None

    # ---- 便捷（需 game_map）----

    def distance_to_gate(self):
        if self.game_map is None or self.me is None or not self.me.current_node_id:
            return None
        return self.game_map.distance_to_gate(self.me.current_node_id)
