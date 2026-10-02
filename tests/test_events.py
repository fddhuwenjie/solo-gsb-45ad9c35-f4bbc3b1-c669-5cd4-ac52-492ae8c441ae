"""赛事事件流测试。

覆盖需求：
- 事务：事件与业务变更同事务写入；显式回滚 / 提交失败均不留事件；
  被拒绝的操作（校验失败、409）不产生事件；
- 序号：每赛事独立、从 1 单调递增、赛事间隔离；并发写请求下
  由数据库事务内原子计数器分配，序号唯一且连续（无重复/空号）；
- 发布：提交失败不广播；并发提交按 seq 顺序到达订阅者；
- SSE：Last-Event-ID 补发、实时推送、重放/实时切换竞态下
  不漏发/不乱序/不重复、断线续传、心跳注释、缓冲溢出回源、
  乱序缺口的挂起-回源合并、赛事隔离；
- 分页：按序号升序分页查询。

SSE 用例在后台线程运行真实 uvicorn 服务器（TestClient/ASGITransport
都会缓冲整个响应，无法消费无限流），用同步 httpx 客户端以线程方式
并发消费流与发起变更。
"""
import asyncio
import concurrent.futures
import json
import socket
import threading
import time
from datetime import datetime

import httpx
import pytest
import uvicorn

from app import config
from app.database import Base, SessionLocal, engine
from app.events.bus import _DROPPED, EventSnapshot, event_bus
from app.main import app
from app.models import Event, Participant, Tournament


# ---------- 基础工具 ----------

def reset_db() -> None:
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


@pytest.fixture()
def db_clean():
    reset_db()
    yield


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    reset_db()
    with TestClient(app) as c:
        yield c


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


def wait_until(cond, timeout=5.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        time.sleep(interval)
    raise AssertionError("condition not met before timeout")


def run_on_loop(coro_factory):
    """在 uvicorn 主事件循环上运行协程（从测试线程跨线程投递）。"""
    loop = event_bus._loop
    assert loop is not None
    return asyncio.run_coroutine_threadsafe(coro_factory(), loop).result(10)


# ---------- 真实服务器（SSE 流式测试用） ----------

class LiveServer:
    """在后台线程运行 uvicorn，提供真实 HTTP/SSE 通道。"""

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
                timeout_graceful_shutdown=1,
            )
        )
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def client(self) -> httpx.Client:
        return httpx.Client(base_url=self.base_url, timeout=10.0)

    def __enter__(self):
        self.thread.start()
        wait_until(lambda: self.server.started, timeout=10)
        return self

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(timeout=10)


@pytest.fixture()
def live_server():
    reset_db()
    with LiveServer() as srv:
        yield srv


# ---------- SSE 消费工具 ----------

def collect_sse(client, url, frames, comments, stop, headers=None):
    """同步消费 SSE：事件帧与注释分别追加到 frames/comments。

    stop(frames, comments) 为真时断开连接并返回（模拟客户端读够后
    主动关闭）。
    """
    with client.stream("GET", url, headers=headers) as resp:
        assert resp.status_code == 200, url
        assert resp.headers["content-type"].startswith("text/event-stream")
        cur: dict[str, str] = {}
        for line in resp.iter_lines():
            if line == "":
                if cur:
                    frames.append(cur)
                    cur = {}
                if stop(frames, comments):
                    return
                continue
            if line.startswith(":"):
                comments.append(line)
                if stop(frames, comments):
                    return
                continue
            field, _, value = line.partition(":")
            cur[field] = value.lstrip(" ")


class Collector(threading.Thread):
    """后台 SSE 收集线程：主线程可轮询 frames/comments。"""

    def __init__(self, client, url, stop, headers=None):
        super().__init__(daemon=True)
        self.frames: list[dict] = []
        self.comments: list[str] = []
        self.error: BaseException | None = None
        self._args = (client, url, stop, headers)

    def run(self):
        client, url, stop, headers = self._args
        try:
            collect_sse(
                client, url, self.frames, self.comments, stop, headers
            )
        except BaseException as exc:  # noqa: BLE001
            self.error = exc

    def join_checked(self, timeout=5.0):
        self.join(timeout=timeout)
        assert not self.is_alive(), "SSE 收集线程未在预期时间内结束"
        if self.error is not None:
            raise self.error


def parsed(frames):
    out = []
    for f in frames:
        data = json.loads(f["data"])
        out.append(
            {
                "id": int(f["id"]),
                "event": f["event"],
                "seq": data["seq"],
                "type": data["type"],
                "tournament_id": data["tournament_id"],
                "occurred_at": data["occurred_at"],
                "payload": data["payload"],
            }
        )
    return out


