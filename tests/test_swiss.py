"""核心配对算法测试：与暴力枚举的所有完美匹配逐一对比。"""
import random
from itertools import combinations, permutations

import pytest

from app.services.swiss import swiss_pair


def brute_force_best(players, points, forbidden):
    """枚举全部完美匹配，返回 (最小代价, 字典序最小配对元组)。"""
    ids = sorted(players)
    best = None

    def recurse(remaining, acc, pairs):
        nonlocal best
        if not remaining:
            cand = tuple(sorted(pairs))
            if best is None or (acc, cand) < best:
                best = (acc, cand)
            return
        a = remaining[0]
        for b in remaining[1:]:
            if (min(a, b), max(a, b)) in forbidden:
                continue
            edge = 0 if points[a] == points[b] else abs(points[a] - points[b])
            recurse(
                [x for x in remaining if x not in (a, b)],
                acc + edge,
                pairs + [(a, b)],
            )

    recurse(ids, 0, [])
    if best is None:
        return None
    return best


def test_first_round_even_pairing_is_lexicographic():
    # 首轮全部同分：代价全为 0，必须取编号字典序最小方案 (1,2)(3,4)
    res = swiss_pair([1, 2, 3, 4], {p: 0 for p in range(1, 5)}, set())
    assert res.pairs == ((1, 2), (3, 4))
    assert res.cost == 0


def test_first_round_six_players():
    res = swiss_pair([1, 2, 3, 4, 5, 6], {p: 0 for p in range(1, 7)}, set())
    assert res.pairs == ((1, 2), (3, 4), (5, 6))


def test_cross_group_minimization():
    # 分组：[1,2]=4分，[3,4]=2分，[5,6]=0分；4分组内部禁忌。
    # 最优：一条跨组链 (4->2) + (2->0)，组内 (5,6)，总代价 4 个半分（2 分差）。
    points = {1: 4, 2: 4, 3: 2, 4: 2, 5: 0, 6: 0}
    forbidden = {(1, 2)}
    res = swiss_pair([1, 2, 3, 4, 5, 6], points, forbidden)
    assert res.cost == 4
    assert res.pairs == ((1, 3), (2, 4), (5, 6))
    cross = [(a, b) for a, b in res.pairs if points[a] != points[b]]
    assert sorted(points[a] - points[b] for a, b in cross) == [2, 2]


def test_lex_tiebreak_among_optima():
    # 两组各 2 人，组内互相禁忌，必须全部跨组；
    # 多个代价相同方案，选字典序最小
    points = {1: 2, 2: 2, 3: 0, 4: 0}
    forbidden = {(1, 2), (3, 4)}
    res = swiss_pair([1, 2, 3, 4], points, forbidden)
    assert res.pairs == ((1, 3), (2, 4))
    assert res.cost == 4


def test_no_feasible_matching_raises():
    # 4 人中选手 1 与所有人都交手过 -> 无可行匹配
    points = {1: 0, 2: 0, 3: 0, 4: 0}
    forbidden = {(1, 2), (1, 3), (1, 4)}
    with pytest.raises(RuntimeError):
        swiss_pair([1, 2, 3, 4], points, forbidden)


def test_odd_size_after_bye_removed():
    with pytest.raises(ValueError):
        swiss_pair([1, 2, 3], {1: 0, 2: 0, 3: 0}, set())


def test_randomized_against_brute_force():
    rng = random.Random(20260920)
    for n in (4, 6, 8):
        for _ in range(150):
            players = list(range(1, n + 1))
            # 积分取少数几个离散值，制造分组
            points = {p: rng.choice([0, 2, 4, 6]) for p in players}
            all_pairs = list(combinations(players, 2))
            k = rng.randint(0, n)
            forbidden = set(rng.sample(all_pairs, k))

            expected = brute_force_best(players, points, forbidden)
            if expected is None:
                with pytest.raises(RuntimeError):
                    swiss_pair(players, points, forbidden)
                continue

            res = swiss_pair(players, points, forbidden)
            assert (res.cost, res.pairs) == expected, (
                f"n={n} points={points} forbidden={sorted(forbidden)} "
                f"got={(res.cost, res.pairs)} want={expected}"
            )


def test_lex_order_independent_of_input_order():
    points = {7: 2, 3: 2, 5: 0, 1: 0}
    forbidden = {(3, 7), (1, 5)}
    order_a = [1, 3, 5, 7]
    order_b = [7, 5, 3, 1]
    ra = swiss_pair(order_a, points, forbidden)
    rb = swiss_pair(order_b, points, forbidden)
    assert ra.pairs == rb.pairs == ((1, 3), (5, 7))


def _sequential_lex_optimum(players, points, forbidden):
    """独立参照：用「序贯固定搭档 + 最优代价可行性」逐位确定字典序
    最小的最优完美匹配，只使用主代价（积分差）。无可行解时返回 None。"""
    import networkx as nx

    def min_cost_on(vertices):
        if not vertices:
            return 0
        g = nx.Graph()
        g.add_nodes_from(vertices)
        for i, a in enumerate(vertices):
            for b in vertices[i + 1:]:
                if (min(a, b), max(a, b)) in forbidden:
                    continue
                g.add_edge(
                    a, b,
                    weight=0 if points[a] == points[b] else abs(points[a] - points[b]),
                )
        matching = nx.min_weight_matching(g, weight="weight")
        if len(matching) != len(vertices) // 2:
            return None
        return sum(
            0 if points[u] == points[v] else abs(points[u] - points[v])
            for u, v in matching
        )

    remaining = sorted(players)
    target = min_cost_on(remaining)
    if target is None:
        return None
    pairs = []
    spent = 0
    while remaining:
        a = remaining[0]
        chosen = None
        for b in remaining[1:]:
            if (min(a, b), max(a, b)) in forbidden:
                continue
            edge = 0 if points[a] == points[b] else abs(points[a] - points[b])
            rest = [x for x in remaining if x not in (a, b)]
            rc = min_cost_on(rest)
            if rc is not None and spent + edge + rc == target:
                chosen = (a, b)
                spent += edge
                remaining = rest
                break
        if chosen is None:
            return None
        pairs.append(chosen)
    return target, tuple(pairs)


def test_lex_perturbation_matches_sequential_optimum():
    rng = random.Random(424242)
    for n in (6, 8, 10, 12):
        for _ in range(60):
            players = list(range(1, n + 1))
            points = {p: rng.choice([0, 2, 4, 6]) for p in players}
            all_pairs = list(combinations(players, 2))
            forbidden = set(rng.sample(all_pairs, rng.randint(0, n)))

            expected = _sequential_lex_optimum(players, points, forbidden)
            if expected is None:
                with pytest.raises(RuntimeError):
                    swiss_pair(players, points, forbidden)
                continue
            res = swiss_pair(players, points, forbidden)
            assert (res.cost, res.pairs) == expected, (
                f"n={n} points={points} forbidden={sorted(forbidden)} "
                f"got={(res.cost, res.pairs)} want={expected}"
            )
