"""工位求解器测试：与暴力枚举逐一对比。

验证 solve_assignments 的三级目标被精确实现：
1. 最大化已分配对局数；
2. 最小化选手相对上一轮的换位次数；
3. 按 (match_id, station_no) 升序序列的字典序固定裁决。

暴力枚举所有「对局 -> 工位」的单射部分映射作为参照。
"""
import random

from app.services.station_plans import _StationInfo, solve_assignments


def brute_force_best(
    match_ids,
    required_by_match,
    players_by_match,
    stations,
    prev_station_by_player,
):
    """枚举全部分配，返回 (match_id -> station_id) 的最优解。

    最优次序：(-已分配数, 换位数, (match_id, station_no) 升序序列)
    字典序最小者。
    """
    best_key = None
    best = None
    # 每个对局：不分配，或分配到某个兼容工位
    choices = []
    for mid in match_ids:
        options = [None]
        for st in stations:
            if required_by_match[mid] <= st.tags:
                options.append(st)
        choices.append(options)

    def moves_of(mid, station_no):
        w, b = players_by_match[mid]
        pw = prev_station_by_player.get(w)
        pb = prev_station_by_player.get(b)
        return int(pw is not None and pw != station_no) + int(
            pb is not None and pb != station_no
        )

    def recurse(i, used_station_ids, assignment):
        nonlocal best_key, best
        if i == len(match_ids):
            pairs = tuple(
                sorted(
                    (mid, st.station_no)
                    for mid, st in assignment.items()
                )
            )
            moves = sum(moves_of(mid, no) for mid, no in pairs)
            key = (-len(pairs), moves, pairs)
            if best_key is None or key < best_key:
                best_key = key
                best = {mid: st.id for mid, st in assignment.items()}
            return
        mid = match_ids[i]
        for option in choices[i]:
            if option is None:
                recurse(i + 1, used_station_ids, assignment)
            elif option.id not in used_station_ids:
                assignment[mid] = option
                recurse(i + 1, used_station_ids | {option.id}, assignment)
                del assignment[mid]

    recurse(0, set(), {})
    # match_id -> station_id
    return best or {}


def random_case(rng):
    k = rng.randint(1, 5)
    n = rng.randint(1, 5)
    tag_pool = ["a", "b", "c"]
    match_ids = [10 * (i + 1) for i in range(k)]  # 乱序 id 更贴近真实
    players = list(range(1, 2 * k + 1))
    rng.shuffle(players)
    players_by_match = {
        mid: (players[2 * i], players[2 * i + 1])
        for i, mid in enumerate(match_ids)
    }
    required_by_match = {}
    for mid in match_ids:
        tags = {t for t in tag_pool if rng.random() < 0.3}
        required_by_match[mid] = frozenset(tags)
    stations = [
        _StationInfo(
            id=100 + j,
            station_no=j + 1,
            tags=frozenset(t for t in tag_pool if rng.random() < 0.5),
        )
        for j in range(n)
    ]
    prev_station_by_player = {}
    for pid in players:
        if rng.random() < 0.7:
            prev_station_by_player[pid] = rng.randint(1, n)
    return (
        match_ids,
        required_by_match,
        players_by_match,
        stations,
        prev_station_by_player,
    )


def test_solver_matches_brute_force_on_random_cases():
    rng = random.Random(20260926)
    for case_no in range(300):
        args = random_case(rng)
        expected = brute_force_best(*args)
        got = solve_assignments(*args)
        assert got == expected, f"case {case_no}: {args}"


def test_solver_maximizes_assignment_count_first():
    # 两个对局争一个工位：只能分一个，且字典序下小编号场优先
    stations = [_StationInfo(id=1, station_no=1, tags=frozenset())]
    got = solve_assignments(
        [7, 3],
        {7: frozenset(), 3: frozenset()},
        {7: (1, 2), 3: (3, 4)},
        stations,
        {},
    )
    assert got == {3: 1}


def test_solver_prefers_fewer_moves_over_lex_order():
    # 纯字典序会给 m1->1（2 人次换位）；换位最少的是 m1->2（0 人次）
    stations = [
        _StationInfo(id=1, station_no=1, tags=frozenset()),
        _StationInfo(id=2, station_no=2, tags=frozenset()),
    ]
    got = solve_assignments(
        [1],
        {1: frozenset()},
        {1: (11, 12)},
        stations,
        {11: 2, 12: 2},
    )
    assert got == {1: 2}


def test_solver_respects_tag_coverage():
    stations = [
        _StationInfo(id=1, station_no=1, tags=frozenset({"x"})),
        _StationInfo(id=2, station_no=2, tags=frozenset()),
    ]
    got = solve_assignments(
        [1, 2],
        {1: frozenset({"x"}), 2: frozenset()},
        {1: (1, 2), 2: (3, 4)},
        stations,
        {},
    )
    # m1 只能用工位 1；m2 用工位 2
    assert got == {1: 1, 2: 2}
