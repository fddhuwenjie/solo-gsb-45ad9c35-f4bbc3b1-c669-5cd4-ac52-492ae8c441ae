"""已提交赛果复核模块测试。

覆盖需求：
- 创建复核：仅非轮空、已提交且尚未生成下一轮的对局可创建；轮空、
  未提交、已生成下一轮、已有待处理复核均 409 且不落库；
- 原比分冻结：待处理复核期间不能修改该场赛果、赛事不能生成下一轮；
- expected_event_seq 乐观快照：不一致 409 且不落库、不占事件序号；
- request_id 幂等：重放返回首次结果（无新事件），同键不同参数 409；
- 双方决定：非本场选手 409、参赛者不存在 404；首次决定 201、重复
  相同决定 200 返回原结果；任一驳回即 rejected 关闭且比分不变；
  双方同意在同一事务内 approved 关闭、更正比分并重算可见排名；
- 改票：pending 期间可改票且原地更新（不产生额外决定记录），
  同意改驳回立即 rejected 关闭；复核关闭后再决定：已决定者返回原
  结果，未决定者 409；
- 查询：复核详情、赛事复核列表，保留原比分/提议/决定/最终状态；
- 事件：result_review.created / result_review.closed 进入事件流，
  被拒绝请求不产生事件；
- 并发：同一对局并发创建仅一成一败；同一复核并发重复决定不产生
  额外记录。
"""
import concurrent.futures
import threading

import pytest


# ---------- 基础工具 ----------

def create_tournament(client, name="复核测试赛"):
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


def standings_map(client, tid):
    return {
        row["participant_id"]: row
        for row in client.get(f"/tournaments/{tid}/standings").json()
    }


def create_review(client, match_id, **body):
    return client.post(f"/matches/{match_id}/result-reviews", json=body)


def decide(client, review_id, participant_id, vote):
    return client.post(
        f"/result-reviews/{review_id}/decisions",
        json={"participant_id": participant_id, "vote": vote},
    )


def get_review(client, review_id):
    return client.get(f"/result-reviews/{review_id}")


def list_reviews(client, tid):
    return client.get(f"/tournaments/{tid}/result-reviews")


def setup_closed_round(client, n=4):
    """n 人赛事，第 1 轮全部以白胜录入并封盘。返回 (tid, players, round)。"""
    tid = create_tournament(client)
    players = add_players(client, tid, [f"选手{i}" for i in range(1, n + 1)])
    rnd = next_round(client, tid)
    real = [m for m in rnd["matches"] if not m["is_bye"]]
    for m in real:
        put_result(client, m, 1.0, 0.0)
    rounds = client.get(f"/tournaments/{tid}/rounds").json()
    assert rounds[-1]["status"] == "closed"
    return tid, players, rnd, real


def review_body(client, tid, **over):
    body = {
        "request_id": "rev-1",
        "expected_event_seq": event_seq(client, tid),
        "score_white": 0.0,
        "score_black": 1.0,
        "reason": "记分表登记有误，实际黑方胜",
    }
    body.update(over)
    return body


# ---------- 一、创建复核的资格校验 ----------

def test_create_review_freezes_original_and_returns_pending(client):
    tid, players, rnd, real = setup_closed_round(client)
    m = real[0]
    w, b = m["player_white"]["id"], m["player_black"]["id"]
    seq = event_seq(client, tid)

    r = create_review(client, m["id"], **review_body(client, tid))
    assert r.status_code == 201, r.text
    rv = r.json()
    assert rv["status"] == "pending"
    assert rv["match_id"] == m["id"]
    assert rv["round_no"] == 1
    assert rv["player_white"]["id"] == w
    assert rv["player_black"]["id"] == b
    # 原比分冻结 + 拟更正比分
    assert rv["original_score_white"] == 1.0
    assert rv["original_score_black"] == 0.0
    assert rv["proposed_score_white"] == 0.0
    assert rv["proposed_score_black"] == 1.0
    assert rv["decisions"] == []
    assert rv["closed_at"] is None

    # 创建写入事件流，序号恰好 +1
    assert event_seq(client, tid) == seq + 1
    ev = events_of(client, tid, "result_review.created")
    assert len(ev) == 1
    assert ev[0]["seq"] == seq + 1
    assert ev[0]["payload"]["review_id"] == rv["id"]
    assert ev[0]["payload"]["original_score_white"] == 1.0
    assert ev[0]["payload"]["proposed_score_black"] == 1.0


