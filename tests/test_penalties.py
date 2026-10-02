"""赛务争议扣分账本模块测试。

覆盖需求：
- 登记扣分：request_id 幂等键 + expected_event_seq 乐观快照 + 半分
  整数倍扣分量 + 理由；仅允许在所有已生成轮次结束且没有待处理赛果
  复核时登记，否则 409 且不落库；
- 撤销：以新记录撤销一笔仍生效的扣分，原扣分置为 revoked 并同事务
  写入事件；重复撤销 / 目标不是扣分条目 409 且不落库；
- 幂等：重复请求（参数一致）原样返回首次结果、不产生新事件；同键
  不同参数 409；快照冲突 409 不落库不占号；
- 排名口径：保留对局积分，返回有效扣分与排名积分（可为负）；排序、
  Buchholz（随对手排名积分重算）与下一轮积分分组均以排名积分为准；
- 账本查询：赛事完整账本、按参赛者查询完整账本（含有效扣分合计）；
- 事件：penalty.recorded / penalty.revoked 进入事件流，被拒绝的
  请求不产生事件；
- 并发：并发撤销同一扣分仅一成一败；并发同键不同参数登记仅一成。
"""
import concurrent.futures
import threading

import pytest


# ---------- 基础工具 ----------

def create_tournament(client, name="扣分测试赛"):
    r = client.post("/tournaments", json={"name": name})
    assert r.status_code == 201
    return r.json()["id"]


def add_players(client, tid, names):
    r = client.post(
        f"/tournaments/{tid}/participants/bulk", json={"names": names}
    )
    assert r.status_code == 201
    return r.json()


def next_round(client, tid):
    r = client.post(f"/tournaments/{tid}/rounds/next")
    assert r.status_code == 201, r.text
    return r.json()


def put_result(client, match, sw, sb):
    mid = match["id"] if isinstance(match, dict) else match
    r = client.put(
        f"/matches/{mid}/result",
        json={"score_white": sw, "score_black": sb},
    )
    assert r.status_code == 200, r.text
    return r.json()


def event_seq(client, tid):
    return client.get(f"/tournaments/{tid}/events").json()["last_seq"]


def events_of(client, tid, type_=None):
    items = client.get(f"/tournaments/{tid}/events?limit=200").json()["items"]
    return [e for e in items if type_ is None or e["type"] == type_]


def standings(client, tid):
    return client.get(f"/tournaments/{tid}/standings").json()


def standings_map(client, tid):
    return {row["participant_id"]: row for row in standings(client, tid)}


def deduct(client, tid, **body):
    return client.post(f"/tournaments/{tid}/penalties", json=body)


def revoke(client, entry_id, **body):
    return client.post(f"/penalties/{entry_id}/revoke", json=body)


def tournament_ledger(client, tid):
    return client.get(f"/tournaments/{tid}/penalties")


def participant_ledger(client, tid, pid):
    return client.get(f"/tournaments/{tid}/participants/{pid}/penalties")


def setup_closed_round(client, n=4):
    """n 人赛事，第 1 轮全部以白胜录入并封盘。返回 (tid, players, real)。"""
    tid = create_tournament(client)
    players = add_players(client, tid, [f"选手{i}" for i in range(1, n + 1)])
    rnd = next_round(client, tid)
    real = [m for m in rnd["matches"] if not m["is_bye"]]
    for m in real:
        put_result(client, m, 1.0, 0.0)
    rounds = client.get(f"/tournaments/{tid}/rounds").json()
    assert rounds[-1]["status"] == "closed"
    return tid, players, real


def deduct_body(client, tid, **over):
    body = {
        "request_id": "pen-1",
        "expected_event_seq": event_seq(client, tid),
        "participant_id": 1,
        "points": 0.5,
        "reason": "迟到 15 分钟",
    }
    body.update(over)
    return body


def revoke_body(client, tid, **over):
    body = {
        "request_id": "pen-1-revoke",
        "expected_event_seq": event_seq(client, tid),
        "reason": "申诉成立，撤销扣分",
    }
    body.update(over)
    return body


# ---------- 一、登记扣分 ----------

