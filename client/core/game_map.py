"""静态地图镜像 GameMap（由 start 消息构建）。

从 start.msg_data 的顶层 nodes[]/edges[] 构建；roles 优先读 map.gameplay.roles，
缺失时（如直接喂 map_config.json）按节点类型推断。提供：
- 相邻节点、边查询
- 到站移动量 move_amount（rules.to_station_move_amount）
- 最短路 shortest_path（按移动量或路线距离两种度量）
- 最短路线距离 route_distance（用于情报射程≤15、冲刺触发≤15 等距离口径）

方向规则（§2.3.3）：每条边从 fromNode→toNode 恒可达；bidirectional 时 toNode→fromNode 也可达。
bidirectional 缺省视为 True（map_config 无该字段；正式 start 会携带）。
"""

import math
from dataclasses import dataclass

from core import pathfind, rules


@dataclass
class Node:
    node_id: str
    name: str
    type: str
    x: int
    y: int
    is_start: bool = False
    is_terminal: bool = False


@dataclass
class Edge:
    edge_id: str
    from_node: str
    to_node: str
    route_type: str
    distance: int
    bidirectional: bool = True


class GameMap:
    def __init__(self, start_data):
        raw_nodes = start_data.get("nodes") or (start_data.get("map", {}) or {}).get("nodes") or []
        raw_edges = start_data.get("edges") or (start_data.get("map", {}) or {}).get("edges") or []

        self.nodes = {}
        for n in raw_nodes:
            node = Node(
                node_id=n.get("nodeId"),
                name=n.get("name"),
                type=n.get("type") or n.get("nodeType"),
                x=n.get("x"),
                y=n.get("y"),
                is_start=bool(n.get("start")) or (n.get("type") or n.get("nodeType")) == "START",
                is_terminal=bool(n.get("terminal")) or (n.get("type") or n.get("nodeType")) in ("FINISH", "TERMINAL"),
            )
            if node.node_id:
                self.nodes[node.node_id] = node

        self.edges = []
        # 邻接表：node -> [(neighbor, weight)]
        self._adj_move = {}
        self._adj_dist = {}
        for i, e in enumerate(raw_edges):
            frm = e.get("fromNodeId") or e.get("fromNode")
            to = e.get("toNodeId") or e.get("toNode")
            if not frm or not to:
                continue
            route_type = e.get("routeType")
            distance = e.get("distance", 0)
            bidir = e.get("bidirectional", True)
            edge = Edge(
                edge_id=e.get("edgeId") or ("E%02d" % (i + 1)),
                from_node=frm, to_node=to, route_type=route_type,
                distance=distance, bidirectional=bidir,
            )
            self.edges.append(edge)
            move_w = rules.to_station_move_amount(distance, route_type)
            self._add_adj(frm, to, move_w, distance)
            if bidir:
                self._add_adj(to, frm, move_w, distance)

        self.roles = self._parse_roles(start_data)
        self.process_nodes = self._parse_process_nodes(start_data)

    def _add_adj(self, a, b, move_w, dist_w):
        self._adj_move.setdefault(a, []).append((b, move_w))
        self._adj_dist.setdefault(a, []).append((b, dist_w))

    def _parse_roles(self, start_data):
        gameplay = (start_data.get("map", {}) or {}).get("gameplay", {}) or {}
        roles = dict(gameplay.get("roles") or {})
        # 缺失时按节点类型 / safeZones / reverifyNode 推断
        if not roles.get("startNodeId"):
            starts = [n.node_id for n in self.nodes.values() if n.is_start]
            if starts:
                roles["startNodeId"] = starts[0]
        if not roles.get("terminalNodeIds"):
            terms = [n.node_id for n in self.nodes.values() if n.is_terminal]
            if terms:
                roles["terminalNodeIds"] = terms
        if not roles.get("gateNodeId"):
            gates = [n.node_id for n in self.nodes.values() if n.type == "GATE"]
            if gates:
                roles["gateNodeId"] = gates[0]
            elif start_data.get("reverifyNode"):
                roles["gateNodeId"] = start_data["reverifyNode"].get("nodeId")
        if not roles.get("safeZoneNodeIds"):
            sz = [z.get("nodeId") for z in (start_data.get("safeZones") or [])]
            if sz:
                roles["safeZoneNodeIds"] = sz
        return roles

    def _parse_process_nodes(self, start_data):
        """固定处理站点集合 node_id -> {processType, processName, processRound}。

        优先 start.map.gameplay.processNodes（英文 processType）；再并入顶层 processNodes
        （map_config：中文 processName）。gate 也可能在其中，策略层单独用 VERIFY_GATE 处理。
        """
        result = {}
        gameplay = (start_data.get("map", {}) or {}).get("gameplay", {}) or {}
        for p in gameplay.get("processNodes", []) or []:
            nid = p.get("nodeId")
            if nid:
                result[nid] = {"processType": p.get("processType"),
                               "processRound": p.get("processRound", 0) or 0}
        for p in start_data.get("processNodes", []) or []:
            nid = p.get("nodeId")
            if nid and nid not in result:
                result[nid] = {"processType": p.get("processType"),
                               "processName": p.get("processName"),
                               "processRound": p.get("processRound", 0) or 0}
        return result

    # ---- 查询 ----

    @property
    def start_node(self):
        return self.roles.get("startNodeId")

    @property
    def gate_node(self):
        return self.roles.get("gateNodeId")

    @property
    def terminal_nodes(self):
        return self.roles.get("terminalNodeIds") or []

    def node(self, node_id):
        return self.nodes.get(node_id)

    def neighbors(self, node_id):
        """按静态边可达的相邻节点列表（不考虑运行期设卡/障碍）。"""
        return [nb for nb, _ in self._adj_move.get(node_id, ())]

    def edge_between(self, a, b):
        """返回从 a 出发、按方向可达 b 的边；无则 None。"""
        for e in self.edges:
            if e.from_node == a and e.to_node == b:
                return e
            if e.bidirectional and e.from_node == b and e.to_node == a:
                return e
        return None

    def move_amount(self, a, b):
        """相邻 a→b 的到站所需移动量；不相邻返回 inf。"""
        e = self.edge_between(a, b)
        if e is None:
            return math.inf
        return rules.to_station_move_amount(e.distance, e.route_type)

    def shortest_path(self, source, target, metric="move"):
        """最短路 (path, cost)。metric='move' 按到站移动量；'distance' 按路线距离。"""
        adj = self._adj_move if metric == "move" else self._adj_dist
        return pathfind.shortest_path(adj, source, target)

    def time_optimal_path(self, source, target, base_move=None, blocked=None):
        """按"帧数"最短路 (path, frames)。边权 = 单边到站帧数 + 目标节点固定处理耗时。

        比纯移动量更贴近真实用时：会为途经的固定处理站点计入读条帧数（宫门 VERIFY 为任何
        路线终局必经，不计入路线差异）。base_move 默认无加速 1000。
        blocked：不可进入的节点集合（障碍/敌方有效设卡）——跳过所有进入这些节点的边，实现绕行。
        """
        bm = rules.BASE_MOVE_NONE if base_move is None else base_move
        blocked = blocked or frozenset()
        adj = {}
        for e in self.edges:
            base = rules.frames_on_edge(e.distance, e.route_type, bm)
            if e.to_node not in blocked:
                adj.setdefault(e.from_node, []).append((e.to_node, base + self._proc_cost(e.to_node)))
            if e.bidirectional and e.from_node not in blocked:
                adj.setdefault(e.to_node, []).append((e.from_node, base + self._proc_cost(e.from_node)))
        return pathfind.shortest_path(adj, source, target)

    def _proc_cost(self, node_id):
        info = self.process_nodes.get(node_id)
        if not info or node_id == self.gate_node:
            return 0
        return info.get("processRound", 0) or 0

    def enumerate_paths(self, source, target, max_paths=3, blocked=None):
        """枚举 source→target 的前 max_paths 条最短路径（按帧数）。

        用于多维度评分：不只比帧数，综合鲜度/资源/任务选择最优路径。
        使用 Yen's 算法简化版：逐条排除已选路径的边，找次短路。
        """
        blocked = blocked or frozenset()
        paths = []
        excluded_edges = set()

        for _ in range(max_paths):
            adj = {}
            for e in self.edges:
                if (e.from_node, e.to_node) in excluded_edges:
                    continue
                base = rules.frames_on_edge(e.distance, e.route_type, rules.BASE_MOVE_NONE)
                if e.to_node not in blocked:
                    adj.setdefault(e.from_node, []).append(
                        (e.to_node, base + self._proc_cost(e.to_node)))
                if e.bidirectional and e.from_node not in blocked:
                    adj.setdefault(e.to_node, []).append(
                        (e.from_node, base + self._proc_cost(e.from_node)))
            path, cost = pathfind.shortest_path(adj, source, target)
            if not path:
                break
            if path not in paths:
                paths.append((path, cost))
            # 排除第一条边找次短路
            if len(path) >= 2:
                excluded_edges.add((path[0], path[1]))
        return paths

    def estimate_delivery_score(self, path, start_freshness=100.0, start_good_fruit=100,
                                  start_task_base=0, start_bounty=0):
        """直接预估走完此路径后的交付总分（任务书 §7.2 公式）。

        逐帧模拟鲜度损耗 + 阈值穿越检测，捕捉 NON-linear 的好果转坏效应。
        返回: (estimated_score, end_freshness, end_good_fruit, total_frames)
        """
        if not path or len(path) < 2:
            return 0, start_freshness, start_good_fruit, 0

        total_frames = 0
        freshness = start_freshness
        good_fruit = start_good_fruit

        for i in range(len(path) - 1):
            e = self.edge_between(path[i], path[i + 1])
            if not e:
                continue
            edge_frames = rules.frames_on_edge(e.distance, e.route_type,
                                               rules.BASE_MOVE_NONE)
            rate = rules.FRESHNESS_LOSS_MOVE.get(e.route_type, 0.065)
            for _ in range(edge_frames):
                before = freshness
                freshness = max(0, before - rate)
                for _ in rules.crossed_good_to_bad_thresholds(before, freshness):
                    if good_fruit > 0:
                        good_fruit -= 1
            total_frames += edge_frames
            proc = self._proc_cost(path[i + 1])
            for _ in range(proc):
                before = freshness
                freshness = max(0, before - rules.FRESHNESS_LOSS_BASE)
                for _ in rules.crossed_good_to_bad_thresholds(before, freshness):
                    if good_fruit > 0:
                        good_fruit -= 1
            total_frames += proc

        from strategy.scoring import estimate_total
        score = estimate_total(total_frames, good_fruit, freshness,
                               start_task_base, start_bounty)
        return score, freshness, good_fruit, total_frames

    def score_path(self, path, resource_nodes=None, task_nodes=None):
        """对路径打分（越低越好）。

        综合维度：
        - frames: 总帧数（权重 1.0）
        - freshness: 估算鲜度损耗（权重 1.8，1鲜度≈1.8分）
        - resources: 沿途资源点数量（奖励 -2/个）
        - tasks: 沿途任务候选点数量（奖励 -3/个）
        """
        if not path or len(path) < 2:
            return float("inf")

        # 帧数
        total_frames = 0
        for i in range(len(path) - 1):
            e = self.edge_between(path[i], path[i + 1])
            if e:
                total_frames += rules.frames_on_edge(e.distance, e.route_type,
                                                     rules.BASE_MOVE_NONE)
                total_frames += self._proc_cost(path[i + 1])

        # 鲜度损耗
        freshness_loss = 0.0
        for i in range(len(path) - 1):
            e = self.edge_between(path[i], path[i + 1])
            if e:
                frames = rules.frames_on_edge(e.distance, e.route_type,
                                              rules.BASE_MOVE_NONE)
                rate = rules.FRESHNESS_LOSS_MOVE.get(e.route_type, 0.065)
                freshness_loss += frames * rate
        # 处理帧的基础损耗
        proc_frames = sum(self._proc_cost(n) for n in path[1:])
        freshness_loss += proc_frames * rules.FRESHNESS_LOSS_BASE

        # 资源奖励
        resource_bonus = 0
        if resource_nodes:
            for n in path:
                if n in resource_nodes:
                    resource_bonus -= 2  # 有资源 -2（好于无资源）

        # 任务奖励
        task_bonus = 0
        if task_nodes:
            for n in path:
                if n in task_nodes:
                    task_bonus -= 3

        # 综合分 = 帧(权重1) + 鲜度(权重30, 1鲜度≈1.8分/0.06帧) + 资源 + 任务
        FRESHNESS_WEIGHT = 30.0
        score = total_frames + freshness_loss * FRESHNESS_WEIGHT + resource_bonus + task_bonus
        return score

    def weather_adjusted_path(self, source, target, weather_type=None,
                               base_move=None, blocked=None):
        """天气感知最短路 (path, frames)。

        根据 forecast/report 的天气类型调整边权：
        - HOT: 全图 ×1.5 鲜度 → 倾向短路径（少帧=少损耗）
        - HEAVY_RAIN: WATER 边 ×1.35 速度倍率 → WATER 边权加重
        - MOUNTAIN_FOG: MOUNTAIN 边 ×1.1 速度倍率 → MOUNTAIN 边权加重
        """
        bm = rules.BASE_MOVE_NONE if base_move is None else base_move
        blocked = blocked or frozenset()

        adj = {}
        for e in self.edges:
            weather_mult = 1000
            if weather_type == "HEAVY_RAIN" and e.route_type == "WATER":
                weather_mult = 1350
            elif weather_type == "MOUNTAIN_FOG" and e.route_type == "MOUNTAIN":
                weather_mult = 1100

            base = rules.frames_on_edge(e.distance, e.route_type, bm, weather_mult)
            if e.to_node not in blocked:
                adj.setdefault(e.from_node, []).append(
                    (e.to_node, base + self._proc_cost(e.to_node)))
            if e.bidirectional and e.from_node not in blocked:
                adj.setdefault(e.to_node, []).append(
                    (e.from_node, base + self._proc_cost(e.from_node)))
        return pathfind.shortest_path(adj, source, target)

    def route_distance(self, source, target):
        """最短路线距离（累计边 distance 之和）；用于情报/冲刺等距离口径。不可达返回 inf。"""
        _, cost = self.shortest_path(source, target, metric="distance")
        return cost

    def distance_to_gate(self, source):
        return self.route_distance(source, self.gate_node) if self.gate_node else math.inf

    # ── 必经节点（chokepoint / articulation point）──

    @property
    def chokepoints(self):
        """所有 start→terminal 路径都必须经过的节点集合。

        算法：从 terminal 反向 BFS 收缩。只沿"原始边方向"的反向追溯：
        对于每条边 from→to，将 from 视为 to 的前驱。
        若某节点的所有原始后继（from→to 中的 to）都在 choke 中，则该节点也是 choke。

        双向边的反向边（to→from）不参与前驱关系（它们代表"倒退"而非"前进"），
        避免了循环图导致的漏判。
        """
        if getattr(self, "_chokepoints", None) is not None:
            return self._chokepoints

        terms = self.terminal_nodes
        if not terms:
            self._chokepoints = set()
            return self._chokepoints

        terminal = terms[0]

        # 前驱关系只沿原始边方向：from→to 意味着 from 是 to 的前驱
        preds = {}
        succs_forward = {}
        for e in self.edges:
            preds.setdefault(e.to_node, set()).add(e.from_node)
            succs_forward.setdefault(e.from_node, set()).add(e.to_node)

        choke = {terminal}
        queue = [terminal]
        while queue:
            cur = queue.pop(0)
            for pred in preds.get(cur, set()):
                if pred in choke:
                    continue
                # pred 的所有前向后继都必须已在 choke 中
                succs = succs_forward.get(pred, set())
                if succs and succs.issubset(choke):
                    choke.add(pred)
                    queue.append(pred)

        self._chokepoints = choke
        return self._chokepoints

    def is_chokepoint(self, node_id):
        """node_id 是否为必经节点。"""
        return node_id in self.chokepoints

    def next_chokepoint(self, source):
        """从 source 出发，去 terminal 路径上的下一个必经节点。

        用于判断"对手是否即将通过关键位置"。
        """
        terminal = self.terminal_nodes[0] if self.terminal_nodes else None
        if not terminal:
            return None
        path, _ = self.time_optimal_path(source, terminal)
        if not path:
            return None
        for nid in path:
            if nid in self.chokepoints:
                return nid
        return None