def test_create_review_on_bye_match_rejected(client):
    tid = create_tournament(client)
    add_players(client, tid, ["甲", "乙", "丙"])
    rnd = next_round(client, tid)
    bye = next(m for m in rnd["matches"] if m["is_bye"])
    seq = event_seq(client, tid)

    r = create_review(client, bye["id"], **review_body(client, tid))
    assert r.status_code == 409
    assert event_seq(client, tid) == seq  # 不落库、不占号


def test_create_review_before_result_submitted_rejected(client):
    tid = create_tournament(client)
    add_players(client, tid, ["甲", "乙"])
    rnd = next_round(client, tid)
    m = rnd["matches"][0]
    seq = event_seq(client, tid)

    r = create_review(client, m["id"], **review_body(client, tid))
    assert r.status_code == 409
    assert event_seq(client, tid) == seq


def test_create_review_after_next_round_generated_rejected(client):
    tid, players, rnd, real = setup_closed_round(client)
    # 生成第 2 轮后，第 1 轮对局不能再复核
    next_round(client, tid)
    seq = event_seq(client, tid)

    r = create_review(client, real[0]["id"], **review_body(client, tid))
    assert r.status_code == 409
    assert "下一轮" in r.json()["detail"]
    assert event_seq(client, tid) == seq


def test_only_one_pending_review_per_match(client):
    tid, players, rnd, real = setup_closed_round(client)
    m = real[0]
    create_review(client, m["id"], **review_body(client, tid))
    seq = event_seq(client, tid)

    # 第二条待处理复核（新 request_id）被拒绝
    r = create_review(
        client, m["id"], **review_body(client, tid, request_id="rev-2")
    )
    assert r.status_code == 409
    assert event_seq(client, tid) == seq


def test_create_review_invalid_scores_rejected(client):
    tid, players, rnd, real = setup_closed_round(client)
    m = real[0]

    # 双方之和不为 1（0.5 + 0）
    r = create_review(
        client,
        m["id"],
        **review_body(client, tid, score_white=0.5, score_black=0.0),
    )
    assert r.status_code == 422
    # 超出 [0, 1] 取值范围（Pydantic 边界校验）
    r = create_review(
        client,
        m["id"],
        **review_body(client, tid, score_white=1.5, score_black=-0.5),
    )
    assert r.status_code == 422
    # 非半分取值按半分舍入（0.3/0.7 -> 0.5/0.5），与 PUT /result
    # 现有行为一致：可被接受，不属于 422
    r = create_review(
        client,
        m["id"],
        **review_body(
            client,
            tid,
            request_id="rev-round",
            score_white=0.3,
            score_black=0.7,
        ),
    )
    assert r.status_code == 201, r.text
    rv = r.json()
    assert rv["proposed_score_white"] == 0.5
    assert rv["proposed_score_black"] == 0.5


def test_create_review_nonexistent_match_404(client):
    create_tournament(client)
    r = client.post(
        "/matches/9999/result-reviews",
        json=review_body(client, 1),
    )
    assert r.status_code == 404


def test_expected_event_seq_conflict_rejects_without_write(client):
    tid, players, rnd, real = setup_closed_round(client)
    m = real[0]
    seq = event_seq(client, tid)

    r = create_review(
        client,
        m["id"],
        **review_body(client, tid, expected_event_seq=seq - 1),
    )
    assert r.status_code == 409
    assert "快照" in r.json()["detail"]
    assert event_seq(client, tid) == seq
    # 没有创建任何复核
    assert list_reviews(client, tid).json() == []


# ---------- 二、request_id 幂等 ----------

def test_create_review_idempotent_replay(client):
    tid, players, rnd, real = setup_closed_round(client)
    m = real[0]
    body = review_body(client, tid)
    r1 = create_review(client, m["id"], **body)
    assert r1.status_code == 201
    rid = r1.json()["id"]
    seq = event_seq(client, tid)

    r2 = create_review(client, m["id"], **body)
    # 重放返回首次结果，HTTP 201（与公告模块一致），无新事件
    assert r2.status_code == 201
    assert r2.json()["id"] == rid
    assert event_seq(client, tid) == seq
    assert len(events_of(client, tid, "result_review.created")) == 1


def test_create_review_same_request_id_different_params_conflict(client):
    tid, players, rnd, real = setup_closed_round(client)
    m = real[0]
    body = review_body(client, tid)
    create_review(client, m["id"], **body)

    r = create_review(
        client, m["id"], **{**body, "score_white": 0.5, "score_black": 0.5}
    )
    assert r.status_code == 409


# ---------- 三、原比分冻结 ----------

