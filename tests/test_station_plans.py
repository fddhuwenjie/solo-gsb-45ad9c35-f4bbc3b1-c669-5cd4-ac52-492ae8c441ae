"""轮次工位分配模块测试。

覆盖：
- 工位维护：新增（单个/批量）、重复编号 409、更新可用状态与
  能力标签、标签规范化与校验；
- 参赛者必需标签：创建时登记、PUT 更新；
- 方案发布：expected_version 乐观并发、固定工位（pins）校验
  失败 409 且不落库、求解三目标（最多分配 → 最少换位 →
  (match_id, station_no) 字典序）、容量不足时保存最大可行方案
  并说明原因、按版本查询、station_plan.published 事件。
"""
import pytest


@pytest.fixture()
def live_server():
    """复用 test_events 的真实 uvicorn 服务器（并发请求测试用）。"""
    from test_events import LiveServer, reset_db

    reset_db()
    with LiveServer() as srv:
        yield srv


def create_tournament(client, name="工位赛"):
    r = client.post("/tournaments", json={"name": name})
    assert r.status_code == 201
    return r.json()["id"]


def add_players(client, tid, names):
    r = client.post(
        f"/tournaments/{tid}/participants/bulk", json={"names": names}
    )
    assert r.status_code == 201
    return r.json()


def add_stations(client, tid, stations):
    r = client.post(
        f"/tournaments/{tid}/stations/bulk", json={"stations": stations}
    )
    assert r.status_code == 201, r.text
    return r.json()


def set_tags(client, tid, number, tags):
    r = client.put(
        f"/tournaments/{tid}/participants/{number}/tags",
        json={"required_tags": tags},
    )
    assert r.status_code == 200, r.text
    return r.json()


def next_round(client, tid):
    r = client.post(f"/tournaments/{tid}/rounds/next")
    assert r.status_code == 201, r.text
    return r.json()


def put_result(client, match, sw, sb):
    r = client.put(
        f"/matches/{match['id']}/result",
        json={"score_white": sw, "score_black": sb},
    )
    assert r.status_code == 200, r.text
    return r.json()


def publish_plan(client, tid, round_no, expected_version, pins=None):
    return client.post(
        f"/tournaments/{tid}/rounds/{round_no}/station-plans",
        json={"expected_version": expected_version, "pins": pins or []},
    )


def assignments_of(plan):
    return {a["match_id"]: a["station_no"] for a in plan["assignments"]}


# ---------- 工位维护 ----------

def test_station_crud(client):
    tid = create_tournament(client)

    # 单个新增
    r = client.post(
        f"/tournaments/{tid}/stations",
        json={"station_no": 2, "tags": [" 直播 ", "直播", "解说"]},
    )
    assert r.status_code == 201
    body = r.json()
    # 标签去空白、去重、排序
    assert body["tags"] == ["直播", "解说"]
    assert body["available"] is True

    # 重复编号 409
    r = client.post(f"/tournaments/{tid}/stations", json={"station_no": 2})
    assert r.status_code == 409

    # 批量新增（编号乱序，列表按编号升序返回）
    add_stations(client, tid, [{"station_no": 3}, {"station_no": 1}])
    stations = client.get(f"/tournaments/{tid}/stations").json()
    assert [s["station_no"] for s in stations] == [1, 2, 3]

    # 批量内含重复 / 与已有冲突：整体不落库
    r = client.post(
        f"/tournaments/{tid}/stations/bulk",
        json={"stations": [{"station_no": 4}, {"station_no": 4}]},
    )
    assert r.status_code == 409
    r = client.post(
        f"/tournaments/{tid}/stations/bulk",
        json={"stations": [{"station_no": 5}, {"station_no": 1}]},
    )
    assert r.status_code == 409
    stations = client.get(f"/tournaments/{tid}/stations").json()
    assert [s["station_no"] for s in stations] == [1, 2, 3]

    # 更新可用状态与标签
    r = client.put(
        f"/tournaments/{tid}/stations/2",
        json={"available": False, "tags": ["残局库"]},
    )
    assert r.status_code == 200
    assert r.json()["available"] is False
    assert r.json()["tags"] == ["残局库"]
    assert (
        client.put(
            f"/tournaments/{tid}/stations/99",
            json={"available": True, "tags": []},
        ).status_code
        == 404
    )

    # 非法标签 422；不存在的赛事 404
    assert (
        client.post(
            f"/tournaments/{tid}/stations",
            json={"station_no": 9, "tags": ["  "]},
        ).status_code
        == 422
    )
    assert client.get("/tournaments/9999/stations").status_code == 404


