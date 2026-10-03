"""赛事维护态（pause/resume/archive）专项测试。

覆盖需求：
- 赛事初始 active，状态经 POST /tournaments/{id}/status 切换；
  active 与 paused 可互相切换，archive 只能从 paused 且所有轮次
  已结束、无待处理复核时进入，archive 后永久只读；
- request_id 幂等：重放返回首次结果（含首次 frozen/resumed 列表与
  首次 current_event_seq），同键不同参数 409；expected_event_seq
  快照冲突 409 且不留痕、不占事件序号；
- pause 在同一事务冻结所有正在运行的轮次时钟；手动暂停的时钟
  保持暂停；resume 只恢复被赛事暂停的时钟；维护期间剩余时间不
  流逝（运行中时钟维护时长补入累计暂停）；
- 维护期间参赛者、轮次、赛果、签到、工位、计时、公告、复核、
  扣分、晋级等写操作统一 409 且不留痕（不占事件序号）；查询与
  事件续传仍可用；已成功请求的 request_id 重放仍返回首次结果；
- archive 后一切写操作（含状态切换）409；
- 状态切换与 tournament.* 事件同事务提交，SSE 与分页查询可见；
- 老库升级：status / tournament_paused 列幂等补列。

时间相关断言通过 monkeypatch app.services.clock.utcnow 注入固定
时间，不依赖真实等待。
"""
from datetime import datetime, timedelta

import pytest

from app.services import clock as clock_service


# ---------- 基础工具 ----------

def create_tournament(client, name="维护测试赛"):
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


def put_result(client, match_id, sw, sb):
    return client.put(
        f"/matches/{match_id}/result",
        json={"score_white": sw, "score_black": sb},
    )


def close_round(client, rnd):
    for m in rnd["matches"]:
        if not m["is_bye"]:
            r = put_result(client, m["id"], 1, 0)
            assert r.status_code == 200, r.text


def clock_action(client, tid, round_no, **body):
    return client.post(
        f"/tournaments/{tid}/rounds/{round_no}/clock/actions", json=body
    )