def seqs(frames):
    return [e["seq"] for e in parsed(frames)]


def sse_url(tid, after=None):
    url = f"/tournaments/{tid}/events/stream"
    if after is not None:
        url += f"?after={after}"
    return url


# ---------- 一、持久化、序号与赛事隔离 ----------

def test_events_persisted_with_monotonic_per_tournament_seq(client):
    tid1 = create_tournament(client, "甲赛")
    tid2 = create_tournament(client, "乙赛")
    add_players(client, tid1, ["A", "B", "C", "D"])
    add_players(client, tid2, ["X", "Y"])

    page1 = client.get(f"/tournaments/{tid1}/events").json()
    page2 = client.get(f"/tournaments/{tid2}/events").json()

    assert [e["seq"] for e in page1["items"]] == [1, 2, 3, 4, 5]
    assert [e["type"] for e in page1["items"]] == [
        "tournament.created",
        "participant.registered",
        "participant.registered",
        "participant.registered",
        "participant.registered",
    ]
    # 赛事隔离：另一赛事序号独立从 1 开始
    assert [e["seq"] for e in page2["items"]] == [1, 2, 3]
    assert all(e["tournament_id"] == tid2 for e in page2["items"])

    first, reg = page1["items"][0], page1["items"][1]
    assert first["payload"] == {}
    assert reg["payload"]["number"] == 1
    assert reg["payload"]["name"] == "A"
    assert "participant_id" in reg["payload"]
    # 发生时间为带 Z 的 UTC ISO 8601
    assert first["occurred_at"].endswith("Z")


def test_full_lifecycle_events_and_payloads(client):
    tid = create_tournament(client)
    add_players(client, tid, ["A", "B", "C", "D"])
    rnd = client.post(f"/tournaments/{tid}/rounds/next").json()
    m12, m34 = rnd["matches"]
    put_result(client, m12, 1.0, 0.0)
    put_result(client, m34, 0.5, 0.5)

    events = client.get(f"/tournaments/{tid}/events").json()["items"]
    assert [e["seq"] for e in events] == [1, 2, 3, 4, 5, 6, 7, 8, 9]
    assert [e["type"] for e in events] == [
        "tournament.created",
        "participant.registered",
        "participant.registered",
        "participant.registered",
        "participant.registered",
        "round.generated",
        "result.submitted",
        "result.submitted",
        "round.closed",
    ]

    gen = events[5]
    assert gen["payload"]["round_no"] == 1
    assert gen["payload"]["round_id"] == rnd["id"]
    assert gen["payload"]["status"] == "open"
    assert sorted(gen["payload"]["match_ids"]) == sorted(
        m["id"] for m in rnd["matches"]
    )

    sub, closed = events[6], events[-1]
    assert sub["payload"]["match_id"] == m12["id"]
    assert sub["payload"]["player_white_id"] == m12["player_white"]["id"]
    assert sub["payload"]["score_white"] == 1.0
    assert sub["payload"]["score_black"] == 0.0
    assert closed["type"] == "round.closed"
    assert closed["payload"]["reason"] == "all_results_submitted"


def test_single_player_round_emits_generated_then_closed(client):
    tid = create_tournament(client)
    add_players(client, tid, ["独狼"])
    rnd = client.post(f"/tournaments/{tid}/rounds/next").json()
    assert rnd["status"] == "closed"

    events = client.get(f"/tournaments/{tid}/events").json()["items"]
    assert [e["type"] for e in events] == [
        "tournament.created",
        "participant.registered",
        "round.generated",
        "round.closed",
    ]
    gen, closed = events[2], events[3]
    assert gen["payload"]["status"] == "closed"
    assert gen["payload"]["bye_match_id"] == rnd["matches"][0]["id"]
    assert closed["payload"]["reason"] == "no_matches"


def test_no_event_for_rejected_operations(client):
    tid = create_tournament(client)
    add_players(client, tid, ["A", "B"])
    rnd = client.post(f"/tournaments/{tid}/rounds/next").json()
    m = rnd["matches"][0]

    # 非法赛果（422）、未完成就再开一轮（409）、首轮后报名（409）
    assert (
        client.put(
            f"/matches/{m['id']}/result",
            json={"score_white": 0.5, "score_black": 1.0},
        ).status_code
        == 422
    )
    assert client.post(f"/tournaments/{tid}/rounds/next").status_code == 409
    assert (
        client.post(
            f"/tournaments/{tid}/participants", json={"name": "C"}
        ).status_code
        == 409
    )

    events = client.get(f"/tournaments/{tid}/events").json()["items"]
    assert [e["type"] for e in events] == [
        "tournament.created",
        "participant.registered",
        "participant.registered",
        "round.generated",
    ]