def test_participant_required_tags(client):
    tid = create_tournament(client)

    # 创建时登记必需标签
    r = client.post(
        f"/tournaments/{tid}/participants",
        json={"name": "A", "required_tags": ["直播", "直播", " 直播 "]},
    )
    assert r.status_code == 201
    assert r.json()[0]["required_tags"] == ["直播"]

    add_players(client, tid, ["B"])
    # 批量录入默认无标签
    players = client.get(f"/tournaments/{tid}/participants").json()
    assert players[1]["required_tags"] == []

    # PUT 更新（整体替换）
    body = set_tags(client, tid, 2, ["安静区"])
    assert body["required_tags"] == ["安静区"]
    body = set_tags(client, tid, 2, [])
    assert body["required_tags"] == []

    assert (
        client.put(
            f"/tournaments/{tid}/participants/99/tags",
            json={"required_tags": []},
        ).status_code
        == 404
    )
    assert (
        client.put(
            f"/tournaments/{tid}/participants/1/tags",
            json={"required_tags": [""]},
        ).status_code
        == 422
    )


# ---------- 方案发布与版本 ----------

def test_plan_publish_versioning_and_query(client):
    tid = create_tournament(client)
    add_players(client, tid, ["甲", "乙", "丙", "丁"])
    add_stations(client, tid, [{"station_no": 1}, {"station_no": 2}])
    rnd = next_round(client, tid)
    m1, m2 = [m["id"] for m in rnd["matches"]]

    # 首个方案：expected_version=0 -> version 1；
    # 无标签约束时按 (match_id, station_no) 字典序：小编号场占小编号工位
    r = publish_plan(client, tid, 1, 0)
    assert r.status_code == 201, r.text
    plan = r.json()
    assert plan["version"] == 1
    assert plan["round_no"] == 1
    assert assignments_of(plan) == {m1: 1, m2: 2}
    assert plan["unassigned"] == []
    assert plan["created_at"].endswith("Z")

    # 版本冲突：期望版本不等于当前最新版本
    assert publish_plan(client, tid, 1, 0).status_code == 409
    assert publish_plan(client, tid, 1, 2).status_code == 409

    # 第二版：工位 1 停用后重发 -> 只能分配工位 2 给小编号场
    client.put(
        f"/tournaments/{tid}/stations/1",
        json={"available": False, "tags": []},
    )
    r = publish_plan(client, tid, 1, 1)
    assert r.status_code == 201
    plan2 = r.json()
    assert plan2["version"] == 2
    assert assignments_of(plan2) == {m1: 2}
    assert [u["match_id"] for u in plan2["unassigned"]] == [m2]

    # 按版本查询：历史版本快照不受工位变更影响
    plans = client.get(f"/tournaments/{tid}/rounds/1/station-plans").json()
    assert [p["version"] for p in plans] == [1, 2]
    v1 = client.get(f"/tournaments/{tid}/rounds/1/station-plans/1").json()
    assert assignments_of(v1) == {m1: 1, m2: 2}

    # 404：不存在的版本 / 轮次 / 赛事
    assert (
        client.get(f"/tournaments/{tid}/rounds/1/station-plans/9").status_code
        == 404
    )
    assert client.get(f"/tournaments/{tid}/rounds/9/station-plans").status_code == 404
    assert publish_plan(client, tid, 9, 0).status_code == 404
    assert publish_plan(client, 9999, 1, 0).status_code == 404


