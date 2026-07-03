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