def test_deduct_success_writes_entry_event_and_standings(client):
    tid, players, real = setup_closed_round(client)
    seq = event_seq(client, tid)

    r = deduct(client, tid, **deduct_body(client, tid, participant_id=1))
    assert r.status_code == 201, r.text
    entry = r.json()
    assert entry["kind"] == "deduct"
    assert entry["status"] == "active"
    assert entry["participant_id"] == 1
    assert entry["number"] == 1
    assert entry["points"] == 0.5
    assert entry["reason"] == "迟到 15 分钟"
    assert entry["revokes_entry_id"] is None
    assert entry["expected_event_seq"] == seq
    assert entry["created_at"] is not None

    # 事件同事务写入，序号恰好 +1
    assert event_seq(client, tid) == seq + 1
    evs = events_of(client, tid, "penalty.recorded")
    assert len(evs) == 1
    assert evs[0]["seq"] == seq + 1
    assert evs[0]["payload"]["entry_id"] == entry["id"]
    assert evs[0]["payload"]["participant_id"] == 1
    assert evs[0]["payload"]["points"] == 0.5

    # 排名：对局积分保留，返回有效扣分与排名积分
    row = standings_map(client, tid)[1]
    assert row["points"] == 1.0
    assert row["penalty"] == 0.5
    assert row["rank_points"] == 0.5


def test_deduct_multiple_accumulates_in_standings(client):
    tid, players, real = setup_closed_round(client)
    deduct(client, tid, **deduct_body(client, tid, participant_id=1))
    deduct(
        client,
        tid,
        **deduct_body(
            client, tid,
            request_id="pen-2", participant_id=1, points=1.0,
            reason="作弊警告",
        ),
    )
    row = standings_map(client, tid)[1]
    assert row["points"] == 1.0
    assert row["penalty"] == 1.5
    assert row["rank_points"] == -0.5  # 排名积分允许为负


def test_deduct_validation_rejected(client):
    tid, players, real = setup_closed_round(client)
    seq = event_seq(client, tid)

    # 非 0.5 整数倍
    r = deduct(client, tid, **deduct_body(client, tid, points=0.3))
    assert r.status_code == 422
    # 零 / 负数
    r = deduct(client, tid, **deduct_body(client, tid, points=0))
    assert r.status_code == 422
    r = deduct(client, tid, **deduct_body(client, tid, points=-1.0))
    assert r.status_code == 422
    # 空理由
    r = deduct(client, tid, **deduct_body(client, tid, reason=""))
    assert r.status_code == 422
    # 均不落库、不占号
    assert event_seq(client, tid) == seq
    assert tournament_ledger(client, tid).json() == []


def test_deduct_participant_not_found(client):
    tid, players, real = setup_closed_round(client)
    seq = event_seq(client, tid)

    # 不属于本赛事的参赛者
    r = deduct(client, tid, **deduct_body(client, tid, participant_id=9999))
    assert r.status_code == 404
    assert event_seq(client, tid) == seq

    # 赛事不存在
    r = deduct(client, 9999, **deduct_body(client, tid))
    assert r.status_code == 404


def test_deduct_blocked_while_round_open(client):
    tid = create_tournament(client)
    add_players(client, tid, ["甲", "乙", "丙", "丁"])
    next_round(client, tid)  # 轮次 open，未录赛果
    seq = event_seq(client, tid)

    r = deduct(client, tid, **deduct_body(client, tid))
    assert r.status_code == 409
    assert "尚未结束" in r.json()["detail"]
    assert event_seq(client, tid) == seq  # 不落库、不占号
    assert tournament_ledger(client, tid).json() == []


def test_deduct_blocked_by_pending_result_review(client):
    tid, players, real = setup_closed_round(client)
    # 发起一条待处理赛果复核
    r = client.post(
        f"/matches/{real[0]['id']}/result-reviews",
        json={
            "request_id": "rev-1",
            "expected_event_seq": event_seq(client, tid),
            "score_white": 0.0,
            "score_black": 1.0,
            "reason": "记分表登记有误",
        },
    )
    assert r.status_code == 201
    seq = event_seq(client, tid)

    r = deduct(client, tid, **deduct_body(client, tid))
    assert r.status_code == 409
    assert "复核" in r.json()["detail"]
    assert event_seq(client, tid) == seq
    assert tournament_ledger(client, tid).json() == []


