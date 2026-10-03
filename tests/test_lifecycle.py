"""赛事维护态与归档（状态控制）专项测试。

覆盖需求：
- 赛事初始为 active；pause/resume/archive 经 request_id 幂等键与
  expected_event_seq 乐观快照切换，每次变更与 tournament.* 事件
  同事务提交；
- 幂等：重复 request_id（参数一致）原样返回首次结果、不产生新事件；
  同键不同参数 409 不落库；序号快照冲突 409 不落库不占号；
- 状态机：active <-> paused 可互相切换；archive 只能从 paused 且
  所有轮次已结束、无待处理复核时进入；archived 为终态；
- pause 在同一事务冻结所有运行中的轮次时钟；resume 只恢复被赛事
  暂停的时钟，手动暂停的保持暂停，剩余时间不因维护流逝；
- 维护期间参赛者/轮次/赛果/签到/工位/计时/公告/复核/扣分/晋级
  写操作统一 409 且不留痕，查询与事件续传（含 SSE）仍可用；
- archive 后永久只读。

时间相关断言通过 monkeypatch app.services.clock.utcnow 注入固定时间
（生命周期服务与计时服务共用同一时钟源），不依赖真实等待。
"""
import concurrent.futures
import json
import socket
import threading
from datetime import datetime, timedelta

import pytest

from app.services import clock as clock_service


# ---------- 基础工具 ----------

def create_tournament(client, name="状态测试赛"):
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


def current_seq(client, tid):
    return client.get(f"/tournaments/{tid}/events").json()["last_seq"]


def event_types(client, tid):
    return [
        e["type"] for e in client.get(f"/tournaments/{tid}/events").json()["items"]
    ]


def do_status(client, tid, action, request_id, expected_event_seq):
    return client.post(
        f"/tournaments/{tid}/status/actions",
        json={
            "request_id": request_id,
            "expected_event_seq": expected_event_seq,
            "action": action,
        },
    )


def pause(client, tid, request_id="op-pause"):
    r = do_status(client, tid, "pause", request_id, current_seq(client, tid))
    assert r.status_code == 200, r.text
    return r.json()


def resume(client, tid, request_id="op-resume"):
    r = do_status(client, tid, "resume", request_id, current_seq(client, tid))
    assert r.status_code == 200, r.text
    return r.json()


def get_status(client, tid):
    r = client.get(f"/tournaments/{tid}")
    assert r.status_code == 200
    return r.json()["status"]


def get_clock(client, tid, round_no):
    r = client.get(f"/tournaments/{tid}/rounds/{round_no}/clock")
    assert r.status_code == 200
    return r.json()


def clock_action(client, tid, round_no, **body):
    return client.post(
        f"/tournaments/{tid}/rounds/{round_no}/clock/actions", json=body
    )


def start_clock(client, tid, round_no, total=1000, request_id="clk-start"):
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


# ---------- 初始状态与基本切换 ----------

def test_initial_status_active(client):
    tid = create_tournament(client)
    assert get_status(client, tid) == "active"
    # 列表接口同样返回状态
    listed = client.get("/tournaments").json()
    assert [t["status"] for t in listed if t["id"] == tid] == ["active"]


def test_pause_resume_cycle_and_events(client):
    tid = create_tournament(client)
    add_players(client, tid, ["甲", "乙"])

    body = pause(client, tid)
    assert body["status"] == "paused"
    assert body["previous_status"] == "active"
    assert body["action"] == "pause"
    assert body["frozen_clocks"] == []
    assert body["resumed_clocks"] == []
    assert body["event_seq"] == current_seq(client, tid)
    assert get_status(client, tid) == "paused"

    body = resume(client, tid)
    assert body["status"] == "active"
    assert body["previous_status"] == "paused"
    assert get_status(client, tid) == "active"

    # 每次变更都与赛事事件同事务提交
    assert event_types(client, tid) == [
        "tournament.created",
        "participant.registered",
        "participant.registered",
        "tournament.paused",
        "tournament.resumed",
    ]
    # 事件载荷含前后状态
    events = client.get(f"/tournaments/{tid}/events").json()["items"]
    paused_event = next(e for e in events if e["type"] == "tournament.paused")
    assert paused_event["payload"]["previous_status"] == "active"
    assert paused_event["payload"]["status"] == "paused"

    # active 与 paused 可反复互相切换
    pause(client, tid, "op-pause-2")
    resume(client, tid, "op-resume-2")
    assert get_status(client, tid) == "active"