# ---------- 二、事务回滚 ----------

def test_explicit_rollback_leaves_no_event_or_data(db_clean):
    from app.events.service import (
        PARTICIPANT_REGISTERED,
        TOURNAMENT_CREATED,
        append_event,
    )

    db = SessionLocal()
    try:
        t = Tournament(name="回滚赛")
        db.add(t)
        db.flush()
        append_event(db, t.id, TOURNAMENT_CREATED, {})
        p = Participant(tournament_id=t.id, number=1, name="X")
        db.add(p)
        db.flush()
        append_event(
            db,
            t.id,
            PARTICIPANT_REGISTERED,
            {"participant_id": p.id, "number": 1, "name": "X"},
        )
        db.rollback()  # 模拟事务失败：业务数据与事件一起回滚
    finally:
        db.close()

    db2 = SessionLocal()
    try:
        assert db2.query(Event).count() == 0
        assert db2.query(Tournament).count() == 0
        assert db2.query(Participant).count() == 0
    finally:
        db2.close()


def test_commit_failure_leaves_no_event(client, monkeypatch):
    from sqlalchemy.orm import Session as SASession

    tid = create_tournament(client)
    assert len(client.get(f"/tournaments/{tid}/events").json()["items"]) == 1

    def boom(self):
        raise RuntimeError("simulated commit failure")

    monkeypatch.setattr(SASession, "commit", boom)
    # 提交失败：端点抛出异常（TestClient 默认向上抛），请求会话关闭
    # 时回滚——既没有参赛者，也没有事件
    with pytest.raises(RuntimeError, match="simulated commit failure"):
        client.post(f"/tournaments/{tid}/participants", json={"name": "Z"})
    monkeypatch.undo()

    events = client.get(f"/tournaments/{tid}/events").json()["items"]
    assert [e["seq"] for e in events] == [1]
    assert client.get(f"/tournaments/{tid}/participants").json() == []

    # 失败事务不消耗序号：下一次成功录入仍为 2
    add_players(client, tid, ["A"])
    events = client.get(f"/tournaments/{tid}/events").json()["items"]
    assert [(e["seq"], e["type"]) for e in events] == [
        (1, "tournament.created"),
        (2, "participant.registered"),
    ]


# ---------- 三、分页查询 ----------

def test_events_pagination(client):
    tid = create_tournament(client)
    add_players(client, tid, ["A", "B", "C", "D", "E"])  # 连同创建共 6 条

    seen: list[int] = []
    after = 0
    while True:
        page = client.get(
            f"/tournaments/{tid}/events",
            params={"after": after, "limit": 2},
        ).json()
        seen.extend(e["seq"] for e in page["items"])
        if not page["has_more"]:
            break
        after = page["last_seq"]
    assert seen == [1, 2, 3, 4, 5, 6]

    # after 取最大值：空页、has_more=False、last_seq 原样回显
    last = client.get(f"/tournaments/{tid}/events", params={"after": 6}).json()
    assert last["items"] == []
    assert last["has_more"] is False
    assert last["last_seq"] == 6

    beyond = client.get(
        f"/tournaments/{tid}/events", params={"after": 999}
    ).json()
    assert beyond["items"] == []
    assert beyond["last_seq"] == 999

    # 资源与参数校验
    assert client.get("/tournaments/9999/events").status_code == 404
    assert (
        client.get(f"/tournaments/{tid}/events", params={"limit": 0}).status_code
        == 422
    )
    assert (
        client.get(f"/tournaments/{tid}/events", params={"after": -1}).status_code
        == 422
    )


# ---------- 四、SSE：补发、实时、心跳、断线续传 ----------

def test_sse_replays_all_persisted_events(live_server):
    with live_server.client() as client:
        tid = create_tournament(client, "SSE 补发赛")
        add_players(client, tid, ["A", "B"])

        frames, comments = [], []
        collect_sse(
            client,
            sse_url(tid),
            frames,
            comments,
            stop=lambda f, c: len(f) >= 3,
        )
        ev = parsed(frames)
        assert [e["seq"] for e in ev] == [1, 2, 3]
        assert ev[0]["event"] == "tournament.created"
        assert ev[0]["id"] == ev[0]["seq"]
        assert any(x.startswith(": connected after=0") for x in comments)


