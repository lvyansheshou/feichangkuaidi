"""锦标赛运行器。

运行我方策略 vs 多种对手的多场对局，输出统计报告。
支持随机地图生成，完整模拟多变战场环境。

用法:
    # 固定地图 vs 指定对手
    python testing/tournament.py --opponents PureRacer,AggressiveGuard --matches 10

    # 随机地图模式
    python testing/tournament.py --opponents PureRacer --matches 5 --random-maps 3

    # 所有对手大混战
    python testing/tournament.py --all-opponents --random-maps 5 --matches 4
"""

import json
import os
import sys
import time
from dataclasses import dataclass, field

# 确保项目根和 client/ 在 import 路径上
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)
sys.path.insert(0, os.path.join(_PROJECT_ROOT, "client"))

from testing.duel_mock_server import DuelMockServer, MatchResult
from testing.opponents.pure_racer import PureRacer
from testing.opponents.aggressive_guard import AggressiveGuard
from testing.opponents.task_focused import TaskFocused
from testing.opponents.balanced import Balanced
from testing.map_generator import MapGenerator
from strategy.decision import DecisionEngine, GameContext


# ── 对手注册表 ──

OPPONENT_REGISTRY = {
    "PureRacer": PureRacer,
    "AggressiveGuard": AggressiveGuard,
    "TaskFocused": TaskFocused,
    "Balanced": Balanced,
}


def load_map(map_path=None):
    """加载地图配置。"""
    if map_path:
        with open(map_path, "r", encoding="utf-8") as f:
            return json.load(f)
    # 默认使用 samples/map_config.json
    default = os.path.join(os.path.dirname(__file__), "..", "samples", "map_config.json")
    with open(default, "r", encoding="utf-8") as f:
        return json.load(f)


def _make_our_factory(map_data, player_id, team_id):
    """创建我方引擎工厂（闭包捕获 map_data）。"""
    def factory():
        return DecisionEngine(GameContext(player_id, team_id, 0, map_data))
    return factory


def _make_opp_factory(opponent_class, player_id, team_id):
    """创建对手工厂。"""
    def factory():
        opp = opponent_class(player_id, team_id)
        class OpponentAdapter:
            def __init__(self, opp):
                self.opp = opp
                self.ctx = type("Ctx", (), {"player_id": player_id, "game_map": None})()
            def decide(self, world):
                self.ctx.game_map = world.game_map
                return self.opp.decide(world)
        return OpponentAdapter(opp)
    return factory


@dataclass
class TournamentResult:
    """锦标赛统计结果。"""
    total_matches: int = 0
    our_wins: int = 0
    draws: int = 0
    our_losses: int = 0
    our_avg_score: float = 0.0
    opp_avg_score: float = 0.0
    avg_score_diff: float = 0.0
    our_delivery_rate: float = 0.0
    opp_delivery_rate: float = 0.0
    per_opponent: dict = field(default_factory=dict)
    matches: list = field(default_factory=list)


def run_tournament(opponent_names, map_data=None, matches_per_pair=10,
                   obstacle_prob=0.3, random_maps=0, map_seed=None,
                   verbose=False) -> TournamentResult:
    """运行锦标赛。

    Args:
        opponent_names: 对手名列表
        map_data: 地图配置 dict，None 则用默认地图
        matches_per_pair: 每对对手打多少场
        obstacle_prob: 障碍概率（固定地图模式下）
        random_maps: 随机地图数量（0=用固定地图）
        map_seed: 随机地图种子
        verbose: 是否打印每场结果
    """
    result = TournamentResult()

    # 准备地图池
    map_pool = []
    if random_maps > 0:
        gen = MapGenerator(map_seed)
        map_pool = gen.generate_batch(random_maps, obstacle_prob=obstacle_prob)
    else:
        if map_data is None:
            map_data = load_map()
        obstacles = [n for n in ["S06", "S08", "S10", "S11"]
                     if __import__("random").random() < obstacle_prob]
        map_pool = [{"map_data": map_data, "obstacle_nodes": obstacles, "seed": 0}]

    total_maps = len(map_pool)
    result.total_matches = len(opponent_names) * matches_per_pair * 2 * total_maps

    for opp_name in opponent_names:
        opp_class = OPPONENT_REGISTRY.get(opp_name)
        if opp_class is None:
            print(f"Unknown opponent: {opp_name}")
            continue

        opp_stats = {"wins": 0, "losses": 0, "draws": 0,
                     "our_scores": [], "opp_scores": []}

        for i in range(matches_per_pair):
            for mi, map_entry in enumerate(map_pool):
                mdata = map_entry["map_data"]
                obstacles = list(map_entry.get("obstacle_nodes", []))
                map_label = f"map{map_entry.get('seed', mi)}"

                for swap_sides in (False, True):
                    if swap_sides:
                        red_f = _make_opp_factory(opp_class, 1001, "RED")
                        blue_f = _make_our_factory(mdata, 1002, "BLUE")
                        our_side = "BLUE"
                    else:
                        red_f = _make_our_factory(mdata, 1001, "RED")
                        blue_f = _make_opp_factory(opp_class, 1002, "BLUE")
                        our_side = "RED"

                server = DuelMockServer(
                    mdata, red_f, blue_f,
                    obstacle_nodes=obstacles,
                    enable_weather=False,
                )
                match = server.run()

                our_player = [p for p in match.players
                              if ((our_side == "RED" and p["teamId"] == "RED") or
                                  (our_side == "BLUE" and p["teamId"] == "BLUE"))][0]
                opp_player = [p for p in match.players
                              if p != our_player][0]

                our_score = our_player["totalScore"]
                opp_score = opp_player["totalScore"]

                if our_score > opp_score:
                    opp_stats["wins"] += 1
                    result.our_wins += 1
                elif our_score < opp_score:
                    opp_stats["losses"] += 1
                    result.our_losses += 1
                else:
                    opp_stats["draws"] += 1
                    result.draws += 1

                opp_stats["our_scores"].append(our_score)
                opp_stats["opp_scores"].append(opp_score)
                result.our_avg_score += our_score
                result.opp_avg_score += opp_score
                result.avg_score_diff += (our_score - opp_score)

                if our_player.get("delivered"):
                    result.our_delivery_rate += 1
                if opp_player.get("delivered"):
                    result.opp_delivery_rate += 1

                result.matches.append({
                    "opponent": opp_name,
                    "map": map_label,
                    "our_side": our_side,
                    "our_score": our_score,
                    "opp_score": opp_score,
                    "our_delivered": our_player.get("delivered"),
                    "opp_delivered": opp_player.get("delivered"),
                    "over_round": match.over_round,
                })

                if verbose:
                    status = "WIN" if our_score > opp_score else ("LOSS" if our_score < opp_score else "DRAW")
                    print(f"  [{status}] vs {opp_name} ({our_side}) {map_label}: "
                          f"our={our_score} opp={opp_score} @r{match.over_round} "
                          f"deliver={our_player.get('delivered')}/{opp_player.get('delivered')}")

        result.per_opponent[opp_name] = opp_stats

    # 归一化平均值
    n = result.total_matches
    if n > 0:
        result.our_avg_score /= n
        result.opp_avg_score /= n
        result.avg_score_diff /= n
        result.our_delivery_rate = result.our_delivery_rate / n * 100
        result.opp_delivery_rate = result.opp_delivery_rate / n * 100

    return result


