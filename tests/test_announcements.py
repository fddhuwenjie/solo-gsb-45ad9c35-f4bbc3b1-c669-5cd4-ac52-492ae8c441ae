"""定向赛务公告与确认回执模块测试。

覆盖需求：
- 发布：三类收件规则（参赛者 / 当前排名区间 / 某版工位方案工位号）
  并集、去重、再应用排除名单，收件人按编号升序冻结；
- expected_event_seq 乐观快照：不一致 409 且不落库、不占事件序号；
- 规则无效（引用非本赛事参赛者、排名越界、轮次/方案版本/工位号
  不存在、并集为空）409 且不落库；
- request_id 幂等：重放返回首次结果（无新事件），同键不同参数 409；
- 公告不可改写：revisions 生成新版本并重新冻结收件人，旧版回执
  不沿用；非收件人不能确认（409）、参赛者不存在 404，重复确认
  返回原回执且不重复产生事件；
- 查询：公告/版本列表详情、已确认与未确认名单；
- 发布与确认写入既有赛事事件流（announcement.published /
  announcement.acknowledged），事务回滚不留事件。
"""
import threading


# ---------- 基础工具 ----------

def create_tournament(client, name="公告测试赛"):
    r = client.post("/tournaments", json={"name": name})
    assert r.status_code == 201
    return r.json()["id"]


def add_players(client, tid, names):
    r = client.post(
        f"/tournaments/{tid}/participants/bulk", json={"names": names}
    )
    assert r.status_code == 201
    return r.json()


def add_stations(client, tid, count):
    stations = [{"station_no": i} for i in range(1, count + 1)]
    r = client.post(
        f"/tournaments/{tid}/stations/bulk", json={"stations": stations}
    )
    assert r.status_code == 201, r.text
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
    r = client.post(
        f"/tournaments/{tid}/rounds/{round_no}/station-plans",
        json={"expected_version": expected_version, "pins": pins or []},
    )
    assert r.status_code == 201, r.text
    return r.json()


def event_seq(client, tid):
    return client.get(f"/tournaments/{tid}/events").json()["last_seq"]


def events_of(client, tid, type_=None):
    items = client.get(f"/tournaments/{tid}/events?limit=200").json()["items"]
    return [e for e in items if type_ is None or e["type"] == type_]


def publish_announcement(client, tid, **body):
    return client.post(f"/tournaments/{tid}/announcements", json=body)


def revise(client, aid, **body):
    return client.post(f"/announcements/{aid}/revisions", json=body)


def ack(client, aid, version, participant_id):
    return client.post(
        f"/announcements/{aid}/revisions/{version}/acknowledgements",
        json={"participant_id": participant_id},
    )


def ack_list(client, aid, version):
    r = client.get(
        f"/announcements/{aid}/revisions/{version}/acknowledgements"
    )
    assert r.status_code == 200, r.text
    return r.json()


def setup_tournament(client, n_players=6, with_plan=True):
    """6 名选手 + 第 1 轮（一局定胜负制造排名）+ 工位方案 v1。"""
    tid = create_tournament(client)
    players = add_players(
        client, tid, [f"选手{i}" for i in range(1, n_players + 1)]
    )
    rnd = next_round(client, tid)
    if with_plan:
        real = [m for m in rnd["matches"] if not m["is_bye"]]
        add_stations(client, tid, len(real) + 1)
        plan = publish_plan(client, tid, 1, 0)
    else:
        plan = None
    return tid, players, rnd, plan


# ---------- 收件规则解析与冻结 ----------

