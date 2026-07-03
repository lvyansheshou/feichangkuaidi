# 策略测试系统设计

> 目标：验证策略正确性 + 评估对战表现 + 防止回归

---

## 1. 现状与缺口

| 已有能力 | 覆盖范围 | 缺口 |
|---------|---------|------|
| 91 项单元测试 | 拆帧/寻路/规则/策略动作 | 只测单帧逻辑，不测多帧博弈 |
| `mock_server.py` | 单人端到端仿真 | 不支持双人对战 |
| `analysis/` 四件套 | 单场赛后分析 | 无批量对比、无回归检测 |

**核心缺口：无法回答"改了策略后，胜率是升了还是降了？"**

---

## 2. 测试金字塔

```
         ┌──────────────┐
         │  锦标赛系统    │  ← 批量对战，统计胜率
         │  (tournament) │
        ┌┴──────────────┴┐
        │  场景回归测试   │  ← 预设场景，断言行为
        │  (regression)  │
       ┌┴───────────────┴┐
       │  双人 Mock 服务   │  ← 完整对局仿真引擎
       │  (mock_server)  │
      ┌┴────────────────┴┐
      │  单元测试 (91项)   │  ← 函数级正确性
      └─────────────────┘
```

---

## 3. 第一层：双人对战 Mock Server

### 3.1 架构

```
┌────────────────────────────────────────────────┐
│              MockServer (双人模式)               │
│                                                  │
│  ┌──────────┐  ┌──────────┐  ┌──────────────┐  │
│  │ 红方引擎  │  │ 蓝方引擎  │  │  游戏状态机   │  │
│  │ (ours)   │  │ (opponent)│  │  (规则引擎)   │  │
│  └────┬─────┘  └────┬─────┘  └──────┬───────┘  │
│       │              │               │          │
│       └──────────────┴───────────────┘          │
│                      │                          │
│              每帧同时收两方动作                    │
│              结算冲突/窗口/移动                    │
│              推进游戏状态                          │
│              下发下一帧 inquire                   │
└────────────────────────────────────────────────┘
```

### 3.2 实现方案

扩展现有 `mock_server.py`，核心改动：

```python
class DuelMockServer:
    """双人对战模拟器。"""

    def __init__(self, map_config, red_engine_factory, blue_engine_factory):
        self.red = red_engine_factory()   # 我方策略
        self.blue = blue_engine_factory() # 对手策略
        self.state = GameState(map_config) # 完整游戏状态
        self.round = 0
        self.log = []

    def step(self):
        """推进一帧：收双方动作 → 结算 → 更新状态 → 返回新 inquire。"""
        red_actions = self.red.decide(self.state.inquire_for("RED"))
        blue_actions = self.blue.decide(self.state.inquire_for("BLUE"))
        events = self.state.resolve(red_actions, blue_actions)
        self.round += 1
        return events

    def run(self) -> MatchResult:
        """运行完整对局直到 over。"""
        while not self.state.is_over:
            self.step()
        return self.state.final_result()
```

### 3.3 关键难点

| 难点 | 说明 | 方案 |
|------|------|------|
| 游戏状态模拟 | 需要完整实现移动/处理/资源/任务/窗口/设卡/攻坚等全部规则 | 渐进式：先实现移动+处理，再逐步加对抗 |
| 窗口争夺 | 3拍出牌、胜负判定、休整 | 双方 `_window_card` 同时调用，查克制表结算 |
| 天气 | 预告→生效→结束 | 按任务书 §2.5 的帧范围随机触发 |
| 任务刷新 | 刷新规则不公开 | 用 `samples/map_config.json` 的固定任务模板预设 |

**简化策略**：不需要完美模拟所有规则细节。只要能：
- 正确模拟移动耗时（到站帧数）
- 正确模拟固定处理
- 正确模拟设卡阻挡/攻坚/FORCED_PASS
- 大致估算鲜度损耗

就可以给出有参考价值的胜负对比。

---

## 4. 第二层：可配置对手

### 4.1 对手基类

```python
class OpponentStrategy(ABC):
    """对手策略基类。"""
    def __init__(self, name):
        self.name = name
        self.actions_log = []

    @abstractmethod
    def decide(self, world: WorldState) -> list:
        """返回本帧动作列表。"""
        ...

class OpponentFactory:
    """根据配置创建对手。"""
    @staticmethod
    def create(preset: str, **kwargs) -> OpponentStrategy:
        ...
```

### 4.2 预置对手类型