def test_plan_respects_tags_and_availability(client):
    tid = create_tournament(client)
    add_players(client, tid, ["A", "B", "C", "D"])
    # 1 号需要「直播」；3 号需要「安静区」
    set_tags(client, tid, 1, ["直播"])
    set_tags(client, tid, 3, ["安静区"])
    add_stations(
        client,
        tid,
        [
            {"station_no": 1, "tags": ["直播"]},
            {"station_no": 2, "tags": ["直播", "安静区"]},
            {"station_no": 3, "available": False},
        ],
    )
    rnd = next_round(client, tid)
    # 首轮配对 (1,2)、(3,4)
    m1, m2 = [m["id"] for m in rnd["matches"]]

    plan = publish_plan(client, tid, 1, 0).json()
    got = assignments_of(plan)
    # m1 需要「直播」：工位 1、2 均可 -> 字典序取 1；
    # m2 需要「安静区」：只有工位 2 覆盖 -> 2
    assert got == {m1: 1, m2: 2}
    assert plan["unassigned"] == []


def test_capacity_shortage_saves_partial_plan_with_reasons(client):
    tid = create_tournament(client)
    add_players(client, tid, ["A", "B", "C", "D", "E", "F"])
    # 只有工位 1 可用；工位 2 停用
    add_stations(
        client, tid, [{"station_no": 1}, {"station_no": 2, "available": False}]
    )
    rnd = next_round(client, tid)
    m1, m2, m3 = [m["id"] for m in rnd["matches"]]

    r = publish_plan(client, tid, 1, 0)
    assert r.status_code == 201
    plan = r.json()
    # 容量不足仍保存最大可行方案：只有一场分到工位（字典序 -> 小编号场）
    assert assignments_of(plan) == {m1: 1}
    unassigned = {u["match_id"]: u for u in plan["unassigned"]}
    assert set(unassigned) == {m2, m3}
    for u in unassigned.values():
        assert u["missing_tags"] == []
        assert u["conflicting_stations"] == [1]
        assert "已被占用" in u["reason"]


def test_unassigned_reasons_missing_tags_and_fragmented(client):
    tid = create_tournament(client)
    add_players(client, tid, ["A", "B", "C", "D"])
    # 1 号需要「直播」，2 号需要「安静区」-> m1 需要两个标签
    set_tags(client, tid, 1, ["直播"])
    set_tags(client, tid, 2, ["安静区"])
    # 工位 1 只有「直播」、工位 2 只有「安静区」：标签分散，无一覆盖全部
    add_stations(
        client, tid, [{"station_no": 1, "tags": ["直播"]}, {"station_no": 2, "tags": ["安静区"]}]
    )
    rnd = next_round(client, tid)
    m1, m2 = [m["id"] for m in rnd["matches"]]

    plan = publish_plan(client, tid, 1, 0).json()
    # m2（无标签需求）字典序占工位 1；m1 无工位可同时覆盖两个标签
    assert assignments_of(plan) == {m2: 1}
    (u,) = plan["unassigned"]
    assert u["match_id"] == m1
    assert u["required_tags"] == ["安静区", "直播"]
    # 两个标签分别都有工位提供：不算缺失，也没有可覆盖的已占工位
    assert u["missing_tags"] == []
    assert u["conflicting_stations"] == []
    assert "没有可同时覆盖全部必需标签" in u["reason"]

    # 调整：工位 2 覆盖两个标签 -> m1 可分配；m2 占工位 1
    client.put(
        f"/tournaments/{tid}/stations/2",
        json={"available": True, "tags": ["直播", "安静区"]},
    )
    plan2 = publish_plan(client, tid, 1, 1).json()
    assert assignments_of(plan2) == {m1: 2, m2: 1}

    # 标签完全无工位提供：missing_tags 指明缺什么
    set_tags(client, tid, 3, ["残局库"])
    plan3 = publish_plan(client, tid, 1, 2).json()
    unassigned3 = {u["match_id"]: u for u in plan3["unassigned"]}
    assert unassigned3[m2]["missing_tags"] == ["残局库"]
    assert unassigned3[m2]["conflicting_stations"] == []
    assert "缺少标签" in unassigned3[m2]["reason"]


def test_pins_honored(client):
    tid = create_tournament(client)
    add_players(client, tid, ["A", "B", "C", "D"])
    add_stations(client, tid, [{"station_no": 1}, {"station_no": 2}])
    rnd = next_round(client, tid)
    m1, m2 = [m["id"] for m in rnd["matches"]]

    # 固定 m1 到工位 2：覆盖字典序结果，其余由求解器补齐
    r = publish_plan(client, tid, 1, 0, pins=[{"match_id": m1, "station_no": 2}])
    assert r.status_code == 201
    assert assignments_of(r.json()) == {m1: 2, m2: 1}