def start_clock(client, tid, round_no, total=1000, request_id="r-start"):
    r = clock_action(
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


def get_clock(client, tid, round_no):
    r = client.get(f"/tournaments/{tid}/rounds/{round_no}/clock")
    assert r.status_code == 200
    return r.json()


def status_action(client, tid, request_id, action, expected_event_seq):
    return client.post(
        f"/tournaments/{tid}/status",
        json={
            "request_id": request_id,
            "expected_event_seq": expected_event_seq,
            "action": action,
        },
    )


def pause(client, tid, seq, request_id="pause-1"):
    r = status_action(client, tid, request_id, "pause", seq)
    assert r.status_code == 200, r.text
    return r.json()


def resume(client, tid, seq, request_id="resume-1"):
    r = status_action(client, tid, request_id, "resume", seq)
    assert r.status_code == 200, r.text
    return r.json()


def archive(client, tid, seq, request_id="archive-1"):
    return status_action(client, tid, request_id, "archive", seq)


def current_seq(client, tid):
    # 分页接口按 after 升序返回；用大 limit 取最后一条的 seq
    r = client.get(f"/tournaments/{tid}/events", params={"limit": 200})
    assert r.status_code == 200
    items = r.json()["items"]
    return items[-1]["seq"] if items else 0


class FakeClock:
    def __init__(self):
        # 固定在真实当前时间之后的锚点（与 test_clock.py 同一思路）：
        # start/pause/resume 与读数推算全部经 app.services.clock.utcnow
        # 注入，剩余时间断言不依赖真实等待
        self.t = datetime(2030, 1, 1, 9, 0, 0)

    def now(self):
        return self.t

    def advance(self, seconds):
        self.t += timedelta(seconds=seconds)


@pytest.fixture()
def fake_time(monkeypatch):
    fc = FakeClock()
    monkeypatch.setattr(clock_service, "utcnow", fc.now)
    return fc


# ---------- 初始状态与非法转换 ----------

def test_tournament_starts_active_and_response_carries_status(client):
    tid = create_tournament(client)
    body = client.get(f"/tournaments/{tid}").json()
    assert body["status"] == "active"
    listing = client.get("/tournaments").json()
    assert listing[0]["status"] == "active"


def test_illegal_transitions_are_409_and_leave_no_trace(client):
    tid = create_tournament(client)
    seq_before = current_seq(client, tid)

    # active 不能 resume / archive
    r = status_action(client, tid, "r1", "resume", seq_before)
    assert r.status_code == 409
    r = status_action(client, tid, "r2", "archive", seq_before)
    assert r.status_code == 409
    # 被拒绝的请求不留痕：新 request_id 仍可正常 pause
    pause(client, tid, seq_before, request_id="r3")
    seq_paused = current_seq(client, tid)
    assert seq_paused == seq_before + 1

    # paused 不能再 pause
    r = status_action(client, tid, "r4", "pause", seq_paused)
    assert r.status_code == 409
    # paused 的状态仍是 paused，被拒绝不占号
    assert current_seq(client, tid) == seq_paused
    assert client.get(f"/tournaments/{tid}").json()["status"] == "paused"


def test_event_seq_snapshot_conflict_is_409_and_uses_no_seq(client):
    tid = create_tournament(client)
    seq = current_seq(client, tid)  # 1（tournament.created）
    assert seq >= 1

    r = status_action(client, tid, "bad-seq", "pause", seq + 5)
    assert r.status_code == 409
    assert "快照" in r.json()["detail"]
    # 状态未变、不占号
    assert client.get(f"/tournaments/{tid}").json()["status"] == "active"
    assert current_seq(client, tid) == seq
    # 该 request_id 未落库，可用相同键携带正确快照成功切换
    pause(client, tid, seq, request_id="bad-seq")


# ---------- 幂等 ----------

def test_status_action_idempotent_replay_returns_first_result(client, fake_time):
    tid = create_tournament(client)
    add_players(client, tid, ["甲", "乙"])
    rnd = next_round(client, tid)
    start_clock(client, tid, rnd["round_no"])
    fake_time.advance(10)

    seq = current_seq(client, tid)
    first = pause(client, tid, seq, request_id="maint")
    assert first["status"] == "paused"
    assert first["frozen_rounds"] == [1]
    assert first["resumed_rounds"] == []
    assert first["current_event_seq"] == seq + 1

    fake_time.advance(120)
    # 重放：不再次冻结、不产生新事件，返回首次结果（即使时间已推进）
    replay = status_action(client, tid, "maint", "pause", seq)
    assert replay.status_code == 200
    assert replay.json() == first
    assert current_seq(client, tid) == seq + 1

    resume_seq = current_seq(client, tid)
    first_resume = resume(client, tid, resume_seq, request_id="go")
    assert first_resume["resumed_rounds"] == [1]
    fake_time.advance(120)
    replay_resume = status_action(client, tid, "go", "resume", resume_seq)
    assert replay_resume.status_code == 200
    assert replay_resume.json() == first_resume
    assert current_seq(client, tid) == resume_seq + 1


def test_same_request_id_with_different_params_is_409(client):
    tid = create_tournament(client)
    seq = current_seq(client, tid)
    assert pause(client, tid, seq, request_id="k")["status"] == "paused"

    # 不同 action / expected_event_seq 均视为指纹不一致
    r = status_action(client, tid, "k", "resume", seq)
    assert r.status_code == 409
    r = status_action(client, tid, "k", "resume", seq + 1)
    assert r.status_code == 409
    # 换一个 request_id 即可正常恢复
    assert resume(client, tid, seq + 1, request_id="k2")["status"] == "active"


# ---------- 时钟冻结/恢复 ----------

def test_pause_freezes_running_clocks_and_resume_restores_only_them(
    client, fake_time
):
    tid = create_tournament(client)
    add_players(client, tid, ["甲", "乙", "丙", "丁"])
    r1 = next_round(client, tid)
    close_round(client, r1)
    r2 = next_round(client, tid)
    start_clock(client, tid, 2, total=1000, request_id="s2")
    fake_time.advance(100)

    body = pause(client, tid, current_seq(client, tid), request_id="p")
    assert body["frozen_rounds"] == [2]

    clock = get_clock(client, tid, 2)
    assert clock["status"] == "paused"
    assert clock["tournament_paused"] is True
    assert clock["version"] == 1  # 冻结不推进时钟版本
    remaining_frozen = clock["matches"][0]["remaining_seconds"]
    assert remaining_frozen == 900

    # 维护持续 300 秒：读数冻结不流逝
    fake_time.advance(300)
    clock = get_clock(client, tid, 2)
    assert clock["status"] == "paused"
    assert clock["matches"][0]["remaining_seconds"] == 900

    body = resume(client, tid, current_seq(client, tid), request_id="r")
    assert body["resumed_rounds"] == [2]
    clock = get_clock(client, tid, 2)
    assert clock["status"] == "running"
    assert clock["tournament_paused"] is False
    assert clock["version"] == 1
    # 恢复瞬间剩余仍是 900：维护 300 秒未计入运行时长
    assert clock["matches"][0]["remaining_seconds"] == 900
    assert clock["accumulated_pause_seconds"] == pytest.approx(300.0)

    fake_time.advance(50)
    clock = get_clock(client, tid, 2)
    assert clock["matches"][0]["remaining_seconds"] == 850


def test_manual_paused_clock_untouched_by_tournament_pause_and_resume(
    client, fake_time
):
    tid = create_tournament(client)
    add_players(client, tid, ["甲", "乙", "丙", "丁"])
    r1 = next_round(client, tid)
    close_round(client, r1)
    r2 = next_round(client, tid)
    start_clock(client, tid, 2, total=1000, request_id="s2")
    fake_time.advance(100)
    # 手动暂停（版本 1）
    r = clock_action(
        client, tid, 2, request_id="mp", expected_version=1, action="pause"
    )
    assert r.status_code == 200
    manual_paused_at = r.json()["paused_at"]
    assert r.json()["tournament_paused"] is False

    body = pause(client, tid, current_seq(client, tid), request_id="p")
    # 手动暂停的时钟不在冻结列表中
    assert body["frozen_rounds"] == []
    clock = get_clock(client, tid, 2)
    assert clock["status"] == "paused"
    assert clock["tournament_paused"] is False
    assert clock["paused_at"] == manual_paused_at

    fake_time.advance(300)  # 维护 300 秒
    body = resume(client, tid, current_seq(client, tid), request_id="r")
    assert body["resumed_rounds"] == []
    clock = get_clock(client, tid, 2)
    # 仍是手动暂停，锚点未被维护触碰
    assert clock["status"] == "paused"
    assert clock["tournament_paused"] is False
    assert clock["paused_at"] == manual_paused_at
    assert clock["accumulated_pause_seconds"] == 0.0

    # 手动恢复：自手动暂停时刻起累计暂停（维护期间本来就暂停着）
    r = clock_action(
        client, tid, 2, request_id="mr", expected_version=2, action="resume"
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "running"
    # 手动暂停自 t=100 起到 t=400 恢复，累计暂停 300 秒（维护期间
    # 时钟本来就是手动暂停，合并计入同一段暂停）
    assert body["accumulated_pause_seconds"] == pytest.approx(300.0)
    assert body["matches"][0]["remaining_seconds"] == 900


def test_two_running_clocks_both_frozen_and_resumed(client, fake_time):
    tid = create_tournament(client)
    add_players(client, tid, ["甲", "乙", "丙", "丁"])
    r1 = next_round(client, tid)
    start_clock(client, tid, 1, total=1000, request_id="s1")
    close_round(client, r1)  # 第 1 轮封盘时其时钟自动 ended
    r2 = next_round(client, tid)
    start_clock(client, tid, 2, total=500, request_id="s2")

    body = pause(client, tid, current_seq(client, tid), request_id="p")
    # ended 的时钟不冻结，只有 running 的第 2 轮
    assert body["frozen_rounds"] == [2]
    assert get_clock(client, tid, 1)["status"] == "ended"

    body = resume(client, tid, current_seq(client, tid), request_id="r")
    assert body["resumed_rounds"] == [2]
    assert get_clock(client, tid, 2)["status"] == "running"


def test_clock_commands_are_409_during_maintenance_except_replay(
    client, fake_time
):
    tid = create_tournament(client)
    add_players(client, tid, ["甲", "乙"])
    next_round(client, tid)
    started = start_clock(client, tid, 1, total=1000)
    pause(client, tid, current_seq(client, tid), request_id="p")
    clock = get_clock(client, tid, 1)
    assert clock["status"] == "paused"
    assert clock["tournament_paused"] is True

    # 手动 resume 赛事冻结的时钟：409
    r = clock_action(
        client, tid, 1, request_id="x1", expected_version=1, action="resume"
    )
    assert r.status_code == 409
    # extend：409
    match_id = started["matches"][0]["match_id"]
    r = clock_action(
        client,
        tid,
        1,
        request_id="x2",
        expected_version=1,
        action="extend",
        match_id=match_id,
        seconds=60,
    )
    assert r.status_code == 409
    # 再 pause：状态不符（且赛事在维护），409
    r = clock_action(
        client, tid, 1, request_id="x3", expected_version=1, action="pause"
    )
    assert r.status_code == 409
    # start（不存在第 2 轮）经赛事状态拦截前 404 轮次不存在；
    # 时钟查询仍可用
    assert get_clock(client, tid, 1)["status"] == "paused"

    # 已成功的 start 命令重放：返回首次结果，不被维护拦截
    r = clock_action(
        client,
        tid,
        1,
        request_id="r-start",
        expected_version=0,
        action="start",
        total_seconds=1000,
    )
    assert r.status_code == 200
    assert r.json()["version"] == 1


# ---------- 归档 ----------

def _tournament_with_closed_round(client):
    tid = create_tournament(client)
    add_players(client, tid, ["甲", "乙"])
    r1 = next_round(client, tid)
    close_round(client, r1)
    return tid


def test_archive_requires_paused_and_closed_rounds_and_no_pending_review(client):
    # active 直接 archive：409
    tid = _tournament_with_closed_round(client)
    seq = current_seq(client, tid)
    r = archive(client, tid, seq)
    assert r.status_code == 409
    assert client.get(f"/tournaments/{tid}").json()["status"] == "active"

    # paused 但有 open 轮次：409
    tid2 = create_tournament(client, "第二轮进行中赛")
    add_players(client, tid2, ["甲", "乙", "丙", "丁"])
    close_round(client, next_round(client, tid2))
    next_round(client, tid2)  # 第 2 轮 open
    seq2 = current_seq(client, tid2)
    pause(client, tid2, seq2, request_id="p2")
    r = archive(client, tid2, seq2 + 1)
    assert r.status_code == 409
    assert "尚未结束" in r.json()["detail"]
    # 归档失败后仍可恢复
    resume(client, tid2, current_seq(client, tid2), request_id="r2")

    # paused、轮次全部结束但存在待处理复核：409
    tid3 = create_tournament(client, "复核未决赛")
    add_players(client, tid3, ["甲", "乙", "丙", "丁"])
    rnd = next_round(client, tid3)
    review_match = rnd["matches"][0]
    # 先让第 1 轮封盘，再对其中一场创建复核（封盘后仍允许复核，
    # 只要尚未生成下一轮）
    for m in rnd["matches"]:
        put_result(client, m["id"], 1, 0)
    seq3 = current_seq(client, tid3)
    r = client.post(
        f"/matches/{review_match['id']}/result-reviews",
        json={
            "request_id": "rev1",
            "expected_event_seq": seq3,
            "score_white": 0.0,
            "score_black": 1.0,
            "reason": "记错比分",
        },
    )
    assert r.status_code == 201, r.text
    pause(client, tid3, current_seq(client, tid3), request_id="p3")
    r = archive(client, tid3, current_seq(client, tid3))
    assert r.status_code == 409
    assert "复核" in r.json()["detail"]
    assert client.get(f"/tournaments/{tid3}").json()["status"] == "paused"


def test_archive_succeeds_then_permanently_read_only(client):
    tid = _tournament_with_closed_round(client)
    seq = current_seq(client, tid)
    pause(client, tid, seq, request_id="p")
    paused_seq = seq + 1
    r = archive(client, tid, paused_seq, request_id="a")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "archived"
    assert body["frozen_rounds"] == []
    assert body["resumed_rounds"] == []
    assert body["current_event_seq"] == paused_seq + 1
    assert client.get(f"/tournaments/{tid}").json()["status"] == "archived"

    # 归档后一切状态切换 409
    for action in ("pause", "resume", "archive"):
        r = status_action(client, tid, f"post-{action}", action, paused_seq + 1)
        assert r.status_code == 409

    # 归档重放返回首次结果
    replay = archive(client, tid, paused_seq, request_id="a")
    assert replay.status_code == 200
    assert replay.json() == body
    # 重放不占号
    assert current_seq(client, tid) == paused_seq + 1


# ---------- 维护期间写保护 ----------

def test_all_writes_409_during_maintenance_and_leave_no_trace(
    client, fake_time
):
    # 丰富的赛事状态：两轮，第 1 轮已结束，第 2 轮进行中且时钟运行
    tid = create_tournament(client)
    add_players(client, tid, ["甲", "乙", "丙", "丁"])
    r1 = next_round(client, tid)
    close_round(client, r1)
    r2 = next_round(client, tid)
    start_clock(client, tid, 2, total=10_000, request_id="s2")
    # 一个工位（维护期间尝试更新）
    assert (
        client.post(
            f"/tournaments/{tid}/stations",
            json={"station_no": 1, "tags": ["t"]},
        ).status_code
        == 201
    )
    # 一份公告（维护期间尝试修订/回执）
    seq_now = current_seq(client, tid)
    ann = client.post(
        f"/tournaments/{tid}/announcements",
        json={
            "request_id": "ann1",
            "expected_event_seq": seq_now,
            "body": "开赛公告",
            "rules": {"participant_ids": [1]},
        },
    )
    assert ann.status_code == 201, ann.text
    announcement_id = ann.json()["id"]

    seq_before_pause = current_seq(client, tid)
    pause(client, tid, seq_before_pause, request_id="p")
    seq_paused = seq_before_pause + 1

    def assert_blocked(resp, label):
        assert resp.status_code == 409, f"{label}: {resp.status_code} {resp.text}"
        assert current_seq(client, tid) == seq_paused, label

    match_id = r2["matches"][0]["id"]

    # 参赛者
    assert_blocked(
        client.post(
            f"/tournaments/{tid}/participants", json={"name": "新人"}
        ),
        "participant create",
    )
    assert_blocked(
        client.put(
            f"/tournaments/{tid}/participants/1/tags",
            json={"required_tags": []},
        ),
        "participant tags",
    )
    # 工位
    assert_blocked(
        client.post(
            f"/tournaments/{tid}/stations", json={"station_no": 9}
        ),
        "station create",
    )
    assert_blocked(
        client.put(
            f"/tournaments/{tid}/stations/1",
            json={"available": False, "tags": []},
        ),
        "station update",
    )
    # 轮次
    assert_blocked(client.post(f"/tournaments/{tid}/rounds/next"), "next round")
    # 赛果
    assert_blocked(put_result(client, match_id, 1, 0), "result")
    # 签到
    assert_blocked(
        client.post(
            f"/tournaments/{tid}/checkin-snapshots",
            json={
                "request_id": "ci1",
                "expected_event_seq": seq_paused,
                "round_no": 3,
                "present_numbers": [1, 2, 3, 4],
                "absent_numbers": [],
            },
        ),
        "checkin submit",
    )
    assert_blocked(
        client.post(
            f"/tournaments/{tid}/checkin-snapshots/confirm",
            json={
                "request_id": "ci2",
                "expected_event_seq": seq_paused,
                "round_no": 3,
            },
        ),
        "checkin confirm",
    )
    # 工位方案
    assert_blocked(
        client.post(
            f"/tournaments/{tid}/rounds/2/station-plans",
            json={"expected_version": 0, "pins": []},
        ),
        "station plan",
    )
    # 计时（非重放）
    assert_blocked(
        clock_action(
            client,
            tid,
            2,
            request_id="new-extend",
            expected_version=1,
            action="extend",
            match_id=match_id,
            seconds=30,
        ),
        "clock extend",
    )
    # 公告：新发布 / 修订 / 回执
    assert_blocked(
        client.post(
            f"/tournaments/{tid}/announcements",
            json={
                "request_id": "ann2",
                "expected_event_seq": seq_paused,
                "body": "维护中？",
                "rules": {"participant_ids": [1]},
            },
        ),
        "announcement publish",
    )
    assert_blocked(
        client.post(
            f"/announcements/{announcement_id}/revisions",
            json={
                "request_id": "ann3",
                "expected_event_seq": seq_paused,
                "body": "修订",
                "rules": {"participant_ids": [1]},
            },
        ),
        "announcement revise",
    )
    assert_blocked(
        client.post(
            f"/announcements/{announcement_id}/revisions/1/acknowledgements",
            json={"participant_id": 1},
        ),
        "ack",
    )
    # 复核：创建
    assert_blocked(
        client.post(
            f"/matches/{r1['matches'][0]['id']}/result-reviews",
            json={
                "request_id": "rr1",
                "expected_event_seq": seq_paused,
                "score_white": 0.5,
                "score_black": 0.5,
                "reason": "改判",
            },
        ),
        "review create",
    )
    # 扣分：登记
    assert_blocked(
        client.post(
            f"/tournaments/{tid}/penalties",
            json={
                "request_id": "pen1",
                "expected_event_seq": seq_paused,
                "participant_id": 1,
                "points": 0.5,
                "reason": "违规",
            },
        ),
        "penalty deduct",
    )
    # 晋级：发布（轮次 open，本来也会 409，但在状态保护之后；
    # 用另一个全部结束的赛事验证状态 409 先于业务校验）
    tid_done = create_tournament(client, "完结赛")
    add_players(client, tid_done, ["甲", "乙"])
    close_round(client, next_round(client, tid_done))
    done_paused_seq = current_seq(client, tid_done)
    pause(client, tid_done, done_paused_seq, request_id="pd")
    assert_blocked(
        client.post(
            f"/tournaments/{tid_done}/promotions",
            json={
                "request_id": "pr1",
                "expected_event_seq": done_paused_seq + 1,
                "qualifying_round_no": 1,
                "slots": 1,
            },
        ),
        "promotion publish",
    )

    # ---- 查询与事件续传在维护期间仍然可用 ----
    assert client.get(f"/tournaments/{tid}").status_code == 200
    assert client.get(f"/tournaments/{tid}/participants").status_code == 200
    assert client.get(f"/tournaments/{tid}/rounds").status_code == 200
    assert client.get(f"/tournaments/{tid}/standings").status_code == 200
    assert client.get(f"/tournaments/{tid}/stations").status_code == 200
    assert client.get(f"/tournaments/{tid}/announcements").status_code == 200
    assert client.get(f"/tournaments/{tid}/rounds/2/clock").status_code == 200
    events_page = client.get(
        f"/tournaments/{tid}/events", params={"after": 0}
    ).json()
    types = [e["type"] for e in events_page["items"]]
    assert "tournament.paused" in types
    assert events_page["last_seq"] == seq_paused

    # ---- 恢复后写操作重新可用 ----
    resume(client, tid, seq_paused, request_id="r")
    assert put_result(client, match_id, 1, 0).status_code == 200


def test_successful_request_id_replays_during_maintenance(client):
    """维护前已成功的幂等写请求，维护期间重放仍返回首次结果。"""
    tid = create_tournament(client)
    add_players(client, tid, ["甲", "乙", "丙", "丁"])
    r1 = next_round(client, tid)
    match_id = r1["matches"][0]["id"]
    # 先提交赛果，复核才能创建
    put_result(client, match_id, 1, 0)
    seq = current_seq(client, tid)
    # 维护前创建一条复核
    r = client.post(
        f"/matches/{match_id}/result-reviews",
        json={
            "request_id": "rr-replay",
            "expected_event_seq": seq,
            "score_white": 0.5,
            "score_black": 0.5,
            "reason": "和棋",
        },
    )
    assert r.status_code == 201, r.text
    first = r.json()

    pause(client, tid, current_seq(client, tid), request_id="p")
    seq_paused = current_seq(client, tid)
    replay = client.post(
        f"/matches/{match_id}/result-reviews",
        json={
            "request_id": "rr-replay",
            "expected_event_seq": seq,
            "score_white": 0.5,
            "score_black": 0.5,
            "reason": "和棋",
        },
    )
    assert replay.status_code == 201
    assert replay.json()["id"] == first["id"]
    # 重放不产生新事件（pause 之后无新增）
    events = client.get(f"/tournaments/{tid}/events").json()
    assert events["last_seq"] == seq_paused


def test_writes_after_archive_remain_409_including_queries_ok(client):
    tid = _tournament_with_closed_round(client)
    seq = current_seq(client, tid)
    pause(client, tid, seq, request_id="p")
    assert archive(client, tid, seq + 1, request_id="a").status_code == 200

    assert (
        client.post(
            f"/tournaments/{tid}/participants", json={"name": "x"}
        ).status_code
        == 409
    )
    assert client.post(f"/tournaments/{tid}/rounds/next").status_code == 409
    assert (
        client.post(
            f"/tournaments/{tid}/penalties",
            json={
                "request_id": "z",
                "expected_event_seq": seq + 2,
                "participant_id": 1,
                "points": 0.5,
                "reason": "x",
            },
        ).status_code
        == 409
    )
    # 查询永久可用
    assert client.get(f"/tournaments/{tid}").status_code == 200
    assert client.get(f"/tournaments/{tid}/events").status_code == 200
    assert client.get(f"/tournaments/{tid}/standings").status_code == 200


# ---------- 事件流 ----------

def test_lifecycle_events_persisted_with_payloads(client):
    tid = create_tournament(client)
    add_players(client, tid, ["甲", "乙"])
    r1 = next_round(client, tid)
    start_clock(client, tid, 1, total=1000)
    close_round(client, r1)

    seq = current_seq(client, tid)
    pause(client, tid, seq, request_id="p")
    body = archive(client, tid, seq + 1, request_id="a")
    assert body.status_code == 200

    items = client.get(
        f"/tournaments/{tid}/events", params={"after": 0, "limit": 50}
    ).json()["items"]
    lifecycle = [e for e in items if e["type"].startswith("tournament.")]
    assert [e["type"] for e in lifecycle] == [
        "tournament.created",
        "tournament.paused",
        "tournament.archived",
    ]
    paused_event = lifecycle[1]
    assert paused_event["payload"] == {"frozen_rounds": []}
    assert paused_event["seq"] == seq + 1
    archived_event = lifecycle[2]
    assert archived_event["payload"] == {}
    assert archived_event["seq"] == seq + 2


def test_pause_event_carries_frozen_round_numbers(client):
    tid = create_tournament(client)
    add_players(client, tid, ["甲", "乙"])
    r1 = next_round(client, tid)
    start_clock(client, tid, 1, total=1000)
    seq = current_seq(client, tid)
    pause(client, tid, seq, request_id="p")
    items = client.get(f"/tournaments/{tid}/events").json()["items"]
    paused_event = next(e for e in items if e["type"] == "tournament.paused")
    assert paused_event["payload"]["frozen_rounds"] == [r1["round_no"]]


# ---------- 老库迁移 ----------

def test_legacy_db_gets_status_and_tournament_paused_columns(tmp_path):
    """升级前的老库（无 status / tournament_paused 列）启动后幂等补列。"""
    import sqlalchemy as sa
    from sqlalchemy.orm import sessionmaker

    from app import database as db_module
    from app.models import RoundClock, Tournament

    legacy_url = f"sqlite:///{tmp_path}/legacy.db"
    legacy_engine = sa.create_engine(legacy_url, future=True)
    with legacy_engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE tournaments (id INTEGER PRIMARY KEY, name VARCHAR(255) NOT NULL, created_at DATETIME NOT NULL)"))
        conn.execute(
            sa.text(
                "CREATE TABLE round_clocks (id INTEGER PRIMARY KEY, "
                "round_id INTEGER NOT NULL UNIQUE, version INTEGER NOT NULL, "
                "status VARCHAR(16) NOT NULL, total_seconds INTEGER NOT NULL, "
                "started_at DATETIME NOT NULL, accumulated_pause_seconds FLOAT NOT NULL, "
                "paused_at DATETIME, ended_at DATETIME, match_extensions JSON NOT NULL)"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO tournaments (id, name, created_at) "
                "VALUES (1, '老赛事', '2026-01-01 00:00:00')"
            )
        )
    legacy_engine.dispose()

    original_url = db_module.DATABASE_URL
    original_engine = db_module.engine
    original_session = db_module.SessionLocal
    try:
        new_engine = sa.create_engine(legacy_url, future=True)
        db_module.engine = new_engine
        db_module.SessionLocal = sessionmaker(bind=new_engine, future=True)
        db_module.init_db()
        db_module.init_db()  # 幂等：再跑一次不应报错

        with db_module.SessionLocal() as session:
            t = session.get(Tournament, 1)
            assert t.status == "active"
            now = datetime.utcnow()
            clock = RoundClock(
                round_id=999,
                version=0,
                status="paused",
                total_seconds=10,
                started_at=now,
                accumulated_pause_seconds=0.0,
                paused_at=now,
                ended_at=None,
                match_extensions={},
            )
            session.add(clock)
            session.commit()
            session.refresh(clock)
            # 老库中既有 paused 时钟视为手动暂停
            assert clock.tournament_paused is False
    finally:
        db_module.DATABASE_URL = original_url
        db_module.engine = original_engine
        db_module.SessionLocal = original_session
        new_engine.dispose()
