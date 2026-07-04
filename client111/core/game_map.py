"""静态地图镜像 GameMap（由 start 消息构建）。

从 start.msg_data 的顶层 nodes[]/edges[] 构建；优先读取顶层字段，
回退到 map.gameplay.roles。提供寻路、距离查询、必经节点检测等。

方向规则（任务书 §2.3.3）：每条边 fromNode→toNode 恒可达；
bidirectional 时 toNode→fromNode 也可达。bidirectional 缺省视为 True。
"""

import math
from dataclasses import dataclass

from core import pathfind, rules


@dataclass
class Node:
    node_id: str
    name: str = ""
    type: str = "STATION"
    x: int = 0
    y: int = 0
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
        raw_nodes = (start_data.get("nodes")
                     or (start_data.get("map", {}) or {}).get("nodes")
                     or [])
        raw_edges = (start_data.get("edges")
                     or (start_data.get("map", {}) or {}).get("edges")
                     or [])

        # 节点
        self.nodes = {}
        for n in raw_nodes:
            node = Node(
                node_id=n.get("nodeId"),
                name=n.get("name", ""),
                type=n.get("type") or n.get("nodeType", "STATION"),
                x=n.get("x", 0),
                y=n.get("y", 0),
                is_start=bool(n.get("start")) or (n.get("type") or n.get("nodeType")) == "START",
                is_terminal=bool(n.get("terminal")) or (n.get("type") or n.get("nodeType")) in ("FINISH", "TERMINAL"),
            )
            if node.node_id:
                self.nodes[node.node_id] = node

        # 边 & 邻接表
        self.edges = []
        self._adj_move = {}   # node -> [(neighbor, move_amount)]
        self._adj_dist = {}   # node -> [(neighbor, distance)]
        for i, e in enumerate(raw_edges):
            frm = e.get("fromNodeId") or e.get("fromNode")
            to = e.get("toNodeId") or e.get("toNode")
            if not frm or not to:
                continue
            rt = e.get("routeType", "ROAD")
            dist = e.get("distance", 0)
            bidir = e.get("bidirectional", True)
            edge = Edge(
                edge_id=e.get("edgeId", "E%02d" % (i + 1)),
                from_node=frm, to_node=to,
                route_type=rt, distance=dist, bidirectional=bidir,
            )
            self.edges.append(edge)
            move_w = rules.to_station_move_amount(dist, rt)
            self._add_adj(frm, to, move_w, dist)
            if bidir:
                self._add_adj(to, frm, move_w, dist)

        # 角色（起点/终点/宫门）
        self.roles = self._parse_roles(start_data)

        # 处理站点 {node_id: {processType, processRound}}
        self.process_nodes = self._parse_process_nodes(start_data)

        # 必经节点缓存
        self._chokepoints_cache = None

    def _add_adj(self, a, b, move_w, dist_w):
        self._adj_move.setdefault(a, []).append((b, move_w))
        self._adj_dist.setdefault(a, []).append((b, dist_w))

    def _parse_roles(self, start_data):
        gp = (start_data.get("map", {}) or {}).get("gameplay", {}) or {}
        roles = dict(gp.get("roles") or {})
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
        if not roles.get("safeZoneNodeIds"):
            sz = [z.get("nodeId") for z in (start_data.get("safeZones") or [])]
            if sz:
                roles["safeZoneNodeIds"] = sz
        return roles

    def _parse_process_nodes(self, start_data):
        result = {}
        gp = (start_data.get("map", {}) or {}).get("gameplay", {}) or {}
        for p in gp.get("processNodes", []) or []:
            nid = p.get("nodeId")
            if nid:
                result[nid] = {
                    "processType": p.get("processType"),
                    "processRound": p.get("processRound", 0) or 0,
                }
        for p in start_data.get("processNodes", []) or []:
            nid = p.get("nodeId")
            if nid and nid not in result:
                result[nid] = {
                    "processType": p.get("processType"),
                    "processRound": p.get("processRound", 0) or 0,
                }
        return result

    # ---- 属性 ----

    @property
    def start_node(self):
        return self.roles.get("startNodeId")

    @property
    def gate_node(self):
        return self.roles.get("gateNodeId")

    @property
    def terminal_nodes(self):
        return self.roles.get("terminalNodeIds") or []

    @property
    def safe_zone_nodes(self):
        return self.roles.get("safeZoneNodeIds") or []

    def node(self, node_id):
        return self.nodes.get(node_id)

    def neighbors(self, node_id):
        return [nb for nb, _ in self._adj_move.get(node_id, ())]

    def edge_between(self, a, b):
        for e in self.edges:
            if e.from_node == a and e.to_node == b:
                return e
            if e.bidirectional and e.from_node == b and e.to_node == a:
                return e
        return None

    # ---- 寻路 ----

    def shortest_path(self, source, target, metric="move"):
        """最短路 (path, cost)。metric='move' 按到站移动量；'distance' 按路线距离。"""
        adj = self._adj_move if metric == "move" else self._adj_dist
        return pathfind.shortest_path(adj, source, target)

    def time_optimal_path(self, source, target, blocked=None):
        """按帧数的最短路 (path, frames)。

        边权 = 单边到站帧数 + 目标节点固定处理耗时。
        blocked: 不可进入的节点集合（障碍/敌方设卡）。
        """
        blocked = blocked or frozenset()
        adj = {}
        for e in self.edges:
            base = rules.frames_on_edge(e.distance, e.route_type, rules.BASE_MOVE_NONE)
            if e.to_node not in blocked:
                adj.setdefault(e.from_node, []).append(
                    (e.to_node, base + self._proc_cost(e.to_node)))
            if e.bidirectional and e.from_node not in blocked:
                adj.setdefault(e.to_node, []).append(
                    (e.from_node, base + self._proc_cost(e.from_node)))
        return pathfind.shortest_path(adj, source, target)

    def _proc_cost(self, node_id):
        """返回节点固定处理帧数（宫门不计入路线差异）。"""
        if node_id == self.gate_node:
            return 0
        info = self.process_nodes.get(node_id)
        return (info or {}).get("processRound", 0) or 0 if info else 0

    def route_distance(self, source, target):
        """最短路线距离（累计边 distance 之和）。"""
        _, cost = self.shortest_path(source, target, metric="distance")
        return cost

    def weather_adjusted_path(self, source, target, weather_type=None,
                               base_move=None, blocked=None):
        """天气感知最短路 (path, frames)。

        - HOT: 倾向短路径（少帧=少损耗）
        - HEAVY_RAIN: WATER 边权加重
        - MOUNTAIN_FOG: MOUNTAIN 边权加重
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

    def freshness_optimal_path(self, source, target, blocked=None,
                                freshness_weight=1.0):
        """鲜度+时间平衡最短路径 (path, frames)。

        边权 = 帧数 + freshness_weight × 预估鲜度损耗帧当量。
        freshness_weight 越大越倾向于低鲜度损耗路线（水路 > 官道 > 山路）。

        鲜度损耗帧当量 = 帧数 × 路线损耗率 / 基准损耗率
        基准取 ROAD=0.055，即 ROAD 边无惩罚，MOUNTAIN 边受惩罚。
        """
        blocked = blocked or frozenset()
        BASE_LOSS = 0.055  # ROAD 基准
        ROUTE_LOSS = {"ROAD": 0.055, "WATER": 0.045, "MOUNTAIN": 0.07, "BRANCH": 0.065}

        adj = {}
        for e in self.edges:
            frames = rules.frames_on_edge(e.distance, e.route_type,
                                          rules.BASE_MOVE_NONE)
            proc = self._proc_cost(e.to_node)

            # 鲜度调整：高于基准的路线类型加惩罚
            loss_rate = ROUTE_LOSS.get(e.route_type, 0.06)
            freshness_penalty = frames * max(0, (loss_rate - BASE_LOSS) / BASE_LOSS)
            weight = frames + proc + freshness_weight * freshness_penalty

            if e.to_node not in blocked:
                adj.setdefault(e.from_node, []).append((e.to_node, weight))
            if e.bidirectional and e.from_node not in blocked:
                adj.setdefault(e.to_node, []).append((e.from_node, weight))

        path, cost = pathfind.shortest_path(adj, source, target)
        if path:
            # 返回真实帧数（非加权后）
            _, real_frames = self.time_optimal_path(source, target, blocked)
            return path, real_frames
        return path, cost

    # ---- 必经节点 (chokepoints) ----

    @property
    def chokepoints(self):
        """所有 start→terminal 路径都必须经过的节点集合。

        算法：从 terminal 反向 BFS 收缩，只沿原始边方向（from→to）。
        """
        if self._chokepoints_cache is not None:
            return self._chokepoints_cache

        terms = self.terminal_nodes
        if not terms:
            self._chokepoints_cache = set()
            return self._chokepoints_cache

        terminal = terms[0]
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
                succs = succs_forward.get(pred, set())
                if succs and succs.issubset(choke):
                    choke.add(pred)
                    queue.append(pred)

        self._chokepoints_cache = choke
        return self._chokepoints_cache

    def is_chokepoint(self, node_id):
        return node_id in self.chokepoints
