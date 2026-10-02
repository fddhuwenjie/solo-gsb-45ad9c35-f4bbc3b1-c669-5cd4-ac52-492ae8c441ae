"""轮次工位方案：求解、落库与事件。

求解目标（按优先级依次满足）：
1. 最大化已分配对局数——每场非轮空对局至多占一个可用工位，
   每个工位至多承接一场对局，工位能力标签必须覆盖双方选手
   全部必需标签的并集；
2. 最小化选手相对上一轮最新方案的换位次数——仅统计两轮都
   被分配且工位编号不同的选手（上一轮未分配或本轮未分配的
   选手不计入换位）；
3. 按 (match_id, station_no) 升序固定裁决——把分配表示为按
   match_id 升序的 (match_id, station_no) 元组序列，取字典序
   最小者（等价于：小编号的场优先占用小编号的可行工位）。

实现：在「对局 × 可用工位」二分图上求最大权匹配
（networkx.max_weight_matching）。三级目标用大整数权重精确
编码（与 swiss.py 同一思路）：权值唯一确定
（分配数, 换位数, 逐场工位选择），最优解唯一，与算法内部
顺序无关。

注意：create_station_plan 只在调用方事务内写入并 flush，不提交；
方案行与 station_plan.published 事件由路由层在同一事务中统一
提交。固定工位（pins）在求解前校验：重复或不兼容直接 409，
不落任何数据。
"""
from dataclasses import dataclass

import networkx as nx
from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..events.service import STATION_PLAN_PUBLISHED, append_event
from ..models import (
    Event,
    Match,
    Participant,
    Round,
    Station,
    StationPlan,
    StationPlanAssignment,
)


@dataclass(frozen=True)
class _StationInfo:
    id: int
    station_no: int
    tags: frozenset[str]


def solve_assignments(
    match_ids: list[int],
    required_by_match: dict[int, frozenset[str]],
    players_by_match: dict[int, tuple[int, int]],
    stations: list[_StationInfo],
    prev_station_by_player: dict[int, int],
) -> dict[int, int]:
    """求最优工位分配，返回 {match_id: station_id}。

    参数
    ----
    match_ids:
        待分配的非轮空对局 id（升序）。
    required_by_match:
        对局 id -> 双方选手必需标签的并集。
    players_by_match:
        对局 id -> (白方选手 id, 黑方选手 id)。
    stations:
        可用工位（按 station_no 升序）。
    prev_station_by_player:
        上一轮最新方案中选手 -> 工位编号（无方案时为空）。
    """
    k = len(match_ids)
    n = len(stations)
    if k == 0 or n == 0:
        return {}

    # 名次序决定字典序裁决与权重进制，统一在此排序，不依赖调用方
    match_ids = sorted(match_ids)
    stations = sorted(stations, key=lambda s: s.station_no)

    # 权重进制（与 swiss.py 的字典序微扰同理，全部整数精确计算）：
    # - 字典序位：base^(k-1-i) * (base-1-j)，i/j 为对局/工位名次；
    #   全部边的字典序位之和 < base^k，且该和按 base 进制展开唯一
    #   对应一组「对局 -> 工位」选择；
    # - 换位权重 move_unit = base^k：一次换位压过所有字典序位；
    # - 场数权重 count_unit = (2k+1) * move_unit：总换位数不超过
    #   2k（每场两人），一场对局压过所有换位。
    base = n + 2
    move_unit = base**k
    count_unit = (2 * k + 1) * move_unit

    graph = nx.Graph()
    for i, match_id in enumerate(match_ids):
        required = required_by_match[match_id]
        white_id, black_id = players_by_match[match_id]
        prev_white = prev_station_by_player.get(white_id)
        prev_black = prev_station_by_player.get(black_id)
        for j, station in enumerate(stations):
            if not required <= station.tags:
                continue  # 工位标签未覆盖双方必需标签：不可行边
            moves = int(
                prev_white is not None and prev_white != station.station_no
            ) + int(
                prev_black is not None and prev_black != station.station_no
            )
            lex = base ** (k - 1 - i) * (base - 1 - j)
            weight = count_unit - move_unit * moves + lex
            graph.add_edge(
                ("m", match_id), ("s", station.id), weight=weight
            )

    if graph.number_of_edges() == 0:
        return {}
    matching = nx.max_weight_matching(graph, weight="weight")
    result: dict[int, int] = {}
    for u, v in matching:
        m_node, s_node = (u, v) if u[0] == "m" else (v, u)
        result[m_node[1]] = s_node[1]
    return result