def test_deduct_allowed_before_any_round(client):
    # 尚未生成任何轮次：所有已生成轮次（零个）视为已结束，允许登记
    tid = create_tournament(client)
    add_players(client, tid, ["甲", "乙"])

    r = deduct(client, tid, **deduct_body(client, tid, points=1.0))
    assert r.status_code == 201, r.text
    row = standings_map(client, tid)[1]
    assert row["points"] == 0.0
    assert row["penalty"] == 1.0
    assert row["rank_points"] == -1.0


def test_deduct_snapshot_conflict_rejects_without_write(client):
    tid, players, real = setup_closed_round(client)
    seq = event_seq(client, tid)

    r = deduct(
        client, tid, **deduct_body(client, tid, expected_event_seq=seq - 1)
    )
    assert r.status_code == 409
    assert "快照" in r.json()["detail"]
    assert event_seq(client, tid) == seq
    assert tournament_ledger(client, tid).json() == []


def test_deduct_idempotent_replay(client):
    tid, players, real = setup_closed_round(client)
    body = deduct_body(client, tid)
    r1 = deduct(client, tid, **body)
    assert r1.status_code == 201
    entry_id = r1.json()["id"]
    seq = event_seq(client, tid)

    r2 = deduct(client, tid, **body)
    # 重放返回首次结果（HTTP 201，与复核模块一致），无新事件
    assert r2.status_code == 201
    assert r2.json() == r1.json()
    assert r2.json()["id"] == entry_id
    assert event_seq(client, tid) == seq
    assert len(events_of(client, tid, "penalty.recorded")) == 1
    assert len(tournament_ledger(client, tid).json()) == 1


def test_deduct_same_request_id_different_params_conflict(client):
    tid, players, real = setup_closed_round(client)
    body = deduct_body(client, tid)
    deduct(client, tid, **body)
    seq = event_seq(client, tid)

    r = deduct(client, tid, **{**body, "points": 1.0})
    assert r.status_code == 409
    r = deduct(client, tid, **{**body, "participant_id": 2})
    assert r.status_code == 409
    assert event_seq(client, tid) == seq
    assert len(tournament_ledger(client, tid).json()) == 1


# ---------- 二、撤销扣分 ----------