def test_pending_review_freezes_result_edits(client):
    # open 轮中一场已提交但整轮未封盘的对局，复核期间同样冻结比分
    tid = create_tournament(client)
    add_players(client, tid, ["甲", "乙", "丙", "丁"])
    rnd = next_round(client, tid)
    matches = [m for m in rnd["matches"] if not m["is_bye"]]
    put_result(client, matches[0], 1.0, 0.0)

    r = create_review(
        client,
        matches[0]["id"],
        **review_body(client, tid),
    )
    assert r.status_code == 201, r.text

    # 待处理期间该场比分不可修改
    r = client.put(
        f"/matches/{matches[0]['id']}/result",
        json={"score_white": 0.0, "score_black": 1.0},
    )
    assert r.status_code == 409
    # 原比分保持不变
    rounds = client.get(f"/tournaments/{tid}/rounds").json()
    stored = next(
        m for m in rounds[0]["matches"] if m["id"] == matches[0]["id"]
    )
    assert stored["score_white"] == 1.0
    assert stored["score_black"] == 0.0


def test_pending_review_blocks_next_round(client):
    tid, players, rnd, real = setup_closed_round(client)
    create_review(client, real[0]["id"], **review_body(client, tid))

    r = client.post(f"/tournaments/{tid}/rounds/next")
    assert r.status_code == 409
    assert "复核" in r.json()["detail"]
    # 依旧只有一轮
    assert len(client.get(f"/tournaments/{tid}/rounds").json()) == 1


# ---------- 四、双方决定 ----------

def _open_review(client, tid, match_):
    r = create_review(client, match_["id"], **review_body(client, tid))
    assert r.status_code == 201, r.text
    return r.json()


def test_non_player_cannot_decide(client):
    tid, players, rnd, real = setup_closed_round(client, n=6)
    m = real[0]
    rv = _open_review(client, tid, m)
    players_of_match = {
        m["player_white"]["id"], m["player_black"]["id"]
    }
    outsider = next(p["id"] for p in players if p["id"] not in players_of_match)

    r = decide(client, rv["id"], outsider, "approve")
    assert r.status_code == 409
    # 不存在的参赛者 404
    r = decide(client, rv["id"], 9999, "approve")
    assert r.status_code == 404


def test_decide_nonexistent_review_404(client):
    tid = create_tournament(client)
    add_players(client, tid, ["甲", "乙"])
    r = decide(client, 9999, 1, "approve")
    assert r.status_code == 404


def test_any_rejection_closes_review_and_keeps_score(client):
    tid, players, rnd, real = setup_closed_round(client)
    m = real[0]
    w, b = m["player_white"]["id"], m["player_black"]["id"]
    rv = _open_review(client, tid, m)
    seq = event_seq(client, tid)

    # 白方同意（仍是 pending）
    r = decide(client, rv["id"], w, "approve")
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "pending"
    assert len(r.json()["decisions"]) == 1
    # 单方同意不产生关闭事件、不改比分
    assert event_seq(client, tid) == seq

    # 黑方驳回 -> rejected 关闭，比分不变
    r = decide(client, rv["id"], b, "reject")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "rejected"
    assert body["closed_at"] is not None
    assert len(body["decisions"]) == 2

    # 比分保持原值（白胜）
    rounds = client.get(f"/tournaments/{tid}/rounds").json()
    stored = next(x for x in rounds[0]["matches"] if x["id"] == m["id"])
    assert stored["score_white"] == 1.0
    assert stored["score_black"] == 0.0

    # 关闭事件
    evs = events_of(client, tid, "result_review.closed")
    assert len(evs) == 1
    assert evs[0]["payload"]["outcome"] == "rejected"
    assert evs[0]["payload"]["score_changed"] is False
    assert evs[0]["payload"]["rejected_by_participant_id"] == b


def test_both_approval_corrects_score_and_recomputes_standings(client):
    tid, players, rnd, real = setup_closed_round(client)
    m = real[0]
    w, b = m["player_white"]["id"], m["player_black"]["id"]

    before = standings_map(client, tid)
    assert before[w]["points"] == 1.0
    assert before[b]["points"] == 0.0

    rv = _open_review(client, tid, m)
    seq = event_seq(client, tid)

    r1 = decide(client, rv["id"], w, "approve")
    assert r1.status_code == 201
    assert r1.json()["status"] == "pending"

    r2 = decide(client, rv["id"], b, "approve")
    assert r2.status_code == 201, r2.text
    body = r2.json()
    assert body["status"] == "approved"
    assert body["closed_at"] is not None

    # 比分已更正为黑胜
    rounds = client.get(f"/tournaments/{tid}/rounds").json()
    stored = next(x for x in rounds[0]["matches"] if x["id"] == m["id"])
    assert stored["score_white"] == 0.0
    assert stored["score_black"] == 1.0

    # 可见排名已重算：黑方 1 分、白方 0 分
    after = standings_map(client, tid)
    assert after[b]["points"] == 1.0
    assert after[w]["points"] == 0.0

    # 关闭事件（approved，含原比分与新比分），序号 +1
    assert event_seq(client, tid) == seq + 1
    evs = events_of(client, tid, "result_review.closed")
    assert len(evs) == 1
    payload = evs[0]["payload"]
    assert payload["outcome"] == "approved"
    assert payload["score_changed"] is True
    assert payload["original_score_white"] == 1.0
    assert payload["score_white"] == 0.0
    assert payload["score_black"] == 1.0

    # 关闭后可以生成下一轮
    r = client.post(f"/tournaments/{tid}/rounds/next")
    assert r.status_code == 201, r.text