def test_status_action_idempotent_replay(client):
    tid = create_tournament(client)
    seq = current_seq(client, tid)

    first = do_status(client, tid, "pause", "op-1", seq)
    assert first.status_code == 200
    replay = do_status(client, tid, "pause", "op-1", seq)
    assert replay.status_code == 200
    # 重复 request_id 原样返回首次结果，不产生新事件
    assert replay.json() == first.json()
    assert current_seq(client, tid) == first.json()["event_seq"]
    assert event_types(client, tid).count("tournament.paused") == 1

    # 同一 request_id 携带不同参数：409 且不留痕
    conflict = do_status(client, tid, "pause", "op-1", seq + 1)
    assert conflict.status_code == 409
    conflict2 = do_status(client, tid, "resume", "op-1", seq)
    assert conflict2.status_code == 409
    assert current_seq(client, tid) == first.json()["event_seq"]
    assert get_status(client, tid) == "paused"


def test_status_action_event_seq_conflict(client):
    tid = create_tournament(client)
    seq = current_seq(client, tid)

    r = do_status(client, tid, "pause", "op-bad-seq", seq + 1)
    assert r.status_code == 409
    assert "快照" in r.json()["detail"]
    # 状态未变、未占事件序号
    assert get_status(client, tid) == "active"
    assert current_seq(client, tid) == seq
    assert "tournament.paused" not in event_types(client, tid)


def test_invalid_transitions(client):
    tid = create_tournament(client)

    # active 上只能 pause
    r = do_status(client, tid, "resume", "op-x1", current_seq(client, tid))
    assert r.status_code == 409
    r = do_status(client, tid, "archive", "op-x2", current_seq(client, tid))
    assert r.status_code == 409

    pause(client, tid)
    # paused 上不能再 pause
    r = do_status(client, tid, "pause", "op-x3", current_seq(client, tid))
    assert r.status_code == 409
    assert get_status(client, tid) == "paused"

    resume(client, tid)
    # 非法迁移均不占事件序号
    assert event_types(client, tid) == [
        "tournament.created",
        "tournament.paused",
        "tournament.resumed",
    ]


# ---------- 时钟冻结与恢复 ----------

def test_pause_freezes_running_clock_and_resume_restores(client, fake_time):
    tid = create_tournament(client)
    add_players(client, tid, ["甲", "乙"])
    next_round(client, tid)
    start_clock(client, tid, 1, total=1000)

    fake_time.advance(100)  # 比赛进行 100 秒
    before = get_clock(client, tid, 1)
    assert before["status"] == "running"
    assert before["matches"][0]["remaining_seconds"] == 900

    body = pause(client, tid)
    # pause 在同一事务冻结运行中的轮次时钟
    assert body["frozen_clocks"] == [
        {"round_id": before["round_id"], "round_no": 1}
    ]
    frozen = get_clock(client, tid, 1)
    assert frozen["status"] == "paused"
    assert frozen["matches"][0]["remaining_seconds"] == 900
    assert frozen["paused_at"] == fake_time.t.isoformat() + "Z"

    fake_time.advance(5000)  # 维护持续 5000 秒
    still_frozen = get_clock(client, tid, 1)
    assert still_frozen["matches"][0]["remaining_seconds"] == 900

    body = resume(client, tid)
    assert body["resumed_clocks"] == [
        {"round_id": before["round_id"], "round_no": 1}
    ]
    restored = get_clock(client, tid, 1)
    assert restored["status"] == "running"
    # 剩余时间不因维护流逝
    assert restored["matches"][0]["remaining_seconds"] == 900

    fake_time.advance(100)
    assert get_clock(client, tid, 1)["matches"][0]["remaining_seconds"] == 800


