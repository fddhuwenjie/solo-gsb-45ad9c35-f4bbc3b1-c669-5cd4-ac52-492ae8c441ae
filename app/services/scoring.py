"""积分、有效扣分与 Buchholz 分计算。

积分以「半分」为单位的整数存储与计算，对外输出时除以 2。
- 胜 = 1，和 = 0.5，负 = 0；
- 轮空按胜场计 1 分，且轮空不计对手 Buchholz 贡献（0）；
- 排名积分 = 对局积分 - 有效扣分（可为负）；排序、Buchholz 与
  下一轮积分分组均以排名积分为准，Buchholz 取对手的排名积分之和。
"""
from dataclasses import dataclass

from sqlalchemy.orm import Session

from ..models import Match, Participant, Round
from .swiss import WIN


@dataclass
class Record:
    participant_id: int
    number: int
    name: str
    points: int          # 对局积分（半分单位，不含扣分）
    penalty: int         # 当前有效扣分（半分单位，非负）
    rank_points: int     # 排名积分 = 对局积分 - 有效扣分（可为负）
    buchholz: int        # 对手累计排名积分之和（半分单位）
    played: int          # 实际对局数（不含轮空）
    byes: int            # 轮空次数


def compute_records(
    db: Session,
    tournament_id: int,
    include_open: bool = False,
    through_round_no: int | None = None,
) -> list[Record]:
    """计算成绩。

    include_open=False（编排下一轮/发布晋级名单时使用）：只统计已结束轮次。
    include_open=True（排名查询使用）：同时实时计入进行中轮次里
    已经录入的对局与在生成轮次时即确定的轮空。
    through_round_no 非空时只统计轮次号不超过它的轮次（晋级名单的
    资格轮次口径）；None 表示统计全部轮次。
    有效扣分（账本中生效的扣分条目）两种口径都计入排名积分。
    """
    # 延迟导入：penalties 服务依赖本模块的 half_to_float
    from .penalties import effective_penalties

    participants = (
        db.query(Participant)
        .filter(Participant.tournament_id == tournament_id)
        .order_by(Participant.number)
        .all()
    )
    rounds = (
        db.query(Round)
        .filter(Round.tournament_id == tournament_id)
        .all()
    )
    if through_round_no is not None:
        rounds = [r for r in rounds if r.round_no <= through_round_no]
    if include_open:
        round_ids = {r.id for r in rounds}
    else:
        round_ids = {r.id for r in rounds if r.status == "closed"}

    matches = (
        db.query(Match)
        .filter(Match.round_id.in_(round_ids))
        .all()
        if round_ids
        else []
    )

    points = {p.id: 0 for p in participants}
    played = {p.id: 0 for p in participants}
    byes = {p.id: 0 for p in participants}
    # 每位选手实际对阵过的对手编号（轮空不含）
    opponents: dict[int, list[int]] = {p.id: [] for p in participants}

    for m in matches:
        if m.player_black_id is None:
            # 轮空：轮空方得 1 分，无对手
            if m.player_white_id in points:
                points[m.player_white_id] += m.score_white or 0
                byes[m.player_white_id] += 1
            continue
        points[m.player_white_id] += m.score_white or 0
        points[m.player_black_id] += m.score_black or 0
        played[m.player_white_id] += 1
        played[m.player_black_id] += 1
        opponents[m.player_white_id].append(m.player_black_id)
        opponents[m.player_black_id].append(m.player_white_id)

    # 有效扣分（半分单位）：排名积分 = 对局积分 - 有效扣分（可为负）
    penalties = effective_penalties(db, tournament_id)
    rank_points = {
        p.id: points[p.id] - penalties.get(p.id, 0) for p in participants
    }

    # Buchholz：实际对手的当前排名积分之和（随对手扣分重算；
    # 点数组已固定，两遍计算）
    records: list[Record] = []
    for p in participants:
        buch = sum(rank_points[o] for o in opponents[p.id])
        records.append(
            Record(
                participant_id=p.id,
                number=p.number,
                name=p.name,
                points=points[p.id],
                penalty=penalties.get(p.id, 0),
                rank_points=rank_points[p.id],
                buchholz=buch,
                played=played[p.id],
                byes=byes[p.id],
            )
        )

    # 排名：排名积分降序 -> Buchholz 降序 -> 编号升序
    records.sort(key=lambda r: (-r.rank_points, -r.buchholz, r.number))
    return records


def half_to_float(v: int) -> float:
    return v / 2.0
