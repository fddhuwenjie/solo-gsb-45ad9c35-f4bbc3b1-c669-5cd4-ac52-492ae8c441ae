"""轮次计时专项测试。

覆盖需求：
- start/pause/resume/extend 状态机：非法状态 409 且不落库、不产生事件；
- expected_version 乐观并发：版本冲突 409，成功命令使版本 +1；
- request_id 幂等：重放返回首次结果（版本不加、加时不叠加、事件不重复），
  同一 request_id 携带不同参数返回 409；
- 剩余时间由 UTC 锚点 + 累计暂停推算：暂停冻结、恢复延续、
  两次读取之间无任何写库（重启后读数连续的同一机制）；
- extend 只作用于指定非轮空对局：轮空 409、跨轮/不存在 404、
  已提交赛果的对局 409；
- 晚于该场截止时刻的赛果 409；extend 加时后可重新录入；
- 轮次封盘后时钟冻结为 ended，后续命令一律 409，已提交赛果不受影响；
- 计时命令与 round_clock.* 事件同事务提交（事件流可见、顺序正确）。

时间相关断言通过 monkeypatch app.services.clock.utcnow 注入固定时间，
不依赖真实等待。
"""
from datetime import datetime, timedelta

import pytest

from app.services import clock as clock_service


# ---------- 基础工具 ----------

def create_tournament(client, name="计时测试赛"):
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
    assert r.status_code == 201
    return r.json()


def do_action(client, tid, round_no, **body):
    return client.post(
        f"/tournaments/{tid}/rounds/{round_no}/clock/actions", json=body
    )


def get_clock(client, tid, round_no):
    r = client.get(f"/tournaments/{tid}/rounds/{round_no}/clock")
    assert r.status_code == 200
    return r.json()


def start_clock(client, tid, round_no, total=1000, request_id="r-start"):
    r = do_action(
        client,
        tid,
        round_no,
        request_id=request_id,
        expected_version=0,
        action="start",
        total_seconds=total,
    )
    assert r.status_code == 200, r.text
    return r.json()


def put_result(client, match_id, sw, sb):
    return client.put(
        f"/matches/{match_id}/result",
        json={"score_white": sw, "score_black": sb},
    )


class FakeClock:
    def __init__(self):
        self.t = datetime(2026, 9, 26, 12, 0, 0)

    def now(self):
        return self.t

    def advance(self, seconds):
        self.t += timedelta(seconds=seconds)


@pytest.fixture()
def fake_time(monkeypatch):
    fc = FakeClock()
    monkeypatch.setattr(clock_service, "utcnow", fc.now)
    return fc


def iso(dt):
    return dt.isoformat() + "Z"


# ---------- 未启动（idle） ----------

def test_clock_idle_before_start(client):
    tid = create_tournament(client)
    add_players(client, tid, ["甲", "乙", "丙", "丁"])
    rnd = next_round(client, tid)

    body = get_clock(client, tid, 1)
    assert body["status"] == "idle"
    assert body["version"] == 0
    assert body["total_seconds"] is None
    assert body["started_at"] is None
    # 各非轮空对局已列出，但无截止时刻与剩余秒数
    real_matches = [m for m in rnd["matches"] if not m["is_bye"]]
    assert [m["match_id"] for m in body["matches"]] == [
        m["id"] for m in real_matches
    ]
    for m in body["matches"]:
        assert m["deadline_at"] is None
        assert m["remaining_seconds"] is None
        assert m["extra_seconds"] == 0


def test_actions_before_start_rejected(client):
    tid = create_tournament(client)
    add_players(client, tid, ["甲", "乙"])
    next_round(client, tid)

    for action, extra in [
        ("pause", {}),
        ("resume", {}),
        ("extend", {"match_id": 1, "seconds": 60}),
    ]:
        r = do_action(
            client,
            tid,
            1,
            request_id=f"early-{action}",
            expected_version=0,
            action=action,
            **extra,
        )
        assert r.status_code == 409, (action, r.text)
    # 全部拒绝后时钟仍未启动，且没有产生任何计时事件
    assert get_clock(client, tid, 1)["version"] == 0
    events = client.get(f"/tournaments/{tid}/events").json()["items"]
    assert not [e for e in events if e["type"].startswith("round_clock.")]


# ---------- start 与读取 ----------