def test_publish_union_dedup_exclude_and_freeze(client):
    tid, players, rnd, plan = setup_tournament(client)
    pids = [p["id"] for p in players]
    seq = event_seq(client, tid)

    # 工位 1 上对局的双方
    a1 = next(a for a in plan["assignments"] if a["station_no"] == 1)
    m1 = next(m for m in rnd["matches"] if m["id"] == a1["match_id"])
    station1 = {m1["player_white"]["id"], m1["player_black"]["id"]}

    rules = {
        # 选手3
        "participant_ids": [pids[2]],
        # 当前排名前 2（未开赛同分，按编号即选手 1、2）
        "rank_ranges": [{"rank_from": 1, "rank_to": 2}],
        # 工位 1 的双方
        "station_plans": [
            {"round_no": 1, "version": 1, "station_nos": [1]}
        ],
        # 并集去重后剔除选手 1
        "exclude_participant_ids": [pids[0]],
    }
    r = publish_announcement(
        client,
        tid,
        request_id="pub-1",
        expected_event_seq=seq,
        body="请相关选手到检录处",
        rules=rules,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    aid = body["id"]
    rev = body["latest_revision"]
    assert rev["version"] == 1

    expected = ({pids[2]} | set(pids[:2]) | station1) - {pids[0]}
    assert rev["recipient_count"] == len(expected)
    assert rev["acknowledged_count"] == 0

    pending = ack_list(client, aid, 1)["pending"]
    # 冻结收件人按编号升序
    assert [p["participant_id"] for p in pending] == sorted(expected)

    # 发布写入既有事件流，序号 +1
    assert event_seq(client, tid) == seq + 1
    ev = events_of(client, tid, "announcement.published")
    assert len(ev) == 1
    assert ev[0]["seq"] == seq + 1
    assert ev[0]["payload"]["announcement_id"] == aid
    assert ev[0]["payload"]["version"] == 1
    assert sorted(ev[0]["payload"]["participant_ids"]) == sorted(expected)


def test_rank_range_uses_current_standings(client):
    tid, players, rnd, plan = setup_tournament(client)
    pids = [p["id"] for p in players]
    # 录入赛果：让编号 6 的选手赢下其对局，排名应进入前列
    real = [m for m in rnd["matches"] if not m["is_bye"]]
    for i, m in enumerate(real):
        if any(
            p["id"] == pids[5]
            for p in (m["player_white"], m["player_black"])
        ):
            if m["player_white"]["id"] == pids[5]:
                put_result(client, m, 1.0, 0.0)
            else:
                put_result(client, m, 0.0, 1.0)
        else:
            put_result(client, m, 1.0, 0.0)
    seq = event_seq(client, tid)
    standings = client.get(f"/tournaments/{tid}/standings").json()
    top1 = standings[0]["participant_id"]

    r = publish_announcement(
        client,
        tid,
        request_id="pub-rank",
        expected_event_seq=seq,
        body="头名选手请领奖",
        rules={"rank_ranges": [{"rank_from": 1, "rank_to": 1}]},
    )
    assert r.status_code == 201, r.text
    aid = r.json()["id"]
    pending = ack_list(client, aid, 1)["pending"]
    assert [p["participant_id"] for p in pending] == [top1]


def test_station_plan_multiple_stations_both_players(client):
    tid, players, rnd, plan = setup_tournament(client)
    seq = event_seq(client, tid)
    station_nos = sorted(a["station_no"] for a in plan["assignments"])[:2]
    expected = set()
    for no in station_nos:
        a = next(x for x in plan["assignments"] if x["station_no"] == no)
        m = next(x for x in rnd["matches"] if x["id"] == a["match_id"])
        expected |= {m["player_white"]["id"], m["player_black"]["id"]}

    r = publish_announcement(
        client,
        tid,
        request_id="pub-stations",
        expected_event_seq=seq,
        body="相关工位选手",
        rules={
            "station_plans": [
                {
                    "round_no": 1,
                    "version": 1,
                    "station_nos": station_nos,
                }
            ]
        },
    )
    assert r.status_code == 201, r.text
    aid = r.json()["id"]
    pending = ack_list(client, aid, 1)["pending"]
    assert {p["participant_id"] for p in pending} == expected
    # 同一工位号重复传：schema 去重，收件人数不变
    r2 = publish_announcement(
        client,
        tid,
        request_id="pub-stations-2",
        expected_event_seq=event_seq(client, tid),
        body="相关工位选手",
        rules={
            "station_plans": [
                {
                    "round_no": 1,
                    "version": 1,
                    "station_nos": station_nos + station_nos[:1],
                }
            ]
        },
    )
    assert r2.status_code == 201, r2.text
    assert r2.json()["latest_revision"]["recipient_count"] == len(expected)


# ---------- 规则无效：409 / 422 且不落库 ----------

def test_invalid_rules_do_not_persist(client):
    tid, players, rnd, plan = setup_tournament(client)
    pids = [p["id"] for p in players]
    seq = event_seq(client, tid)
    n_ann_before = len(client.get(f"/tournaments/{tid}/announcements").json())

    bad_rules = [
        # 收件名单中的参赛者不存在
        {"participant_ids": [99999]},
        # 排除名单中的参赛者不存在
        {
            "participant_ids": [pids[0]],
            "exclude_participant_ids": [99999],
        },
        # 排名区间越界
        {"rank_ranges": [{"rank_from": 1, "rank_to": 99}]},
        # 轮次不存在
        {
            "station_plans": [
                {"round_no": 99, "version": 1, "station_nos": [1]}
            ]
        },
        # 方案版本不存在
        {
            "station_plans": [
                {"round_no": 1, "version": 99, "station_nos": [1]}
            ]
        },
        # 工位号不在该版方案中
        {
            "station_plans": [
                {"round_no": 1, "version": 1, "station_nos": [999]}
            ]
        },
        # 并集后被排除名单清空
        {
            "participant_ids": [pids[0]],
            "exclude_participant_ids": [pids[0]],
        },
    ]
    for i, rules in enumerate(bad_rules):
        r = publish_announcement(
            client,
            tid,
            request_id=f"bad-{i}",
            expected_event_seq=seq,
            body="x",
            rules=rules,
        )
        assert r.status_code == 409, (i, r.text)

    # schema 层校验：三类选择器一项都不给
    r = publish_announcement(
        client,
        tid,
        request_id="bad-schema",
        expected_event_seq=seq,
        body="x",
        rules={},
    )
    assert r.status_code == 422
    # 排名区间起止颠倒
    r = publish_announcement(
        client,
        tid,
        request_id="bad-schema-2",
        expected_event_seq=seq,
        body="x",
        rules={"rank_ranges": [{"rank_from": 3, "rank_to": 2}]},
    )
    assert r.status_code == 422
    # 空工位号列表
    r = publish_announcement(
        client,
        tid,
        request_id="bad-schema-3",
        expected_event_seq=seq,
        body="x",
        rules={
            "station_plans": [
                {"round_no": 1, "version": 1, "station_nos": []}
            ]
        },
    )
    assert r.status_code == 422

    # 全部拒绝：无公告、无新事件
    assert (
        len(client.get(f"/tournaments/{tid}/announcements").json())
        == n_ann_before
    )
    assert event_seq(client, tid) == seq
    assert events_of(client, tid, "announcement.published") == []


def test_station_plan_rule_must_belong_to_tournament(client):
    # 另一赛事的轮次/方案不能被本赛事引用
    tid1, players1, rnd1, plan1 = setup_tournament(client)
    tid2 = create_tournament(client, "隔壁赛")
    add_players(client, tid2, ["路人甲", "路人乙"])
    seq2 = event_seq(client, tid2)

    # tid2 还没有轮次：引用第 1 轮方案无效
    r = publish_announcement(
        client,
        tid2,
        request_id="foreign",
        expected_event_seq=seq2,
        body="x",
        rules={
            "station_plans": [
                {"round_no": 1, "version": 1, "station_nos": [1]}
            ]
        },
    )
    assert r.status_code == 409

    # 其他赛事参赛者的 id 出现在规则中同样无效
    other_pid = players1[0]["id"]
    r = publish_announcement(
        client,
        tid2,
        request_id="foreign-pid",
        expected_event_seq=seq2,
        body="x",
        rules={"participant_ids": [other_pid]},
    )
    assert r.status_code == 409
    assert event_seq(client, tid2) == seq2


# ---------- expected_event_seq 乐观快照 ----------

def test_stale_event_seq_rejected_without_persist(client):
    tid, players, rnd, plan = setup_tournament(client)
    seq = event_seq(client, tid)

    r = publish_announcement(
        client,
        tid,
        request_id="stale",
        expected_event_seq=seq - 1,
        body="x",
        rules={"participant_ids": [players[0]["id"]]},
    )
    assert r.status_code == 409
    assert "快照已变化" in r.json()["detail"]
    # 不落库、不占序号
    assert client.get(f"/tournaments/{tid}/announcements").json() == []
    assert event_seq(client, tid) == seq

    # 以最新序号重发成功
    r = publish_announcement(
        client,
        tid,
        request_id="fresh",
        expected_event_seq=seq,
        body="x",
        rules={"participant_ids": [players[0]["id"]]},
    )
    assert r.status_code == 201, r.text


def test_concurrent_publish_with_same_expected_seq_only_one_wins(client):
    """两请求持同一快照并发：后到者序号已变，409 不落库。"""
    import httpx
    from test_events import LiveServer, reset_db

    reset_db()
    with LiveServer() as srv:
        h = httpx.Client(base_url=srv.base_url, timeout=10.0)
        tid = h.post("/tournaments", json={"name": "并发公告赛"}).json()["id"]
        h.post(
            f"/tournaments/{tid}/participants/bulk",
            json={"names": ["甲", "乙", "丙", "丁"]},
        ).raise_for_status()
        h.post(f"/tournaments/{tid}/rounds/next").raise_for_status()
        h.post(
            f"/tournaments/{tid}/stations/bulk",
            json={
                "stations": [
                    {"station_no": i} for i in range(1, 5)
                ]
            },
        ).raise_for_status()
        h.post(
            f"/tournaments/{tid}/rounds/1/station-plans",
            json={"expected_version": 0, "pins": []},
        ).raise_for_status()
        players = h.get(f"/tournaments/{tid}/participants").json()
        seq = h.get(f"/tournaments/{tid}/events").json()["last_seq"]

        barrier = threading.Barrier(2)
        results = []

        def worker(reqid, pid):
            barrier.wait()
            resp = h.post(
                f"/tournaments/{tid}/announcements",
                json={
                    "request_id": reqid,
                    "expected_event_seq": seq,
                    "body": "同时发布",
                    "rules": {"participant_ids": [pid]},
                },
            )
            results.append((resp.status_code, resp.json()))

        t1 = threading.Thread(target=worker, args=("c1", players[0]["id"]))
        t2 = threading.Thread(target=worker, args=("c2", players[1]["id"]))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert sorted(s for s, _ in results) == [201, 409], results
        # 仅一条公告、序号只推进一次
        anns = h.get(f"/tournaments/{tid}/announcements").json()
        assert len(anns) == 1
        assert h.get(f"/tournaments/{tid}/events").json()["last_seq"] == seq + 1


# ---------- 幂等 ----------

def test_publish_request_id_replay_returns_original(client):
    tid, players, rnd, plan = setup_tournament(client)
    seq = event_seq(client, tid)
    payload = {
        "request_id": "idem",
        "expected_event_seq": seq,
        "body": "幂等公告",
        "rules": {"participant_ids": [players[0]["id"], players[1]["id"]]},
    }
    r1 = publish_announcement(client, tid, **payload)
    assert r1.status_code == 201
    first = r1.json()

    r2 = publish_announcement(client, tid, **payload)
    assert r2.status_code == 201
    assert r2.json() == first
    # 重放不产生第二条事件、不占新序号
    assert event_seq(client, tid) == seq + 1
    assert len(events_of(client, tid, "announcement.published")) == 1

    # 同 request_id 不同参数：409
    changed = dict(payload)
    changed["body"] = "另一个正文"
    r3 = publish_announcement(client, tid, **changed)
    assert r3.status_code == 409


def test_revise_request_id_replay(client):
    tid, players, rnd, plan = setup_tournament(client)
    seq = event_seq(client, tid)
    first = publish_announcement(
        client,
        tid,
        request_id="p",
        expected_event_seq=seq,
        body="v1",
        rules={"participant_ids": [players[0]["id"]]},
    ).json()
    aid = first["id"]
    payload = {
        "request_id": "rev-1",
        "expected_event_seq": seq + 1,
        "body": "v2",
        "rules": {"participant_ids": [players[0]["id"], players[1]["id"]]},
    }
    r1 = revise(client, aid, **payload)
    assert r1.status_code == 201, r1.text
    assert r1.json()["latest_version"] == 2
    r2 = revise(client, aid, **payload)
    assert r2.json() == r1.json()
    assert event_seq(client, tid) == seq + 2


# ---------- 版本化：不可改写、重新冻结、旧回执不沿用 ----------

def test_revision_recreates_recipients_and_keeps_old_acks(client):
    tid, players, rnd, plan = setup_tournament(client)
    pids = [p["id"] for p in players]
    seq = event_seq(client, tid)
    first = publish_announcement(
        client,
        tid,
        request_id="p",
        expected_event_seq=seq,
        body="第一版",
        rules={"participant_ids": [pids[0], pids[1]]},
    ).json()
    aid = first["id"]

    # v1 收件人之一确认
    r = ack(client, aid, 1, pids[0])
    assert r.status_code == 201, r.text
    receipt_v1 = r.json()
    assert receipt_v1["version"] == 1

    # 发布新版本，收件人变为选手 2、3
    r = revise(
        client,
        aid,
        request_id="rev",
        expected_event_seq=seq + 2,
        body="第二版",
        rules={"participant_ids": [pids[1], pids[2]]},
    )
    assert r.status_code == 201, r.text
    rev2 = r.json()
    assert rev2["latest_version"] == 2

    # 新版本重新冻结：选手1 不在 v2 名单，旧回执不沿用
    v2 = ack_list(client, aid, 2)
    assert sorted(p["participant_id"] for p in v2["pending"]) == [
        pids[1],
        pids[2],
    ]
    assert v2["acknowledged"] == []
    r = ack(client, aid, 2, pids[0])
    assert r.status_code == 409

    # v1 历史版本与回执原样保留，公告正文不可变（v1 仍是旧正文）
    v1_detail = client.get(f"/announcements/{aid}/revisions/1").json()
    assert v1_detail["body"] == "第一版"
    v1 = ack_list(client, aid, 1)
    assert [p["participant_id"] for p in v1["acknowledged"]] == [pids[0]]

    # 选手2 可在 v2 确认；版本列表按版本升序
    r = ack(client, aid, 2, pids[1])
    assert r.status_code == 201
    revs = client.get(f"/announcements/{aid}/revisions").json()
    assert [x["version"] for x in revs] == [1, 2]
    counts = {x["version"]: x["recipient_count"] for x in revs}
    assert counts == {1: 2, 2: 2}
    ack_counts = {x["version"]: x["acknowledged_count"] for x in revs}
    assert ack_counts == {1: 1, 2: 1}


def test_revision_stale_seq_and_invalid_rules_do_not_persist(client):
    tid, players, rnd, plan = setup_tournament(client)
    seq = event_seq(client, tid)
    first = publish_announcement(
        client,
        tid,
        request_id="p",
        expected_event_seq=seq,
        body="v1",
        rules={"participant_ids": [players[0]["id"]]},
    ).json()
    aid = first["id"]

    # 快照冲突
    r = revise(
        client,
        aid,
        request_id="rev-stale",
        expected_event_seq=0,
        body="v2",
        rules={"participant_ids": [players[0]["id"]]},
    )
    assert r.status_code == 409
    # 规则无效
    r = revise(
        client,
        aid,
        request_id="rev-bad",
        expected_event_seq=seq + 1,
        body="v2",
        rules={"participant_ids": [99999]},
    )
    assert r.status_code == 409
    # 仍只有 v1
    revs = client.get(f"/announcements/{aid}/revisions").json()
    assert [x["version"] for x in revs] == [1]
    assert event_seq(client, tid) == seq + 1


# ---------- 确认回执 ----------

def test_ack_only_for_recipients_and_idempotent(client):
    tid, players, rnd, plan = setup_tournament(client)
    pids = [p["id"] for p in players]
    seq = event_seq(client, tid)
    first = publish_announcement(
        client,
        tid,
        request_id="p",
        expected_event_seq=seq,
        body="请确认",
        rules={"participant_ids": [pids[0], pids[1]]},
    ).json()
    aid = first["id"]

    # 非收件人
    r = ack(client, aid, 1, pids[2])
    assert r.status_code == 409
    # 参赛者不存在
    r = ack(client, aid, 1, 99999)
    assert r.status_code == 404
    # 公告/版本不存在
    assert ack(client, 9999, 1, pids[0]).status_code == 404
    assert ack(client, aid, 9, pids[0]).status_code == 404

    # 首次确认
    r = ack(client, aid, 1, pids[0])
    assert r.status_code == 201, r.text
    receipt = r.json()
    assert receipt["participant_id"] == pids[0]
    assert receipt["number"] == players[0]["number"]
    assert receipt["name"] == players[0]["name"]
    assert receipt["acknowledged_at"]

    # 重复提交：返回原结果（200、同一确认时间），无新事件
    r2 = ack(client, aid, 1, pids[0])
    assert r2.status_code == 200
    assert r2.json() == receipt
    acks = events_of(client, tid, "announcement.acknowledged")
    assert len(acks) == 1
    assert acks[0]["payload"] == {
        "announcement_id": aid,
        "version": 1,
        "participant_id": pids[0],
    }

    # 第二人确认后名单正确
    assert ack(client, aid, 1, pids[1]).status_code == 201
    lst = ack_list(client, aid, 1)
    assert [p["participant_id"] for p in lst["acknowledged"]] == [
        pids[0],
        pids[1],
    ]
    assert lst["pending"] == []


def test_frozen_recipients_unaffected_by_later_changes(client):
    """冻结后再录入赛果改变排名，不影响已发布版本的收件人。"""
    tid, players, rnd, plan = setup_tournament(client)
    seq = event_seq(client, tid)
    first = publish_announcement(
        client,
        tid,
        request_id="p",
        expected_event_seq=seq,
        body="冻结",
        rules={"rank_ranges": [{"rank_from": 1, "rank_to": 3}]},
    ).json()
    aid = first["id"]
    frozen_before = [
        p["participant_id"] for p in ack_list(client, aid, 1)["pending"]
    ]
    # 录入全部赛果改变排名
    real = [m for m in rnd["matches"] if not m["is_bye"]]
    for m in real:
        put_result(client, m, 0.5, 0.5)
    frozen_after = [
        p["participant_id"] for p in ack_list(client, aid, 1)["pending"]
    ]
    assert frozen_after == frozen_before


# ---------- 查询与 404 ----------

def test_list_and_get_endpoints(client):
    tid, players, rnd, plan = setup_tournament(client)
    seq = event_seq(client, tid)
    a1 = publish_announcement(
        client,
        tid,
        request_id="p1",
        expected_event_seq=seq,
        body="公告一",
        rules={"participant_ids": [players[0]["id"]]},
    ).json()
    a2 = publish_announcement(
        client,
        tid,
        request_id="p2",
        expected_event_seq=seq + 1,
        body="公告二",
        rules={"participant_ids": [players[1]["id"]]},
    ).json()

    # 赛事内列表
    lst = client.get(f"/tournaments/{tid}/announcements").json()
    assert [x["id"] for x in lst] == [a1["id"], a2["id"]]
    assert lst[0]["latest_revision"]["body"] == "公告一"

    # 单个公告（含最新版）
    got = client.get(f"/announcements/{a2['id']}").json()
    assert got["id"] == a2["id"]
    assert got["latest_version"] == 1
    assert client.get("/announcements/9999").status_code == 404

    # 不存在的赛事
    assert client.post(
        "/tournaments/9999/announcements",
        json={
            "request_id": "x",
            "expected_event_seq": 0,
            "body": "x",
            "rules": {"participant_ids": [1]},
        },
    ).status_code == 404
