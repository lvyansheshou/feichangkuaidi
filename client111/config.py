"""集中配置。

禁止写死 playerId / host / port / 阵营（由启动参数与 start 动态决定）。
所有时间单位为秒（除非显式标注帧）。

v4.4: 基于对手分析 — 慢就是快，保鲜就是得分。
对方 r561 到达、鲜度 88.57。我方 r469 到达、鲜度 74.68。
结论：多花 92 帧绕行官道/水路，换 14 点鲜度 = 净赚 25 分。
"""

# ---- 客户端标识 ----
CLIENT_VERSION = "1.0"
DEFAULT_PLAYER_NAME = "litchi-agent"

# ---- 帧格式（协议 §1）----
LENGTH_PREFIX_WIDTH = 5          # 5 位十进制长度前缀
MAX_FRAME_BODY_BYTES = 99999     # body 的 UTF-8 字节数上限

# ---- socket ----
RECV_CHUNK = 65536               # 单次 recv 读取字节数
CONNECT_TIMEOUT = 10.0           # 建连超时

# ---- 时序超时 ----
HANDSHAKE_TIMEOUT = 30.0         # 等待 start 的最长时间
RECV_LOOP_TIMEOUT = 1.0          # 主循环从收包队列取消息的等待间隔
DECISION_BUDGET = 0.4            # 单帧决策软预算（超 400ms 记告警）

# ---- 日志 ----
LOG_DIR = "logs"                 # 相对启动工作目录

# ---- 策略参数 v4.4: 基于对手分析 ----
# 鲜度阈值（好果转坏）：首次低于这些值触发转坏
FRESHNESS_THRESHOLDS = (90, 80, 70, 60, 50, 40, 30, 20, 10)

# 冰鉴 — 对方用了 2+ 次冰鉴保 88 鲜度。我方必须同样激进
ICE_BOX_USE_BELOW = 96.0         # 鲜度 < 96 立即用冰鉴
CLAIM_ICE_BOX_KEEP = 5           # 期望至少持有 5 个（疯狂囤积）
ICE_BOX_DETOUR_RANGE = 5         # 沿路径向前探测冰鉴的跳数

# 马 — 尽早使用
HORSE_MIN_REMAINING_DISTANCE = 15

# 急策 — RUSH 立即护果
RUSH_PROTECT_BELOW = 99.0

# 安全余量 — 对方 r561 仍交付，600 帧上限下允许更从容
DELIVER_TIME_MARGIN = 5          # 交付时间安全余量（帧）

# 任务 — 对方任务分 165、鲜度 88。我方任务 180、鲜度 74。
# 教训：任务绕路换来的 15 分，被鲜度损失 25 分反超。不再绕路做任务。
SKIP_TASK_TEMPLATES = ("T04", "T06")
TASK_DETOUR_MAX_EXTRA = 0        # 不绕路做任务（对方教训）

# 对抗 — 好果即分数，不清障
KEEP_GOOD_FRUIT_MIN = 98         # 永远不清障（对方好果 99，我方 97）
GATE_SCOUT_MIN_FRAMES = 8
GATE_SCOUT_MAX_FRAMES = 40
INTEL_RANGE = 15
REROUTE_VS_CLEAR_EXTRA = 100     # 永远绕行不清障
SQUAD_AHEAD_MIN_HOPS = 2
REJECT_BLOCK_ROUNDS = 4
ENABLE_OFFENSIVE = False
FP_RETRY_LIMIT = 1               # 立即强制通行
FP_RETRY_COOLDOWN = 10           # 缩短冷却

# 鲜度感知路由 v4.4 — 对方走官道/水路绕行拿下 88 鲜度
FRESHNESS_FIRST_MAX_EXTRA = 120      # 允许大幅绕行换鲜度
FRESHNESS_ROUTE_SLACK = 10           # 几乎总是启用鲜度路由
TARGET_DELIVER_ROUND = 560           # 目标交付回合（对齐对方 r561）
TARGET_FRESHNESS = 88.0              # 目标到达鲜度（对齐对方 88.57）
