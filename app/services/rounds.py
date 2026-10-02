"""轮次编排：生成下一轮（轮空决策 + 瑞士配对）。

注意：generate_next_round 只在调用方事务内写入并 flush，不提交；
轮次/对局与 round.generated（必要时 round.closed）事件由路由层
在同一事务中统一提交。
"""
from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..events.service import (
    ROUND_CLOSED,
    ROUND_GENERATED,
    append_event,
)
from ..models import Event, Match, Participant, Round
from .checkins import active_snapshot_for_next_round
from .result_reviews import has_pending_review_for_tournament
from .scoring import compute_records
from .swiss import WIN, swiss_pair


def generate_next_round(
    db: Session, tournament_id: int
) -> tuple[Round, list[Event]]:
    tournament_participants = (
        db.query(Participant)
        .filter(Participant.tournament_id == tournament_id)
        .order_by(Participant.number)
        .all()
    )
    if len(tournament_participants) < 1:
        raise HTTPException(status_code=400, detail="赛事还没有参赛者，无法生成轮次")

    open_round = (
        db.query(Round)
        .filter(
            Round.tournament_id == tournament_id,
            Round.status == "open",
        )
        .first()
    )
    if open_round is not None:
        raise HTTPException(
            status_code=409,
            detail=f"第 {open_round.round_no} 轮尚未完成，请先录入全部赛果",
        )

    # 赛事存在待处理赛果复核时禁止生成下一轮：复核可能更正已提交
    # 比分并改变后续配对依据，必须先等复核关闭
    if has_pending_review_for_tournament(db, tournament_id):
        raise HTTPException(
            status_code=409,
            detail="赛事存在待处理的赛果复核，需待复核关闭后才能生成下一轮",
        )

    rounds = (
        db.query(Round)
        .filter(Round.tournament_id == tournament_id)
        .order_by(Round.round_no)
        .all()
    )
    next_no = (rounds[-1].round_no + 1) if rounds else 1

    # 现场签到快照存在时，仅到场者参与本轮瑞士配对与轮空；缺席者
    # 保留此前积分/历史，但本轮不会得到任何积分或新增对阵。未提交
    # 快照时沿用当前全员名单。
    checkin = active_snapshot_for_next_round(db, tournament_id, next_no)
    participants = tournament_participants
    if checkin is not None:
        present_numbers = set(checkin.present_numbers)
        participants = [
            p for p in tournament_participants if p.number in present_numbers
        ]

    # 当前排名（仅统计已结束轮次）：排名积分降序 -> Buchholz 降序 ->
    # 编号升序；下一轮积分分组以排名积分（对局积分 - 有效扣分）为准
    records = compute_records(db, tournament_id)
    if checkin is not None:
        present_ids = {p.id for p in participants}
        records = [r for r in records if r.participant_id in present_ids]
    ranking_ids = [r.participant_id for r in records]
    points_by_id = {r.participant_id: r.rank_points for r in records}

    # 已经轮空过的选手（轮空在生成轮次时即生成并记分）
    closed_round_ids = [r.id for r in rounds if r.status == "closed"]
    byed_ids: set[int] = set()
    if closed_round_ids:
        bye_matches = (
            db.query(Match)
            .filter(
                Match.round_id.in_(closed_round_ids),
                Match.player_black_id.is_(None),
            )
            .all()
        )
        byed_ids = {m.player_white_id for m in bye_matches}

    bye_id: int | None = None
    if len(participants) % 2 == 1:
        # 轮空给「尚未轮空且排名最低」的选手；
        # 若所有人都轮空过，则取排名最低者
        eligible = [pid for pid in ranking_ids if pid not in byed_ids]
        bye_id = eligible[-1] if eligible else ranking_ids[-1]

    pairing_players = [
        pid for pid in ranking_ids if pid != bye_id
    ]

    # 禁止重复交手：取所有已结束轮次中出现过的对阵
    forbidden: set[tuple[int, int]] = set()
    if closed_round_ids:
        played_matches = (
            db.query(Match)
            .filter(
                Match.round_id.in_(closed_round_ids),
                Match.player_black_id.is_not(None),
            )
            .all()
        )
        for m in played_matches:
            forbidden.add(
                (min(m.player_white_id, m.player_black_id),
                 max(m.player_white_id, m.player_black_id))
            )

    try:
        result = swiss_pair(
            players=pairing_players,
            points=points_by_id,
            forbidden=forbidden,
            bye_player=bye_id,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    round_obj = Round(
        tournament_id=tournament_id,
        round_no=next_no,
        status="open",
    )
    db.add(round_obj)
    db.flush()  # 取得 round_obj.id

    if checkin is not None:
        # 轮次一旦生成即冻结所采用的签到快照；后续拒绝修改/确认
        checkin.consumed = True
        db.flush()

    # 普通对局（swiss_pair 已按小白号升序返回），暂不记分
    for white_id, black_id in result.pairs:
        db.add(
            Match(
                round_id=round_obj.id,
                player_white_id=white_id,
                player_black_id=black_id,
                score_white=None,
                score_black=None,
            )
        )
    db.flush()  # 取得各场对局 id，供事件使用

    # 轮空对局：轮空方直接记 1 分，无对手
    bye_match_id: int | None = None
    if bye_id is not None:
        bye_match = Match(
            round_id=round_obj.id,
            player_white_id=bye_id,
            player_black_id=None,
            score_white=WIN,
            score_black=None,
        )
        db.add(bye_match)
        db.flush()
        bye_match_id = bye_match.id

    # 没有需要录入的普通对局（如仅 1 名参赛者）时直接结束本轮
    if not result.pairs:
        round_obj.status = "closed"

    db.flush()

    # 事件与轮次/对局在同一事务：生成事件（含即时封盘状态）；
    # 即时封盘时追加一条 round.closed，序号紧随 round.generated
    generated_payload = {
        "round_id": round_obj.id,
        "round_no": round_obj.round_no,
        "status": round_obj.status,
        # 本轮全部场次 id（普通对局 + 轮空对局，按 Match.id 排序）
        "match_ids": [m.id for m in round_obj.matches],
    }
    if bye_match_id is not None:
        generated_payload["bye_match_id"] = bye_match_id
    if checkin is not None:
        generated_payload["checkin_snapshot_id"] = checkin.id
        generated_payload["checkin_version"] = checkin.version
        generated_payload["present_numbers"] = list(checkin.present_numbers)
        generated_payload["absent_numbers"] = list(checkin.absent_numbers)
    events = [
        append_event(db, tournament_id, ROUND_GENERATED, generated_payload)
    ]

    if not result.pairs:
        events.append(
            append_event(
                db,
                tournament_id,
                ROUND_CLOSED,
                {
                    "round_id": round_obj.id,
                    "round_no": round_obj.round_no,
                    "reason": "no_matches",
                },
            )
        )

    return round_obj, events
