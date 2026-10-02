"""晋级名单模块测试。

覆盖需求：
- 发布：仅允许在所有已生成轮次结束且没有待处理赛果复核时，按
  request_id / expected_event_seq / 资格轮次 / 晋级名额发布；同一事务
  内重算排名积分与 Buchholz，按排名积分 → Buchholz → 编号固定裁决，
  冻结前 N 名席位与候补顺序；资格轮次不存在、名额非法、轮次未结束、
  有待处理复核、快照冲突均 409（或参数级 422）且不落库、不占号；
- 冻结：发布后排名与扣分变化不改写该版本；不同资格轮次/版本各自冻结；
- 放弃：只有当前有效晋级者可标记放弃，同一事务内最前候补递补该席位，
  无候补时保持空席；重复 request_id 返回首次结果；名单冻结后即使轮次
  进行中也可标记放弃；
- 查询：赛事全部版本与指定版本，含每席位原始/当前参赛者、状态与依据；
- 事件：promotion.published / promotion.substituted 进入事件流，
  被拒绝的请求不产生事件；
- 并发：并发放弃同一席位仅一成一败；并发同键不同参数发布仅一成落库。
"""
import concurrent.futures
import threading

import pytest


# ---------- 基础工具 ----------

def create_tournament(client, name="晋级测试赛"):
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


def publish(client, tid, **body):
    return client.post(f"/tournaments/{tid}/promotions", json=body)


def forfeit(client, list_id, **body):
    return client.post(f"/promotion-lists/{list_id}/forfeits", json=body)


def publish_body(client, tid, **over):
    body = {
        "request_id": "prom-1",
        "expected_event_seq": event_seq(client, tid),
        "qualifying_round_no": 1,
        "slots": 2,
    }
    body.update(over)
    return body


def forfeit_body(**over):
    body = {"request_id": "forfeit-1", "participant_id": 1, "reason": "个人原因退赛"}
    body.update(over)
    return body


def setup_closed_round(client, n=5):
    """n 人赛事，第 1 轮全部以白胜录入并封盘。返回 (tid, players, real)。

    5 人时第 1 轮为 (1,2)、(3,4)、5 轮空；全部白胜后排名为
    1、3、5（1 分）→ 2、4（0 分，Buchholz 1.0）。
    """
    tid = create_tournament(client)
    players = add_players(client, tid, [f"选手{i}" for i in range(1, n + 1)])
    rnd = next_round(client, tid)
    real = [m for m in rnd["matches"] if not m["is_bye"]]
    for m in real:
        put_result(client, m, 1.0, 0.0)
    rounds = client.get(f"/tournaments/{tid}/rounds").json()
    assert rounds[-1]["status"] == "closed"
    return tid, players, real


def get_list(client, tid, version):
    return client.get(f"/tournaments/{tid}/promotions/{version}")


def list_lists(client, tid):
    return client.get(f"/tournaments/{tid}/promotions")


# ---------- 一、发布 ----------