def test_resume_keeps_manual_pause(client, fake_time):
    tid = create_tournament(client)
    add_players(client, tid, ["甲", "乙"])
    next_round(client, tid)
    start_clock(client, tid, 1, total=1000)

    fake_time.advance(50)
    # 运营方手动暂停时钟
    r = clock_action(
        client, tid, 1, request_id="clk-pause", expected_version=1, action="pause"
    )
    assert r.status_code == 200

    fake_time.advance(30)
    body = pause(client, tid)
    # 手动暂停的时钟不是「运行中」，不在冻结列表
    assert body["frozen_clocks"] == []

    fake_time.advance(5000)
    body = resume(client, tid)
    # resume 只恢复被赛事暂停的时钟：手动暂停的保持暂停
    assert body["resumed_clocks"] == []
    clock = get_clock(client, tid, 1)
    assert clock["status"] == "paused"
    assert clock["matches"][0]["remaining_seconds"] == 950

    # 手动恢复仍可用，且维护窗口计入累计暂停
    r = clock_action(
        client, tid, 1, request_id="clk-resume", expected_version=2, action="resume"
    )
    assert r.status_code == 200
    clock = get_clock(client, tid, 1)
    assert clock["status"] == "running"
    assert clock["matches"][0]["remaining_seconds"] == 950


def test_maintenance_pause_then_manual_pause_cycle(client, fake_time):
    """赛事恢复后时钟回到运行；之后的手动暂停不受后续维护影响。"""
    tid = create_tournament(client)
    add_players(client, tid, ["甲", "乙"])
    next_round(client, tid)
    start_clock(client, tid, 1, total=1000)

    fake_time.advance(10)
    pause(client, tid, "op-p1")
    fake_time.advance(100)
    resume(client, tid, "op-r1")
    assert get_clock(client, tid, 1)["status"] == "running"

    # 恢复后手动暂停；再次维护不影响它
    r = clock_action(
        client, tid, 1, request_id="clk-p2", expected_version=1, action="pause"
    )
    assert r.status_code == 200
    fake_time.advance(20)
    body = pause(client, tid, "op-p2")
    assert body["frozen_clocks"] == []
    body = resume(client, tid, "op-r2")
    assert body["resumed_clocks"] == []
    clock = get_clock(client, tid, 1)
    assert clock["status"] == "paused"
    assert clock["matches"][0]["remaining_seconds"] == 990


# ---------- 维护期间写操作统一 409 且不留痕 ----------

def _setup_open_round_tournament(client):
    """含进行中轮次、工位、公告与待处理复核的赛事（用于维护态拦截）。"""
    tid = create_tournament(client)
    players = add_players(client, tid, ["甲", "乙", "丙", "丁"])
    rnd = next_round(client, tid)
    match = rnd["matches"][0]

    r = client.post(
        f"/tournaments/{tid}/stations",
        json={"station_no": 1, "available": True, "tags": []},
    )
    assert r.status_code == 201

    r = client.post(
        f"/tournaments/{tid}/announcements",
        json={
            "request_id": "ann-1",
            "expected_event_seq": current_seq(client, tid),
            "body": "请注意比赛纪律",
            "rules": {"participant_ids": [players[0]["id"]]},
        },
    )
    assert r.status_code == 201
    announcement_id = r.json()["id"]

    # 提交一场赛果并发起复核（复核待处理）
    r = put_result(client, match["id"], 1.0, 0.0)
    assert r.status_code == 200
    r = client.post(
        f"/matches/{match['id']}/result-reviews",
        json={
            "request_id": "rev-1",
            "expected_event_seq": current_seq(client, tid),
            "score_white": 0.5,
            "score_black": 0.5,
            "reason": "比分登记有误",
        },
    )
    assert r.status_code == 201
    review_id = r.json()["id"]
    return tid, players, rnd, match, announcement_id, review_id