def test_sse_replays_only_events_after_last_event_id(live_server):
    with live_server.client() as client:
        tid = create_tournament(client)
        add_players(client, tid, ["A", "B"])

        # Last-Event-ID=2：只补发 seq > 2
        frames, _ = [], []
        collect_sse(
            client,
            sse_url(tid),
            frames,
            _,
            stop=lambda f, c: len(f) >= 1,
            headers={"Last-Event-ID": "2"},
        )
        ev = parsed(frames)
        assert [e["seq"] for e in ev] == [3]
        assert ev[0]["type"] == "participant.registered"

        # ?after= 查询参数等价
        frames2, _2 = [], []
        collect_sse(
            client,
            sse_url(tid, after=1),
            frames2,
            _2,
            stop=lambda f, c: len(f) >= 2,
        )
        assert seqs(frames2) == [2, 3]

        # 非法 Last-Event-ID 返回 400（不建立流）
        r = client.get(sse_url(tid), headers={"Last-Event-ID": "abc"})
        assert r.status_code == 400
        r = client.get(sse_url(tid), headers={"Last-Event-ID": "-3"})
        assert r.status_code == 400
        # 不存在的赛事 404
        assert client.get(sse_url(9999)).status_code == 404


def test_sse_live_push_heartbeat_and_disconnect_resume(
    live_server, monkeypatch
):
    monkeypatch.setattr(config, "SSE_HEARTBEAT_SECONDS", 0.05)

    with live_server.client() as client:
        tid = create_tournament(client, "直播赛")
        add_players(client, tid, ["A", "B"])

        # 后台订阅；随后实时产生：生成轮次（4）+ 赛果（5）+ 封盘（6）
        collector = Collector(
            client, sse_url(tid), stop=lambda f, c: len(f) >= 6
        )
        collector.start()
        wait_until(lambda: event_bus.subscriber_count(tid) == 1)

        rnd = client.post(f"/tournaments/{tid}/rounds/next").json()
        m = rnd["matches"][0]
        put_result(client, m, 1.0, 0.0)
        collector.join_checked()

        assert seqs(collector.frames) == [1, 2, 3, 4, 5, 6]
        assert [e["type"] for e in parsed(collector.frames)][3:] == [
            "round.generated",
            "result.submitted",
            "round.closed",
        ]
        # 每条消息都带 SSE id，浏览器会自动用于 Last-Event-ID
        assert all("id" in f for f in collector.frames)

        # 断线续传：Last-Event-ID=4 只补 5、6，无重复
        frames2, comments2 = [], []
        collect_sse(
            client,
            sse_url(tid),
            frames2,
            comments2,
            stop=lambda f, c: len(f) >= 2,
            headers={"Last-Event-ID": "4"},
        )
        assert seqs(frames2) == [5, 6]

        # 无新事件时只收心跳注释，不收到事件
        frames3, comments3 = [], []
        collect_sse(
            client,
            sse_url(tid),
            frames3,
            comments3,
            stop=lambda f, c: sum(
                1 for x in c if x.startswith(": keep-alive")
            )
            >= 2,
            headers={"Last-Event-ID": "6"},
        )
        assert frames3 == []


def test_sse_replay_live_switch_race_no_loss_no_dup_no_reorder(live_server):
    """重放/实时切换竞态（确定性注入）。

    订阅建立后、补发尚未追平时，向订阅队列注入与数据库重叠
    （seq 2、3）以及超前（seq 4）的实时事件，验证：
    - 重叠事件在边界被去重，不重复；
    - 超前事件在数据库追平后按序补发，不漏不乱。
    """
    now = datetime.utcnow()

    with live_server.client() as client:
        tid = create_tournament(client, "竞态赛")
        add_players(client, tid, ["A", "B"])  # 已持久化 seq 1..3

        collector = Collector(
            client, sse_url(tid), stop=lambda f, c: len(f) >= 4
        )
        collector.start()
        wait_until(lambda: event_bus.subscriber_count(tid) == 1)

        # 直接经事件总线注入：2、3 与数据库补发重叠，4 只在内存
        event_bus.publish(
            [
                EventSnapshot(
                    tid, 2, "participant.registered",
                    {"participant_id": 1, "number": 1, "name": "A"}, now,
                ),
                EventSnapshot(
                    tid, 3, "participant.registered",
                    {"participant_id": 2, "number": 2, "name": "B"}, now,
                ),
                EventSnapshot(
                    tid, 4, "round.generated",
                    {
                        "round_id": 1,
                        "round_no": 1,
                        "status": "open",
                        "match_ids": [1],
                    },
                    now,
                ),
            ]
        )
        collector.join_checked()

        got = parsed(collector.frames)
        assert [e["seq"] for e in got] == [1, 2, 3, 4]
        assert len({e["seq"] for e in got}) == 4  # 无重复
        assert got[-1]["type"] == "round.generated"


