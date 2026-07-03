"""随机地图生成器。

生成符合任务书约束的随机地图变体，用于策略鲁棒性测试。

约束（任务书 §2.2）：
- S01(5,50) 起点、S14(76,18) 宫门、S15(78,18) 终点固定
- 节点 S01-S15，类型和名称固定
- 路线类型: ROAD/WATER/MOUNTAIN/BRANCH
- 固定处理站点: S02/S04/S05/S11/S13（处理类型固定，帧数可调）
- 资源投放站点和类型参考基础地图
- 障碍候选点: S06/S08/S10/S11
"""

import copy
import json
import math
import os
import random


# ── 固定节点定义 ──

FIXED_NODES = [
    {"nodeId": "S01", "name": "岭南果园",   "type": "START",          "x": 5,  "y": 50},
    {"nodeId": "S02", "name": "南岭驿",     "type": "CHECKPOINT",     "x": 15, "y": 44},
    {"nodeId": "S03", "name": "梅关驿",     "type": "PASS",           "x": 22, "y": 38},
    {"nodeId": "S04", "name": "江南码头",   "type": "DOCK",           "x": 22, "y": 52},
    {"nodeId": "S05", "name": "洞庭水驿",   "type": "WATER_STATION",  "x": 38, "y": 48},
    {"nodeId": "S06", "name": "五岭山道",   "type": "MOUNTAIN_NODE",  "x": 14, "y": 32},
    {"nodeId": "S07", "name": "荆襄大驿",   "type": "STATION",        "x": 40, "y": 36},
    {"nodeId": "S08", "name": "秦岭栈道",   "type": "MOUNTAIN_PASS",  "x": 42, "y": 24},
    {"nodeId": "S09", "name": "洛阳驿",     "type": "STATION",        "x": 55, "y": 32},
    {"nodeId": "S10", "name": "武关",       "type": "KEY_PASS",       "x": 62, "y": 26},
    {"nodeId": "S11", "name": "潼关驿",     "type": "PASS",           "x": 66, "y": 22},
    {"nodeId": "S12", "name": "关中平原",   "type": "JUNCTION",       "x": 70, "y": 20},
    {"nodeId": "S13", "name": "灞桥驿",     "type": "PALACE_STATION", "x": 73, "y": 19},
    {"nodeId": "S14", "name": "朱雀门",     "type": "GATE",           "x": 76, "y": 18},
    {"nodeId": "S15", "name": "兴庆宫",     "type": "FINISH",         "x": 78, "y": 18},
]

# 可变节点（坐标可以调整）
VARIABLE_NODES = {"S02", "S03", "S04", "S05", "S06", "S07", "S08",
                  "S09", "S10", "S11", "S12", "S13"}
FIXED_COORD_NODES = {"S01", "S14", "S15"}

# ── 基础路线模板（连接关系固定，距离/类型可变）──

BASE_EDGES = [
    # 官道主线
    ("S01", "S02", "ROAD"),
    ("S02", "S03", "ROAD"),
    ("S03", "S07", "ROAD"),
    ("S07", "S09", "ROAD"),
    ("S09", "S10", "ROAD"),
    ("S10", "S11", "ROAD"),
    ("S11", "S12", "ROAD"),
    ("S12", "S13", "ROAD"),
    ("S13", "S14", "ROAD"),
    ("S14", "S15", "ROAD"),
    # 水路
    ("S02", "S04", "ROAD"),
    ("S04", "S05", "WATER"),
    ("S05", "S07", "BRANCH"),
    # 山路捷径
    ("S01", "S06", "MOUNTAIN"),
    ("S06", "S08", "MOUNTAIN"),
    ("S08", "S10", "BRANCH"),
    # 连接支线
    ("S03", "S06", "BRANCH"),
    ("S05", "S09", "WATER"),
    ("S07", "S08", "MOUNTAIN"),
    ("S04", "S07", "BRANCH"),
    ("S08", "S09", "BRANCH"),
]

# ── 固定处理站点 ──

BASE_PROCESS_NODES = [
    {"nodeId": "S02", "nodeName": "南岭驿",   "processName": "驿站换乘", "processRound": 4},
    {"nodeId": "S04", "nodeName": "江南码头", "processName": "码头登船", "processRound": 7},
    {"nodeId": "S05", "nodeName": "洞庭水驿", "processName": "水驿换乘", "processRound": 6},
    {"nodeId": "S11", "nodeName": "潼关驿",   "processName": "关驿通行", "processRound": 5},
    {"nodeId": "S13", "nodeName": "灞桥驿",   "processName": "宫前换乘", "processRound": 5},
    {"nodeId": "S14", "nodeName": "朱雀门",   "processName": "宫门验核", "processRound": 6},
]

