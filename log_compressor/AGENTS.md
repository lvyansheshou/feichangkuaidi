# AGENTS.md — log_compressor 能力基线

> 本文件是日志压缩与关键信息提取系统的**唯一能力基线**。
> 姊妹文档：项目根 `AGENTS.md`（主项目能力基线）。

- 最后更新：2026-07-04
- 当前版本：v1.0
- 日志格式：文本 key=value 格式（`HH:MM:SS.ms Type key1=val1, key2=val2, ...`）

---

## 1. 模块目标

将 client 产出的文本格式运行日志（.log）压缩为适合 git 上传的紧凑格式，同时保留关键分析数据。

**核心需求**：
- 原始日志 ~342KB/场，1744+ 场 = 355MB+，远超 git 限制
- 提取并压缩关键信息，压缩后 <10KB/场
- 压缩产物沉淀到 git，实现"日志分析结果可追溯"

## 2. 模块架构

```
log_compressor/
├── AGENTS.md              # 本文件 — 能力基线
├── __init__.py            # 模块入口，re-export 公共 API
├── __main__.py            # CLI 入口（python -m log_compressor）
├── datatypes.py           # 数据容器：FrameRecord / MatchLog
├── parser.py              # LogParser: 文本日志 → MatchLog
├── compressor.py          # LogCompressor: digest / compact / archive
└── restorer.py            # DigestRestorer: digest JSON → MatchLog
```

**数据流**：
```
.log 文件 ──[parser]──> MatchLog ──[compressor.digest]──> .digest.json (git)
                   │                   └──[compressor.compact]──> .compact.jsonl.gz
                   │                   └──[compressor.archive]──> .tar.gz
                   └──[restorer]──<── .digest.json ──> 分析报告
```

## 3. 能力矩阵

图例：✅ 已实现　🟡 部分实现　❌ 未实现

### 3.1 日志解析 (parser.py)

| 能力 | 状态 | 备注 |
|------|------|------|
| 12 种日志条目解析（Startup/Register/Start/Ready/Frame/Projection/Eta/Action/ModeChange/Over/Score/Shutdown） | ✅ | `LogParser._parse_line()`; key=value 按 ", " 分割 |
| Frame 数据提取（round/phase/node/state/fresh/goodFruit/taskScore/verified/delivered/events） | ✅ | 强类型转换；events 按 `\|` 分割 |
| Projection 提取（myScore/oppScore/gap/mode/myDeliver/oppDeliver/confidence） | ✅ | 浮点 confidence |
| ETA 提取（oppFrom/toGate/toFinish/verified/conf） | ✅ | |
| Action 提取（action/target/note） | ✅ | 区分 NONE（心跳）与真实动作 |
| Score 双视角解析（己方/对手） | ✅ | `me=True/False` 字段区分 |
| 衍生数据计算（路径节点序列、鲜度曲线采样、动作分布） | ✅ | 20 帧采样；action_dist |
| 错误容错（单行 JSON 异常不中断整文件解析） | ✅ | try/except 包裹每行 |

### 3.2 压缩 — digest 模式 (compressor.py)

| 能力 | 状态 | 备注 |
|------|------|------|
| 身份摘要（mid/pid/pn/tid） | ✅ | 短键名 |
| 结果摘要（ts/os/w/dv/dr/or/ff/gf/tsk/bty/of/odr/rt/ore） | ✅ | 己方+对手双视角 |
| 路线摘要（pa: "S01→S06→..."; nc: 节点数） | ✅ | |
| 鲜度曲线采样（fc: [{r, f}]） | ✅ | 每 20 帧 |
| 动作分布（ac: {action: count}; tc: 总次数） | ✅ | |
| 关键时刻（km: 首次动作、节点进入、交付、模式切换） | ✅ | 去重 |
| 得分投影片段（pj: 去冗余，gap 变化 ≥5 才保留） | ✅ | |
| ETA 片段（et: 去冗余，toFinish 变化 ≥20 或 verified 变化才保留） | ✅ | |
| 错误/异常检测（er: FAILED/ERROR 事件） | ✅ | 最多 20 条 |
| 帧统计（tf/sd/up/wp: 总帧数/状态分布/有用帧率/浪费帧率） | ✅ | |
| 批量处理 | ✅ | `batch_digest()` |
| 压缩比 | ✅ | **~81:1**（342KB → 4.1KB）实测 |