def test_sse_rapid_live_burst_delivered_contiguously(live_server):
    """订阅建立后连续快速产生多条事件，序号连续且与数据库一致。"""
    with live_server.client() as client:
        tid = create_tournament(client, "连发赛")
        add_players(client, tid, ["A", "B", "C", "D"])

        collector = Collector(
            client, sse_url(tid), stop=lambda f, c: len(f) >= 9
        )
        collector.start()
        wait_until(lambda: event_bus.subscriber_count(tid) == 1)

        rnd = client.post(f"/tournaments/{tid}/rounds/next").json()  # 6
        m1, m2 = [x for x in rnd["matches"] if not x["is_bye"]]
        put_result(client, m1, 1.0, 0.0)  # 7
        put_result(client, m2, 0.0, 1.0)  # 8 + 封盘 9
        collector.join_checked()

        assert seqs(collector.frames) == list(range(1, 10))
        assert [e["type"] for e in parsed(collector.frames)][-4:] == [
            "round.generated",
            "result.submitted",
            "result.submitted",
            "round.closed",
        ]

        # 与持久化事件完全一致（可续传的依据）
        page = client.get(
            f"/tournaments/{tid}/events", params={"limit": 50}
        ).json()
        assert [e["seq"] for e in page["items"]] == seqs(collector.frames)


def test_sse_isolation_between_tournaments(live_server, monkeypatch):
    monkeypatch.setattr(config, "SSE_HEARTBEAT_SECONDS", 0.05)

    with live_server.client() as client:
        tid1 = create_tournament(client, "隔离甲")
        tid2 = create_tournament(client, "隔离乙")

        # 订阅 tid1（跳过其创建事件），只制造 tid2 的变更
        collector = Collector(
            client,
            sse_url(tid1),
            stop=lambda f, c: sum(
                1 for x in c if x.startswith(": keep-alive")
            )
            >= 2,
            headers={"Last-Event-ID": "1"},
        )
        collector.start()
        wait_until(lambda: event_bus.subscriber_count(tid1) == 1)
        add_players(client, tid2, ["X"])
        add_players(client, tid2, ["Y"])
        collector.join_checked()

        # tid1 的流上没有任何事件；断开后订阅被清理
        assert collector.frames == []
        wait_until(lambda: event_bus.subscriber_count(tid1) == 0)

        # tid2 的事件独立编号
        page2 = client.get(f"/tournaments/{tid2}/events").json()
        assert [e["seq"] for e in page2["items"]] == [1, 2, 3]
        assert all(e["tournament_id"] == tid2 for e in page2["items"])


def test_sse_disconnect_then_resume_with_last_event_id(live_server):
    """客户端断开后订阅被清理；用最后收到的 id 重连可精确续传。"""
    with live_server.client() as client:
        tid = create_tournament(client, "断线赛")
        add_players(client, tid, ["A", "B"])

        # 收满 3 条（创建 + 两名参赛者）后断开
        frames, comments = [], []
        collect_sse(
            client,
            sse_url(tid),
            frames,
            comments,
            stop=lambda f, c: len(f) >= 3,
        )
        assert seqs(frames) == [1, 2, 3]
        wait_until(lambda: event_bus.subscriber_count(tid) == 0)

        # 断线期间发生变更：seq 4..6
        rnd = client.post(f"/tournaments/{tid}/rounds/next").json()
        put_result(client, rnd["matches"][0], 1.0, 0.0)

        # 用最后收到的 SSE id 重连：恰好补发断线期间的 4..6
        last_id = frames[-1]["id"]
        assert last_id == "3"
        frames2, comments2 = [], []
        collect_sse(
            client,
            sse_url(tid),
            frames2,
            comments2,
            stop=lambda f, c: len(f) >= 3,
            headers={"Last-Event-ID": last_id},
        )
        assert seqs(frames2) == [4, 5, 6]
        assert [e["type"] for e in parsed(frames2)] == [
            "round.generated",
            "result.submitted",
            "round.closed",
        ]