# ── 资源模板 ──

BASE_RESOURCES = [
    {"nodeId": "S03", "resourceType": "ICE_BOX",       "resourceName": "冰鉴"},
    {"nodeId": "S03", "resourceType": "PASS_TOKEN",     "resourceName": "过关凭证"},
    {"nodeId": "S03", "resourceType": "INTEL",          "resourceName": "情报"},
    {"nodeId": "S04", "resourceType": "SHORT_HORSE",    "resourceName": "短程驿马"},
    {"nodeId": "S04", "resourceType": "BOAT_RIGHT",     "resourceName": "船权"},
    {"nodeId": "S04", "resourceType": "INTEL",          "resourceName": "情报"},
    {"nodeId": "S06", "resourceType": "ICE_BOX",        "resourceName": "冰鉴"},
    {"nodeId": "S06", "resourceType": "INTEL",          "resourceName": "情报"},
    {"nodeId": "S08", "resourceType": "SHORT_HORSE",    "resourceName": "短程驿马"},
    {"nodeId": "S08", "resourceType": "PASS_TOKEN",     "resourceName": "过关凭证"},
    {"nodeId": "S08", "resourceType": "INTEL",          "resourceName": "情报"},
    {"nodeId": "S07", "resourceType": "ICE_BOX",        "resourceName": "冰鉴"},
    {"nodeId": "S07", "resourceType": "SHORT_HORSE",    "resourceName": "短程驿马"},
    {"nodeId": "S09", "resourceType": "FAST_HORSE",     "resourceName": "快马"},
    {"nodeId": "S09", "resourceType": "OFFICIAL_PERMIT", "resourceName": "官凭"},
    {"nodeId": "S10", "resourceType": "INTEL",          "resourceName": "情报"},
    {"nodeId": "S11", "resourceType": "INTEL",          "resourceName": "情报"},
    {"nodeId": "S13", "resourceType": "PASS_TOKEN",     "resourceName": "过关凭证"},
    {"nodeId": "S13", "resourceType": "OFFICIAL_PERMIT", "resourceName": "官凭"},
    {"nodeId": "S13", "resourceType": "INTEL",          "resourceName": "情报"},
]

# ── 任务模板 ──

TASK_TEMPLATES = [
    {"taskTemplateId": "T01", "name": "限时过关", "baseScore": 30, "processRound": 3,
     "candidateNodes": ["S03"]},
    {"taskTemplateId": "T02", "name": "抵驿催运", "baseScore": 30, "processRound": 4,
     "candidateNodes": ["S07", "S10"]},
    {"taskTemplateId": "T04", "name": "清障任务", "baseScore": 30, "processRound": 6,
     "candidateNodes": ["S06", "S08"]},
    {"taskTemplateId": "T06", "name": "争马换乘", "baseScore": 30, "processRound": 3,
     "candidateNodes": ["S09", "S04", "S06"]},
    {"taskTemplateId": "T08", "name": "码头争船", "baseScore": 30, "processRound": 4,
     "candidateNodes": ["S04", "S05"]},
    {"taskTemplateId": "T11", "name": "栈道复核", "baseScore": 30, "processRound": 4,
     "candidateNodes": ["S08", "S10", "S11"]},
    {"taskTemplateId": "T12", "name": "官道关验", "baseScore": 15, "processRound": 5,
     "candidateNodes": ["S11", "S13"]},
    {"taskTemplateId": "T13", "name": "水陆联运", "baseScore": 15, "processRound": 5,
     "candidateNodes": ["S13", "S09", "S12"]},
    {"taskTemplateId": "T14", "name": "山口急递", "baseScore": 15, "processRound": 5,
     "candidateNodes": ["S10", "S11", "S12"]},
]

# ── 障碍候选 ──

OBSTACLE_CANDIDATES = ["S06", "S08", "S10", "S11"]

# ── 路线类型距离范围 ──

ROUTE_DISTANCE_RANGES = {
    "ROAD":     (15, 55),
    "WATER":    (30, 55),
    "MOUNTAIN": (35, 60),
    "BRANCH":   (30, 65),
}