def test_publish_success_freezes_seats_waitlist_and_event(client):
    tid, players, real = setup_closed_round(client)  # 5 人
    seq = event_seq(client, tid)

    r = publish(client, tid, **publish_body(client, tid, slots=2))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["tournament_id"] == tid
    assert body["version"] == 1
    assert body["qualifying_round_no"] == 1
    assert body["slots"] == 2
    assert body["expected_event_seq"] == seq
    assert body["created_at"] is not None

    # 冻结裁决：排名积分 → Buchholz → 编号；前 2 名为席位，其余为候补
    seats = body["seats"]
    assert [s["seat_no"] for s in seats] == [1, 2]
    assert [s["original"]["participant_id"] for s in seats] == [1, 3]
    assert [s["current"]["participant_id"] for s in seats] == [1, 3]
    assert all(s["status"] == "active" for s in seats)
    assert all(s["current"]["source"] == "original" for s in seats)
    # 冻结依据：1 分、Buchholz 0（对手为 0 分选手）
    assert seats[0]["original"]["rank"] == 1
    assert seats[0]["original"]["rank_points"] == 1.0
    assert seats[0]["original"]["buchholz"] == 0.0

    waitlist = body["waitlist"]
    assert [w["position"] for w in waitlist] == [1, 2, 3]
    assert [w["participant"]["participant_id"] for w in waitlist] == [5, 2, 4]
    assert [w["participant"]["rank"] for w in waitlist] == [3, 4, 5]
    assert all(w["status"] == "waiting" for w in waitlist)

    # 事件同事务写入，序号恰好 +1
    assert event_seq(client, tid) == seq + 1
    evs = events_of(client, tid, "promotion.published")
    assert len(evs) == 1
    payload = evs[0]["payload"]
    assert payload["list_id"] == body["id"]
    assert payload["version"] == 1
    assert payload["qualifying_round_no"] == 1
    assert payload["slots"] == 2
    assert [s["participant_id"] for s in payload["seats"]] == [1, 3]
    assert [w["participant_id"] for w in payload["waitlist"]] == [5, 2, 4]

    # 查询：赛事列表与指定版本返回同一冻结内容
    assert list_lists(client, tid).json() == [body]
    assert get_list(client, tid, 1).json() == body


def test_publish_blocked_while_round_open(client):
    tid = create_tournament(client)
    add_players(client, tid, ["甲", "乙", "丙", "丁"])
    next_round(client, tid)  # 轮次 open，未录赛果
    seq = event_seq(client, tid)

    r = publish(client, tid, **publish_body(client, tid))
    assert r.status_code == 409
    assert "尚未结束" in r.json()["detail"]
    assert event_seq(client, tid) == seq  # 不落库、不占号
    assert list_lists(client, tid).json() == []


def test_publish_blocked_by_pending_result_review(client):
    tid, players, real = setup_closed_round(client, n=4)
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

    r = publish(client, tid, **publish_body(client, tid))
    assert r.status_code == 409
    assert "复核" in r.json()["detail"]
    assert event_seq(client, tid) == seq
    assert list_lists(client, tid).json() == []


def test_publish_invalid_qualifying_round(client):
    tid, players, real = setup_closed_round(client, n=4)
    seq = event_seq(client, tid)

    # 资格轮次不存在（只生成了第 1 轮）
    r = publish(client, tid, **publish_body(client, tid, qualifying_round_no=2))
    assert r.status_code == 409
    assert "资格轮次" in r.json()["detail"]
    # 参数级非法
    r = publish(client, tid, **publish_body(client, tid, qualifying_round_no=0))
    assert r.status_code == 422
    assert event_seq(client, tid) == seq
    assert list_lists(client, tid).json() == []


def test_publish_invalid_slots(client):
    tid, players, real = setup_closed_round(client, n=4)
    seq = event_seq(client, tid)

    # 名额超过参赛人数
    r = publish(client, tid, **publish_body(client, tid, slots=5))
    assert r.status_code == 409
    assert "名额" in r.json()["detail"]
    # 参数级非法
    r = publish(client, tid, **publish_body(client, tid, slots=0))
    assert r.status_code == 422
    assert event_seq(client, tid) == seq
    assert list_lists(client, tid).json() == []


def test_publish_snapshot_conflict_rejects_without_write(client):
    tid, players, real = setup_closed_round(client)
    seq = event_seq(client, tid)

    r = publish(
        client, tid, **publish_body(client, tid, expected_event_seq=seq - 1)
    )
    assert r.status_code == 409
    assert "快照" in r.json()["detail"]
    assert event_seq(client, tid) == seq
    assert list_lists(client, tid).json() == []


