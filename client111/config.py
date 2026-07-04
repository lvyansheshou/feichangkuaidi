"""集中配置。

禁止写死 playerId / host / port / 阵营（由启动参数与 start 动态决定）。
所有时间单位为秒（除非显式标注帧）。

v4.5: 移植 demo 对手优势 — 冰鉴≤90、鲜度λ路由、预算豁免、任务追180。
"""

# ---- 客户端标识 ----
CLIENT_VERSION = "1.0"
DEFAULT_PLAYER_NAME = "litchi-agent"

# ---- 帧格式（协议 §1）----
LENGTH_PREFIX_WIDTH = 5
MAX_FRAME_BODY_BYTES = 99999

# ---- socket ----
RECV_CHUNK = 65536
CONNECT_TIMEOUT = 10.0

# ---- 时序超时 ----
HANDSHAKE_TIMEOUT = 30.0
RECV_LOOP_TIMEOUT = 1.0
DECISION_BUDGET = 0.4

# ---- 日志 ----
LOG_DIR = "logs"

# ================================================================
#  策略参数 v4.5: 移植 demo 对手优势
# ================================================================

# -- 冰鉴（demo 核心优势）--
# demo 洞察: ≤90 用不撞 100 上限; 线性损耗下冰鉴+10为永久偏移; 2个叠20可把80阈值延后到交付后
ICE_BOX_CAP_AVOID = 90.0           # 鲜度≤此值即用（不撞100上限，全效存活到交付）
ICE_BOX_LEAD = 7.0                 # （保留兼容）
ICE_BOX_HOT_USE_BELOW = 88.0       # （保留兼容）
CLAIM_ICE_BOX_KEEP = 3             # 期望至少持有的冰鉴数
ICE_BOX_DETOUR_KEEP = 2            # 绕路收集冰鉴的目标持有量（达到即停）
ICE_BOX_DETOUR_PROJECTED_BELOW = 88.0  # v4.5fix2: 投影鲜度<88绕路领冰鉴（平衡版：85太严/92太松）
ICE_BOX_DETOUR_MAX_EXTRA_FRAMES = 60   # v4.5fix2: 对齐demo默认值
ICE_BOX_DETOUR_NET_MIN = 6.0       # v4.5fix2: 对齐demo默认值，排除山路绕路

# -- 马 --
HORSE_MIN_REMAINING_DISTANCE = 30

# -- 急策 --
RUSH_PROTECT_FRESHNESS_BELOW = 90.0  # RUSH阶段鲜度低于此用护果令

# -- 安全余量 --
DELIVER_TIME_SAFETY_MARGIN = 25   # 交付时间安全余量(帧)
TASK_DETOUR_SAFETY_MARGIN = 15    # 任务绕路专用更紧余量

# -- 任务（demo: 追180封顶）--
TASK_SEEK_TARGET = 180            # 任务分达此值即不再绕路
SKIP_TASK_TEMPLATES = ("T04", "T06")
TASK_DETOUR_MAX_EXTRA_FRAMES = 70  # 绕路做任务最大额外帧
RESOURCE_CLAIM_ROUND = 2           # 资源领取读条帧数

# -- 对抗 --
KEEP_GOOD_FRUIT_MIN = 1            # 攻坚/清障后最低好果
GATE_SCOUT_MIN_FRAMES = 8
GATE_SCOUT_MAX_FRAMES = 40
INTEL_RANGE = 15
REROUTE_VS_CLEAR_EXTRA = 20        # 绕行>此帧数改清障
SQUAD_AHEAD_MIN_HOPS = 2
REJECT_BLOCK_ROUNDS = 4
ENABLE_OFFENSIVE = False           # 进攻设卡(暂关，后续迭代)
FP_RETRY_LIMIT = 4
FP_RETRY_COOLDOWN = 30

# -- 鲜度路由（demo: λ=5.0 差分式）--
FRESHNESS_ROUTE_LAMBDA = 5.0       # 路由鲜度权重λ: 边权+=λ×帧数×(路线损耗-WATER损耗)
FRESHNESS_DETOUR_FLOOR = 65.0      # 绕路做任务的鲜度地板
FRESHNESS_LOSS_ASSUME = 0.06       # 鲜度预算估算用每帧损耗(保守)

# -- 后期前置宫门 --
RUSH_PREPOSITION_ROUND = 360       # 此帧后未验核→直奔宫门

# -- 目标 --
TARGET_DELIVER_ROUND = 540           # v4.5fix: 降低目标交付回合（原560过宽，第1场官道未到达）
TARGET_FRESHNESS = 88.0
MAX_DELIVER_ROUND_HARD = 580         # v4.5fix: 硬性上限，超过此值强制退回时间最优路径
