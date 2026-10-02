"""现场报到签到快照与缺席者编排测试。"""


def create_tournament(client, name="签到测试赛"):
    r = client.post("/tournaments", json={"name": name})
    assert r.status_code == 201
    return r.json()["id"]


def add_players(client, tid, names):
    r = client.post(
        f"/tournaments/{tid}/participants/bulk", json={"names": names}
    )
    assert r.status_code == 201, r.text
    return r.json()


def put_result(client, match, sw, sb):
    r = client.put(
        f"/matches/{match['id']}/result",
        json={"score_white": sw, "score_black": sb},
    )
    assert r.status_code == 200, r.text
    return r.json()


def event_count(client, tid):
    page = client.get(
        f"/tournaments/{tid}/events",
        params={"after": 0, "limit": 200},
    )
    assert page.status_code == 200
    assert page.json()["has_more"] is False
    items = page.json()["items"]
    return items[-1]["seq"] if items else 0


def submit_snapshot(client, tid, round_no, present, absent, request_id="snap", seq=None):
    if seq is None:
        seq = event_count(client, tid)
    r = client.put(
        f"/tournaments/{tid}/checkin-snapshots",
        json={
            "request_id": request_id,
            "expected_event_seq": seq,
            "round_no": round_no,
            "present_numbers": present,
            "absent_numbers": absent,
        },
    )
    return r


def confirm_snapshot(client, tid, round_no, request_id="confirm", seq=None):
    if seq is None:
        seq = event_count(client, tid)
    return client.post(
        f"/tournaments/{tid}/checkin-snapshots/confirm",
        json={
            "request_id": request_id,
            "expected_event_seq": seq,
            "round_no": round_no,
        },
    )


def test_snapshot_version_query_idempotency_and_confirmation(client):
    tid = create_tournament(client)
    players = add_players(client, tid, ["A", "B", "C"])
    base_seq = event_count(client, tid)

    r = submit_snapshot(client, tid, 1, [1, 2], [3], "s1")
    assert r.status_code == 200, r.text
    snap = r.json()
    assert snap["version"] == 1
    assert snap["confirmed"] is False
    assert snap["consumed"] is False
    assert snap["present_numbers"] == [1, 2]
    assert snap["absent_numbers"] == [3]
    assert [(p["number"], p["attended"]) for p in snap["roster"]] == [
        (1, True),
        (2, True),
        (3, False),
    ]

    # 相同 request_id 重放：返回首次结果，不新增版本/事件
    replay = submit_snapshot(client, tid, 1, [1, 2], [3], "s1", base_seq)
    assert replay.status_code == 200
    assert replay.json() == snap
    assert event_count(client, tid) == base_seq + 1

    # 新的提交更新同一份快照，版本 +1 并重置确认
    r = submit_snapshot(client, tid, 1, [1], [2, 3], "s2", base_seq + 1)
    assert r.status_code == 200
    assert r.json()["version"] == 2
    assert r.json()["present_numbers"] == [1]

    r = confirm_snapshot(client, tid, 1, "c1", base_seq + 2)
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 2
    assert r.json()["confirmed"] is True
    assert r.json()["confirmed_at"] is not None

    replay = confirm_snapshot(client, tid, 1, "c1", base_seq + 2)
    assert replay.status_code == 200
    assert replay.json() == r.json()
    assert event_count(client, tid) == base_seq + 3

    # 查询返回版本和完整名单
    got = client.get(f"/tournaments/{tid}/checkin-snapshots/1")
    assert got.status_code == 200
    assert got.json()["version"] == 2
    assert len(client.get(f"/tournaments/{tid}/checkin-snapshots").json()) == 1

    events = client.get(f"/tournaments/{tid}/events").json()["items"]
    assert [e["type"] for e in events[-3:]] == [
        "checkin.snapshot_changed",
        "checkin.snapshot_changed",
        "checkin.snapshot_confirmed",
    ]
    changed = events[-2]
    assert changed["payload"]["version"] == 2
    assert changed["payload"]["present_numbers"] == [1]


def test_invalid_snapshots_are_rejected_without_events_or_actions(client):
    tid = create_tournament(client)
    add_players(client, tid, ["A", "B", "C"])
    base_seq = event_count(client, tid)

    def bad(present, absent, req):
        return submit_snapshot(client, tid, 1, present, absent, req, base_seq)

    assert bad([1, 1, 2], [3], "dup-present").status_code == 409
    assert bad([1], [2, 2], "dup-absent").status_code == 409
    assert bad([1, 2], [2], "overlap").status_code == 409
    assert bad([1, 2], [], "missing").status_code == 409
    assert bad([1, 2], [4], "unknown").status_code == 409
    assert bad([1, 2, 3], [], "stale").status_code == 200
    assert (
        submit_snapshot(
            client, tid, 1, [1, 2], [3], "stale-seq", base_seq
        ).status_code
        == 409
    )
    # 同 request_id 不同参数
    assert (
        submit_snapshot(
            client, tid, 1, [1], [2, 3], "stale", base_seq + 1
        ).status_code
        == 409
    )

    # 只有第一次合法提交产生一个事件；拒绝请求不留动作/事件
    assert event_count(client, tid) == base_seq + 1
    listing = client.get(f"/tournaments/{tid}/checkin-snapshots").json()
    assert listing[0]["version"] == 1