def test_publish_idempotent_replay_and_conflict(client):
    tid, players, real = setup_closed_round(client)
    body = publish_body(client, tid)
    r1 = publish(client, tid, **body)
    assert r1.status_code == 201
    seq = event_seq(client, tid)

    # 重放：原样返回首次结果，无新事件、无新版本
    r2 = publish(client, tid, **body)
    assert r2.status_code == 201
    assert r2.json() == r1.json()
    assert event_seq(client, tid) == seq
    assert len(events_of(client, tid, "promotion.published")) == 1
    assert len(list_lists(client, tid).json()) == 1

    # 同键不同参数 -> 409
    r = publish(client, tid, **{**body, "slots": 3})
    assert r.status_code == 409
    r = publish(client, tid, **{**body, "qualifying_round_no": 1, "slots": 2,
                                    "expected_event_seq": seq})
    assert r.status_code == 409  # expected_event_seq 变化也算不同参数
    assert event_seq(client, tid) == seq
    assert len(list_lists(client, tid).json()) == 1


def test_publish_frozen_against_later_penalty(client):
    """发布后扣分改变排名，已发布版本的冻结依据不改写；新版本按当时排名冻结。"""
    tid, players, real = setup_closed_round(client)
    r1 = publish(client, tid, **publish_body(client, tid, slots=1))
    assert r1.status_code == 201
    assert r1.json()["seats"][0]["original"]["participant_id"] == 1
    assert r1.json()["seats"][0]["original"]["rank_points"] == 1.0

    # 给 1 号扣 1.0 分：排名积分变为 0，排名跌出榜首
    r = client.post(
        f"/tournaments/{tid}/penalties",
        json={
            "request_id": "pen-1",
            "expected_event_seq": event_seq(client, tid),
            "participant_id": 1,
            "points": 1.0,
            "reason": "违规",
        },
    )
    assert r.status_code == 201

    # 已发布版本不变
    v1 = get_list(client, tid, 1).json()
    assert v1["seats"][0]["original"]["participant_id"] == 1
    assert v1["seats"][0]["original"]["rank_points"] == 1.0
    assert v1["seats"][0]["original"]["rank"] == 1

    # 新版本按当时排名重算：3 号居首（1 号排名积分 0）
    r2 = publish(
        client,
        tid,
        **publish_body(
            client, tid, request_id="prom-2", slots=1,
        ),
    )
    assert r2.status_code == 201, r2.text
    assert r2.json()["version"] == 2
    assert r2.json()["seats"][0]["original"]["participant_id"] == 3
    # 1 号以 0 分进入候补，依据冻结为扣分后的值
    demoted = [
        w for w in r2.json()["waitlist"]
        if w["participant"]["participant_id"] == 1
    ]
    assert len(demoted) == 1
    assert demoted[0]["participant"]["rank_points"] == 0.0
    # v1 依旧不变
    assert get_list(client, tid, 1).json() == v1


def test_publish_qualifying_round_bounds_ranking(client):
    """资格轮次决定排名口径：只统计该轮（含）之前的已结束轮次。"""
    tid = create_tournament(client)
    add_players(client, tid, ["甲", "乙", "丙", "丁"])
    rnd1 = next_round(client, tid)  # (1,2)、(3,4)
    for m in rnd1["matches"]:
        put_result(client, m, 1.0, 0.0)  # 1>2、3>4
    rnd2 = next_round(client, tid)  # (1,3)、(2,4)
    real2 = sorted(
        (m for m in rnd2["matches"] if not m["is_bye"]),
        key=lambda m: m["player_white"]["id"],
    )
    assert [m["player_white"]["id"] for m in real2] == [1, 2]
    put_result(client, real2[0], 0.0, 1.0)  # 3 胜 1
    put_result(client, real2[1], 1.0, 0.0)  # 2 胜 4
    rounds = client.get(f"/tournaments/{tid}/rounds").json()
    assert all(r["status"] == "closed" for r in rounds)

    # 以第 1 轮为资格轮次：1、3 居首（各 1 分）
    r1 = publish(
        client,
        tid,
        **publish_body(client, tid, qualifying_round_no=1, slots=2),
    )
    assert r1.status_code == 201, r1.text
    assert [s["original"]["participant_id"] for s in r1.json()["seats"]] == [1, 3]
    assert [w["participant"]["participant_id"] for w in r1.json()["waitlist"]] == [2, 4]

    # 以第 2 轮为资格轮次：3 号 2 分居首，1 号 1 分（Buchholz 3.0）次之
    r2 = publish(
        client,
        tid,
        **publish_body(
            client, tid, request_id="prom-2", qualifying_round_no=2, slots=2
        ),
    )
    assert r2.status_code == 201, r2.text
    assert r2.json()["version"] == 2
    assert [s["original"]["participant_id"] for s in r2.json()["seats"]] == [3, 1]
    seat2 = r2.json()["seats"][1]["original"]
    assert seat2["rank_points"] == 1.0
    assert seat2["buchholz"] == 3.0
    # v1 不受 v2 影响
    assert get_list(client, tid, 1).json() == r1.json()