def explain_unassigned(
    required: frozenset[str],
    available: list[_StationInfo],
    occupied_station_nos: set[int],
) -> dict:
    """为一场未分配对局生成说明。

    - missing_tags：所有可用工位都不提供的必需标签（此时不存在
      任何可承接该对局的工位）；
    - conflicting_stations：能覆盖全部必需标签、但在本方案中已被
      其他对局占用的工位编号（容量不足的表现）。
    """
    covered: set[str] = set()
    for station in available:
        covered |= station.tags
    missing = sorted(required - covered)
    compatible = [s for s in available if required <= s.tags]
    conflicting = sorted(
        s.station_no for s in compatible if s.station_no in occupied_station_nos
    )
    if missing:
        reason = "缺少标签：" + "、".join(missing) + "（所有可用工位均不提供）"
    elif conflicting:
        reason = "可覆盖全部必需标签的工位已被占用：" + "、".join(
            str(no) for no in conflicting
        )
    elif not available:
        reason = "赛事当前没有可用工位"
    else:
        reason = "没有可同时覆盖全部必需标签的可用工位"
    return {
        "missing_tags": missing,
        "conflicting_stations": conflicting,
        "reason": reason,
    }


def _validate_pins(
    pins,
    matches_by_id: dict[int, Match],
    required_by_match: dict[int, frozenset[str]],
    stations_by_no: dict[int, Station],
    round_no: int,
) -> None:
    """校验固定工位；任一问题即 409（调用方尚未落库任何数据）。"""
    problems: list[str] = []
    seen_matches: set[int] = set()
    seen_stations: set[int] = set()
    for pin in pins:
        match = matches_by_id.get(pin.match_id)
        if match is None:
            problems.append(
                f"对局 {pin.match_id} 不是第 {round_no} 轮的非轮空对局"
            )
            continue
        station = stations_by_no.get(pin.station_no)
        if station is None:
            problems.append(f"工位 {pin.station_no} 不存在")
            continue
        if pin.match_id in seen_matches:
            problems.append(f"对局 {pin.match_id} 被重复固定")
        seen_matches.add(pin.match_id)
        if pin.station_no in seen_stations:
            problems.append(f"工位 {pin.station_no} 被重复固定")
        seen_stations.add(pin.station_no)
        if not station.available:
            problems.append(f"工位 {pin.station_no} 不可用")
            continue
        lack = sorted(required_by_match[pin.match_id] - set(station.tags or []))
        if lack:
            problems.append(
                f"工位 {pin.station_no} 缺少标签：" + "、".join(lack)
            )
    if problems:
        raise HTTPException(status_code=409, detail="；".join(problems))