def test_writes_rejected_during_maintenance(client):
    tid, players, rnd, match, announcement_id, review_id = (
        _setup_open_round_tournament(client)
    )
    seq_before = current_seq(client, tid)
    pause(client, tid)
    seq_paused = current_seq(client, tid)
    assert seq_paused == seq_before + 1  # 仅多了 tournament.paused

    seq = seq_paused
    pid = players[0]["id"]
    write_attempts = [
        # 参赛者
        ("POST", f"/tournaments/{tid}/participants", {"name": "戊"}),
        ("POST", f"/tournaments/{tid}/participants/bulk", {"names": ["戊"]}),
        ("PUT", f"/tournaments/{tid}/participants/1/tags",
         {"required_tags": []}),
        # 轮次与赛果
        ("POST", f"/tournaments/{tid}/rounds/next", None),
        ("PUT", f"/matches/{match['id']}/result",
         {"score_white": 0.5, "score_black": 0.5}),
        # 签到
        ("PUT", f"/tournaments/{tid}/checkin-snapshots",
         {"request_id": "ci-1", "expected_event_seq": seq, "round_no": 2,
          "present_numbers": [1, 2, 3, 4], "absent_numbers": []}),
        ("POST", f"/tournaments/{tid}/checkin-snapshots/confirm",
         {"request_id": "ci-2", "expected_event_seq": seq, "round_no": 2}),
        # 工位与工位方案
        ("POST", f"/tournaments/{tid}/stations",
         {"station_no": 2, "available": True, "tags": []}),
        ("PUT", f"/tournaments/{tid}/stations/1",
         {"available": False, "tags": []}),
        ("POST", f"/tournaments/{tid}/rounds/1/station-plans",
         {"expected_version": 0}),
        # 计时
        ("POST", f"/tournaments/{tid}/rounds/1/clock/actions",
         {"request_id": "clk-1", "expected_version": 0, "action": "start",
          "total_seconds": 600}),
        # 公告
        ("POST", f"/tournaments/{tid}/announcements",
         {"request_id": "ann-2", "expected_event_seq": seq,
          "body": "维护通知", "rules": {"participant_ids": [pid]}}),
        ("POST", f"/announcements/{announcement_id}/revisions",
         {"request_id": "ann-3", "expected_event_seq": seq,
          "body": "修订", "rules": {"participant_ids": [pid]}}),
        ("POST",
         f"/announcements/{announcement_id}/revisions/1/acknowledgements",
         {"participant_id": pid}),
        # 复核
        ("POST", f"/matches/{match['id']}/result-reviews",
         {"request_id": "rev-2", "expected_event_seq": seq,
          "score_white": 0.0, "score_black": 1.0, "reason": "再次更正"}),
        ("POST", f"/result-reviews/{review_id}/decisions",
         {"participant_id": pid, "vote": "approve"}),
        # 扣分与晋级
        ("POST", f"/tournaments/{tid}/penalties",
         {"request_id": "pen-1", "expected_event_seq": seq,
          "participant_id": pid, "points": 0.5, "reason": "迟到"}),
        ("POST", f"/tournaments/{tid}/promotions",
         {"request_id": "promo-1", "expected_event_seq": seq,
          "qualifying_round_no": 1, "slots": 1}),
    ]
    for method, url, body in write_attempts:
        r = client.request(method, url, json=body)
        assert r.status_code == 409, (method, url, r.text)
        assert "维护态" in r.json()["detail"], (method, url, r.json())

    # 不留痕：事件序号未推进，各类数据未被创建/修改
    assert current_seq(client, tid) == seq_paused
    assert event_types(client, tid).count("tournament.paused") == 1
    assert len(client.get(f"/tournaments/{tid}/participants").json()) == 4
    assert len(client.get(f"/tournaments/{tid}/rounds").json()) == 1
    assert len(client.get(f"/tournaments/{tid}/stations").json()) == 1
    assert len(client.get(f"/tournaments/{tid}/announcements").json()) == 1
    reviews = client.get(f"/tournaments/{tid}/result-reviews").json()
    assert len(reviews) == 1 and reviews[0]["status"] == "pending"
    assert client.get(f"/tournaments/{tid}/penalties").json() == []
    assert client.get(f"/tournaments/{tid}/promotions").json() == []
    assert client.get(f"/tournaments/{tid}/checkin-snapshots").json() == []
    assert get_clock(client, tid, 1)["version"] == 0
    # 赛果未被改写
    rnd_after = client.get(f"/tournaments/{tid}/rounds/1").json()
    m = next(m for m in rnd_after["matches"] if m["id"] == match["id"])
    assert (m["score_white"], m["score_black"]) == (1.0, 0.0)


