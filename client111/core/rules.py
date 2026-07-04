"""规则公式镜像（任务书 §2/§3/§6/§7）。

纯函数，无副作用。所有公式严格对齐任务书数值。
"""

import math

# ---- 路线移动 (§2.3.2) ----

# 路线耗时系数：每 1 点路线距离所需移动量
ROUTE_COST = {
    "ROAD": 1380,
    "WATER": 1250,
    "MOUNTAIN": 1780,
    "BRANCH": 1550,
}

# 基础每帧移动量
BASE_MOVE_NONE = 1000       # 无加速
BASE_MOVE_FAST_HORSE = 1200  # 快马
BASE_MOVE_SHORT_HORSE = 1150  # 短程马
BASE_MOVE_RUSH_SPEED = 1300   # 疾行令

# 天气通行倍率 (§2.3.2)
WEATHER_MOVE_PENALTY = {
    None: 1000,
    "HOT": 1000,
    "HEAVY_RAIN": 1350,    # 命中水路
    "MOUNTAIN_FOG": 1100,  # 命中山路
}


def to_station_move_amount(distance, route_type):
    """到站所需移动量 = ceil(distance × 路线耗时系数)。"""
    cost = ROUTE_COST.get(route_type, 1380)
    return math.ceil(distance * cost)


def per_frame_move_amount(base_move, weather_type=None, on_route_type=None):
    """每帧移动量 = floor(base_move × 1000 / 天气通行倍率)。

    天气只影响对应路线类型：
    - HEAVY_RAIN 命中 WATER
    - MOUNTAIN_FOG 命中 MOUNTAIN
    """
    penalty = 1000
    if weather_type == "HEAVY_RAIN" and on_route_type == "WATER":
        penalty = 1350
    elif weather_type == "MOUNTAIN_FOG" and on_route_type == "MOUNTAIN":
        penalty = 1100
    return math.floor(base_move * 1000 / penalty)


def frames_on_edge(distance, route_type, base_move=BASE_MOVE_NONE, weather_mult=1000):
    """单条路线边的到站帧数 = ceil(到站移动量 / (base_move × 1000 / weather_mult))。

    简化：使用天气通行倍率直接调整每帧移动量。
    """
    total_move = to_station_move_amount(distance, route_type)
    per_frame = math.floor(base_move * 1000 / weather_mult) if weather_mult > 0 else 1
    if per_frame <= 0:
        per_frame = 1
    # 每帧前进 per_frame 移动量
    return math.ceil(total_move / per_frame)


# ---- 鲜度损耗 (§3.2.2) ----

# 每帧基础鲜度扣除值（按状态/路线）
FRESHNESS_LOSS_BASE = 0.05       # 停靠/处理/验核/窗口/休整/强制通行额外等待
FRESHNESS_LOSS_MOVE = {
    "ROAD": 0.055,
    "WATER": 0.045,
    "MOUNTAIN": 0.07,
    "BRANCH": 0.065,
}
FRESHNESS_LOSS_MOVE_MIN = min(FRESHNESS_LOSS_MOVE.values())  # 水路 0.045


def route_freshness_loss(route_type):
    """返回给定路线类型的每帧鲜度损耗。"""
    return FRESHNESS_LOSS_MOVE.get(route_type, 0.06)


def weather_move_multiplier(route_type, active_weather_type):
    """天气通行倍率：HEAVY_RAIN×WATER=1350, MOUNTAIN_FOG×MOUNTAIN=1100，其余 1000。"""
    if active_weather_type == "HEAVY_RAIN" and route_type == "WATER":
        return 1350
    if active_weather_type == "MOUNTAIN_FOG" and route_type == "MOUNTAIN":
        return 1100
    return 1000


# 好果转坏阈值（降序）
GOOD_TO_BAD_THRESHOLDS = (90, 80, 70, 60, 50, 40, 30, 20, 10)

# 鲜度系数
FRESHNESS_WEATHER_MULT = {
    None: 1.0,
    "HOT": 1.5,
    "HEAVY_RAIN": 1.3,
    "MOUNTAIN_FOG": 1.0,
}

FRESHNESS_RUSH_SPEED_MULT = 1.25
FRESHNESS_RUSH_PROTECT_MULT = 0.2