def test_only_present_players_are_paired_and_absent_keeps_history(client):
    tid = create_tournament(client)
    add_players(client, tid, ["A", "B", "C", "D", "E"])

    # 首轮无快照：全员编排。5 号轮空；1-2、3-4 均由 1/3 胜
    r1 = client.post(f"/tournaments/{tid}/rounds/next")
    assert r1.status_code == 201
    r1 = r1.json()
    real1 = [m for m in r1["matches"] if not m["is_bye"]]
    put_result(client, real1[0], 1.0, 0.0)
    put_result(client, real1[1], 1.0, 0.0)

    # 第 2 轮：5 号缺席。到场 1、2、3、4；1/3 同为 1 分，2/4 同为 0 分
    seq = event_count(client, tid)
    r = submit_snapshot(
        client, tid, 2, [1, 2, 3, 4], [5], "r2", seq
    )
    assert r.status_code == 200
    r2 = client.post(f"/tournaments/{tid}/rounds/next").json()
    pairs = [
        (m["player_white"]["number"], m["player_black"]["number"])
        for m in r2["matches"]
        if not m["is_bye"]
    ]
    assert pairs == [(1, 3), (2, 4)]
    assert all(5 not in pair for pair in pairs)
    assert not any(m["is_bye"] for m in r2["matches"])

    # 生成后快照冻结，变更/确认拒绝且不留事件
    gen_seq = event_count(client, tid)
    assert (
        submit_snapshot(
            client, tid, 2, [1, 2, 3], [4, 5], "late", gen_seq
        ).status_code
        == 409
    )
    assert confirm_snapshot(client, tid, 2, "late-confirm", gen_seq).status_code == 409
    assert event_count(client, tid) == gen_seq

    snap = client.get(f"/tournaments/{tid}/checkin-snapshots/2").json()
    assert snap["consumed"] is True
    assert snap["present_numbers"] == [1, 2, 3, 4]
    gen_event = client.get(
        f"/tournaments/{tid}/events", params={"after": gen_seq - 1, "limit": 1}
    ).json()["items"][0]
    assert gen_event["type"] == "round.generated"
    assert gen_event["payload"]["checkin_snapshot_id"] == snap["id"]
    assert gen_event["payload"]["absent_numbers"] == [5]

    # 第 2 轮结束。缺席的 5 号仍保留首轮轮空 1 分，且没有第 2 轮积分/对手
    r2_real = [m for m in r2["matches"] if not m["is_bye"]]
    put_result(client, r2_real[0], 1.0, 0.0)
    put_result(client, r2_real[1], 1.0, 0.0)
    standings = client.get(f"/tournaments/{tid}/standings").json()
    fifth = next(s for s in standings if s["number"] == 5)
    assert fifth["points"] == 1.0
    assert fifth["played"] == 0
    assert fifth["byes"] == 1

    rounds = client.get(f"/tournaments/{tid}/rounds").json()
    assert len(rounds) == 2


def test_snapshot_with_one_present_creates_bye_and_zero_present_closes(client):
    tid = create_tournament(client)
    add_players(client, tid, ["A", "B", "C"])

    seq = event_count(client, tid)
    r = submit_snapshot(client, tid, 1, [2], [1, 3], "one", seq)
    assert r.status_code == 200
    rnd = client.post(f"/tournaments/{tid}/rounds/next").json()
    assert rnd["status"] == "closed"
    assert len(rnd["matches"]) == 1
    assert rnd["matches"][0]["is_bye"] is True
    assert rnd["matches"][0]["player_white"]["number"] == 2

    standings = client.get(f"/tournaments/{tid}/standings").json()
    by_number = {s["number"]: s for s in standings}
    assert by_number[2]["points"] == 1.0
    assert by_number[1]["points"] == 0.0
    assert by_number[3]["points"] == 0.0

    # 已结束的第 1 轮不能再提交快照；下一轮快照只能面向第 2 轮
    now_seq = event_count(client, tid)
    assert (
        submit_snapshot(client, tid, 1, [1, 2], [3], "old-round", now_seq).status_code
        == 409
    )
    assert (
        submit_snapshot(client, tid, 3, [1, 2], [3], "future-round", now_seq).status_code
        == 409
    )
    r2_snapshot = submit_snapshot(
        client, tid, 2, [], [1, 2, 3], "none-present", now_seq
    )
    assert r2_snapshot.status_code == 200
    r2 = client.post(f"/tournaments/{tid}/rounds/next").json()
    assert r2["status"] == "closed"
    assert r2["matches"] == []
    # 全员缺席不获得积分
    assert all(s["points"] == 0.0 for s in client.get(
        f"/tournaments/{tid}/standings"
    ).json() if s["number"] in {1, 3})