### 3.3 压缩 — compact 模式 (compressor.py)

| 能力 | 状态 | 备注 |
|------|------|------|
| 帧心跳去重（连续 N 帧 state/node/phase 不变 → 跳过） | ✅ | HEARTBEAT_WINDOW=3，保留前 3 帧保持精度 |
| 投影去冗余（gap 变化 <5 跳过） | ✅ | |
| ETA 去冗余（toFinish 变化 <20 且 verified 不变 → 跳过） | ✅ | |
| NONE 动作全部移除 | ✅ | |
| 事件过滤（移除 FRESHNESS_DROP/MOVE_PROGRESS/PROCESS_PROGRESS） | ✅ | |
| 短键名编码 | ✅ | k=F/P/E/A/S/R/O 等 |
| gzip 最高压缩 | ✅ | compresslevel=9 |
| 压缩比 | ✅ | **~53:1**（342KB → 6.3KB gzip）实测 |

### 3.4 压缩 — archive 模式 (compressor.py)

| 能力 | 状态 | 备注 |
|------|------|------|
| tar.gz 全量打包 | ✅ | compresslevel=9 |
| 保留原始文件名 | ✅ | arcname=fname |
| 压缩比 | ✅ | ~5:1 估计 |

### 3.5 还原 (restorer.py)

| 能力 | 状态 | 备注 |
|------|------|------|
| digest → MatchLog 重建 | ✅ | 含结果/路线/鲜度/动作/帧/投影/ETA/错误 |
| 批量还原 | ✅ | `batch_restore()` |
| 还原后分析报告生成 | ✅ | `analyze_from_digests()` 输出 Markdown |
| 报告内容（汇总统计 + 每场详情表） | ✅ | 胜率/交付率/均分/鲜度/路线 |

## 4. CLI 接口

```bash
# 通过模块运行
python -m log_compressor                         # 批量 digest
python -m log_compressor --input match.log       # 单文件
python -m log_compressor --mode compact          # 批量 compact
python -m log_compressor --mode archive -o a.tar.gz
python -m log_compressor --stats                 # 统计
python -m log_compressor --restore --report r.md  # 还原分析

# 通过根目录包装脚本（向后兼容）
python log_compressor.py [同上参数]
```

## 5. Digest 键名速查

| 短键 | 全称 | 说明 |
|------|------|------|
| mid | matchId | 对局 ID |
| pid | playerId | 玩家 ID |
| pn | playerName | 玩家名 |
| tid | teamId | 阵营 (RED/BLUE) |
| ts | totalScore | 总分 |
| os | oppScore | 对手分 |
| w | iWon | 是否获胜 |
| dv | delivered | 是否送达 |
| dr | deliverRound | 送达回合 |
| or | overRound | 结束回合 |
| ff | finalFreshness | 最终鲜度 |
| gf | goodFruit | 好果数 |
| tsk | taskScore | 任务分 |
| bty | bountyScore | 悬赏分 |
| pa | path | 路线 (→ 连接) |
| nc | nodeCount | 节点数 |
| fc | freshnessCurve | 鲜度曲线 [{r, f}] |
| ac | actionCounts | 动作分布 |
| tc | totalActions | 总动作数 |
| km | keyMoments | 关键时刻 |
| pj | projections | 得分投影 |
| et | etas | ETA 序列 |
| er | errors | 错误列表 |
| tf | totalFrames | 总帧数 |
| sd | stateDist | 状态分布 |
| up | usefulPct | 有用帧率 |
| wp | wastedPct | 浪费帧率 |

## 6. Agent 职责与工作原则

1. AGENTS.md 是唯一能力基线（SSOT）。
2. 压缩后的 digest 文件应纳入 git，实现可追溯。
3. 原始 .log 和 .gz 文件由 .gitignore 排除。
4. 日志格式变更时同步更新 parser 和 digest 键名。
5. 所有公开 API 通过 `__init__.py` 导出。
6. 保持与根目录 `log_compressor.py` 包装脚本的向后兼容。
