"""轮次计时接口。

- POST /tournaments/{id}/rounds/{round_no}/clock/actions：执行计时
  命令（start / pause / resume / extend）。请求携带幂等键
  request_id 与乐观并发版本 expected_version；重复 request_id
  原样返回首次结果，非法状态或版本冲突返回 409 且不落库。
  成功的命令与对应 round_clock.* 事件在同一事务中提交。
- GET /tournaments/{id}/rounds/{round_no}/clock：查询时钟版本、
  状态及各非轮空对局的截止时刻与剩余秒数。读数由持久化的
  UTC 锚点与累计暂停时长实时推算，无逐秒写库任务。
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..events.service import commit_and_publish
from ..locking import writer_lock
from ..models import Round
from ..schemas import ClockActionRequest, ClockResponse
from ..services.clock import apply_clock_action, get_clock, serialize_clock
from .tournaments import get_tournament_or_404

router = APIRouter()


def _get_round_or_404(db: Session, tournament_id: int, round_no: int) -> Round:
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
    return round_obj


@router.post(
    "/tournaments/{tournament_id}/rounds/{round_no}/clock/actions",
    response_model=ClockResponse,
)
def clock_action(
    tournament_id: int,
    round_no: int,
    payload: ClockActionRequest,
    db: Session = Depends(get_db),
):
    get_tournament_or_404(db, tournament_id)
    # 写请求进程级串行：幂等判定、状态机校验、版本检查、事件序号与
    # 「提交→发布」全程持锁
    with writer_lock():
        round_obj = _get_round_or_404(db, tournament_id, round_no)
        response, event = apply_clock_action(
            db, tournament_id, round_obj, payload
        )
        # 幂等重放（event 为 None）没有任何写入，直接返回首次结果
        if event is not None:
            commit_and_publish(db, [event])
        return response


@router.get(
    "/tournaments/{tournament_id}/rounds/{round_no}/clock",
    response_model=ClockResponse,
)
def get_round_clock(
    tournament_id: int,
    round_no: int,
    db: Session = Depends(get_db),
):
    get_tournament_or_404(db, tournament_id)
    round_obj = _get_round_or_404(db, tournament_id, round_no)
    clock = get_clock(db, round_obj.id)
    return serialize_clock(db, tournament_id, round_obj, clock)