def test_pins_invalid_return_409_and_persist_nothing(client):
    tid = create_tournament(client)
    add_players(client, tid, ["A", "B", "C"])
    set_tags(client, tid, 1, ["直播"])
    add_stations(
        client,
        tid,
        [{"station_no": 1}, {"station_no": 2, "available": False}],
    )
    rnd = next_round(client, tid)
    real = next(m for m in rnd["matches"] if not m["is_bye"])
    bye = next(m for m in rnd["matches"] if m["is_bye"])
    m1 = real["id"]

    def events_now():
        return client.get(f"/tournaments/{tid}/events").json()["items"]

    def plans_now():
        return client.get(f"/tournaments/{tid}/rounds/1/station-plans").json()

    base_events = len(events_now())

    bad_pins = [
        # 固定工位重复（同一工位两次）
        [{"match_id": m1, "station_no": 1}, {"match_id": m1, "station_no": 1}],
        # 同一对局重复固定
        [{"match_id": m1, "station_no": 1}, {"match_id": m1, "station_no": 2}],
        # 工位不可用
        [{"match_id": m1, "station_no": 2}],
        # 工位不覆盖必需标签（1 号需要「直播」，工位 1 没有）
        [{"match_id": m1, "station_no": 1}],
        # 对局不存在
        [{"match_id": 9999, "station_no": 1}],
        # 轮空对局不占工位
        [{"match_id": bye["id"], "station_no": 1}],
        # 工位不存在
        [{"match_id": m1, "station_no": 99}],
    ]
    for pins in bad_pins:
        r = publish_plan(client, tid, 1, 0, pins=pins)
        assert r.status_code == 409, (pins, r.text)
        # 不落库：无方案版本、无事件
        assert plans_now() == []
        assert len(events_now()) == base_events

    # 合法固定（工位 1 覆盖不了「直播」-> 必须固定到不存在的可用工位之外：
    # 这里直接不加固定，验证 409 之后版本号仍从 1 开始）
    r = publish_plan(client, tid, 1, 0)
    assert r.status_code == 201
    assert r.json()["version"] == 1


def test_bye_match_occupies_no_station(client):
    tid = create_tournament(client)
    add_players(client, tid, ["A", "B", "C"])
    add_stations(client, tid, [{"station_no": 1}])
    rnd = next_round(client, tid)
    real = next(m for m in rnd["matches"] if not m["is_bye"])

    plan = publish_plan(client, tid, 1, 0).json()
    # 只有非轮空对局参与分配；轮空不出现在未分配列表
    assert assignments_of(plan) == {real["id"]: 1}
    assert plan["unassigned"] == []


def test_minimize_moves_relative_to_previous_round(client):
    """换位次数：第二轮方案优先让选手留在上一轮的工位。

    第 1 轮 (1,2)->工位1、(3,4)->工位2、(5,6)->工位3；
    第 2 轮配对 (1,3)、(2,5)、(4,6)。纯字典序会给出
    m4->1、m5->2、m6->3（4 人次换位）；最优为 3 人次换位，
    其中字典序最小的是 m4->1、m5->3、m6->2。
    """
    tid = create_tournament(client)
    add_players(client, tid, ["P1", "P2", "P3", "P4", "P5", "P6"])
    add_stations(client, tid, [{"station_no": 1}, {"station_no": 2}, {"station_no": 3}])

    rnd1 = next_round(client, tid)
    pairs1 = [
        (m["player_white"]["number"], m["player_black"]["number"])
        for m in rnd1["matches"]
    ]
    assert pairs1 == [(1, 2), (3, 4), (5, 6)]
    m1, m2, m3 = [m["id"] for m in rnd1["matches"]]
    plan1 = publish_plan(client, tid, 1, 0).json()
    assert assignments_of(plan1) == {m1: 1, m2: 2, m3: 3}

    # 1、3、5 胜 -> 第 2 轮配对 (1,3)、(2,5)、(4,6)
    put_result(client, rnd1["matches"][0], 1.0, 0.0)
    put_result(client, rnd1["matches"][1], 1.0, 0.0)
    put_result(client, rnd1["matches"][2], 1.0, 0.0)

    rnd2 = next_round(client, tid)
    pairs2 = [
        (m["player_white"]["number"], m["player_black"]["number"])
        for m in rnd2["matches"]
    ]
    assert pairs2 == [(1, 3), (2, 5), (4, 6)]
    m4, m5, m6 = [m["id"] for m in rnd2["matches"]]

    plan2 = publish_plan(client, tid, 2, 0).json()
    # 最少换位（3 人次）前提下字典序最小
    assert assignments_of(plan2) == {m4: 1, m5: 3, m6: 2}

    # 第 2 轮再发一版：基线仍是第 1 轮，结果一致（幂等可重算）
    plan2b = publish_plan(client, tid, 2, 1).json()
    assert plan2b["version"] == 2
    assert assignments_of(plan2b) == {m4: 1, m5: 3, m6: 2}


