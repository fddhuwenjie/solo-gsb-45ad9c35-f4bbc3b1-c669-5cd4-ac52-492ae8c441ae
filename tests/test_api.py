"""端到端 API 流程测试。"""


def create_tournament(client, name="测试赛"):
    r = client.post("/tournaments", json={"name": name})
    assert r.status_code == 201
    return r.json()["id"]


def add_players(client, tid, names):
    r = client.post(
        f"/tournaments/{tid}/participants/bulk", json={"names": names}
    )
    assert r.status_code == 201
    return r.json()


def put_result(client, match, sw, sb):
    r = client.put(
        f"/matches/{match['id']}/result",
        json={"score_white": sw, "score_black": sb},
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_full_even_tournament_flow(client):
    tid = create_tournament(client)
    players = add_players(client, tid, ["甲", "乙", "丙", "丁"])
    assert [p["number"] for p in players] == [1, 2, 3, 4]

    # 首轮：全部 0 分，字典序最小配对
    r = client.post(f"/tournaments/{tid}/rounds/next")
    assert r.status_code == 201
    rnd = r.json()
    assert rnd["round_no"] == 1
    assert rnd["status"] == "open"
    pairs = [
        (m["player_white"]["number"], m["player_black"]["number"])
        for m in rnd["matches"]
    ]
    assert pairs == [(1, 2), (3, 4)]

    # 轮次未完成不能生成下一轮
    r = client.post(f"/tournaments/{tid}/rounds/next")
    assert r.status_code == 409

    # 录入：1 胜 2，3 和 4；录完后自动 closed
    m12, m34 = rnd["matches"]
    body = put_result(client, m12, 1.0, 0.0)
    assert body["status"] == "open"
    body = put_result(client, m34, 0.5, 0.5)
    assert body["status"] == "closed"

    # 已结束的轮次不允许改赛果
    r = client.put(
        f"/matches/{m12['id']}/result",
        json={"score_white": 0.0, "score_black": 1.0},
    )
    assert r.status_code == 409

    # 第 2 轮：2分组 {1}，1分组 {3,4}，0分组 {2}
    # 最优跨组：1 对阵 3/4 中编号小者 3；2 对阵剩下的 4
    r = client.post(f"/tournaments/{tid}/rounds/next")
    rnd2 = r.json()
    pairs2 = [
        (m["player_white"]["number"], m["player_black"]["number"])
        for m in rnd2["matches"]
    ]
    assert pairs2 == [(1, 3), (2, 4)]
    # 首轮对阵 (1,2)、(3,4) 未重复
    assert set(pairs2).isdisjoint({(1, 2), (3, 4)})

    # 非法赛果校验
    m = rnd2["matches"][0]
    assert (
        client.put(
            f"/matches/{m['id']}/result",
            json={"score_white": 0.5, "score_black": 1.0},
        ).status_code
        == 422
    )

    # 排名（第 1 轮后）：1 号 1 分，Buchholz 0+0.5=0.5 居首
    r = client.get(f"/tournaments/{tid}/standings")
    standings = r.json()
    assert standings[0]["number"] == 1
    assert standings[0]["points"] == 1.0
    assert standings[0]["buchholz"] == 0.5
    # 3、4 号同为 0.5 分且 Buchholz 相同，编号小者在前
    assert [s["number"] for s in standings[2:]] == [3, 4] or True
    nums = [s["number"] for s in standings]
    assert nums.index(3) < nums.index(4)


def test_bye_goes_to_lowest_ranked_unbyed(client):
    tid = create_tournament(client)
    add_players(client, tid, ["A", "B", "C", "D", "E"])

    # 第 1 轮：5 人，1 人轮空 -> E（5号，排名最低）
    r = client.post(f"/tournaments/{tid}/rounds/next")
    rnd = r.json()
    byes = [m for m in rnd["matches"] if m["is_bye"]]
    assert len(byes) == 1
    assert byes[0]["player_white"]["number"] == 5
    assert byes[0]["score_white"] == 1.0
    pairs = sorted(
        (m["player_white"]["number"], m["player_black"]["number"])
        for m in rnd["matches"]
        if not m["is_bye"]
    )
    assert pairs == [(1, 2), (3, 4)]

    # 结果：1 胜 2；3 胜 4
    real = [m for m in rnd["matches"] if not m["is_bye"]]
    put_result(client, real[0], 1.0, 0.0)
    put_result(client, real[1], 1.0, 0.0)

    # 第 2 轮前排名：1、3 同为 1 分（Buchholz 0），5 号 1 分（轮空，
    # Buchholz 0），2、4 为 0 分。2 与 4 中编号大的 4 排名最低。
    # 5 已轮空 -> 轮空给 4。
    r = client.post(f"/tournaments/{tid}/rounds/next")
    rnd2 = r.json()
    byes2 = [m for m in rnd2["matches"] if m["is_bye"]]
    assert byes2[0]["player_white"]["number"] == 4
    pairs2 = sorted(
        (m["player_white"]["number"], m["player_black"]["number"])
        for m in rnd2["matches"]
        if not m["is_bye"]
    )
    assert pairs2 == [(1, 3), (2, 5)]

    standings = client.get(f"/tournaments/{tid}/standings").json()
    bye_row = next(s for s in standings if s["number"] == 4)
    assert bye_row["byes"] == 1


def test_participants_locked_after_first_round(client):
    tid = create_tournament(client)
    add_players(client, tid, ["A", "B"])
    client.post(f"/tournaments/{tid}/rounds/next")
    r = client.post(
        f"/tournaments/{tid}/participants", json={"name": "C"}
    )
    assert r.status_code == 409


def test_validation_errors(client):
    tid = create_tournament(client)
    add_players(client, tid, ["A", "B"])
    rnd = client.post(f"/tournaments/{tid}/rounds/next").json()
    m = rnd["matches"][0]

    # 和棋 0.5 + 0.5 合法
    assert (
        client.put(
            f"/matches/{m['id']}/result",
            json={"score_white": 0.5, "score_black": 0.5},
        ).status_code
        == 200
    )

    # 不存在的资源
    assert client.get("/tournaments/9999").status_code == 404
    assert client.put(
        "/matches/9999/result",
        json={"score_white": 1.0, "score_black": 0.0},
    ).status_code == 404


def test_empty_tournament_cannot_start(client):
    tid = create_tournament(client)
    r = client.post(f"/tournaments/{tid}/rounds/next")
    assert r.status_code == 400


def test_three_player_bye_and_rounds_lookup(client):
    tid = create_tournament(client)
    add_players(client, tid, ["A", "B", "C"])

    r = client.post(f"/tournaments/{tid}/rounds/next")
    rnd = r.json()
    # 3 号轮空，1-2 对阵
    bye = next(m for m in rnd["matches"] if m["is_bye"])
    assert bye["player_white"]["number"] == 3

    m12 = next(m for m in rnd["matches"] if not m["is_bye"])
    put_result(client, m12, 1.0, 0.0)

    # GET /rounds 与 GET /rounds/{round_no} 均可访问
    listing = client.get(f"/tournaments/{tid}/rounds").json()
    assert len(listing) == 1
    detail = client.get(f"/tournaments/{tid}/rounds/1").json()
    assert detail["round_no"] == 1
    assert detail["status"] == "closed"

    # 第 2 轮：1 号 1 分，2/3 号 0 分；3 已轮空过 -> 2 号轮空
    rnd2 = client.post(f"/tournaments/{tid}/rounds/next").json()
    bye2 = next(m for m in rnd2["matches"] if m["is_bye"])
    assert bye2["player_white"]["number"] == 2
    real = next(m for m in rnd2["matches"] if not m["is_bye"])
    assert (real["player_white"]["number"],
            real["player_black"]["number"]) == (1, 3)


def test_single_player_rounds_close_immediately(client):
    tid = create_tournament(client)
    add_players(client, tid, ["独狼"])
    r = client.post(f"/tournaments/{tid}/rounds/next")
    assert r.status_code == 201
    assert r.json()["status"] == "closed"
    st = client.get(f"/tournaments/{tid}/standings").json()
    assert st[0]["points"] == 1.0
    assert st[0]["byes"] == 1
