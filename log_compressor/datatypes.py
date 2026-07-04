"""数据结构：FrameRecord 与 MatchLog。

日志解析和压缩的核心数据容器，被 parser / compressor / restorer 共用。
"""

from dataclasses import dataclass, field


@dataclass
class FrameRecord:
    """单帧快照。"""
    round: int = 0
    phase: str = ""
    node: str = ""
    state: str = ""
    fresh: float = 0.0
    good_fruit: int = 0
    task_score: int = 0
    verified: bool = False
    delivered: bool = False
    events: list = field(default_factory=list)


@dataclass
class MatchLog:
    """一场对局的完整结构化日志。

    由 parser 产出，供 compressor / restorer 消费。
    """
    match_id: str = ""
    player_id: int = 0
    player_name: str = ""
    team_id: str = ""

    # 帧序列
    frames: list = field(default_factory=list)          # list[FrameRecord]
    # 动作序列 [{round, action, target, note}]
    actions: list = field(default_factory=list)
    # 得分投影 [{round, myScore, oppScore, gap, mode, ...}]
    projections: list = field(default_factory=list)
    # ETA 序列 [{round, oppFrom, toGate, toFinish, verified, conf}]
    etas: list = field(default_factory=list)
    # 模式切换 [{round, reason, from, to}]
    mode_changes: list = field(default_factory=list)

    # 结算
    winner_id: int = None
    i_won: bool = False
    over_round: int = 0
    over_reason: str = ""
    result_type: str = ""

    # 最终分数（己方）
    total_score: int = 0
    delivered: bool = False
    deliver_round: int = 0
    final_freshness: float = 0.0
    final_good_fruit: int = 0
    task_score: int = 0
    bounty_score: int = 0
    retired: bool = False

    # 对手分数
    opp_score: int = 0
    opp_deliver_round: int = 0
    opp_freshness: float = 0.0

    # 衍生数据
    path_nodes: list = field(default_factory=list)
    freshness_curve: list = field(default_factory=list)   # [(round, fresh)]
    action_dist: dict = field(default_factory=dict)       # {action: count}
    errors: list = field(default_factory=list)