| 对手 | 策略描述 | 测试目的 |
|------|---------|---------|
| **PureRacer** | 最短路 + 固定处理 + 交付，不做任务不设卡 | 基线对比：纯竞速天花板 |
| **TaskFocused** | 顺路做任务追求 90 阈值，不设卡 | 测试我方控场对任务型对手的压制 |
| **AggressiveGuard** | 在 S10/S11 必经节点设卡（防守值最大），积极守窗 | 测试我方突破能力 |
| **Balanced** | 领先时设卡，落后时追任务 | 测试我方态势判定的准确性 |
| **EarlyRush** | 用疾行令 + 破关令最快冲刺 | 测试我方对速攻的应对 |
| **RandomValid** | 在当前状态下随机选合法动作 | 压力测试：鲁棒性 |
| **Mirror** | 使用我们的 M7 策略 | 回归测试：M8 vs M7 提升幅度 |

### 4.3 对手实现示例

```python
class AggressiveGuard(OpponentStrategy):
    """激进设卡型对手：在必经关隘设卡，积极守窗。"""

    def decide(self, world):
        me = world.me
        gm = world.game_map

        if me.delivered:
            return []

        # 窗口出牌：总是出最强牌
        card = self._best_window_card(world)
        if card:
            return [card]

        if me.state != "IDLE":
            return self._state_actions(me)

        # 核心策略：在必经节点设卡
        if gm.is_chokepoint(me.current_node_id):
            if self._can_set_guard(me):
                return [actions.set_guard(me.current_node_id, extra_good_fruit=2)]

        # 最短路推进
        path, _ = gm.time_optimal_path(me.current_node_id, gm.terminal_nodes[0])
        if path and len(path) > 1:
            return [actions.move(path[1])]
        return []
```

---

## 5. 第三层：场景回归测试

### 5.1 设计思路

预设地图 + 预设对手行为 → 断言我方应该产生特定动作或达到特定分数。

```python
class ScenarioTest:
    """场景 = 地图 + 对手脚本 + 断言条件。"""

    def __init__(self, name, map_config, opponent_script, assertions):
        self.name = name
        self.map = map_config
        self.opponent_script = opponent_script  # 对手每帧的预设动作
        self.assertions = assertions             # 断言列表
```

### 5.2 预置场景清单

| 场景 | 地图特征 | 对手行为 | 断言 |
|------|---------|---------|------|
| `test_evade_s06_obstacle` | S06 有障碍 | 纯竞速 | 我方应绕行官道（不 CLEAR） |
| `test_break_enemy_guard_s10` | S10 敌方设卡(防7) | 设卡后退到 S11 | 我方应攒坏果或 FORCED_PASS |
| `test_set_guard_when_leading` | 我方先到 S10 | 落后 50 帧 | 我方应在 S10 设卡 |
| `test_no_guard_when_trailing` | 对手先到 S10 | 对手在 S10 设卡 | 我方不应设卡（应突破） |
| `test_rush_break_order` | RUSH 阶段 | — | 我方应在 S14 用破关令验核 |
| `test_forced_pass_retry_limit` | 敌方 S10 设卡(防7) + 坏果=0 | 对手积极守窗连赢 5 次 | 我方第 5 次应 WAIT 而非 FORCED_PASS |
| `test_window_card_counter` | 对手出 BING_ZHENG(3次) | 预设窗口 | 我方应学习并出 XIAN_GONG |
| `test_task_90_threshold` | S08/S10 有 T11/T02 活跃 | 纯竞速 | 我方应绕路做任务达到 90 分 |

### 5.3 实现

```python
def test_evade_s06_obstacle():
    """S06 有障碍 → 应走官道绕行而非 CLEAR。"""
    map_data = load_map_with_obstacle("S06")
    server = DuelMockServer(map_data, OurEngine, PureRacer)

    # 运行到我们到达 S02（说明走了官道）
    for _ in range(200):
        server.step()
        me = server.state.get_player("RED")
        if me.current_node_id == "S02":
            break

    # 断言：我们走了官道（S02），而非清除 S06 障碍
    red_log = server.get_action_log("RED")
    clear_actions = [a for a in red_log if a.get("action") == "CLEAR"]
    assert len(clear_actions) == 0, "不应该 CLEAR S06，应绕行官道"
    assert me.good_fruit == 100, "不应消耗好果"
```

---

## 6. 第四层：锦标赛系统

### 6.1 设计

