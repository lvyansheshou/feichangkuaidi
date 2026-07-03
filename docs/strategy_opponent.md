# 对手博弈策略设计文档（M8）

> 版本：M8
> 最后更新：2026-07-03
> 依赖：`client/strategy/opponent_model.py`、`client/strategy/decision.py`、`client/core/game_map.py`

---

## 1. 设计动机

### 1.1 为什么要做对抗？

当前 Agent（M7）是纯竞速型：最短路径 → 固定处理 → 宫门验核 → 交付。在无障碍、无对手的理想对局中可达 ~630 分。

但《一骑红尘》是**双人对抗**游戏，地图拓扑决定了对抗的必然性：

```
所有路径最终汇聚到 S10 → S11 → S12 → S13 → S14 → S15（线性必经链）
```

**S10 武关是必经关隘**——先到者在 S10 设卡，后到者必须付出 50 帧时间税或消耗大量果品攻坚。一次成功的 S10 设卡可造成 **+24~50 分的相对优势**。

### 1.2 博弈三阶段

```
┌──────────────────┬─────────────────────┬──────────────────────┐
│  阶段1: 竞速期    │  阶段2: 控场期       │  阶段3: 冲刺期         │
│  frame 1 ~ S10   │  到达S10 → RUSH触发  │  RUSH → 交付          │
│                  │                      │                      │
│  目标: 先到S10    │  目标: 设卡/突破      │  目标: 最快交付        │
│  对抗: 无         │  对抗: ★★★ 核心      │  对抗: 窗口争夺        │
│  策略: 最快路径    │  策略: 条件设卡       │  策略: 破关令验核      │
│       + 资源收集  │       + 悬赏狩猎      │       + 急策选择       │
│       + 顺路任务  │       + 小分队增援     │                      │
└──────────────────┴─────────────────────┴──────────────────────┘
```

---

## 2. 对手模型（OpponentModel）

### 2.1 追踪字段

| 类别 | 字段 | 说明 |
|------|------|------|
| 轨迹 | `visited_nodes` | 对手经过的节点序列 `[(nodeId, round)]` |
| 轨迹 | `last_node` / `last_state` | 当前位置 / 状态 |
| 资产 | `good_fruit` / `bad_fruit` / `freshness` | 对手货物状态 |
| 资产 | `task_score` / `squad_available` | 对手进度 |
| 资产 | `guard_action_point` / `delivered` | 对手战术资产 |
| 行为 | `window_history` | 窗口出牌历史 `[{type, round, card, won}]` |
| 行为 | `guard_nodes` | 对手设卡位置集合 |

### 2.2 路径预测

```
estimate_opponent_path(gm):
  if 对手走过官道节点(S02/S03/S04) → 预测官道路径
  if 对手走过山路节点(S06/S08)     → 预测山路路径
  else                             → time_optimal_path (和我们一样的逻辑)
```

### 2.3 ETA 估算

```
estimate_eta_to(gm, target_node):
  返回 time_optimal_path(对手当前位置, target_node) 的帧数
  用于判断对手何时到达关键位置
```

---

## 3. 战略态势判定

### 3.1 五态模型

```
输入: world（当前帧）、me（我方）、gm（地图）
输出: posture ∈ {racing, leading, trailing, contested, sprinting}
```

```
if world.is_rush or 任一方已交付:
    → sprinting

计算 lead_frames = 对手到终点ETA - 我方到终点ETA

if   lead_frames > 50:   → leading      大幅领先，可以设卡
elif lead_frames > 10:   → racing       小幅领先，继续竞速
elif lead_frames > -20:  → contested    胶着状态
else:                    → trailing     落后，需要追分
```

### 3.2 态势对策略的影响

| 态势 | 任务策略 | 设卡 | 窗口出牌 | 移动策略 |
|------|---------|------|---------|---------|
| **leading** | 保守：绕路预算-20 | ✅ 可设卡 | 消耗战 | 优先保持领先 |
| **racing** | 正常：机会式 | ✅ 可设卡 | 正常 | 最快路径 |
| **contested** | 正常：机会式 | ❌ 不设卡 | 积极反制 | 争夺先手 |
| **trailing** | 激进：绕路预算+40 | ❌ 不设卡 | 积极反制 | 追逐模式 |
| **sprinting** | 不做任务 | ❌ 不设卡 | 资源耗尽则弃权 | 最快冲刺 |

---

## 4. 自适应窗口出牌

### 4.1 对手出牌模式学习

每帧从 `world.contests` 的已结算结果中提取对手出牌，按窗口类型分组存储：

```
window_history = [
  {contestType: "PASS", round: 245, card: "BING_ZHENG", won: true},
  {contestType: "PASS", round: 248, card: "BING_ZHENG", won: true},
  {contestType: "TASK",  round: 180, card: "XIAN_GONG",  won: false},
  ...
]
```

### 4.2 反制逻辑