def test_penalty_and_promotion_writes_rejected_during_maintenance(client):
    """全部轮次结束后才可登记扣分/发布晋级；维护态下同样一律 409。"""
    tid = create_tournament(client)
    players = add_players(client, tid, ["甲", "乙"])
    rnd = next_round(client, tid)
    r = put_result(client, rnd["matches"][0]["id"], 1.0, 0.0)
    assert r.json()["status"] == "closed"

    # 维护前登记一笔扣分、发布一份晋级名单（供撤销/放弃请求使用）
    r = client.post(
        f"/tournaments/{tid}/penalties",
        json={
            "request_id": "pen-0",
            "expected_event_seq": current_seq(client, tid),
            "participant_id": players[0]["id"],
            "points": 0.5,
            "reason": "迟到",
        },
    )
    assert r.status_code == 201
    entry_id = r.json()["id"]
    r = client.post(
        f"/tournaments/{tid}/promotions",
        json={
            "request_id": "promo-0",
            "expected_event_seq": current_seq(client, tid),
            "qualifying_round_no": 1,
            "slots": 1,
        },
    )
    assert r.status_code == 201
    list_id = r.json()["id"]

    pause(client, tid)
    seq_paused = current_seq(client, tid)
    seq = seq_paused
    write_attempts = [
        ("POST", f"/tournaments/{tid}/penalties",
         {"request_id": "pen-1", "expected_event_seq": seq,
          "participant_id": players[1]["id"], "points": 1.0,
          "reason": "违规"}),
        ("POST", f"/penalties/{entry_id}/revoke",
         {"request_id": "pen-2", "expected_event_seq": seq,
          "reason": "误判"}),
        ("POST", f"/tournaments/{tid}/promotions",
         {"request_id": "promo-1", "expected_event_seq": seq,
          "qualifying_round_no": 1, "slots": 1}),
        ("POST", f"/promotion-lists/{list_id}/forfeits",
         {"request_id": "promo-2", "participant_id": players[0]["id"],
          "reason": "退赛"}),
    ]
    for method, url, body in write_attempts:
        r = client.request(method, url, json=body)
        assert r.status_code == 409, (method, url, r.text)
        assert "维护态" in r.json()["detail"], (method, url, r.json())

    # 不留痕：扣分仍生效、名单席位未放弃、事件序号未推进
    assert current_seq(client, tid) == seq_paused
    ledger = client.get(f"/tournaments/{tid}/penalties").json()
    assert len(ledger) == 1 and ledger[0]["status"] == "active"
    plist = client.get(f"/tournaments/{tid}/promotions/1").json()
    assert plist["seats"][0]["status"] == "active"
    assert plist["seats"][0]["current"]["participant_id"] == players[0]["id"]


