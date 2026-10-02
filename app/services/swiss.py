"""瑞士轮配对核心算法。

规则（对应需求）：
1. 按当前积分分组，优先在同组内配对；
2. 在「禁止重复交手」前提下，全局最小化跨分组积分差之和；
3. 存在多个最优方案时，按参赛者编号固定排序（取字典序最小的配对集合）。

实现方式：在不含禁忌边的完全图上求最小权完美匹配
（Edmonds 带花树算法，由 networkx 提供，O(n^3)）。
边权 = |积分差|（同组为 0，积分以「半分」为单位的整数）；
同代价方案通过可加的整数次级权重保证取字典序最小者。
"""
from dataclasses import dataclass

import networkx as nx

# 得分（半分单位）：胜 2、和 1、负 0
WIN = 2
DRAW = 1
LOSS = 0


@dataclass(frozen=True)
class PairingResult:
    # 元素为 (小白号, 小黑号)，小白号 < 小黑号，整体按小白号升序
    pairs: tuple[tuple[int, int], ...]
    # 跨分组积分差之和（半分单位，2 表示 1 分差）
    cost: int
    bye_player: int | None = None


def swiss_pair(
    players: list[int],
    points: dict[int, int],
    forbidden: set[tuple[int, int]],
    bye_player: int | None = None,
) -> PairingResult:
    """生成一组配对。

    参数
    ----
    players:
        本轮需要配对的选手编号（奇数人轮空应先由调用方决定并剔除）。
    points:
        选手编号 -> 当前积分（半分单位整数）。
    forbidden:
        禁止重复交手的无序编号对 (a, b)，a < b。
    bye_player:
        仅用于回填结果，不参与匹配。
    """
    ids = sorted(p for p in players if p != bye_player)
    n = len(ids)
    if n % 2 != 0:
        raise ValueError("去掉轮空选手后人数必须为偶数")
    if n == 0:
        return PairingResult(pairs=(), cost=0, bye_player=bye_player)

    # 编号 -> 名次序（1..n，编号最小者为 1），用于字典序微扰
    rank = {pid: i + 1 for i, pid in enumerate(ids)}

    # 次级权重的进制：M - 1 > n 时，靠前配对的微扰位严格压过
    # 所有后续配对微扰之和，从而精确编码「配对元组的字典序」。
    M = 2 * n + 10
    SCALE = M**n

    G = nx.Graph()
    G.add_nodes_from(ids)
    edge_cost: dict[frozenset[int], int] = {}
    for i in range(n):
        for j in range(i + 1, n):
            a, b = ids[i], ids[j]
            if (a, b) in forbidden:
                continue  # 禁忌边不进图
            primary = 0 if points[a] == points[b] else abs(points[a] - points[b])
            # 次级：编号越小的一方，其搭档编号的权重位越高
            secondary = M ** (n - rank[a]) * rank[b]
            w = primary * SCALE + secondary
            G.add_edge(a, b, weight=w)
            edge_cost[frozenset((a, b))] = primary

    # min_weight_matching 返回最大基数匹配中的最小权匹配；
    # 当图存在完美匹配时即为最小权完美匹配。
    matching = nx.min_weight_matching(G, weight="weight")
    if len(matching) != n // 2:
        raise RuntimeError("不存在满足约束的配对方案")

    pairs = tuple(sorted((min(u, v), max(u, v)) for u, v in matching))
    cost = sum(edge_cost[frozenset(p)] for p in pairs)
    return PairingResult(pairs=pairs, cost=cost, bye_player=bye_player)
