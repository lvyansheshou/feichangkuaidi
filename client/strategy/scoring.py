"""得分预估引擎。

从文档公式出发，纯函数、无副作用。
用于回答"如果走这条路/做这个任务/设这个卡，最终能得多少分？"

任务书 §7.2:
  送达基础分 = min(240, 120 + floor(task_base × 4/3))
  好果数量分 = floor(good_fruit / 100 × 180)
  鲜度品质分 = floor(freshness / 100 × 180)
  原始用时分 = floor((600 - deliver_round) / 600 × 70)
  用时分     = floor(原始用时分 × min(task_base, 90) / 90)
  皇榜任务分 = min(180, task_base + milestone)
  破关悬赏分 = min(raw_bounty, 80) + 20 (if >0)
  最终总分   = 各项之和 - 惩罚
"""

import math

# ── 任务里程碑 ──

def milestone_bonus(task_base):
    """任务里程碑奖励。"""
    if task_base >= 110:
        return 50
    if task_base >= 90:
        return 35
    if task_base >= 60:
        return 15
    return 0


def milestone_delta(before, after):
    """任务基础分从 before 到 after 的里程碑增量。"""
    return milestone_bonus(after) - milestone_bonus(before)


# ── 分项计算 ──

def delivery_base_score(task_base, delivered=True):
    """送达基础分。未交付=0。"""
    if not delivered:
        return 0
    return min(240, 120 + (task_base * 4) // 3)


def good_fruit_score(good_fruit, delivered=True):
    """好果数量分。"""
    if not delivered:
        return 0
    return math.floor(good_fruit / 100 * 180)


def freshness_score(freshness, delivered=True):
    """鲜度品质分。"""
    if not delivered:
        return 0
    return math.floor(freshness / 100 * 180)


def raw_time_score(deliver_round):
    """原始用时分。"""
    return math.floor((600 - deliver_round) / 600 * 70)


def time_score(deliver_round, task_base, delivered=True):
    """用时分 = 原始 × 任务系数。"""
    if not delivered:
        return 0
    raw = raw_time_score(deliver_round)
    coef = min(task_base, 90) / 90.0
    return math.floor(raw * coef)


def task_score(task_base, delivered=True):
    """皇榜任务分。未交付封顶 80 且无里程碑。"""
    if not delivered:
        return min(task_base, 80)
    return min(180, task_base + milestone_bonus(task_base))


def bounty_score(raw_bounty, delivered=True):
    """破关悬赏分。"""
    if raw_bounty <= 0:
        return 0
    if not delivered:
        return min(raw_bounty, 25)
    return min(raw_bounty, 80) + 20


def penalty_score(illegal_count, post_delivery_violations=0):
    """惩罚分。"""
    p = 0
    if illegal_count > 5:
        p += min(20, illegal_count - 5)
    p += post_delivery_violations * 5
    return p


# ── 综合预估 ──

def estimate_total(deliver_round, good_fruit, freshness, task_base,
                   raw_bounty=0, illegal_count=0, delivered=True):
    """综合预估总分。"""
    if not delivered:
        return (task_score(task_base, False)
                + bounty_score(raw_bounty, False)
                - penalty_score(illegal_count))

    return (delivery_base_score(task_base)
            + good_fruit_score(good_fruit)
            + freshness_score(freshness)
            + time_score(deliver_round, task_base)
            + task_score(task_base)
            + bounty_score(raw_bounty)
            - penalty_score(illegal_count))


# ── 边际分析 ──

def marginal_task_value(current_base, task_points, extra_frames):
    """做一个任务的边际得分价值。

    返回 (score_gain, is_worth_it)
    """
    before = estimate_total(400, 100, 95, current_base)
    after = estimate_total(400 + extra_frames, 100,
                           95 - extra_frames * 0.06,
                           current_base + task_points)
    gain = after - before
    # 每帧 ≈ 0.12 用时分的代价
    frame_cost = extra_frames * 0.12
    return gain, gain > frame_cost


def marginal_guard_value(our_cost_frames, our_cost_fruit,
                          opp_cost_frames, opp_cost_fruit):
    """设卡的边际价值（相对优势）。

    our_cost: 我们付出的帧数和好果
    opp_cost: 对手被迫付出的帧数和好果
    返回净相对优势（分）
    """
    our_loss = our_cost_frames * 0.12 + our_cost_fruit * 1.8
    opp_loss = opp_cost_frames * 0.12 + opp_cost_fruit * 1.8
    return opp_loss - our_loss


def is_cliff_edge(current_base, target_points):
    """检查完成任务后是否会触发悬崖效应（跨越里程碑阈值）。"""
    after = current_base + target_points
    thresholds = [60, 90, 110]
    for t in thresholds:
        if current_base < t <= after:
            return True, t
    return False, None