def test_queries_available_during_maintenance(client):
    tid, players, rnd, match, announcement_id, review_id = (
        _setup_open_round_tournament(client)
    )
    pause(client, tid)

    get_urls = [
        f"/tournaments/{tid}",
        "/tournaments",
        f"/tournaments/{tid}/participants",
        f"/tournaments/{tid}/rounds",
        f"/tournaments/{tid}/rounds/1",
        f"/tournaments/{tid}/standings",
        f"/tournaments/{tid}/stations",
        f"/tournaments/{tid}/rounds/1/station-plans",
        f"/tournaments/{tid}/rounds/1/clock",
        f"/tournaments/{tid}/checkin-snapshots",
        f"/tournaments/{tid}/announcements",
        f"/announcements/{announcement_id}",
        f"/announcements/{announcement_id}/revisions",
        f"/announcements/{announcement_id}/revisions/1",
        f"/announcements/{announcement_id}/revisions/1/acknowledgements",
        f"/tournaments/{tid}/result-reviews",
        f"/result-reviews/{review_id}",
        f"/tournaments/{tid}/penalties",
        f"/tournaments/{tid}/participants/{players[0]['id']}/penalties",
        f"/tournaments/{tid}/promotions",
        f"/tournaments/{tid}/events",
    ]
    for url in get_urls:
        r = client.get(url)
        assert r.status_code == 200, (url, r.text)
    # 事件续传：after 参数照常分页
    page = client.get(f"/tournaments/{tid}/events", params={"after": 1}).json()
    assert page["items"] and page["items"][0]["seq"] == 2


def test_resume_restores_write_access(client):
    tid, players, rnd, match, announcement_id, review_id = (
        _setup_open_round_tournament(client)
    )
    pause(client, tid)
    r = client.post(
        f"/tournaments/{tid}/stations",
        json={"station_no": 2, "available": True, "tags": []},
    )
    assert r.status_code == 409

    resume(client, tid)
    # 恢复后写操作照常
    r = client.post(
        f"/tournaments/{tid}/stations",
        json={"station_no": 2, "available": True, "tags": []},
    )
    assert r.status_code == 201
    # 另一场对局可继续录入赛果
    other = next(m for m in rnd["matches"] if m["id"] != match["id"])
    r = put_result(client, other["id"], 0.5, 0.5)
    assert r.status_code == 200
    # 复核决定也可继续
    r = client.post(
        f"/result-reviews/{review_id}/decisions",
        json={"participant_id": players[0]["id"], "vote": "reject"},
    )
    assert r.status_code == 201


# ---------- 归档 ----------

def test_archive_requires_paused_and_closed_rounds(client):
    tid = create_tournament(client)
    add_players(client, tid, ["甲", "乙", "丙", "丁"])
    next_round(client, tid)

    # active 状态不能归档
    r = do_status(client, tid, "archive", "op-a1", current_seq(client, tid))
    assert r.status_code == 409

    pause(client, tid)
    # 第 1 轮仍在进行：不能归档
    r = do_status(client, tid, "archive", "op-a2", current_seq(client, tid))
    assert r.status_code == 409
    assert "尚未结束" in r.json()["detail"]
    assert get_status(client, tid) == "paused"