def test_station_plan_published_event(client):
    tid = create_tournament(client)
    add_players(client, tid, ["A", "B", "C", "D"])
    add_stations(client, tid, [{"station_no": 1}])
    rnd = next_round(client, tid)
    m1, m2 = [m["id"] for m in rnd["matches"]]

    plan = publish_plan(client, tid, 1, 0).json()
    events = client.get(f"/tournaments/{tid}/events").json()["items"]
    published = [e for e in events if e["type"] == "station_plan.published"]
    assert len(published) == 1
    payload = published[0]["payload"]
    assert payload["round_id"] == rnd["id"]
    assert payload["round_no"] == 1
    assert payload["plan_id"] == plan["id"]
    assert payload["version"] == 1
    assert payload["assignments"] == [{"match_id": m1, "station_no": 1}]
    assert payload["unassigned_match_ids"] == [m2]

    # 被拒绝的发布（版本冲突）不产生事件
    assert publish_plan(client, tid, 1, 0).status_code == 409
    events2 = client.get(f"/tournaments/{tid}/events").json()["items"]
    assert len(events2) == len(events)


def test_plan_isolated_between_tournaments(client):
    tid1 = create_tournament(client, "甲赛")
    tid2 = create_tournament(client, "乙赛")
    add_players(client, tid1, ["A", "B"])
    add_players(client, tid2, ["X", "Y"])
    add_stations(client, tid1, [{"station_no": 1}])
    # 乙赛没有工位：方案全部未分配且说明没有可用工位
    next_round(client, tid1)
    next_round(client, tid2)

    plan2 = publish_plan(client, tid2, 1, 0).json()
    assert plan2["assignments"] == []
    (u,) = plan2["unassigned"]
    assert u["reason"] == "赛事当前没有可用工位"

    plan1 = publish_plan(client, tid1, 1, 0).json()
    assert len(plan1["assignments"]) == 1
    # 两赛事版本互不影响
    assert plan1["version"] == 1
    assert plan2["version"] == 1


def test_concurrent_publish_only_one_wins(live_server):
    """并发发布同一轮的首个方案：恰好一个 201，其余 409。

    expected_version 乐观并发 + 进程级写锁串行：两个请求都期望
    版本 0 时，先到者发布版本 1，后者看到版本已变而 409；
    事件序号依旧连续（失败请求不占号、不广播）。
    """
    import threading

    with live_server.client() as client:
        tid = create_tournament(client, "并发工位赛")
        add_players(client, tid, ["A", "B"])
        add_stations(client, tid, [{"station_no": 1}])
        next_round(client, tid)

        barrier = threading.Barrier(2)
        statuses = []

        def call():
            local = live_server.client()
            try:
                barrier.wait(timeout=10)
                r = local.post(
                    f"/tournaments/{tid}/rounds/1/station-plans",
                    json={"expected_version": 0, "pins": []},
                )
                statuses.append(r.status_code)
            finally:
                local.close()

        threads = [threading.Thread(target=call) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        assert sorted(statuses) == [201, 409]
        plans = client.get(f"/tournaments/{tid}/rounds/1/station-plans").json()
        assert [p["version"] for p in plans] == [1]
        events = client.get(f"/tournaments/{tid}/events").json()["items"]
        assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
        published = [e for e in events if e["type"] == "station_plan.published"]
        assert len(published) == 1
