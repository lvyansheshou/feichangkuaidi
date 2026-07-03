"""战略阶段判定。

从任务书 §1.4 对局流程出发：
  RACING:  开局 → 到达第一个必经节点（S10）
  CONTROL: 到达 S10 → RUSH 触发
  SPRINT:  RUSH 触发 → 交付

每个阶段有不同的目标、约束和策略权重。
"""

from protocol.enums import Phase as GamePhase


class StrategyPhase:
    RACING = "racing"       # 竞速期：最快到达 S10
    CONTROL = "control"     # 控场期：设卡/突破/悬赏
    SPRINT = "sprint"       # 冲刺期：最快交付


def determine_phase(world, me, gm):
    """根据游戏状态判定当前战略阶段。

    阶段转换规则（任务书 §1.4 + §6.5）：
    - RUSH 触发 → SPRINT（不可逆）
    - 到达/越过第一个必经节点 → CONTROL（不可逆）
    - 否则 → RACING
    """
    if world.is_rush or me.delivered:
        return StrategyPhase.SPRINT

    chokes = gm.chokepoints
    terminal = gm.terminal_nodes[0] if gm.terminal_nodes else None
    if not chokes or not terminal:
        return StrategyPhase.RACING

    # 找第一个必经节点（距离起点最近的非终点的必经节点）
    start = gm.start_node
    first_choke = None
    best_dist = float("inf")
    for cn in chokes:
        if cn == terminal:
            continue
        _, d = gm.time_optimal_path(start, cn)
        if d < best_dist:
            best_dist = d
            first_choke = cn

    if not first_choke:
        return StrategyPhase.RACING

    # 我方是否已到达/越过第一个必经节点
    my_node = me.current_node_id
    if my_node:
        _, dist_to_choke = gm.time_optimal_path(my_node, first_choke)
        _, dist_to_terminal = gm.time_optimal_path(my_node, terminal)
        _, choke_to_terminal = gm.time_optimal_path(first_choke, terminal)
        # 如果到终点的距离 < 必经节点到终点的距离 → 已越过
        if (dist_to_terminal != float("inf")
                and choke_to_terminal != float("inf")
                and dist_to_terminal < choke_to_terminal):
            return StrategyPhase.CONTROL
        # 如果就在必经节点上
        if my_node == first_choke or my_node in chokes:
            return StrategyPhase.CONTROL

    return StrategyPhase.RACING


def phase_strategy_weights(phase, opponent_posture):
    """返回 (frame_weight, freshness_weight, resource_bonus, task_bonus)。

    原则：
    - RACING: 速度 > 鲜度（要先到 S10）
    - CONTROL+胶着: 速度优先（抢先控场）
    - CONTROL+领先: 鲜度保护（稳扎稳打）
    - SPRINT: 纯速度
    """
    if phase == StrategyPhase.SPRINT:
        return (2.0, 5.0, 0, 0)          # 冲刺：什么都不重要，只要速度

    if phase == StrategyPhase.RACING:
        if opponent_posture in ("contested",):
            return (1.5, 15.0, 0, -2)     # 胶着抢 S10：速度优先，可做顺路任务
        return (1.2, 25.0, -1, -3)        # 正常竞速：鲜度开始重要

    # CONTROL
    if opponent_posture == "trailing":
        return (1.5, 20.0, 0, 0)          # 落后追分
    if opponent_posture == "contested":
        return (1.3, 30.0, 0, 0)          # 胶着控场
    if opponent_posture == "racing":
        return (1.0, 50.0, -2, -3)        # 领先保护
    # leading
    return (0.8, 70.0, -3, -5)            # 大幅领先：鲜度最大化