def test_duplicate_decision_returns_original_without_extra_record(client):
    tid, players, rnd, real = setup_closed_round(client)
    m = real[0]
    w = m["player_white"]["id"]
    rv = _open_review(client, tid, m)
    seq = event_seq(client, tid)

    r1 = decide(client, rv["id"], w, "approve")
    assert r1.status_code == 201
    first = r1.json()["decisions"][0]

    r2 = decide(client, rv["id"], w, "approve")
    assert r2.status_code == 200  # 重复决定
    decs = r2.json()["decisions"]
    assert len(decs) == 1  # 无额外记录
    assert decs[0]["id"] == first["id"]
    assert decs[0]["decided_at"] == first["decided_at"]
    assert decs[0]["updated_at"] == first["updated_at"]
    assert event_seq(client, tid) == seq  # 不产生事件


def test_vote_change_in_place_during_pending(client):
    # open 轮场景：一方先同意（不关闭），再改票为驳回 -> 原地更新、
    # 立即 rejected，且不产生额外决定记录
    tid = create_tournament(client)
    add_players(client, tid, ["甲", "乙", "丙", "丁"])
    rnd = next_round(client, tid)
    mm = [m for m in rnd["matches"] if not m["is_bye"]][0]
    put_result(client, mm, 1.0, 0.0)
    rv = _open_review(client, tid, mm)
    w2 = mm["player_white"]["id"]

    r = decide(client, rv["id"], w2, "approve")
    assert r.status_code == 201
    assert r.json()["status"] == "pending"
    first = r.json()["decisions"][0]

    # 改票为驳回：原地更新且立即关闭
    r = decide(client, rv["id"], w2, "reject")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "rejected"
    wdec = next(d for d in body["decisions"] if d["participant_id"] == w2)
    assert wdec["vote"] == "reject"
    assert len(body["decisions"]) == 1
    assert wdec["id"] == first["id"]
    assert wdec["updated_at"] >= wdec["decided_at"]

    # 比分不变
    rounds = client.get(f"/tournaments/{tid}/rounds").json()
    stored = next(x for x in rounds[0]["matches"] if x["id"] == mm["id"])
    assert stored["score_white"] == 1.0
    assert stored["score_black"] == 0.0


def test_rejection_immediately_closes_with_single_decision(client):
    tid, players, rnd, real = setup_closed_round(client)
    m = real[0]
    w = m["player_white"]["id"]
    rv = _open_review(client, tid, m)

    r = decide(client, rv["id"], w, "reject")
    assert r.status_code == 201
    assert r.json()["status"] == "rejected"
    # 只有一条决定也已关闭
    assert len(r.json()["decisions"]) == 1


def test_decisions_after_closed(client):
    tid, players, rnd, real = setup_closed_round(client)
    m = real[0]
    w, b = m["player_white"]["id"], m["player_black"]["id"]
    rv = _open_review(client, tid, m)
    decide(client, rv["id"], w, "reject")
    closed_seq = event_seq(client, tid)

    # 已决定者重复提交：返回原复核（200，无写入无事件）
    r = decide(client, rv["id"], w, "reject")
    assert r.status_code == 200
    assert r.json()["status"] == "rejected"
    assert event_seq(client, tid) == closed_seq

    # 已决定者即便换票也返回原结果（关闭后不可改）
    r = decide(client, rv["id"], w, "approve")
    assert r.status_code == 200
    assert r.json()["status"] == "rejected"

    # 未决定者在关闭后决定：409
    r = decide(client, rv["id"], b, "approve")
    assert r.status_code == 409


# ---------- 五、关闭后的后续行为 ----------