```python
class Tournament:
    """运行 N 场对局，统计胜负和得分分布。"""

    def __init__(self, our_engine_factory, opponent_presets, map_configs,
                 matches_per_pair=10):
        self.our_factory = our_engine_factory
        self.opponents = [OpponentFactory.create(p) for p in opponent_presets]
        self.maps = map_configs
        self.n = matches_per_pair
        self.results = []

    def run(self):
        for opp in self.opponents:
            for m in self.maps:
                for i in range(self.n):
                    result = self._play_match(m, opp)
                    self.results.append(result)
        return self.summarize()

    def _play_match(self, map_config, opponent):
        # 随机分配红蓝方
        if random.random() > 0.5:
            server = DuelMockServer(map_config, self.our_factory, opponent)
            our_side = "RED"
        else:
            server = DuelMockServer(map_config, opponent, self.our_factory)
            our_side = "BLUE"
        return server.run()

    def summarize(self):
        return TournamentReport(self.results)
```

### 6.2 输出报告

```
═══════════════════════════════════════════
          M8 策略 锦标赛报告
═══════════════════════════════════════════

对手: PureRacer     对局: 20   胜率: 85%   平均分差: +42
对手: TaskFocused   对局: 20   胜率: 78%   平均分差: +28
对手: AggressiveGuard 对局: 20 胜率: 55%   平均分差: +5
对手: Balanced      对局: 20   胜率: 62%   平均分差: +15
对手: Mirror(M7)    对局: 20   胜率: 68%   平均分差: +22
───────────────────────────────────────────
总计: 100 场   综合胜率: 69.6%   平均分差: +22.4

M8 vs M7 提升: 胜率 +18%, 平均分 +22
```

---

## 7. 实施路线

### Phase 1：核心基础设施（2-3 天）

```
□ 扩展现有 mock_server.py 支持双人模式
  - 双 DecisionEngine 同时注入
  - 基础规则模拟（移动、处理、交付）
  - 基础冲突解决（同时到达同一节点等）

□ 对手基类 + PureRacer + AggressiveGuard
  - 纯竞速：只用 shortest_path + 固定处理
  - 激进设卡：必经节点设卡+守窗
```

### Phase 2：场景回归（1-2 天）

```
□ 8 个核心场景（见 §5.2）
□ 每个场景一个 test_ 函数
□ 集成到 CI（push 前自动跑）
```

### Phase 3：锦标赛（1-2 天）

```
□ Tournament 运行器
□ 7 种预置对手
□ 报告生成（胜率/分差/得分分布）
□ M7 vs M8 对比模式
```

### Phase 4：持续改进（按需）

```
□ 对手 AI 升级（用我们的旧版本策略作为对手）
□ 更多地图变体
□ 性能基准（决策耗时分布）
□ 可视化回放
```

---

## 8. 测试策略正确性的检查清单

| 类别 | 检查项 | 方法 |
|------|--------|------|
| **交付稳定性** | 600 帧内必定交付 | 1000 场随机地图仿真，交付率 ≥ 99% |
| **动作合法性** | 无非法动作扣分 | 检查 actionResults 中 accepted=false 的次数 |
| **好果保护** | 交付好果 ≥ 95 | 障碍场景中 squad 预清障是否生效 |
| **任务达成** | task_base ≥ 60 概率 > 80% | 多场统计 |
| **设卡时机** | 领先时在必经节点设卡 | 场景断言 |
| **突破能力** | 面对防7设卡不超时 | 压力场景 |
| **FORCED_PASS退避** | 失败4次后 WAIT | 场景断言 |
| **窗口反制** | 对重复出牌的反制率 > 60% | 统计对手出 BING_ZHENG 3次后我方是否出 XIAN_GONG |
| **M8 > M7** | 对相同对手胜率提升 > 10% | A/B 对比 |

---

## 9. 文件结构

```
testing/
├── __init__.py
├── duel_mock_server.py        # 双人对战模拟器
├── opponents/
│   ├── __init__.py
│   ├── base.py                # OpponentStrategy 基类
│   ├── pure_racer.py          # 纯竞速
│   ├── aggressive_guard.py    # 激进设卡
│   ├── balanced.py            # 平衡型
│   ├── task_focused.py        # 任务优先
│   ├── mirror.py              # M7 镜像
│   └── random_valid.py        # 随机合法动作
├── scenarios/
│   ├── __init__.py
│   └── test_scenarios.py      # 8 个场景回归测试
├── tournament.py              # 锦标赛运行器
├── tournament_report.py       # 报告生成
└── README.md
```