def test_sse_buffer_overflow_backfills_without_loss(live_server):
    """订阅者缓冲溢出（哨兵）后，端点回源数据库并继续实时推送。"""
    with live_server.client() as client:
        tid = create_tournament(client, "溢出赛")
        add_players(client, tid, ["A", "B"])  # seq 1..3

        # 从 seq 3 之后订阅：补发为空，生成器停在实时等待
        collector = Collector(
            client,
            sse_url(tid),
            stop=lambda f, c: len(f) >= 1,
            headers={"Last-Event-ID": "3"},
        )
        collector.start()
        wait_until(lambda: event_bus.subscriber_count(tid) == 1)

        # 模拟总线溢出路径：向该订阅队列投递丢弃哨兵
        queue = next(iter(event_bus._subscribers[tid]))
        queue.put_nowait(_DROPPED)

        # 哨兵之后的新事件（seq 4）仍须按序送达
        add_players(client, tid, ["C"])
        collector.join_checked()
        assert seqs(collector.frames) == [4]
        ev = parsed(collector.frames)[0]
        assert ev["type"] == "participant.registered"
        assert ev["payload"]["name"] == "C"


# ---------- 五、并发：原子序号、发布顺序与提交失败不广播 ----------

def _run_concurrently(fn, n, barrier):
    """n 个线程在同一屏障处同时放行执行 fn(i)，返回 (results, errors)。"""
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


def test_concurrent_participant_writes_get_unique_contiguous_seqs(live_server):
    """并发录入单个/批量参赛者：编号与事件序号都唯一且连续。

    旧实现「查 max/count 后插入」在该场景下会撞唯一约束（500）
    或产生重号；现在进程级写锁串行化 + 计数器原子分配。
    """
    with live_server.client() as client:
        tid = create_tournament(client, "并发报名赛")  # seq 1

        n_single, bulk_size, bulk_count = 12, 3, 4
        total_added = n_single + bulk_size * bulk_count  # 24

        def call(i):
            local = live_server.client()
            try:
                if i < n_single:
                    r = local.post(
                        f"/tournaments/{tid}/participants",
                        json={"name": f"S{i}"},
                    )
                else:
                    j = i - n_single
                    r = local.post(
                        f"/tournaments/{tid}/participants/bulk",
                        json={"names": [f"B{j}-{k}" for k in range(bulk_size)]},
                    )
                assert r.status_code == 201, r.text
                return r.json()
            finally:
                local.close()

        barrier = threading.Barrier(n_single + bulk_count)
        results = _run_concurrently(call, n_single + bulk_count, barrier)

        # 参赛者编号 1..24 无重号、无空号
        numbers = sorted(p["number"] for batch in results for p in batch)
        assert numbers == list(range(1, total_added + 1))

        # 事件：seq 1..25 唯一连续；类型为 1 条 created + 24 条 registered
        page = client.get(
            f"/tournaments/{tid}/events", params={"limit": 200}
        ).json()
        assert page["has_more"] is False
        ev_seqs = [e["seq"] for e in page["items"]]
        assert ev_seqs == list(range(1, total_added + 2))
        types = [e["type"] for e in page["items"]]
        assert types[0] == "tournament.created"
        assert types[1:] == ["participant.registered"] * total_added
        # 编号与事件 payload 一一对应（不漏不串）
        payload_numbers = sorted(e["payload"]["number"] for e in page["items"][1:])
        assert payload_numbers == numbers


def test_concurrent_participant_vs_round_generation(live_server):
    """并发报名与生成首轮：恰好一方成功，事件序号依旧连续不乱。"""
    with live_server.client() as client:
        tid = create_tournament(client, "报名开轮竞争赛")
        add_players(client, tid, ["A", "B"])  # seq 2、3

        def call(i):
            local = live_server.client()
            try:
                barrier.wait(timeout=10)
                if i % 2 == 0:
                    r = local.post(
                        f"/tournaments/{tid}/participants",
                        json={"name": f"P{i}"},
                    )
                else:
                    r = local.post(f"/tournaments/{tid}/rounds/next")
                return r.status_code
            finally:
                local.close()

        n = 10
        barrier = threading.Barrier(n)
        statuses = _run_concurrently(call, n, barrier)

        rounds = client.get(f"/tournaments/{tid}/rounds").json()
        assert len(rounds) == 1  # 只生成了一轮
        successful_registrations = statuses.count(201)
        # 一旦首轮生成（201），其后的报名全部 409；先报名成功的
        # 参赛者与轮次生成二者的成功总数 == 201 个数
        assert successful_registrations >= 1
        assert statuses.count(409) == n - successful_registrations
        assert set(statuses) <= {201, 409}  # 不允许 500（唯一约束冲突等）

        page = client.get(
            f"/tournaments/{tid}/events", params={"limit": 200}
        ).json()
        ev_seqs = [e["seq"] for e in page["items"]]
        # 序号从 1 连续无缺、无重
        assert ev_seqs == list(range(1, len(ev_seqs) + 1))
        # 恰好一条 round.generated，且它之后不存在 participant 事件
        gen_indexes = [
            i for i, e in enumerate(page["items"])
            if e["type"] == "round.generated"
        ]
        assert len(gen_indexes) == 1
        assert all(
            e["type"] != "participant.registered"
            for e in page["items"][gen_indexes[0] + 1:]
        )