def test_start_sets_deadlines_and_version(client, fake_time):
    tid = create_tournament(client)
    add_players(client, tid, ["甲", "乙", "丙", "丁"])
    rnd = next_round(client, tid)

    body = start_clock(client, tid, 1, total=1000)
    assert body["status"] == "running"
    assert body["version"] == 1
    assert body["total_seconds"] == 1000
    assert body["started_at"] == iso(fake_time.t)
    assert body["server_time"] == iso(fake_time.t)
    assert len(body["matches"]) == 2
    for m in body["matches"]:
        assert m["deadline_at"] == iso(fake_time.t + timedelta(seconds=1000))
        assert m["remaining_seconds"] == 1000

    # 时间推进后读数随之减少：期间没有任何写库请求，
    # 剩余时间完全由持久化锚点推算（重启后读数连续的同一机制）
    fake_time.advance(100)
    body = get_clock(client, tid, 1)
    assert body["version"] == 1
    for m in body["matches"]:
        assert m["remaining_seconds"] == 900
        assert m["deadline_at"] == iso(fake_time.t + timedelta(seconds=900))


def test_start_idempotent_replay(client, fake_time):
    tid = create_tournament(client)
    add_players(client, tid, ["甲", "乙"])
    next_round(client, tid)

    first = start_clock(client, tid, 1, total=1000, request_id="r1")
    fake_time.advance(30)
    # 相同 request_id + 相同参数：原样返回首次结果（含首次的 server_time）
    r = do_action(
        client,
        tid,
        1,
        request_id="r1",
        expected_version=0,
        action="start",
        total_seconds=1000,
    )
    assert r.status_code == 200
    assert r.json() == first
    # 版本不推进、事件不重复
    assert get_clock(client, tid, 1)["version"] == 1
    events = client.get(f"/tournaments/{tid}/events").json()["items"]
    started = [e for e in events if e["type"] == "round_clock.started"]
    assert len(started) == 1


def test_request_id_reused_with_different_params(client):
    tid = create_tournament(client)
    add_players(client, tid, ["甲", "乙"])
    next_round(client, tid)
    start_clock(client, tid, 1, total=1000, request_id="r1")

    # 同一 request_id 但参数不同 -> 409
    r = do_action(
        client,
        tid,
        1,
        request_id="r1",
        expected_version=0,
        action="start",
        total_seconds=2000,
    )
    assert r.status_code == 409
    # 同一 request_id 但命令不同 -> 409
    r = do_action(
        client,
        tid,
        1,
        request_id="r1",
        expected_version=1,
        action="pause",
    )
    assert r.status_code == 409
    assert get_clock(client, tid, 1)["version"] == 1


def test_version_conflict_rejected_without_persist(client):
    tid = create_tournament(client)
    add_players(client, tid, ["甲", "乙"])
    next_round(client, tid)

    # start 时期望版本不是 0 -> 409
    r = do_action(
        client,
        tid,
        1,
        request_id="bad-v",
        expected_version=3,
        action="start",
        total_seconds=100,
    )
    assert r.status_code == 409
    assert get_clock(client, tid, 1)["version"] == 0

    start_clock(client, tid, 1, total=1000)
    # pause 时期望版本滞后 -> 409，状态不变
    r = do_action(
        client, tid, 1, request_id="bad-v2", expected_version=0, action="pause"
    )
    assert r.status_code == 409
    body = get_clock(client, tid, 1)
    assert body["version"] == 1
    assert body["status"] == "running"
    # 被拒绝的命令不产生事件
    events = client.get(f"/tournaments/{tid}/events").json()["items"]
    clock_events = [e for e in events if e["type"].startswith("round_clock.")]
    assert [e["type"] for e in clock_events] == ["round_clock.started"]


# ---------- 状态机 ----------

def test_illegal_state_transitions(client):
    tid = create_tournament(client)
    add_players(client, tid, ["甲", "乙"])
    next_round(client, tid)
    start_clock(client, tid, 1, total=1000)

    # 重复 start（新 request_id、版本正确）-> 409
    r = do_action(
        client,
        tid,
        1,
        request_id="start-again",
        expected_version=1,
        action="start",
        total_seconds=500,
    )
    assert r.status_code == 409
    # 运行中 resume -> 409
    r = do_action(
        client, tid, 1, request_id="resume-running", expected_version=1,
        action="resume",
    )
    assert r.status_code == 409
    # 正常暂停后再次 pause -> 409
    r = do_action(
        client, tid, 1, request_id="p1", expected_version=1, action="pause"
    )
    assert r.status_code == 200
    r = do_action(
        client, tid, 1, request_id="p2", expected_version=2, action="pause"
    )
    assert r.status_code == 409
    # 版本未被失败的命令推进
    assert get_clock(client, tid, 1)["version"] == 2