```
adaptive_window_card(world, me, contest):
  1. predict_opponent_card(contestType):
       → 取同类型历史中对手最常用的牌

  2. counter_card(predicted_card):
       → 查克制表，找能击败对手的牌

  3. 验证资源可用性:
       BING_ZHENG  → guard_action_point > 0
       XIAN_GONG   → freshness ≥ 80 AND good_fruit > KEEP_GOOD_FRUIT_MIN
       YAN_DIE     → PASS_TOKEN > 0 OR OFFICIAL_PERMIT > 0
       QIANG_XING  → 有马类 buff 或持有马资源

  4. 若克制牌不可用 → 回退固定优先级
```

### 4.3 窗口牌克制表（任务书 §5.4.4）

| 对手出 ↓ / 我方出 → | YAN_DIE | QIANG_XING | XIAN_GONG | BING_ZHENG |
|---------------------|---------|------------|-----------|------------|
| **YAN_DIE** | 平 | 负 | **胜** | **胜** |
| **QIANG_XING** | **胜** | 平 | 负 | **胜** |
| **XIAN_GONG** | 负 | **胜** | 平 | 负 |
| **BING_ZHENG** | 负 | 负 | **胜** | 平 |

最佳反制速查：

| 对手预测出 | 我方反制 | 成本 |
|-----------|---------|------|
| BING_ZHENG | XIAN_GONG | 1好果 + 鲜度≥80 |
| XIAN_GONG | QIANG_XING | 马资源 |
| QIANG_XING | BING_ZHENG | 1护卫行动点 |
| YAN_DIE | BING_ZHENG | 1护卫行动点 |

### 4.4 回退固定优先级

当对手模型无数据或反制不可用时：

```
BING_ZHENG > XIAN_GONG > YAN_DIE > QIANG_XING > ABSTAIN
```

---

## 5. 条件设卡（Guard Offense）

### 5.1 设卡准入条件

```
should_set_guard(world, me, gm, node_id):

  ✅ config.ENABLE_OFFENSIVE = True
  ✅ 态势为 racing 或 leading（我方领先）
  ✅ node 是必经节点（chokepoint）
  ✅ 对手尚未通过此节点
  ✅ 节点无敌方有效设卡
  ✅ 好果 ≥ 20 + 设卡基础成本 + KEEP_GOOD_FRUIT_MIN
  ✅ 我方在此节点或已通过此节点
```

### 5.2 最优好果投入

| 节点类型 | 好果 ≥ | extra | 防守值 | 风化延迟 | 对手攻坚成本 |
|---------|--------|-------|--------|---------|------------|
| KEY_PASS | 22 | 2 | 6 | 45帧 | 2好+1坏(=7) 或 FORCED_PASS 50帧 |
| KEY_PASS | 21 | 1 | 4 | 45帧 | 2好(=4) 或 FORCED_PASS 35帧 |
| 普通 | 21 | 1 | 4 | 30帧 | 2好(=4) 或 FORCED_PASS 30帧 |
| 任意 | <21 | 0 | 2 | 30帧 | 1好(=2) 或 FORCED_PASS 20帧 |

### 5.3 设卡时机建议

最佳设卡点（按优先级）：
1. **S10 武关**（KEY_PASS，必经，防守上限7，悬赏18分）
2. **S11 潼关驿**（必经，带固定处理，对手被迫停留5帧）
3. **S13 灞桥驿**（必经，带固定处理）

不设卡的节点：
- S14 朱雀门（防守上限仅4，成本1好果，ROI低）
- S15 兴庆宫（禁止设卡）

---

## 6. 必经节点检测（Chokepoint）

### 6.1 算法

```
chokepoints():
  从 terminal 反向 BFS 收缩:
    choke = {terminal}
    queue = [terminal]
    while queue:
      cur = queue.pop()
      for pred in 原始边前驱(cur):  # 只沿 from→to 方向的反向
        if pred 的所有前向后继 ⊆ choke:
          choke.add(pred)
          queue.append(pred)
    return choke
```

### 6.2 当前地图（medium）必经节点

```
S10(武关) → S11(潼关驿) → S12(关中平原) → S13(灞桥驿) → S14(朱雀门) → S15(兴庆宫)
```

从 S10 开始，所有后续节点都是必经——这是一条不可绕行的线性链。

---

## 7. FORCED_PASS 退避机制

### 7.1 问题

当 `bad_fruit = 0` 时，最大攻坚值 = 2好果 × 2 = 4。若敌方设卡防守值 ≥ 5，攻坚不可行，只能 FORCED_PASS。如果 PASS 窗口持续失利，Agent 会陷入无限循环：

```
FORCED_PASS → PASS窗口 → 失利 → RESTING 8帧 → 再 FORCED_PASS → ...
```

### 7.2 方案

```
连续失败计数器: fp_failures[nodeId]

FORCED_PASS 提交前:
  if fp_failures[nodeId] >= FP_RETRY_LIMIT (4):
      return []  ← WAIT，等对手交付后 PASS 窗口自动弃权

成功后（到达目标节点 / 攻坚破卡成功）:
  重置计数器
```