def _deducted(client, tid, participant_id=1, points=0.5, request_id="pen-1"):
    r = deduct(
        client,
        tid,
        **deduct_body(
            client, tid,
            request_id=request_id, participant_id=participant_id,
            points=points,
        ),
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_revoke_success_marks_entry_and_recomputes_standings(client):
    tid, players, real = setup_closed_round(client)
    entry = _deducted(client, tid)
    assert standings_map(client, tid)[1]["penalty"] == 0.5
    seq = event_seq(client, tid)

    r = revoke(client, entry["id"], **revoke_body(client, tid))
    assert r.status_code == 201, r.text
    rv = r.json()
    assert rv["kind"] == "revoke"
    assert rv["status"] == "applied"
    assert rv["revokes_entry_id"] == entry["id"]
    assert rv["points"] == 0.5  # 冗余被撤销的量
    assert rv["participant_id"] == 1

    # 事件同事务写入，序号恰好 +1
    assert event_seq(client, tid) == seq + 1
    evs = events_of(client, tid, "penalty.revoked")
    assert len(evs) == 1
    assert evs[0]["payload"]["entry_id"] == rv["id"]
    assert evs[0]["payload"]["revoked_entry_id"] == entry["id"]

    # 原扣分保留但已撤销；有效扣分归零
    ledger = tournament_ledger(client, tid).json()
    assert [e["id"] for e in ledger] == [entry["id"], rv["id"]]
    assert ledger[0]["status"] == "revoked"
    assert ledger[1]["kind"] == "revoke"
    row = standings_map(client, tid)[1]
    assert row["penalty"] == 0.0
    assert row["rank_points"] == row["points"] == 1.0


def test_revoke_nonexistent_entry_404(client):
    tid, players, real = setup_closed_round(client)
    r = revoke(client, 9999, **revoke_body(client, tid))
    assert r.status_code == 404


def test_revoke_already_revoked_conflict_without_write(client):
    tid, players, real = setup_closed_round(client)
    entry = _deducted(client, tid)
    r = revoke(client, entry["id"], **revoke_body(client, tid))
    assert r.status_code == 201
    seq = event_seq(client, tid)

    # 重复撤销（新 request_id）：409 且不落库、不占号
    r = revoke(
        client,
        entry["id"],
        **revoke_body(client, tid, request_id="pen-again"),
    )
    assert r.status_code == 409
    assert "重复撤销" in r.json()["detail"]
    assert event_seq(client, tid) == seq
    assert len(tournament_ledger(client, tid).json()) == 2


def test_revoke_a_revoke_entry_rejected(client):
    tid, players, real = setup_closed_round(client)
    entry = _deducted(client, tid)
    rv = revoke(client, entry["id"], **revoke_body(client, tid)).json()
    seq = event_seq(client, tid)

    # 撤销记录本身不是扣分条目，不能再被撤销
    r = revoke(
        client,
        rv["id"],
        **revoke_body(client, tid, request_id="pen-rev-rev"),
    )
    assert r.status_code == 409
    assert "不是扣分条目" in r.json()["detail"]
    assert event_seq(client, tid) == seq


def test_revoke_blocked_while_round_open(client):
    tid, players, real = setup_closed_round(client)
    entry = _deducted(client, tid)
    next_round(client, tid)  # 第 2 轮 open
    seq = event_seq(client, tid)

    r = revoke(client, entry["id"], **revoke_body(client, tid))
    assert r.status_code == 409
    assert "尚未结束" in r.json()["detail"]
    assert event_seq(client, tid) == seq
    # 扣分仍然生效
    assert standings_map(client, tid)[1]["penalty"] == 0.5


def test_revoke_snapshot_conflict_rejects_without_write(client):
    tid, players, real = setup_closed_round(client)
    entry = _deducted(client, tid)
    seq = event_seq(client, tid)

    r = revoke(
        client,
        entry["id"],
        **revoke_body(client, tid, expected_event_seq=seq - 1),
    )
    assert r.status_code == 409
    assert "快照" in r.json()["detail"]
    assert event_seq(client, tid) == seq
    assert standings_map(client, tid)[1]["penalty"] == 0.5


def test_revoke_idempotent_replay(client):
    tid, players, real = setup_closed_round(client)
    entry = _deducted(client, tid)
    body = revoke_body(client, tid)
    r1 = revoke(client, entry["id"], **body)
    assert r1.status_code == 201
    seq = event_seq(client, tid)

    r2 = revoke(client, entry["id"], **body)
    assert r2.status_code == 201
    assert r2.json() == r1.json()
    assert event_seq(client, tid) == seq
    assert len(events_of(client, tid, "penalty.revoked")) == 1
    assert len(tournament_ledger(client, tid).json()) == 2


def test_request_id_shared_across_deduct_and_revoke(client):
    # request_id 在赛事内唯一：撤销复用扣分请求的键（参数不同）-> 409
    tid, players, real = setup_closed_round(client)
    entry = _deducted(client, tid, request_id="shared-key")
    seq = event_seq(client, tid)

    r = revoke(
        client,
        entry["id"],
        **revoke_body(client, tid, request_id="shared-key"),
    )
    assert r.status_code == 409
    assert event_seq(client, tid) == seq


# ---------- 三、排名口径 ----------

def test_standings_sort_and_buchholz_use_rank_points(client):
    # 第 1 轮：1 胜 2、3 胜 4；给 1 号扣 0.5 分
    tid, players, real = setup_closed_round(client)
    before = standings_map(client, tid)
    assert before[2]["buchholz"] == 1.0  # 对手 1 号的对局积分

    deduct(client, tid, **deduct_body(client, tid, participant_id=1))
    rows = standings(client, tid)
    order = [r["participant_id"] for r in rows]
    # 排名积分：3 号 1.0 > 1 号 0.5 > 4 号/2 号 0.0；
    # 4 号 Buchholz（对手 3 号的排名积分 1.0）> 2 号（对手 1 号 0.5）
    assert order == [3, 1, 4, 2]
    by_id = {r["participant_id"]: r for r in rows}
    assert by_id[1]["points"] == 1.0
    assert by_id[1]["penalty"] == 0.5
    assert by_id[1]["rank_points"] == 0.5
    # Buchholz 随对手的排名积分重算
    assert by_id[2]["buchholz"] == 0.5
    assert by_id[2]["points"] == 0.0
    assert by_id[2]["rank_points"] == 0.0
    assert by_id[4]["buchholz"] == 1.0
    # 名次连续
    assert [r["rank"] for r in rows] == [1, 2, 3, 4]


def test_negative_rank_points_sorted_last(client):
    tid, players, real = setup_closed_round(client)
    # 2 号第 1 轮告负（0 分），再扣 1.5 -> 排名积分 -1.5 垫底
    deduct(
        client,
        tid,
        **deduct_body(client, tid, participant_id=2, points=1.5),
    )
    rows = standings(client, tid)
    assert rows[-1]["participant_id"] == 2
    assert rows[-1]["rank_points"] == -1.5
    assert rows[-1]["points"] == 0.0
    assert rows[-1]["penalty"] == 1.5


def test_next_round_grouping_uses_rank_points(client):
    # 6 人：第 1 轮 1>2、3>4、5>6（均白胜）。
    # 无扣分时第 2 轮配对为 {(1,3),(2,5),(4,6)}；
    # 给 1 号扣 1.0 后，1 号落入 0 分组，配对变为 {(1,4),(2,6),(3,5)}。
    tid = create_tournament(client)
    players = add_players(client, tid, [f"选手{i}" for i in range(1, 7)])
    rnd = next_round(client, tid)
    real = sorted(
        (m for m in rnd["matches"] if not m["is_bye"]),
        key=lambda m: m["player_white"]["id"],
    )
    assert [m["player_white"]["id"] for m in real] == [1, 3, 5]
    for m in real:
        put_result(client, m, 1.0, 0.0)

    deduct(
        client,
        tid,
        **deduct_body(client, tid, participant_id=1, points=1.0),
    )
    assert standings_map(client, tid)[1]["rank_points"] == 0.0

    rnd2 = next_round(client, tid)
    pairs = {
        (m["player_white"]["id"], m["player_black"]["id"])
        for m in rnd2["matches"]
        if not m["is_bye"]
    }
    assert pairs == {(1, 4), (2, 6), (3, 5)}


# ---------- 四、账本查询 ----------

def test_participant_ledger_full_history(client):
    tid, players, real = setup_closed_round(client)
    e1 = _deducted(client, tid, participant_id=1, points=0.5,
                   request_id="pen-1")
    e2 = _deducted(client, tid, participant_id=1, points=1.0,
                   request_id="pen-2")
    _deducted(client, tid, participant_id=3, points=2.0, request_id="pen-3")
    rv = revoke(client, e1["id"], **revoke_body(client, tid)).json()

    r = participant_ledger(client, tid, 1)
    assert r.status_code == 200
    body = r.json()
    assert body["participant_id"] == 1
    assert body["number"] == 1
    # 有效扣分：e1 已撤销，仅 e2 生效
    assert body["effective_penalty"] == 1.0
    kinds = [(e["kind"], e["status"], e["points"]) for e in body["entries"]]
    assert kinds == [
        ("deduct", "revoked", 0.5),
        ("deduct", "active", 1.0),
        ("revoke", "applied", 0.5),
    ]
    assert body["entries"][2]["revokes_entry_id"] == e1["id"]

    # 赛事完整账本按登记顺序包含全部条目（含 3 号的扣分）
    ledger = tournament_ledger(client, tid).json()
    assert [e["id"] for e in ledger] == [e1["id"], e2["id"],
                                         ledger[2]["id"], rv["id"]]
    assert len(ledger) == 4

    # 3 号账本独立
    body3 = participant_ledger(client, tid, 3).json()
    assert body3["effective_penalty"] == 2.0
    assert len(body3["entries"]) == 1


def test_participant_ledger_not_found(client):
    tid, players, real = setup_closed_round(client)
    r = participant_ledger(client, tid, 9999)
    assert r.status_code == 404
    r = participant_ledger(client, 9999, 1)
    assert r.status_code == 404
    r = tournament_ledger(client, 9999)
    assert r.status_code == 404


# ---------- 五、并发 ----------

def _run_concurrently(fn, n, barrier):
    results: list = [None] * n
    errors: list[BaseException] = []

    def worker(i):
        try:
            barrier.wait(timeout=10)
            results[i] = fn(i)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    with concurrent.futures.ThreadPoolExecutor(max_workers=n) as pool:
        futs = [pool.submit(worker, i) for i in range(n)]
        concurrent.futures.wait(futs, timeout=30)
    assert not errors, errors
    return results


def test_concurrent_revoke_only_one_succeeds(live_server_factory):
    """并发撤销同一笔扣分：恰好一条撤销记录，其余 409。"""
    with live_server_factory() as srv:
        client = srv.client()
        tid, players, real = setup_closed_round(client)
        entry = _deducted(client, tid)
        seq = event_seq(client, tid)
        n = 6
        barrier = threading.Barrier(n)

        def call(i):
            local = srv.client()
            try:
                barrier.wait(timeout=10)
                r = local.post(
                    f"/penalties/{entry['id']}/revoke",
                    json=revoke_body(client, tid, request_id=f"rv-{i}"),
                )
                return r.status_code
            finally:
                local.close()

        statuses = _run_concurrently(call, n, barrier)
        assert statuses.count(201) == 1
        assert statuses.count(409) == n - 1

        ledger = tournament_ledger(client, tid).json()
        assert len([e for e in ledger if e["kind"] == "revoke"]) == 1
        assert event_seq(client, tid) == seq + 1  # 仅一次撤销占一个序号
        assert standings_map(client, tid)[1]["penalty"] == 0.0


def test_concurrent_deduct_same_key_conflict(live_server_factory):
    """并发以同一 request_id 提交不同参数：恰好一笔落库，其余 409。"""
    with live_server_factory() as srv:
        client = srv.client()
        tid, players, real = setup_closed_round(client)
        seq = event_seq(client, tid)
        n = 6
        barrier = threading.Barrier(n)

        def call(i):
            local = srv.client()
            try:
                barrier.wait(timeout=10)
                r = local.post(
                    f"/tournaments/{tid}/penalties",
                    json=deduct_body(
                        client, tid,
                        request_id="same-key", points=0.5 * (i + 1),
                    ),
                )
                return r.status_code
            finally:
                local.close()

        statuses = _run_concurrently(call, n, barrier)
        assert statuses.count(201) == 1
        assert statuses.count(409) == n - 1
        assert len(tournament_ledger(client, tid).json()) == 1
        assert event_seq(client, tid) == seq + 1


# ---------- 实时服务器夹具（与 test_result_reviews 同款，后台 uvicorn） ----------

@pytest.fixture()
def live_server_factory():
    import socket
    import time

    import httpx
    import uvicorn

    from app.database import Base, engine
    from app.main import app

    def wait_until(cond, timeout=10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cond():
                return
            time.sleep(0.01)
        raise AssertionError("server not started")

    class Server:
        def __init__(self):
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
            sock.close()
            self.server = uvicorn.Server(
                uvicorn.Config(
                    app,
                    host="127.0.0.1",
                    port=self.port,
                    log_level="error",
                    access_log=False,
                )
            )
            self.thread = threading.Thread(target=self.server.run, daemon=True)

        def client(self):
            return httpx.Client(
                base_url=f"http://127.0.0.1:{self.port}", timeout=10.0
            )

        def __enter__(self):
            Base.metadata.drop_all(bind=engine)
            Base.metadata.create_all(bind=engine)
            self.thread.start()
            wait_until(lambda: self.server.started)
            return self

        def __exit__(self, *exc):
            self.server.should_exit = True
            self.thread.join(timeout=10)

    return Server