# 天气鲜度系数
FRESHNESS_WEATHER_COEF = {"HOT": 1.5, "HEAVY_RAIN": 1.3, "MOUNTAIN_FOG": 1.0}

# 障碍时间税
OBSTACLE_TIME_TAX = 8
SET_GUARD_PROCESS_FRAMES = 4
NODE_MAX_DEFENSE = {"normal": 6, "key_pass": 7, "gate": 4, "obstacle_node": 5}


def freshness_loss_per_frame(state, route_type=None, weather_type=None,
                              buff_types=None):
    """计算本帧鲜度损耗值。"""
    buff_types = buff_types or frozenset()

    # 基础值
    if state == "MOVING" and route_type:
        base = FRESHNESS_LOSS_MOVE.get(route_type, 0.065)
    elif state == "FORCED_PASSING" and route_type:
        base = FRESHNESS_LOSS_MOVE.get(route_type, 0.065)
    else:
        base = FRESHNESS_LOSS_BASE

    # 天气系数
    weather_mult = FRESHNESS_WEATHER_MULT.get(weather_type, 1.0)

    # 急策系数
    rush_mult = 1.0
    if "RUSH_SPEED" in buff_types:
        rush_mult = FRESHNESS_RUSH_SPEED_MULT
    if "RUSH_PROTECT" in buff_types:
        rush_mult = FRESHNESS_RUSH_PROTECT_MULT

    return base * weather_mult * rush_mult


# ---- 好果转坏阈值 (§3.2.1) ----
FRESHNESS_THRESHOLDS = (90, 80, 70, 60, 50, 40, 30, 20, 10)


def crossed_good_to_bad_thresholds(before, after):
    """返回在 (before, after] 区间内首次穿越的阈值列表。"""
    result = []
    for t in FRESHNESS_THRESHOLDS:
        if after < t <= before and t not in result:
            result.append(t)
    return result


# ---- 设卡 (§6.2) ----

def guard_defense(extra_good_fruit, node_type="STATION"):
    """设卡防守值 = min(上限, 2 + extra × 2)。"""
    limits = {"KEY_PASS": 7, "GATE": 4}
    cap = limits.get(node_type, 6)
    return min(cap, 2 + extra_good_fruit * 2)


def guard_weathering_start(node_type, final_defense):
    """首次风化帧数。"""
    if node_type == "KEY_PASS" and final_defense >= 4:
        return 45
    return 30


def guard_time_tax(defense, node_type="STATION"):
    """设卡时间税（强制通行时）。"""
    if node_type == "KEY_PASS":
        return min(50, 15 + defense * 5)
    if node_type == "GATE":
        return min(32, 12 + defense * 5)
    # 已有道路障碍的站点
    return min(40, 10 + defense * 5)


def obstacle_time_tax():
    """道路障碍时间税固定 8 帧。"""
    return 8


# ---- 攻坚破卡 (§6.3.1) ----
def break_guard_attack(good_fruit, bad_fruit, has_break_order=False):
    """攻坚值 = 好果×2 + 坏果×3 + 破关令(+3)。"""
    total = good_fruit * 2 + bad_fruit * 3
    if has_break_order:
        total += 3
    return total


# ---- 得分公式 (§7.2) ----

def milestone_bonus(task_base):
    """任务里程碑奖励。"""
    if task_base >= 110:
        return 50
    if task_base >= 90:
        return 35
    if task_base >= 60:
        return 15
    return 0


def delivery_base_score(task_base, delivered=True):
    """送达基础分。"""
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
    """皇榜任务分。"""
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


def total_score(deliver_round, good_fruit, freshness, task_base,
                raw_bounty=0, penalty=0, delivered=True):
    """最终总分 = 各项之和 - 惩罚，最低 0。"""
    if not delivered:
        raw = (task_score(task_base, False)
               + bounty_score(raw_bounty, False)
               - penalty)
    else:
        raw = (delivery_base_score(task_base)
               + good_fruit_score(good_fruit)
               + freshness_score(freshness)
               + time_score(deliver_round, task_base)
               + task_score(task_base)
               + bounty_score(raw_bounty)
               - penalty)
    return max(0, raw)