def test_concurrent_result_submits_sse_contiguous_ordered(live_server):
    """并发提交不同场次赛果：SSE 实时流收到连续且升序的序号。"""
    with live_server.client() as client:
        tid = create_tournament(client, "并发赛果赛")
        add_players(client, tid, [f"P{i}" for i in range(1, 9)])  # seq 2..9
        rnd = client.post(f"/tournaments/{tid}/rounds/next").json()  # seq 10
        real = [m for m in rnd["matches"] if not m["is_bye"]]
        assert len(real) == 4

        # 订阅实时流（seq 10 之后）：等待 4 条 result + 1 条 closed
        collector = Collector(
            client,
            sse_url(tid),
            stop=lambda f, c: len(f) >= 5,
            headers={"Last-Event-ID": "10"},
        )
        collector.start()
        wait_until(lambda: event_bus.subscriber_count(tid) == 1)

        def call(i):
            local = live_server.client()
            try:
                barrier.wait(timeout=10)
                r = local.put(
                    f"/matches/{real[i]['id']}/result",
                    json={"score_white": 1.0, "score_black": 0.0},
                )
                assert r.status_code == 200, r.text
            finally:
                local.close()

        barrier = threading.Barrier(4)
        _run_concurrently(call, 4, barrier)
        collector.join_checked(timeout=15)

        got = parsed(collector.frames)
        # 实时流：序号严格升序且连续（11..15），末条封盘
        assert [e["seq"] for e in got] == [11, 12, 13, 14, 15]
        assert [e["type"] for e in got[:4]] == ["result.submitted"] * 4
        assert got[-1]["type"] == "round.closed"

        # 实时流与持久化完全一致（续传依据），且比赛场次互不重复
        page = client.get(
            f"/tournaments/{tid}/events", params={"after": 10, "limit": 50}
        ).json()
        assert [e["seq"] for e in page["items"]] == [11, 12, 13, 14, 15]
        match_ids = [e["payload"]["match_id"] for e in page["items"][:4]]
        assert sorted(match_ids) == sorted(m["id"] for m in real)


def test_publish_ordered_by_seq_under_concurrent_commits(live_server):
    """并发提交（不同赛事类型混排）经总线投递到订阅队列时按 seq 升序。

    进程级写锁保证「提交顺序 == 发布顺序」；订阅队列内
    snapshot 的 seq 必须严格升序。
    """
    with live_server.client() as client:
        tid = create_tournament(client, "发布顺序赛")
        add_players(client, tid, ["A", "B"])

        wait_until(lambda: event_bus.subscriber_count(tid) == 0)
        # subscribe() 本身是同步的（Queue 延迟绑定循环），在测试线程
        # 创建即可；投递/读取均 run_on_loop 在主循环上进行
        queue = event_bus.subscribe(tid)
        try:
            n = 8

            def call(i):
                local = live_server.client()
                try:
                    barrier.wait(timeout=10)
                    r = local.post(
                        f"/tournaments/{tid}/participants",
                        json={"name": f"X{i}"},
                    )
                    assert r.status_code == 201, r.text
                finally:
                    local.close()

            barrier = threading.Barrier(n)
            _run_concurrently(call, n, barrier)

            # seq 3 之后已有的 4..11 全部入队（创建=1、两名参赛者=2、3）
            delivered = []
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    item = run_on_loop(
                        lambda: asyncio.wait_for(queue.get(), timeout=0.5)
                    )
                except TimeoutError:
                    if len(delivered) >= n:
                        break
                    continue
                assert item is not _DROPPED
                delivered.append(item.seq)
            assert delivered == list(range(4, 4 + n)), delivered
        finally:
            event_bus.unsubscribe(tid, queue)