def create_station_plan(
    db: Session,
    tournament_id: int,
    round_no: int,
    expected_version: int,
    pins: list,
) -> tuple[StationPlan, Event]:
    """求解并写入新一轮工位方案（flush，不提交），返回方案与事件。"""
    round_obj = (
        db.query(Round)
        .filter(
            Round.tournament_id == tournament_id,
            Round.round_no == round_no,
        )
        .first()
    )
    if round_obj is None:
        raise HTTPException(status_code=404, detail="该轮次不存在")

    # 乐观并发：期望版本必须等于当前最新版本（尚无方案时为 0）
    current_version = (
        db.query(func.max(StationPlan.version))
        .filter(StationPlan.round_id == round_obj.id)
        .scalar()
        or 0
    )
    if expected_version != current_version:
        raise HTTPException(
            status_code=409,
            detail=(
                f"版本冲突：当前最新方案版本为 {current_version}，"
                f"请求期望 {expected_version}"
            ),
        )

    # 本轮非轮空对局（轮空不占工位），按 Match.id 升序
    matches = [m for m in round_obj.matches if m.player_black_id is not None]
    matches_by_id = {m.id: m for m in matches}

    participants = (
        db.query(Participant)
        .filter(Participant.tournament_id == tournament_id)
        .all()
    )
    tags_by_pid = {
        p.id: frozenset(p.required_tags or []) for p in participants
    }
    required_by_match = {
        m.id: tags_by_pid.get(m.player_white_id, frozenset())
        | tags_by_pid.get(m.player_black_id, frozenset())
        for m in matches
    }
    players_by_match = {
        m.id: (m.player_white_id, m.player_black_id) for m in matches
    }

    stations = (
        db.query(Station)
        .filter(Station.tournament_id == tournament_id)
        .order_by(Station.station_no)
        .all()
    )
    stations_by_no = {s.station_no: s for s in stations}

    # 固定工位校验失败：抛 409，本次请求不落任何数据
    _validate_pins(
        pins, matches_by_id, required_by_match, stations_by_no, round_no
    )
    pinned = {pin.match_id: pin.station_no for pin in pins}

    # 换位基线：上一轮最新发布方案中每位选手的工位编号
    prev_station_by_player: dict[int, int] = {}
    prev_round = (
        db.query(Round)
        .filter(
            Round.tournament_id == tournament_id,
            Round.round_no == round_no - 1,
        )
        .first()
    )
    if prev_round is not None:
        prev_plan = (
            db.query(StationPlan)
            .filter(StationPlan.round_id == prev_round.id)
            .order_by(StationPlan.version.desc())
            .first()
        )
        if prev_plan is not None:
            prev_matches = {m.id: m for m in prev_round.matches}
            for assignment in prev_plan.assignments:
                prev_match = prev_matches.get(assignment.match_id)
                if prev_match is None:
                    continue
                for pid in (
                    prev_match.player_white_id,
                    prev_match.player_black_id,
                ):
                    if pid is not None:
                        prev_station_by_player[pid] = assignment.station_no

    # 求解：固定工位先从资源中移除，其余交给最优化
    available = [s for s in stations if s.available]
    pinned_station_nos = set(pinned.values())
    free_stations = [
        _StationInfo(s.id, s.station_no, frozenset(s.tags or []))
        for s in available
        if s.station_no not in pinned_station_nos
    ]
    free_match_ids = [m.id for m in matches if m.id not in pinned]
    solved = solve_assignments(
        free_match_ids,
        required_by_match,
        players_by_match,
        free_stations,
        prev_station_by_player,
    )

    station_no_by_id = {s.id: s.station_no for s in stations}
    assignments: dict[int, int] = dict(solved)  # match_id -> station_id
    for match_id, station_no in pinned.items():
        assignments[match_id] = stations_by_no[station_no].id
    occupied_station_nos = {
        station_no_by_id[sid] for sid in assignments.values()
    }

    # 容量不足时仍保存最大可行方案，并逐场说明未分配原因
    available_info = [
        _StationInfo(s.id, s.station_no, frozenset(s.tags or []))
        for s in available
    ]
    unassigned = []
    for m in matches:
        if m.id in assignments:
            continue
        required = required_by_match[m.id]
        unassigned.append(
            {
                "match_id": m.id,
                "required_tags": sorted(required),
                **explain_unassigned(
                    required, available_info, occupied_station_nos
                ),
            }
        )

    plan = StationPlan(
        round_id=round_obj.id,
        version=expected_version + 1,
        unassigned=unassigned,
    )
    db.add(plan)
    db.flush()  # 取得 plan.id，供分配行与事件使用

    for match_id in sorted(assignments):
        db.add(
            StationPlanAssignment(
                plan_id=plan.id,
                match_id=match_id,
                station_id=assignments[match_id],
                station_no=station_no_by_id[assignments[match_id]],
            )
        )
    db.flush()

    event = append_event(
        db,
        tournament_id,
        STATION_PLAN_PUBLISHED,
        {
            "round_id": round_obj.id,
            "round_no": round_obj.round_no,
            "plan_id": plan.id,
            "version": plan.version,
            # 按 match_id 升序的分配快照
            "assignments": [
                {
                    "match_id": match_id,
                    "station_no": station_no_by_id[assignments[match_id]],
                }
                for match_id in sorted(assignments)
            ],
            "unassigned_match_ids": [u["match_id"] for u in unassigned],
        },
    )
    return plan, event