def test_new_review_allowed_after_rejection_closed(client):
    tid, players, rnd, real = setup_closed_round(client)
    m = real[0]
    rv = _open_review(client, tid, m)
    w = m["player_white"]["id"]
    decide(client, rv["id"], w, "reject")

    # 关闭后可就同场再申请（新 request_id、最新快照）
    r = create_review(
        client,
        m["id"],
        **review_body(
            client,
            tid,
            request_id="rev-again",
            score_white=0.5,
            score_black=0.5,
            reason="仲裁组复查后建议和棋",
        ),
    )
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "pending"
    # 原比分仍冻结为该场当时比分
    assert r.json()["original_score_white"] == 1.0


def test_closed_review_detail_preserves_full_audit_trail(client):
    tid, players, rnd, real = setup_closed_round(client)
    m = real[0]
    w, b = m["player_white"]["id"], m["player_black"]["id"]
    rv = _open_review(client, tid, m)
    decide(client, rv["id"], w, "approve")
    decide(client, rv["id"], b, "approve")

    r = get_review(client, rv["id"])
    assert r.status_code == 200
    body = r.json()
    # 原比分、提议、双方决定、最终状态全部保留
    assert body["original_score_white"] == 1.0
    assert body["original_score_black"] == 0.0
    assert body["proposed_score_white"] == 0.0
    assert body["proposed_score_black"] == 1.0
    assert body["reason"]
    assert body["status"] == "approved"
    votes = {d["participant_id"]: d["vote"] for d in body["decisions"]}
    assert votes == {w: "approve", b: "approve"}
    assert body["closed_at"] is not None


def test_review_list_per_tournament(client):
    tid, players, rnd, real = setup_closed_round(client, n=6)
    # 第 1 场：驳回关闭；第 2 场：保持 pending
    rv1 = _open_review(client, tid, real[0])
    decide(client, rv1["id"], real[0]["player_white"]["id"], "reject")
    rv2 = _open_review_for(client, tid, real[1], request_id="rev-x")

    r = list_reviews(client, tid)
    assert r.status_code == 200
    items = r.json()
    assert [x["id"] for x in items] == [rv1["id"], rv2["id"]]
    assert items[0]["status"] == "rejected"
    assert items[1]["status"] == "pending"


def _open_review_for(client, tid, match_, request_id="rev-1"):
    body = review_body(client, tid, request_id=request_id)
    r = create_review(client, match_["id"], **body)
    assert r.status_code == 201, r.text
    return r.json()


# ---------- 六、并发 ----------

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


def test_concurrent_review_creation_only_one_succeeds(live_server_factory):
    """同一对局并发创建复核：恰好一条 pending，其余 409。"""
    with live_server_factory() as srv:
        client = srv.client()
        tid, players, rnd, real = setup_closed_round(client)
        mid = real[0]["id"]
        seq = event_seq(client, tid)
        n = 6
        barrier = threading.Barrier(n)

        def call(i):
            local = srv.client()
            try:
                barrier.wait(timeout=10)
                r = local.post(
                    f"/matches/{mid}/result-reviews",
                    json=review_body(client, tid, request_id=f"c-{i}"),
                )
                return r.status_code
            finally:
                local.close()

        statuses = _run_concurrently(call, n, barrier)
        assert statuses.count(201) == 1
        assert statuses.count(409) == n - 1

        items = list_reviews(client, tid).json()
        pending = [x for x in items if x["status"] == "pending"]
        assert len(pending) == 1
        # 仅一次创建占用一个事件序号
        assert event_seq(client, tid) == seq + 1


def test_concurrent_duplicate_decisions_create_single_record(live_server_factory):
    """同一选手对同一复核并发重复同意：只有一条决定记录。"""
    with live_server_factory() as srv:
        client = srv.client()
        tid, players, rnd, real = setup_closed_round(client)
        w = real[0]["player_white"]["id"]
        rv = _open_review(client, tid, real[0])
        # 仅让白方并发提交相同 approve（黑方不参与，复核保持 pending）
        n = 6
        barrier = threading.Barrier(n)

        def call(i):
            local = srv.client()
            try:
                barrier.wait(timeout=10)
                r = local.post(
                    f"/result-reviews/{rv['id']}/decisions",
                    json={"participant_id": w, "vote": "approve"},
                )
                return r.status_code
            finally:
                local.close()

        statuses = _run_concurrently(call, n, barrier)
        assert statuses.count(201) == 1
        assert statuses.count(200) == n - 1

        body = get_review(client, rv["id"]).json()
        wdec = [d for d in body["decisions"] if d["participant_id"] == w]
        assert len(wdec) == 1  # 并发重复不产生额外记录
        assert body["status"] == "pending"


# ---------- 实时服务器夹具（与 test_events 同款，后台 uvicorn） ----------

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
