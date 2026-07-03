"""集中配置：参数、超时、开关。

禁止写死 playerId / host / port / 阵营（由启动参数与 start 动态决定）。
所有时间单位为秒（除非显式标注）。
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
DECISION_BUDGET = 0.4            # 单帧决策软预算（超过则记录告警；协议建议 500ms 内）

# ---- 日志 ----
LOG_DIR = "logs"                 # 相对启动工作目录；main 会解析为项目根下的 logs/
DEBUG = False                    # 调试开关：True 时额外向 stderr 打印

# ---- 策略调参（M4 收益策略）----
ICE_BOX_USE_BELOW = 78.0            # 鲜度低于此且持有冰鉴时使用（护住 70/80 阈值与鲜度分）
CLAIM_ICE_BOX_KEEP = 1              # 期望至少持有的冰鉴数（低于则在有货节点领取）
HORSE_MIN_REMAINING_DISTANCE = 30   # 剩余到终点路线距离大于此，才值得领取/使用马
RUSH_PROTECT_FRESHNESS_BELOW = 90.0  # RUSH 阶段鲜度低于此用护果令保鲜
DELIVER_TIME_SAFETY_MARGIN = 25     # 交付时间安全余量(帧)：估算做额外读条后仍能按时交付
RESOURCE_CLAIM_ROUND = 2            # 资源领取读条帧数估算（用于时间预算）
SKIP_TASK_TEMPLATES = ("T04", "T06")  # 机会式跳过：T04 需障碍上下文(仅突破时按清障任务处理)，T06 需消耗马

# ---- 策略调参（M5 对抗）----
KEEP_GOOD_FRUIT_MIN = 1             # 攻坚/清障投入好果后必须保留的最低好果（保证仍能交付，好果>0）
GATE_SCOUT_MIN_FRAMES = 8          # 派小分队探路宫门的最小剩余帧（太近来不及/无意义）
GATE_SCOUT_MAX_FRAMES = 40         # 最大剩余帧（探路标记 45 帧有效，避免过早派出而过期）

# ---- 策略调参（M7 能力补全）----
REJECT_BLOCK_ROUNDS = 4             # 被拒移动目标临时拉黑帧数（拒绝反馈，防止重复撞同一阻塞）
INTEL_RANGE = 15                    # 情报射程上限（累计路线距离，任务书 §3.3.4）
TASK_DETOUR_MAX_EXTRA_FRAMES = 70   # 绕路做任务允许的最大额外帧（相对直达终点）
REROUTE_VS_CLEAR_EXTRA = 20         # 绕行比直路多这么多帧时改为就地清障（清障≈6帧+1好果）
SQUAD_AHEAD_MIN_HOPS = 2            # 小分队预清障/削弱要求阻塞位于路径第 N 跳之后（留延迟落地余量）
ENABLE_OFFENSIVE = False            # 主动设卡/增援等进攻干扰（默认关闭：delivery-first，占用己方交付时间）
FP_RETRY_LIMIT = 4                  # FORCED_PASS 连续失败上限（超过则 WAIT）
FP_RETRY_COOLDOWN = 30              # FORCED_PASS 冷却后重试间隔（帧）