def test_pause_freezes_and_resume_continues(client, fake_time):
    tid = create_tournament(client)
    add_players(client, tid, ["甲", "乙"])
    next_round(client, tid)
    start_clock(client, tid, 1, total=1000)

    fake_time.advance(100)
    r = do_action(
        client, tid, 1, request_id="p1", expected_version=1, action="pause"
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "paused"
    assert body["version"] == 2
    assert body["paused_at"] == iso(fake_time.t)
    # 暂停时截止时刻未知（恢复后才确定），剩余时间冻结
    assert body["matches"][0]["deadline_at"] is None
    assert body["matches"][0]["remaining_seconds"] == 900

    # 暂停期间时间流逝不影响读数：全局冻结
    fake_time.advance(500)
    body = get_clock(client, tid, 1)
    assert body["matches"][0]["remaining_seconds"] == 900
    assert body["matches"][0]["deadline_at"] is None

    # 恢复后从冻结处延续
    r = do_action(
        client, tid, 1, request_id="r1", expected_version=2, action="resume"
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "running"
    assert body["version"] == 3
    assert body["accumulated_pause_seconds"] == 500.0
    assert body["matches"][0]["remaining_seconds"] == 900

    fake_time.advance(100)
    body = get_clock(client, tid, 1)
    assert body["matches"][0]["remaining_seconds"] == 800
    # 截止时刻 = 起点 + 累计暂停 + 总时长
    assert body["matches"][0]["deadline_at"] == iso(
        fake_time.t + timedelta(seconds=800)
    )


# ---------- extend ----------

def test_extend_only_target_match(client, fake_time):
    tid = create_tournament(client)
    add_players(client, tid, ["甲", "乙", "丙", "丁"])
    rnd = next_round(client, tid)
    start_clock(client, tid, 1, total=1000)
    m1, m2 = [m["id"] for m in rnd["matches"]]

    fake_time.advance(100)
    r = do_action(
        client,
        tid,
        1,
        request_id="e1",
        expected_version=1,
        action="extend",
        match_id=m1,
        seconds=300,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == 2
    by_id = {m["match_id"]: m for m in body["matches"]}
    assert by_id[m1]["extra_seconds"] == 300
    assert by_id[m1]["remaining_seconds"] == 1200
    assert by_id[m2]["extra_seconds"] == 0
    assert by_id[m2]["remaining_seconds"] == 900

    # 加时累计叠加；幂等重放不叠加
    r = do_action(
        client,
        tid,
        1,
        request_id="e2",
        expected_version=2,
        action="extend",
        match_id=m1,
        seconds=60,
    )
    assert r.status_code == 200
    replay = do_action(
        client,
        tid,
        1,
        request_id="e2",
        expected_version=2,
        action="extend",
        match_id=m1,
        seconds=60,
    )
    assert replay.status_code == 200
    assert replay.json() == r.json()
    body = get_clock(client, tid, 1)
    by_id = {m["match_id"]: m for m in body["matches"]}
    assert by_id[m1]["extra_seconds"] == 360
    assert body["version"] == 3


def test_extend_rejects_bye_and_unknown_match(client):
    tid = create_tournament(client)
    add_players(client, tid, ["甲", "乙", "丙"])
    rnd = next_round(client, tid)  # 3 人：1 场对局 + 1 场轮空
    start_clock(client, tid, 1, total=1000)
    bye = next(m for m in rnd["matches"] if m["is_bye"])

    # 轮空对局 -> 409
    r = do_action(
        client,
        tid,
        1,
        request_id="e-bye",
        expected_version=1,
        action="extend",
        match_id=bye["id"],
        seconds=60,
    )
    assert r.status_code == 409
    # 不存在的对局 -> 404
    r = do_action(
        client,
        tid,
        1,
        request_id="e-404",
        expected_version=1,
        action="extend",
        match_id=999999,
        seconds=60,
    )
    assert r.status_code == 404
    # 其他轮次的对局 -> 404
    tid2 = create_tournament(client, "另一赛事")
    add_players(client, tid2, ["A", "B"])
    rnd2 = next_round(client, tid2)
    r = do_action(
        client,
        tid,
        1,
        request_id="e-other",
        expected_version=1,
        action="extend",
        match_id=rnd2["matches"][0]["id"],
        seconds=60,
    )
    assert r.status_code == 404
    assert get_clock(client, tid, 1)["version"] == 1


def test_extend_allowed_while_paused(client, fake_time):
    tid = create_tournament(client)
    add_players(client, tid, ["甲", "乙"])
    rnd = next_round(client, tid)
    start_clock(client, tid, 1, total=1000)
    do_action(
        client, tid, 1, request_id="p1", expected_version=1, action="pause"
    )
    mid = rnd["matches"][0]["id"]
    r = do_action(
        client,
        tid,
        1,
        request_id="e1",
        expected_version=2,
        action="extend",
        match_id=mid,
        seconds=120,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "paused"
    assert body["matches"][0]["extra_seconds"] == 120
    assert body["matches"][0]["remaining_seconds"] == 1120


# ---------- 截止时刻与赛果 ----------

def test_result_after_deadline_rejected_then_extend_reopens(client, fake_time):
    tid = create_tournament(client)
    add_players(client, tid, ["甲", "乙"])
    rnd = next_round(client, tid)
    mid = rnd["matches"][0]["id"]
    start_clock(client, tid, 1, total=100)

    # 截止时刻之后提交 -> 409，赛果不落库
    fake_time.advance(150)
    r = put_result(client, mid, 1.0, 0.0)
    assert r.status_code == 409
    assert "截止" in r.json()["detail"]
    assert client.get(f"/tournaments/{tid}/rounds/1").json()["matches"][0][
        "score_white"
    ] is None

    # 给该场加时 200 秒后可重新录入
    r = do_action(
        client,
        tid,
        1,
        request_id="e1",
        expected_version=1,
        action="extend",
        match_id=mid,
        seconds=200,
    )
    assert r.status_code == 200
    r = put_result(client, mid, 1.0, 0.0)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "closed"


def test_result_within_deadline_accepted_and_unaffected(client, fake_time):
    tid = create_tournament(client)
    add_players(client, tid, ["甲", "乙", "丙", "丁"])
    rnd = next_round(client, tid)
    m1, m2 = [m["id"] for m in rnd["matches"]]
    start_clock(client, tid, 1, total=1000)

    fake_time.advance(100)
    r = put_result(client, m1, 1.0, 0.0)
    assert r.status_code == 200

    # 已提交赛果的对局不能再加时
    r = do_action(
        client,
        tid,
        1,
        request_id="e-submitted",
        expected_version=1,
        action="extend",
        match_id=m1,
        seconds=60,
    )
    assert r.status_code == 409

    # 后续全局暂停/恢复与对另一场的加时不影响已提交的赛果
    do_action(
        client, tid, 1, request_id="p1", expected_version=1, action="pause"
    )
    do_action(
        client, tid, 1, request_id="r1", expected_version=2, action="resume"
    )
    r = do_action(
        client,
        tid,
        1,
        request_id="e2",
        expected_version=3,
        action="extend",
        match_id=m2,
        seconds=60,
    )
    assert r.status_code == 200
    matches = {
        m["id"]: m
        for m in client.get(f"/tournaments/{tid}/rounds/1").json()["matches"]
    }
    assert matches[m1]["score_white"] == 1.0
    assert matches[m1]["score_black"] == 0.0


def test_pause_freezes_deadline_for_result_submission(client, fake_time):
    tid = create_tournament(client)
    add_players(client, tid, ["甲", "乙"])
    rnd = next_round(client, tid)
    mid = rnd["matches"][0]["id"]
    start_clock(client, tid, 1, total=100)

    fake_time.advance(90)
    do_action(
        client, tid, 1, request_id="p1", expected_version=1, action="pause"
    )
    # 暂停中真实时间流逝不计：冻结在剩余 10 秒，仍可提交
    fake_time.advance(10000)
    r = put_result(client, mid, 0.5, 0.5)
    assert r.status_code == 200, r.text


# ---------- 封盘 ----------

def test_closed_round_freezes_clock_and_rejects_commands(client, fake_time):
    tid = create_tournament(client)
    add_players(client, tid, ["甲", "乙"])
    rnd = next_round(client, tid)
    mid = rnd["matches"][0]["id"]
    start_clock(client, tid, 1, total=1000)

    fake_time.advance(100)
    r = put_result(client, mid, 1.0, 0.0)
    assert r.status_code == 200
    assert r.json()["status"] == "closed"

    # 封盘后时钟冻结为 ended，读数不再变化
    body = get_clock(client, tid, 1)
    assert body["status"] == "ended"
    assert body["ended_at"] == iso(fake_time.t)
    assert body["matches"][0]["remaining_seconds"] == 900
    fake_time.advance(5000)
    body = get_clock(client, tid, 1)
    assert body["matches"][0]["remaining_seconds"] == 900

    # 已结束轮次拒绝一切计时命令
    for action, extra, ev in [
        ("start", {"total_seconds": 100}, 3),
        ("pause", {}, 3),
        ("resume", {}, 3),
        ("extend", {"match_id": mid, "seconds": 60}, 3),
    ]:
        r = do_action(
            client,
            tid,
            1,
            request_id=f"after-close-{action}",
            expected_version=ev,
            action=action,
            **extra,
        )
        assert r.status_code == 409, (action, r.text)

    # 已提交赛果不受影响
    match = client.get(f"/tournaments/{tid}/rounds/1").json()["matches"][0]
    assert match["score_white"] == 1.0


def test_clock_not_started_round_close_unaffected(client):
    """未启动计时的轮次：赛果录入与封盘行为与旧版一致。"""
    tid = create_tournament(client)
    add_players(client, tid, ["甲", "乙"])
    rnd = next_round(client, tid)
    mid = rnd["matches"][0]["id"]
    r = put_result(client, mid, 1.0, 0.0)
    assert r.status_code == 200
    assert r.json()["status"] == "closed"
    body = get_clock(client, tid, 1)
    assert body["status"] == "idle"
    assert body["version"] == 0


# ---------- 事件同事务 ----------

def test_clock_commands_emit_events_in_order(client, fake_time):
    tid = create_tournament(client)
    add_players(client, tid, ["甲", "乙"])
    rnd = next_round(client, tid)
    mid = rnd["matches"][0]["id"]

    start_clock(client, tid, 1, total=1000)
    do_action(
        client, tid, 1, request_id="p1", expected_version=1, action="pause"
    )
    do_action(
        client, tid, 1, request_id="r1", expected_version=2, action="resume"
    )
    do_action(
        client,
        tid,
        1,
        request_id="e1",
        expected_version=3,
        action="extend",
        match_id=mid,
        seconds=30,
    )

    events = client.get(
        f"/tournaments/{tid}/events", params={"limit": 100}
    ).json()["items"]
    clock_events = [e for e in events if e["type"].startswith("round_clock.")]
    assert [e["type"] for e in clock_events] == [
        "round_clock.started",
        "round_clock.paused",
        "round_clock.resumed",
        "round_clock.extended",
    ]
    seqs = [e["seq"] for e in clock_events]
    assert seqs == sorted(seqs)
    # 事件载荷携带版本与命令参数
    assert clock_events[0]["payload"]["total_seconds"] == 1000
    assert clock_events[0]["payload"]["version"] == 1
    assert clock_events[3]["payload"]["match_id"] == mid
    assert clock_events[3]["payload"]["seconds"] == 30
    assert clock_events[3]["payload"]["extra_seconds"] == 30
    assert clock_events[3]["payload"]["version"] == 4


# ---------- 参数校验与 404 ----------

def test_request_validation(client):
    tid = create_tournament(client)
    add_players(client, tid, ["甲", "乙"])
    next_round(client, tid)

    # start 缺 total_seconds -> 422
    r = do_action(
        client, tid, 1, request_id="v1", expected_version=0, action="start"
    )
    assert r.status_code == 422
    # total_seconds 非正数 -> 422
    r = do_action(
        client,
        tid,
        1,
        request_id="v2",
        expected_version=0,
        action="start",
        total_seconds=0,
    )
    assert r.status_code == 422
    # extend 缺参数 -> 422
    r = do_action(
        client, tid, 1, request_id="v3", expected_version=0, action="extend",
        match_id=1,
    )
    assert r.status_code == 422
    # 未知命令 -> 422
    r = do_action(
        client, tid, 1, request_id="v4", expected_version=0, action="reset"
    )
    assert r.status_code == 422
    # 校验失败不落库
    assert get_clock(client, tid, 1)["version"] == 0


def test_clock_404s(client):
    tid = create_tournament(client)
    add_players(client, tid, ["甲", "乙"])
    next_round(client, tid)

    r = client.get(f"/tournaments/{tid}/rounds/99/clock")
    assert r.status_code == 404
    r = do_action(
        client,
        tid,
        99,
        request_id="x",
        expected_version=0,
        action="start",
        total_seconds=100,
    )
    assert r.status_code == 404
    r = client.get("/tournaments/999/rounds/1/clock")
    assert r.status_code == 404
    r = do_action(
        client,
        999,
        1,
        request_id="x",
        expected_version=0,
        action="start",
        total_seconds=100,
    )
    assert r.status_code == 404