### 7.3 检测逻辑

```
每帧 _apply_rejection_feedback():
  if 上帧动作 == FORCED_PASS:
    if 当前位置 == 目标节点 → 成功，重置
    if 状态 == RESTING       → 失败（计数已在 _breakthrough 中递增）
```

---

## 8. 态势感知任务优先级

### 8.1 动态绕路预算

| 当前任务分 | 基础预算 | 原因 |
|-----------|---------|------|
| < 60 | 70 帧 | 追求第一个里程碑（+15 任务分） |
| 60-89 | **110 帧** | 逼近 90 阈值（解锁满额送达 240 + 用时系数 1.0） |
| 90-109 | 70 帧 | 追求 110 里程碑（+15 任务分） |
| ≥ 110 | 20 帧 | 已全部解锁，随缘 |

### 8.2 态势修正

```
最终预算 = 基础预算 + 态势修正

leading:   -20  ← 保持领先优先
racing:      0  ← 正常
contested:   0  ← 正常
trailing:  +40  ← 激进追分
sprinting:   0  ← 不做任务（RUSH 阶段跳过）
```

---

## 9. 三种典型对局推演

### 9.1 我方先到 S10

```
竞速期: 最快路径 → S10
  优先速度，顺路任务才做
  收集冰鉴（S06）+ 短程马（S08）
  squad 预清 S08 障碍

控场期: S10 设卡
  SET_GUARD@S10 (extra=2, defense=6)
  继续推进到 S11→S12→S13→S14
  squad SCOUT@S14（验核 6→3）
  如果对手开始攻坚 → squad REINFORCE@S10

冲刺期:
  VERIFY_GATE + BREAK_ORDER (3帧)
  RUSH_SPEED（无马时）/ RUSH_PROTECT（鲜度低时）
  DELIVER
  → 对手被迫 FORCED_PASS S10 设卡（+50帧+窗口风险）
```

### 9.2 对手先到 S10

```
竞速期: 加速到 S10
  激进做任务（需要追分弥补设卡损失）
  走山路捷径（接受鲜度代价，抢时间）

控场期: 突破敌方 S10 设卡
  策略A: 攒坏果 → 攻坚破卡（1好+2坏=8攻坚值，成本约4分）
  策略B: 坏果不足 → FORCED_PASS（50帧+窗口争夺）
  窗口出牌: 追踪对手出牌模式，反制

冲刺期:
  最快交付（不再恋战）
  冰鉴补鲜度
```

### 9.3 双方胶着

```
→ 谁先到 S10 谁赢
→ 山路捷径是关键优势
→ 必要时放弃任务追求速度
→ 窗口争夺: 积极反制对手
→ squad 预清障保好果（好果=100 → 180分）
```

---

## 10. 配置参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `ENABLE_OFFENSIVE` | `False` | 主动设卡总开关（开启后由 OpponentModel 条件触发） |
| `FP_RETRY_LIMIT` | `4` | FORCED_PASS 连续失败上限 |
| `KEEP_GOOD_FRUIT_MIN` | `1` | 攻坚/清障后至少保留的好果 |
| `TASK_DETOUR_MAX_EXTRA_FRAMES` | `70` | 任务绕路基础预算（态势和里程碑会动态修正） |
| `REROUTE_VS_CLEAR_EXTRA` | `20` | 绕行比直路多出此帧数时改为就地清障 |

---

## 11. 预期效果

| 维度 | M7（纯竞速） | M8（博弈对抗） | 提升 |
|------|------------|--------------|------|
| 领先时胜率 | 依赖纯竞速 | +15~25%（设卡控场） | 控场致胜 |
| 落后时翻盘 | 被动等待 | 激进任务+攻坚突破 | 追分路径 |
| 窗口胜率 | ~50%（固定优先级） | ~65%（反制+回退） | +15% |
| FORCED_PASS 死循环 | 存在风险 | 已消除（4次退避） | 鲁棒性 |
| 宫门验核 | 6帧 | 3帧（破关令） | -3帧 |
| 任务分 60→90 | ~30% 概率 | ~70% 概率 | 解锁满额送达 |
| 冰鉴效用 | 固定78阈值 | 阈值感知提前预警 | 减少好果转坏 |
| 最坏情况概率 | ~5% | <1% | FORCED_PASS退避 |

---

## 12. 文件清单

| 文件 | 操作 | 行数 | 说明 |
|------|------|------|------|
| `client/strategy/opponent_model.py` | 新建 | ~220 | 对手模型：追踪、预测、态势、自适应 |
| `client/strategy/decision.py` | 修改 | +90 | 集成模型、破关令验核、态势感知、FORCED_PASS退避 |
| `client/core/game_map.py` | 修改 | +40 | 必经节点检测 `chokepoints` / `is_chokepoint` |
| `client/config.py` | 修改 | +1 | `FP_RETRY_LIMIT = 4` |

测试: `client/tests/` 91/91 通过，`test_strategy.py` / `test_advanced.py` 已同步更新。