def print_report(result: TournamentResult):
    """打印锦标赛报告。"""
    n = result.total_matches
    wr = result.our_wins / n * 100 if n > 0 else 0

    print()
    print("═" * 60)
    print("           锦标赛报告")
    print("═" * 60)
    print(f"  总场次: {n}")
    print(f"  胜: {result.our_wins}  负: {result.our_losses}  平: {result.draws}")
    print(f"  胜率: {wr:.1f}%")
    print(f"  我方平均分: {result.our_avg_score:.0f}")
    print(f"  对手平均分: {result.opp_avg_score:.0f}")
    print(f"  平均分差: {result.avg_score_diff:+.0f}")
    print(f"  我方交付率: {result.our_delivery_rate:.0f}%")
    print(f"  对手交付率: {result.opp_delivery_rate:.0f}%")
    print()

    for opp_name, stats in result.per_opponent.items():
        total = stats["wins"] + stats["losses"] + stats["draws"]
        wr_opp = stats["wins"] / total * 100 if total > 0 else 0
        avg_our = sum(stats["our_scores"]) / len(stats["our_scores"]) if stats["our_scores"] else 0
        avg_opp = sum(stats["opp_scores"]) / len(stats["opp_scores"]) if stats["opp_scores"] else 0
        diff = avg_our - avg_opp
        print(f"  vs {opp_name:<20s}  W:{stats['wins']:>2} L:{stats['losses']:>2} "
              f"WR:{wr_opp:>5.0f}%  Δ:{diff:>+6.0f}分")
    print("═" * 60)


# ── CLI ──

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="策略锦标赛 — 多对手 × 多地图 × 红蓝互换")
    ap.add_argument("--opponents", default="PureRacer",
                    help="逗号分隔的对手名: PureRacer,AggressiveGuard,TaskFocused,Balanced")
    ap.add_argument("--all-opponents", action="store_true",
                    help="使用所有已注册对手")
    ap.add_argument("--matches", type=int, default=10,
                    help="每个对手×每张地图打多少场（红蓝互换，实际 ×2）")
    ap.add_argument("--obstacle-prob", type=float, default=0.3,
                    help="障碍候选节点生成概率")
    ap.add_argument("--random-maps", type=int, default=0,
                    help="随机地图数量（0=使用 samples/map_config.json）")
    ap.add_argument("--map-seed", type=int, default=None,
                    help="随机地图种子")
    ap.add_argument("--verbose", action="store_true", help="打印每场结果")
    ap.add_argument("--map", help="自定义地图路径（覆盖 --random-maps）")
    ap.add_argument("--list-opponents", action="store_true",
                    help="列出所有可用对手")
    args = ap.parse_args()

    if args.list_opponents:
        print("可用对手:")
        for name, cls in OPPONENT_REGISTRY.items():
            print(f"  {name:<20s} — {cls.__doc__.split(chr(10))[0].strip() if cls.__doc__ else '无描述'}")
        sys.exit(0)

    if args.all_opponents:
        opponent_names = list(OPPONENT_REGISTRY.keys())
    else:
        opponent_names = [n.strip() for n in args.opponents.split(",")]

    map_data = load_map(args.map) if args.map else None

    print(f"对手: {opponent_names}")
    print(f"地图模式: {'随机 ×' + str(args.random_maps) if args.random_maps > 0 else '固定地图'}")
    print(f"每对手×每地图场次: {args.matches} × 2(红蓝互换) = {args.matches * 2} 场")
    n_maps = max(args.random_maps, 1)
    print(f"总场次: {len(opponent_names)} × {n_maps} × {args.matches * 2} = {len(opponent_names) * n_maps * args.matches * 2}")
    print()

    result = run_tournament(
        opponent_names,
        map_data=map_data,
        matches_per_pair=args.matches,
        obstacle_prob=args.obstacle_prob,
        random_maps=args.random_maps,
        map_seed=args.map_seed,
        verbose=args.verbose,
    )

    print_report(result)