def test_archive_requires_no_pending_review(client):
    tid = create_tournament(client)
    players = add_players(client, tid, ["甲", "乙"])
    rnd = next_round(client, tid)
    match = rnd["matches"][0]
    r = put_result(client, match["id"], 1.0, 0.0)
    assert r.json()["status"] == "closed"  # 唯一一场录完即封盘

    r = client.post(
        f"/matches/{match['id']}/result-reviews",
        json={
            "request_id": "rev-1",
            "expected_event_seq": current_seq(client, tid),
            "score_white": 0.5,
            "score_black": 0.5,
            "reason": "比分有误",
        },
    )
    assert r.status_code == 201
    review_id = r.json()["id"]

    pause(client, tid)
    # 轮次已结束但存在待处理复核：不能归档
    r = do_status(client, tid, "archive", "op-a1", current_seq(client, tid))
    assert r.status_code == 409
    assert "复核" in r.json()["detail"]

    # 恢复 -> 驳回复核 -> 再暂停 -> 归档成功
    resume(client, tid)
    r = client.post(
        f"/result-reviews/{review_id}/decisions",
        json={"participant_id": players[0]["id"], "vote": "reject"},
    )
    assert r.status_code == 201
    pause(client, tid, "op-p2")
    r = do_status(client, tid, "archive", "op-a2", current_seq(client, tid))
    assert r.status_code == 200
    assert r.json()["status"] == "archived"
    assert r.json()["previous_status"] == "paused"


def test_archive_without_rounds_allowed(client):
    """没有任何轮次时「所有轮次已结束」 vacuously 成立，可归档。"""
    tid = create_tournament(client)
    add_players(client, tid, ["甲", "乙"])
    pause(client, tid)
    r = do_status(client, tid, "archive", "op-a1", current_seq(client, tid))
    assert r.status_code == 200
    assert r.json()["status"] == "archived"


def test_archive_permanent_readonly(client):
    tid = create_tournament(client)
    players = add_players(client, tid, ["甲", "乙"])
    rnd = next_round(client, tid)
    match = rnd["matches"][0]
    r = put_result(client, match["id"], 1.0, 0.0)
    assert r.json()["status"] == "closed"

    pause(client, tid)
    body = do_status(client, tid, "archive", "op-a1", current_seq(client, tid))
    assert body.status_code == 200
    body = body.json()
    assert body["status"] == "archived"
    assert get_status(client, tid) == "archived"
    assert "tournament.archived" in event_types(client, tid)

    # 归档是终态：任何状态切换都被拒（新 request_id）
    for action in ("pause", "resume", "archive"):
        r = do_status(
            client, tid, action, f"op-after-{action}",
            current_seq(client, tid),
        )
        assert r.status_code == 409, (action, r.text)
    # 归档请求本身幂等：重复 request_id 原样返回首次结果
    replay = do_status(client, tid, "archive", "op-a1", body["expected_event_seq"])
    assert replay.status_code == 200
    assert replay.json() == body

    # 永久只读：各类写操作一律 409
    seq = current_seq(client, tid)
    write_attempts = [
        ("POST", f"/tournaments/{tid}/participants", {"name": "丙"}),
        ("POST", f"/tournaments/{tid}/rounds/next", None),
        ("PUT", f"/matches/{match['id']}/result",
         {"score_white": 0.0, "score_black": 1.0}),
        ("POST", f"/tournaments/{tid}/stations",
         {"station_no": 1, "available": True, "tags": []}),
        ("POST", f"/tournaments/{tid}/penalties",
         {"request_id": "pen-1", "expected_event_seq": seq,
          "participant_id": players[0]["id"], "points": 0.5,
          "reason": "迟到"}),
        ("POST", f"/tournaments/{tid}/promotions",
         {"request_id": "promo-1", "expected_event_seq": seq,
          "qualifying_round_no": 1, "slots": 1}),
    ]
    for method, url, payload in write_attempts:
        r = client.request(method, url, json=payload)
        assert r.status_code == 409, (method, url, r.text)
        assert "归档" in r.json()["detail"], (method, url, r.json())
    # 不留痕
    assert current_seq(client, tid) == seq

    # 查询与事件续传仍可用
    assert client.get(f"/tournaments/{tid}/standings").status_code == 200
    assert client.get(f"/tournaments/{tid}/events").status_code == 200


# ---------- 并发 ----------

