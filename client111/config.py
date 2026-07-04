"""集中配置。

禁止写死 playerId / host / port / 阵营（由启动参数与 start 动态决定）。
所有时间单位为秒（除非显式标注帧）。
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

# ---- 策略参数 ----
# 鲜度阈值（好果转坏）：首次低于这些值触发转坏
FRESHNESS_THRESHOLDS = (90, 80, 70, 60, 50, 40, 30, 20, 10)

# 冰鉴 — 鲜度最高优先：鲜度 < 94 即用，尽早保鲜
ICE_BOX_USE_BELOW = 94.0         # 鲜度低于此且持有冰鉴时使用
CLAIM_ICE_BOX_KEEP = 3           # 期望至少持有的冰鉴数

# 马
HORSE_MIN_REMAINING_DISTANCE = 20  # 剩余到终点路线距离大于此才用马/领马

# 急策 — RUSH 立即护果
RUSH_PROTECT_BELOW = 98.0       # RUSH 阶段鲜度低于此用护果令（几乎立即）

# 安全余量
DELIVER_TIME_MARGIN = 10         # 交付时间安全余量（帧）

# 任务 — 鲜度优先，几乎不绕路做任务
SKIP_TASK_TEMPLATES = ("T04", "T06")  # 跳过：T04 需障碍上下文，T06 需消耗马
TASK_DETOUR_MAX_EXTRA = 20       # 绕路做任务最大额外帧

# 对抗 — 好果优先保护
KEEP_GOOD_FRUIT_MIN = 3          # 攻坚/清障后最低好果（不轻易消耗）
GATE_SCOUT_MIN_FRAMES = 8        # 小分队探路宫门最小剩余帧
GATE_SCOUT_MAX_FRAMES = 40       # 最大剩余帧
INTEL_RANGE = 15                 # 情报射程上限（累计路线距离）
REROUTE_VS_CLEAR_EXTRA = 20      # 绕行多出此帧数改清障
SQUAD_AHEAD_MIN_HOPS = 2         # 小分队预清障最小跳跃数
REJECT_BLOCK_ROUNDS = 4          # 拒绝反馈拉黑帧数
ENABLE_OFFENSIVE = False         # 主动设卡开关（delivery-first，默认关）
FP_RETRY_LIMIT = 4               # FORCED_PASS 连续失败上限
FP_RETRY_COOLDOWN = 30           # FORCED_PASS 冷却帧数

# 鲜度感知路由 v4.1：时间预算内优先选鲜度损耗最低的路线
FRESHNESS_FIRST_MAX_EXTRA = 60       # 换鲜度更优路线最多额外帧数
FRESHNESS_ROUTE_SLACK = 50           # 剩余帧 > 最快路径 + 此值时才启用鲜度路由
TARGET_DELIVER_ROUND = 460           # 目标交付回合（平均对局长度）