def test_commit_failure_publishes_nothing(client, monkeypatch):
    """提交失败时事件与业务数据回滚，且绝不向事件总线广播。"""
    from sqlalchemy.orm import Session as SASession

    tid = create_tournament(client)

    # 订阅总线并包装 publish，记录真正被广播的快照（spy 在整个用例
    # 期间有效：失败请求不广播、成功请求才广播）
    queue = event_bus.subscribe(tid)
    published: list = []
    real_publish = event_bus.publish

    def spy(snapshots):
        published.extend(snapshots)
        real_publish(snapshots)

    event_bus.publish = spy
    try:
        def boom(self):
            raise RuntimeError("simulated commit failure")

        monkeypatch.setattr(SASession, "commit", boom)
        with pytest.raises(RuntimeError, match="simulated commit failure"):
            client.post(f"/tournaments/{tid}/participants", json={"name": "Z"})
        monkeypatch.undo()

        # 失败请求没有任何事件被广播
        assert published == []

        # 之后的成功请求正常广播且序号连续（失败事务未消耗序号）
        add_players(client, tid, ["A"])
        assert [s.seq for s in published] == [2]
    finally:
        event_bus.publish = real_publish
        event_bus.unsubscribe(tid, queue)

    events = client.get(f"/tournaments/{tid}/events").json()["items"]
    assert [(e["seq"], e["type"]) for e in events] == [
        (1, "tournament.created"),
        (2, "participant.registered"),
    ]


def test_atomic_counter_rollback_does_not_consume_seq(db_clean):
    """直接验证数据库层原子分配：并发事务不重号；回滚不占序号。"""
    from app.events.service import append_event
    from app.locking import writer_lock

    db = SessionLocal()
    t = Tournament(name="原子计数赛")
    db.add(t)
    db.flush()
    db.commit()
    tid = t.id
    db.close()

    # 模拟端点：持进程写锁，事务内原子分配序号
    seqs = []

    def write_one(rollback=False):
        s = SessionLocal()
        try:
            with writer_lock():
                ev = append_event(s, tid, "participant.registered", {"i": 1})
                seqs.append(ev.seq)
                if rollback:
                    s.rollback()
                else:
                    s.commit()
        finally:
            s.close()

    write_one()                 # seq 1，提交
    write_one(rollback=True)    # seq 2，回滚——不占号
    write_one()                 # seq 2，提交
    write_one()                 # seq 3，提交
    assert seqs == [1, 2, 2, 3]

    rows = (
        SessionLocal()
        .query(Event.seq)
        .filter(Event.tournament_id == tid)
        .order_by(Event.seq)
        .all()
    )
    assert [r for (r,) in rows] == [1, 2, 3]


def test_sse_out_of_order_live_gap_is_buffered_and_merged(live_server):
    """实时事件乱序到达（先 5 后 4）：5 挂起、回源后按序 4、5 输出。

    旧实现在 seq 缺口时直接 ``continue`` 丢弃超前事件，会漏发；
    现在超前事件进入 pending 堆，回源合并后不漏、不重、不乱。
    """
    now = datetime.utcnow()

    with live_server.client() as client:
        tid = create_tournament(client, "乱序缺口赛")
        add_players(client, tid, ["A", "B"])  # 持久化 seq 1..3

        collector = Collector(
            client, sse_url(tid), stop=lambda f, c: len(f) >= 5
        )
        collector.start()
        wait_until(lambda: event_bus.subscriber_count(tid) == 1)

        # 先发 seq 5（缺口），再发 seq 4（补齐）：模拟并发提交下
        # 总线投递瞬时乱序
        event_bus.publish(
            [
                EventSnapshot(
                    tid, 5, "round.closed",
                    {"round_id": 1, "round_no": 1,
                     "reason": "all_results_submitted"},
                    now,
                )
            ]
        )
        time.sleep(0.1)
        event_bus.publish(
            [
                EventSnapshot(
                    tid, 4, "result.submitted",
                    {"match_id": 1, "round_id": 1, "round_no": 1,
                     "player_white_id": 1, "player_black_id": 2,
                     "score_white": 1.0, "score_black": 0.0},
                    now,
                )
            ]
        )
        collector.join_checked(timeout=10)

        got = parsed(collector.frames)
        assert [e["seq"] for e in got] == [1, 2, 3, 4, 5]
        assert len({e["seq"] for e in got}) == 5
        assert got[-1]["type"] == "round.closed"
        assert got[-2]["type"] == "result.submitted"