def test_concurrent_pause_only_one_succeeds(client):
    tid = create_tournament(client)
    seq = current_seq(client, tid)

    def pause_with(rid):
        return do_status(client, tid, "pause", rid, seq)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(pause_with, ["op-c1", "op-c2"]))
    codes = sorted(r.status_code for r in results)
    assert codes == [200, 409]
    assert get_status(client, tid) == "paused"
    # 只有一次切换落库：一个 tournament.paused 事件
    assert event_types(client, tid).count("tournament.paused") == 1


# ---------- SSE 事件流在维护期间可用 ----------

class _LiveServer:
    """在后台线程运行 uvicorn，提供真实 HTTP/SSE 通道。

    TestClient/ASGITransport 会缓冲整个响应，无法消费无限 SSE 流。
    """

    def __init__(self):
        import uvicorn

        from app.main import app

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
                timeout_graceful_shutdown=1,
            )
        )
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self):
        self.thread.start()
        assert _wait_until(lambda: self.server.started, timeout=10)
        return self

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(timeout=10)


@pytest.fixture()
def live_server_client():
    import httpx

    from app.database import Base, engine

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with _LiveServer() as srv:
        with httpx.Client(base_url=srv.base_url, timeout=10.0) as client:
            yield client, srv.base_url


def _wait_until(predicate, timeout=5.0):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_sse_stream_available_during_maintenance(live_server_client):
    """维护期间事件续传（SSE）照常：订阅后能实时收到状态变更事件。"""
    client, base_url = live_server_client
    tid = create_tournament(client)
    add_players(client, tid, ["甲", "乙"])

    import httpx

    frames: list[dict] = []
    connected = threading.Event()
    error: list[BaseException] = []
    seq0 = current_seq(client, tid)  # 只订阅之后的新事件

    def consume():
        """订阅事件流：收满 pause/resume 两帧后主动断开。"""
        try:
            with httpx.Client(base_url=base_url, timeout=10.0) as http:
                with http.stream(
                    "GET", f"/tournaments/{tid}/events/stream?after={seq0}"
                ) as resp:
                    assert resp.status_code == 200
                    cur: dict[str, str] = {}
                    for line in resp.iter_lines():
                        if line.startswith(":"):
                            if line.startswith(": connected"):
                                connected.set()
                            continue
                        if line == "":
                            if cur:
                                frames.append(cur)
                                cur = {}
                                if len(frames) >= 2:
                                    return
                            continue
                        field, _, value = line.partition(":")
                        cur[field] = value.lstrip(" ")
        except BaseException as exc:  # noqa: BLE001
            error.append(exc)

    t = threading.Thread(target=consume, daemon=True)
    t.start()
    assert connected.wait(timeout=5)

    # 维护态切换事件实时推送给订阅者
    pause(client, tid)
    assert _wait_until(lambda: len(frames) >= 1)
    first = frames[0]
    assert first["event"] == "tournament.paused"
    data = json.loads(first["data"])
    assert data["payload"]["status"] == "paused"

    resume(client, tid)
    t.join(timeout=5)
    assert not t.is_alive()
    if error:
        raise error[0]
    assert frames[1]["event"] == "tournament.resumed"

    # 维护期间断线续传：凭 Last-Event-ID 回源补齐错过的事件
    with httpx.Client(base_url=base_url, timeout=10.0) as http:
        with http.stream(
            "GET",
            f"/tournaments/{tid}/events/stream",
            headers={"Last-Event-ID": str(int(first["id"]) - 1)},
        ) as resp:
            assert resp.status_code == 200
            cur: dict[str, str] = {}
            replayed = None
            for line in resp.iter_lines():
                if line.startswith(":"):
                    continue
                if line == "":
                    if cur:
                        replayed = cur
                        break
                    continue
                field, _, value = line.partition(":")
                cur[field] = value.lstrip(" ")
            assert replayed is not None
            assert replayed["event"] == "tournament.paused"
            assert int(replayed["id"]) == int(first["id"])