def test_publish_tournament_not_found(client):
    r = publish(client, 9999, **{
        "request_id": "prom-x", "expected_event_seq": 0,
        "qualifying_round_no": 1, "slots": 1,
    })
    assert r.status_code == 404
    r = list_lists(client, 9999)
    assert r.status_code == 404


# ---------- 二、放弃与递补 ----------

def _published(client, tid, slots=2, request_id="prom-1", qualifying_round_no=1):
    r = publish(
        client,
        tid,
        **publish_body(
            client, tid,
            request_id=request_id, slots=slots,
            qualifying_round_no=qualifying_round_no,
        ),
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_forfeit_promotes_earliest_waitlist(client):
    tid, players, real = setup_closed_round(client)  # 席位 1、3；候补 5、2、4
    plist = _published(client, tid, slots=2)
    seq = event_seq(client, tid)

    r = forfeit(client, plist["id"], **forfeit_body(participant_id=1))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["list_id"] == plist["id"]
    assert body["version"] == 1
    assert body["seat_no"] == 1
    assert body["seat_status"] == "active"
    assert body["forfeited"]["participant_id"] == 1
    assert body["forfeited"]["rank"] == 1  # 放弃者的冻结依据
    assert body["forfeited"]["rank_points"] == 1.0
    assert body["promoted"]["participant_id"] == 5
    assert body["promoted"]["source"] == "waitlist"
    assert body["promoted"]["rank"] == 3  # 候补冻结依据
    assert body["reason"] == "个人原因退赛"

    # 事件同事务写入
    assert event_seq(client, tid) == seq + 1
    evs = events_of(client, tid, "promotion.substituted")
    assert len(evs) == 1
    payload = evs[0]["payload"]
    assert payload["list_id"] == plist["id"]
    assert payload["seat_no"] == 1
    assert payload["forfeited_participant_id"] == 1
    assert payload["promoted_participant_id"] == 5
    assert payload["seat_status"] == "active"

    # 名单反映：席位 1 当前持有者为 5 号（候补递补），原始持有者不变
    v = get_list(client, tid, 1).json()
    seat1 = v["seats"][0]
    assert seat1["original"]["participant_id"] == 1
    assert seat1["current"]["participant_id"] == 5
    assert seat1["current"]["source"] == "waitlist"
    assert seat1["status"] == "active"
    wl = {w["participant"]["participant_id"]: w for w in v["waitlist"]}
    assert wl[5]["status"] == "promoted"
    assert wl[2]["status"] == "waiting"

    # 链式递补：3 号放弃 -> 最前候补 2 号上位
    r = forfeit(
        client, plist["id"],
        **forfeit_body(request_id="forfeit-2", participant_id=3),
    )
    assert r.status_code == 201
    assert r.json()["seat_no"] == 2
    assert r.json()["promoted"]["participant_id"] == 2

    # 递补者再放弃 -> 下一个候补 4 号上位
    r = forfeit(
        client, plist["id"],
        **forfeit_body(request_id="forfeit-3", participant_id=2),
    )
    assert r.status_code == 201
    assert r.json()["seat_no"] == 2
    assert r.json()["forfeited"]["participant_id"] == 2
    assert r.json()["promoted"]["participant_id"] == 4


def test_forfeit_only_current_holder(client):
    tid, players, real = setup_closed_round(client)
    plist = _published(client, tid, slots=2)
    seq = event_seq(client, tid)

    # 候补不是当前有效晋级者
    r = forfeit(client, plist["id"], **forfeit_body(participant_id=5))
    assert r.status_code == 409
    assert "当前有效晋级者" in r.json()["detail"]
    # 从未入名单的参赛者不存在
    r = forfeit(client, plist["id"], **forfeit_body(participant_id=9999))
    assert r.status_code == 404
    # 名单不存在
    r = forfeit(client, 9999, **forfeit_body())
    assert r.status_code == 404
    assert event_seq(client, tid) == seq
    assert events_of(client, tid, "promotion.substituted") == []

    # 已放弃者不再是当前有效晋级者（新 request_id 也 409）
    r = forfeit(client, plist["id"], **forfeit_body(participant_id=1))
    assert r.status_code == 201
    seq = event_seq(client, tid)
    r = forfeit(
        client, plist["id"],
        **forfeit_body(request_id="forfeit-again", participant_id=1),
    )
    assert r.status_code == 409
    assert event_seq(client, tid) == seq


def test_forfeit_idempotent_replay_and_conflict(client):
    tid, players, real = setup_closed_round(client)
    plist = _published(client, tid, slots=2)
    body = forfeit_body(participant_id=1)
    r1 = forfeit(client, plist["id"], **body)
    assert r1.status_code == 201
    seq = event_seq(client, tid)

    # 重放：原样返回首次结果，席位不再次递补、无新事件
    r2 = forfeit(client, plist["id"], **body)
    assert r2.status_code == 201
    assert r2.json() == r1.json()
    assert event_seq(client, tid) == seq
    assert len(events_of(client, tid, "promotion.substituted")) == 1
    v = get_list(client, tid, 1).json()
    assert v["seats"][0]["current"]["participant_id"] == 5

    # 同键不同参数 -> 409
    r = forfeit(client, plist["id"], **{**body, "participant_id": 3})
    assert r.status_code == 409
    assert event_seq(client, tid) == seq


def test_forfeit_vacant_when_no_waitlist(client):
    tid, players, real = setup_closed_round(client, n=3)  # 席位 1、3、2，无候补
    plist = _published(client, tid, slots=3)
    assert plist["waitlist"] == []
    seq = event_seq(client, tid)

    r = forfeit(client, plist["id"], **forfeit_body(participant_id=3))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["seat_no"] == 2
    assert body["seat_status"] == "vacant"
    assert body["promoted"] is None

    # 事件记录空席递补结果
    assert event_seq(client, tid) == seq + 1
    payload = events_of(client, tid, "promotion.substituted")[0]["payload"]
    assert payload["promoted_participant_id"] is None
    assert payload["seat_status"] == "vacant"

    # 空席保持：当前持有者为 null，原始持有者与依据保留
    v = get_list(client, tid, 1).json()
    seat2 = v["seats"][1]
    assert seat2["status"] == "vacant"
    assert seat2["current"] is None
    assert seat2["original"]["participant_id"] == 3
    assert seat2["original"]["rank_points"] == 1.0


def test_forfeit_allowed_after_tournament_continues(client):
    """名单冻结后赛事继续（新一轮进行中）仍可标记放弃；名单内容不受影响。"""
    tid, players, real = setup_closed_round(client)
    plist = _published(client, tid, slots=2)
    next_round(client, tid)  # 第 2 轮 open

    r = forfeit(client, plist["id"], **forfeit_body(participant_id=1))
    assert r.status_code == 201, r.text
    assert r.json()["promoted"]["participant_id"] == 5

    v = get_list(client, tid, 1).json()
    assert v["seats"][0]["current"]["participant_id"] == 5
    assert v["seats"][0]["original"]["rank_points"] == 1.0  # 冻结依据不变


def test_forfeit_request_id_namespace_shared_with_publish(client):
    """publish 与 forfeit 共用赛事内 request_id 命名空间。"""
    tid, players, real = setup_closed_round(client)
    plist = _published(client, tid, slots=2, request_id="shared-key")
    seq = event_seq(client, tid)

    r = forfeit(client, plist["id"], **forfeit_body(request_id="shared-key"))
    assert r.status_code == 409
    assert event_seq(client, tid) == seq


# ---------- 三、查询 ----------

def test_get_version_not_found(client):
    tid, players, real = setup_closed_round(client)
    _published(client, tid, slots=2)
    r = get_list(client, tid, 2)
    assert r.status_code == 404
    r = get_list(client, 9999, 1)
    assert r.status_code == 404


def test_lists_isolated_per_tournament(client):
    tid1, _, _ = setup_closed_round(client)
    tid2, _, _ = setup_closed_round(client)
    _published(client, tid1, slots=2, request_id="p1")
    assert len(list_lists(client, tid1).json()) == 1
    assert list_lists(client, tid2).json() == []


# ---------- 四、并发 ----------

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


def test_concurrent_forfeit_same_seat_only_one_succeeds(live_server_factory):
    """并发放弃同一席位持有者：恰好一次递补，其余 409。"""
    with live_server_factory() as srv:
        client = srv.client()
        tid, players, real = setup_closed_round(client)
        plist = _published(client, tid, slots=2)
        seq = event_seq(client, tid)
        n = 6
        barrier = threading.Barrier(n)

        def call(i):
            local = srv.client()
            try:
                barrier.wait(timeout=10)
                r = local.post(
                    f"/promotion-lists/{plist['id']}/forfeits",
                    json=forfeit_body(request_id=f"ff-{i}", participant_id=1),
                )
                return r.status_code
            finally:
                local.close()

        statuses = _run_concurrently(call, n, barrier)
        assert statuses.count(201) == 1
        assert statuses.count(409) == n - 1

        # 只递补一次：席位 1 当前持有者为 5 号，候补 2、4 仍在等
        v = get_list(client, tid, 1).json()
        assert v["seats"][0]["current"]["participant_id"] == 5
        wl = {w["participant"]["participant_id"]: w["status"]
              for w in v["waitlist"]}
        assert wl == {5: "promoted", 2: "waiting", 4: "waiting"}
        assert len(events_of(client, tid, "promotion.substituted")) == 1
        assert event_seq(client, tid) == seq + 1


def test_concurrent_publish_same_key_conflict(live_server_factory):
    """并发以同一 request_id 发布不同参数：恰好一份名单落库，其余 409。"""
    with live_server_factory() as srv:
        client = srv.client()
        tid, players, real = setup_closed_round(client)
        seq = event_seq(client, tid)
        n = 5
        barrier = threading.Barrier(n)

        def call(i):
            local = srv.client()
            try:
                barrier.wait(timeout=10)
                r = local.post(
                    f"/tournaments/{tid}/promotions",
                    json=publish_body(
                        client, tid,
                        request_id="same-key", slots=i + 1,
                    ),
                )
                return r.status_code
            finally:
                local.close()

        statuses = _run_concurrently(call, n, barrier)
        assert statuses.count(201) == 1
        assert statuses.count(409) == n - 1
        assert len(list_lists(client, tid).json()) == 1
        assert event_seq(client, tid) == seq + 1


# ---------- 实时服务器夹具（与 test_penalties 同款，后台 uvicorn） ----------

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