class MapGenerator:
    """随机地图生成器。

    用法:
        gen = MapGenerator(seed=42)
        map_data = gen.generate(
            jitter_coords=True,      # S02-S13 坐标随机偏移
            jitter_distances=True,   # 路线距离随机变化
            shuffle_resources=True,  # 资源随机增减
            obstacle_prob=0.3,       # 障碍生成概率
        )
    """

    def __init__(self, seed=None):
        self.rng = random.Random(seed)

    def generate(self, jitter_coords=True, jitter_distances=True,
                 shuffle_resources=True, obstacle_prob=0.3):
        """生成完整地图配置。"""
        nodes = copy.deepcopy(FIXED_NODES)

        # 1. 坐标扰动
        if jitter_coords:
            for n in nodes:
                if n["nodeId"] in VARIABLE_NODES:
                    n["x"] += self.rng.randint(-3, 3)
                    n["y"] += self.rng.randint(-3, 3)
                    n["x"] = max(1, min(79, n["x"]))
                    n["y"] = max(1, min(59, n["y"]))

        # 2. 路线边
        edges = []
        for i, (frm, to, rtype) in enumerate(BASE_EDGES):
            edge = {
                "edgeId": "E%02d" % (i + 1),
                "fromNodeId": frm,
                "toNodeId": to,
                "routeType": rtype,
                "bidirectional": True,
            }
            if jitter_distances:
                lo, hi = ROUTE_DISTANCE_RANGES.get(rtype, (20, 50))
                edge["distance"] = self.rng.randint(lo, hi)
            else:
                edge["distance"] = 30
            edges.append(edge)

        # 3. 固定处理
        process_nodes = copy.deepcopy(BASE_PROCESS_NODES)
        if jitter_distances:
            for p in process_nodes:
                delta = self.rng.randint(-1, 2)
                p["processRound"] = max(2, p["processRound"] + delta)

        # 4. 资源
        resources = copy.deepcopy(BASE_RESOURCES)
        if shuffle_resources:
            n = len(resources)
            keep = max(n // 2, self.rng.randint(n // 2, n))
            resources = self.rng.sample(resources, keep)

        # 5. 障碍
        obstacle_nodes = [n for n in OBSTACLE_CANDIDATES
                          if self.rng.random() < obstacle_prob]

        # 6. 任务模板
        task_templates = copy.deepcopy(TASK_TEMPLATES)

        # 7. 组装 standard map_config 格式
        gameplay = {
            "roles": {
                "startNodeId": "S01",
                "gateNodeId": "S14",
                "terminalNodeIds": ["S15"],
                "safeZoneNodeIds": ["S15"],
            },
            "processNodes": [
                {"nodeId": p["nodeId"], "processType": p["processName"],
                 "processRound": p["processRound"]}
                for p in process_nodes
            ],
        }

        map_data = {
            "mapName": "随机生成地图",
            "map": {
                "maxX": 80, "maxY": 60,
                "gameplay": gameplay,
            },
            "nodes": nodes,
            "edges": edges,
            "processNodes": process_nodes,
            "visibleResources": resources,
            "taskTemplates": task_templates,
            "safeZones": [{"nodeId": "S15", "name": "兴庆宫", "type": "FINISH"}],
            "reverifyNode": {"nodeId": "S14", "name": "朱雀门", "type": "GATE"},
            "durationRound": 600,
        }

        return map_data, obstacle_nodes

    def generate_batch(self, n, **kwargs):
        """批量生成 n 个地图。"""
        maps = []
        for i in range(n):
            seed = self.rng.randint(0, 99999)
            gen = MapGenerator(seed)
            m, obs = gen.generate(**kwargs)
            maps.append({"map_data": m, "obstacle_nodes": obs, "seed": seed})
        return maps


# ── CLI 测试 ──

if __name__ == "__main__":
    gen = MapGenerator(42)
    map_data, obstacles = gen.generate(
        jitter_coords=True,
        jitter_distances=True,
        shuffle_resources=True,
        obstacle_prob=0.4,
    )
    print(f"障碍节点: {obstacles}")
    print(f"节点数: {len(map_data['nodes'])}")
    print(f"边数: {len(map_data['edges'])}")
    print(f"资源数: {len(map_data['visibleResources'])}")
    print(f"处理站点: {len(map_data['processNodes'])}")

    # 打印边信息
    print("\n边列表:")
    for e in map_data["edges"]:
        print(f"  {e['edgeId']}: {e['fromNodeId']}→{e['toNodeId']} "
              f"{e['routeType']} d={e['distance']}")

    # 保存
    out_path = os.path.join(os.path.dirname(__file__), "..", "samples", "random_map.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(map_data, f, ensure_ascii=False, indent=2)
    print(f"\n已保存到 {out_path}")
