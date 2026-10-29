"""SSE 断线续传测试。

核心断言只有一个：**断线不影响生成**。做法是读到第 2、3 块就断开，
再从那个 `id:` 续上 —— 如果生产者还跟连接绑在一起（改造前的实现），
续传这里要么收到 error 帧、要么永远等不到 `done`，拼接结果也不可能
等于完整回答。

跑的是真 MySQL + 真 Redis（缓冲就在 Redis Stream 里），LLM 用桩。
"""

import asyncio
import json
import time

import pytest
from app.core.config import Settings
from app.db.enums import MessageStatus
from app.schemas.chat import ChatRequest
from app.services.conversation_service import TurnContext
from app.services.llm_service import StreamFinished, TokenDelta
from app.services.stream_hub import (
    STREAM_START,
    StreamBuffer,
    TurnRunner,
    stream_frames,
)
from fastapi.testclient import TestClient
from redis.asyncio import Redis

CHAT_STREAM = "/api/v1/chat/stream"
CONVERSATIONS = "/api/v1/conversations"
CODE_PATH = "/api/v1/auth/verification-code"
REGISTER_PATH = "/api/v1/auth/register"

PASSWORD = "correct horse battery staple"


# ------------------------------------------------------------------ 工具


def _signup(client: TestClient, mail, email: str) -> dict[str, str]:
    assert client.post(CODE_PATH, json={"email": email}).status_code == 200
    response = client.post(
        REGISTER_PATH,
        json={"email": email, "password": PASSWORD, "code": mail.last_code},
    )
    assert response.status_code == 201, response.text
    token = response.json()["data"]["token"]["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _parse_frame(lines: list[str]) -> dict[str, str | None]:
    """一帧 → {event, id, data}。`id` 是续传的锚点，必须能取出来。"""
    frame: dict[str, str | None] = {"event": None, "id": None, "data": None}
    for line in lines:
        if line.startswith("event: "):
            frame["event"] = line[7:]
        elif line.startswith("id: "):
            frame["id"] = line[4:]
        elif line.startswith("data: "):
            frame["data"] = line[6:]
        # 注释行 `: ping ...` 直接忽略
    return frame


def _collect(response, stop_after_deltas: int | None = None) -> list[dict[str, str | None]]:
    """读 SSE 帧；`stop_after_deltas` 用来在"还没说完"时主动断开。"""
    frames: list[dict[str, str | None]] = []
    buffer: list[str] = []
    seen = 0
    for line in response.iter_lines():
        if line == "":
            if not buffer:
                continue
            frames.append(_parse_frame(buffer))
            buffer = []
            if stop_after_deltas is not None and frames[-1]["event"] == "delta":
                seen += 1
                if seen >= stop_after_deltas:
                    return frames  # 出 with 就断线
            continue
        buffer.append(line)
    if buffer:
        frames.append(_parse_frame(buffer))
    return frames


def _text(frames: list[dict[str, str | None]]) -> str:
    return "".join(json.loads(f["data"])["delta"] for f in frames if f["event"] == "delta")


def _events(frames: list[dict[str, str | None]]) -> list[str | None]:
    return [f["event"] for f in frames]


def _ids(frames: list[dict[str, str | None]]) -> list[str]:
    return [f["id"] for f in frames if f["id"]]


def _id_key(raw: str) -> tuple[int, int]:
    """Redis Stream id（`<毫秒>-<序号>`）按数值比较。"""
    milliseconds, _, sequence = raw.partition("-")
    return int(milliseconds), int(sequence)


def _start(
    client: TestClient, headers: dict[str, str], message: str, stop_after: int | None = None
):
    """发一轮流式对话；返回 (帧, start 里的会话/消息 ID)。"""
    with client.stream("POST", CHAT_STREAM, json={"message": message}, headers=headers) as response:
        assert response.status_code == 200, response.read()
        frames = _collect(response, stop_after_deltas=stop_after)
    assert frames[0]["event"] == "start", frames[:2]
    return frames, json.loads(frames[0]["data"])


def _resume(client: TestClient, headers: dict[str, str], message_id: str, last_id: str | None):
    extra = {"Last-Event-ID": last_id} if last_id else {}
    with client.stream(
        "GET", f"{CHAT_STREAM}/{message_id}", headers={**headers, **extra}
    ) as response:
        assert response.status_code == 200, response.read()
        return _collect(response)


def _wait_last_message(
    client: TestClient,
    headers: dict[str, str],
    conversation_id: str,
    expected: set[str],
    timeout: float = 20.0,
) -> dict:
    """轮询最后一条消息，直到状态进入 expected。

    生成在服务端独立进行，测试只能靠"看库"来等它 —— 没有连接可等。
    """
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        items = client.get(f"{CONVERSATIONS}/{conversation_id}/messages", headers=headers).json()[
            "data"
        ]["items"]
        last = items[-1]
        if last["status"] in expected:
            return last
        time.sleep(0.2)
    raise AssertionError(f"等待状态 {expected} 超时，最后状态：{last.get('status')}")


def _drop_buffer(test_redis_url: str, message_id: str) -> None:
    """删掉这一轮的缓冲，模拟 TTL 到期 / 进程重启。"""

    async def drop() -> None:
        client = Redis.from_url(test_redis_url, decode_responses=True)
        try:
            keys = [key async for key in client.scan_iter(match=f"sse:*:{message_id}")]
            if keys:
                await client.delete(*keys)
        finally:
            await client.aclose()

    asyncio.run(drop())


# ------------------------------------------------------------------ 帧格式


def test_frames_carry_stream_ids(
    auth_client: TestClient, auth_headers: dict[str, str], stub_llm
) -> None:
    """start 帧给出身份，delta 帧带 Redis Stream id —— 那是续传的锚点。"""
    stub_llm(answer="你好世界")

    frames, ids = _start(auth_client, auth_headers, "你好")

    assert frames[0]["event"] == "start"
    # start 帧**不带** id：客户端在收到第一块之前断开时 Last-Event-ID 为空，
    # 续传从 0-0 开始，正好是"从头重放"
    assert frames[0]["id"] is None
    assert ids["conversation_id"].isdigit() and ids["message_id"].isdigit()

    deltas = [f for f in frames if f["event"] == "delta"]
    assert deltas and all(f["id"] for f in deltas)
    ids_seen = _ids(frames)
    assert len(set(ids_seen)) == len(ids_seen), "id 必须唯一，重复了就会读到重复内容"
    assert sorted(ids_seen, key=_id_key) == ids_seen, "id 必须单调递增"
    assert _events(frames)[-1] == "done"


# ------------------------------------------------------------------ 续传


def test_resume_from_last_event_id(
    auth_client: TestClient, auth_headers: dict[str, str], stub_llm
) -> None:
    """断线后从最后收到的 id 续上，前后拼起来必须等于完整回答。

    这条是整个功能的验收用例：它同时证明了
      1. 断开连接没有把生成掐掉（否则续传等不到 done）；
      2. 续传没有重复下发已收到的块（否则文本会出现重影）；
      3. 续传没有漏块（否则文本会缺字）。
    """
    answer = "一二三四五六七八九十甲乙丙丁"
    # 每块 50ms，读到第 3 块时生成还远没结束
    stub_llm(answer=answer, delay=0.05)

    first, ids = _start(auth_client, auth_headers, "讲个故事", stop_after=3)
    seen = _text(first)
    last_id = _ids(first)[-1]
    assert len(seen) == 3, f"断线时应该只收到 3 块，实际 {seen!r}"

    rest = _resume(auth_client, auth_headers, ids["message_id"], last_id)

    # 带游标续读**不重发 start**：客户端既然能拿出 id，就说明它收到过帧，
    # 会话/消息 ID 早已知道。服务端能判断的事不该推给客户端幂等。
    assert rest[0]["event"] == "delta"
    assert _events(rest)[-2:] == ["meta", "done"]
    assert seen + _text(rest) == answer

    # 库里也必须是完整的：断线不落库的话，刷新页面就只剩半句
    last = _wait_last_message(auth_client, auth_headers, ids["conversation_id"], {"complete"})
    assert last["content"] == answer


def test_resume_without_id_replays_from_beginning(
    auth_client: TestClient, auth_headers: dict[str, str], stub_llm
) -> None:
    """不带 Last-Event-ID = 从头重放（刷新页面继续看就是这条）。"""
    answer = "完整回答在这里"
    stub_llm(answer=answer)

    frames, ids = _start(auth_client, auth_headers, "你好")
    replay = _resume(auth_client, auth_headers, ids["message_id"], None)

    # 没有游标 = 从头重放，这时候才需要 start 帧
    assert replay[0]["event"] == "start"
    assert _text(replay) == answer
    assert _events(replay)[-2:] == ["meta", "done"]


def test_resume_after_done_ends_immediately(
    auth_client: TestClient, auth_headers: dict[str, str], stub_llm
) -> None:
    """已经读到 done 之后再续：立刻回一个 done，不能挂着等永远不会来的数据。"""
    stub_llm(answer="短回答")

    frames, ids = _start(auth_client, auth_headers, "你好")
    done_id = _ids(frames)[-1]

    tail = _resume(auth_client, auth_headers, ids["message_id"], done_id)

    # 只补了一个 done：内容客户端已经有了，start 也不需要（有游标就没重发）
    assert _events(tail) == ["done"]


def test_resume_expired_buffer_returns_410(
    test_redis_url: str, auth_client: TestClient, auth_headers: dict[str, str], stub_llm
) -> None:
    """缓冲过期 → 410 而不是 404。

    消息还在库里，只是流的分片没了。回 404 会让前端以为消息被删了，
    实际上它应该去拉消息全文。
    """
    stub_llm(answer="回答")
    _, ids = _start(auth_client, auth_headers, "你好")

    _drop_buffer(test_redis_url, ids["message_id"])

    response = auth_client.get(
        f"{CHAT_STREAM}/{ids['message_id']}",
        headers=auth_headers,
        params={"last_event_id": "0-0"},
    )
    assert response.status_code == 410
    assert response.json()["code"] == 22410


def test_resume_requires_login(auth_client: TestClient, stub_llm) -> None:
    stub_llm(answer="回答")

    response = auth_client.get(f"{CHAT_STREAM}/373824473754763264")

    assert response.status_code == 401
    assert response.json()["code"] == 11401


def test_resume_rejects_other_users_message(
    auth_client: TestClient, auth_headers: dict[str, str], stub_mail, make_email, stub_llm
) -> None:
    """别人的消息：404，和"不存在"同一个响应，不给探测留信号。"""
    stub_llm(answer="回答")
    _, ids = _start(auth_client, auth_headers, "你好")
    other = _signup(auth_client, stub_mail, make_email())

    response = auth_client.get(f"{CHAT_STREAM}/{ids['message_id']}", headers=other)

    assert response.status_code == 404
    assert response.json()["code"] == 10404


def test_resume_unknown_message(auth_client: TestClient, auth_headers: dict[str, str]) -> None:
    response = auth_client.get(f"{CHAT_STREAM}/373824473754763264", headers=auth_headers)

    assert response.status_code == 404


def test_resume_rejects_malformed_last_event_id(
    auth_client: TestClient, auth_headers: dict[str, str], stub_llm
) -> None:
    """格式不对的 id 直接 400：放过去会让 XREAD 抛错变成 500。"""
    stub_llm(answer="回答")
    _, ids = _start(auth_client, auth_headers, "你好")

    response = auth_client.get(
        f"{CHAT_STREAM}/{ids['message_id']}",
        headers={**auth_headers, "Last-Event-ID": "not-an-id;DROP"},
    )

    assert response.status_code == 400
    assert response.json()["code"] == 10400


# ------------------------------------------------- 生成与连接的解耦（单元级）


class StubStreamingService:
    """只实现 TurnRunner 用到的东西：`model` 和 `astream_chat`。"""

    model = "stub-model"

    def __init__(self, answer: str, delay: float = 0.0) -> None:
        self.answer = answer
        self.delay = delay

    async def astream_chat(self, payload: object, history: object):
        for piece in self.answer:
            if self.delay:
                await asyncio.sleep(self.delay)
            yield TokenDelta(piece)
        yield StreamFinished(
            model=self.model,
            prompt_tokens=1,
            completion_tokens=len(self.answer),
        )


class RecordingConversations:
    """记录收尾调用，不碰数据库 —— 这两条用例只关心"生成有没有被连接带走"。"""

    def __init__(self) -> None:
        self.completed: list[dict] = []
        self.failed: list[dict] = []

    async def complete_turn(
        self, context, *, answer: str, status: MessageStatus = MessageStatus.COMPLETE, **_: object
    ) -> None:
        self.completed.append({"answer": answer, "status": status})

    async def fail_turn(self, context, *, reason: str, answer: str = "", **_: object) -> None:
        self.failed.append({"reason": reason, "answer": answer})


async def _cleanup_sse_keys(redis: Redis, turn_id: int) -> None:
    keys = [key async for key in redis.scan_iter(match=f"sse:*:{turn_id}")]
    if keys:
        await redis.delete(*keys)


def _make_runner(buffer: StreamBuffer, service, conversations, turn_id: int, settings):
    return TurnRunner(
        service=service,
        conversations=conversations,
        context=TurnContext(conversation_id=1, assistant_message_id=turn_id),
        payload=ChatRequest(message="问题"),
        history=[],
        buffer=buffer,
        settings=settings,
    )


def test_generation_continues_after_consumer_disappears(test_redis_url: str) -> None:
    """消费者读到一半就走，生成仍然把话说完、把库写完。

    这条用例绕开了 HTTP：TestClient 不会真的发 disconnect，用它根本模拟不出
    "连接没了"这个动作，测出来的东西也就不作数。
    """
    answer = "一二三四五六七八"
    turn_id = 990001

    async def scenario() -> tuple[list[dict], list[str]]:
        redis = Redis.from_url(test_redis_url, decode_responses=True)
        try:
            buffer = StreamBuffer(redis, 600)
            settings = Settings(_env_file=None, SSE_RESUME_GRACE_SECONDS=30)
            conversations = RecordingConversations()
            runner = _make_runner(
                buffer, StubStreamingService(answer, delay=0.03), conversations, turn_id, settings
            )
            task = asyncio.create_task(runner.run())

            # 消费者读两块就 aclose()，等价于浏览器把连接关了
            stream = stream_frames(
                buffer=buffer,
                context=TurnContext(conversation_id=1, assistant_message_id=turn_id),
                after=STREAM_START,
                settings=settings,
            )
            deltas = 0
            async for frame in stream:
                if frame.event == "delta":
                    deltas += 1
                    if deltas == 2:
                        break
            await stream.aclose()
            assert deltas == 2

            # 生成端不受影响，应该自己跑完
            await asyncio.wait_for(task, timeout=10)

            frames = await buffer.read(turn_id, STREAM_START, block_ms=None)
            return conversations.completed, [frame.event for frame in frames]
        finally:
            await _cleanup_sse_keys(redis, turn_id)
            await redis.aclose()

    completed, events = asyncio.run(scenario())

    assert len(completed) == 1
    # 关键：断线之后仍然把整段话说完了，而且落库是 complete
    assert completed[0]["answer"] == answer
    assert completed[0]["status"] is MessageStatus.COMPLETE
    assert events[-2:] == ["meta", "done"]


def test_generation_stops_when_nobody_listens(test_redis_url: str) -> None:
    """断线后一直没人回来：到 grace 就停，落库为 interrupted。

    这是续传必须付的代价的另一面 —— 不间断地等下去就是无上限烧 token，
    所以要有一个"离开多久算放弃"的界线。
    """
    answer = "长" * 40  # 4 秒才能说完，远长于下面的 grace
    turn_id = 990002

    async def scenario() -> tuple[list[dict], list[tuple[str, str]]]:
        redis = Redis.from_url(test_redis_url, decode_responses=True)
        try:
            buffer = StreamBuffer(redis, 600)
            settings = Settings(_env_file=None, SSE_RESUME_GRACE_SECONDS=2)
            conversations = RecordingConversations()
            runner = _make_runner(
                buffer, StubStreamingService(answer, delay=0.1), conversations, turn_id, settings
            )

            # 没有任何消费者：start 立的"有人在听"标记 2 秒后自然过期
            await asyncio.wait_for(runner.run(), timeout=20)

            frames = await buffer.read(turn_id, STREAM_START, block_ms=None)
            return conversations.completed, [(frame.event, frame.data) for frame in frames]
        finally:
            await _cleanup_sse_keys(redis, turn_id)
            await redis.aclose()

    completed, frames = asyncio.run(scenario())

    assert len(completed) == 1
    assert completed[0]["status"] is MessageStatus.INTERRUPTED
    # 已生成的部分要留着：用户回来看到的是"说到哪儿了"，不是空白
    partial = completed[0]["answer"]
    assert 0 < len(partial) < len(answer)
    assert answer.startswith(partial)

    events = [event for event, _ in frames]
    assert "done" not in events, "被中止的流不该发 done，客户端会当成正常结束"
    errors = [json.loads(data) for event, data in frames if event == "error"]
    assert errors and errors[-1]["code"] == 22408


# ------------------------------------------------- 心跳与数据流解耦（P0 回归）


class SlowFirstTokenService:
    """首字很慢的桩：TTFT 期间一个 token 都不产出。

    真实场景是推理类模型 —— 20-40 秒才开始吐字，而心跳 TTL 只有 30 秒。
    旧实现把心跳挂在数据流上，这段时间一次都不刷，消费端就会判"生产者已死"。
    """

    model = "stub-model"

    def __init__(self, answer: str, ttft: float) -> None:
        self.answer = answer
        self.ttft = ttft

    async def astream_chat(self, payload: object, history: object):
        await asyncio.sleep(self.ttft)
        for piece in self.answer:
            yield TokenDelta(piece)
        yield StreamFinished(model=self.model, prompt_tokens=1, completion_tokens=len(self.answer))


def test_heartbeat_keeps_running_during_long_first_token(
    test_redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """首字延迟超过心跳 TTL 时，消费端不能误判"生产者已死"。

    这条用例锁住的是最贵的那个 bug：心跳挂在数据流上时，模型 30 秒不吐字，
    beat 键过期 → 消费端 producer_alive=False → 给用户发 22408 并断开，
    而生产者还在跑、还在烧 token。用户看到失败，账单继续走。
    """
    import app.services.stream_hub as hub

    # 把时间尺度压小：TTL 1 秒、心跳 0.2 秒、TTFT 1.6 秒
    monkeypatch.setattr(hub, "_BEAT_TTL_SECONDS", 1)
    monkeypatch.setattr(hub, "_BEAT_INTERVAL_SECONDS", 0.2)
    monkeypatch.setattr(hub, "_WATCHDOG_TICK_SECONDS", 0.1)

    answer = "慢启动模型"
    turn_id = 990003

    async def scenario() -> tuple[list[str], list[dict]]:
        redis = Redis.from_url(test_redis_url, decode_responses=True)
        try:
            buffer = StreamBuffer(redis, 600)
            settings = Settings(_env_file=None, SSE_RESUME_GRACE_SECONDS=30)
            conversations = RecordingConversations()
            runner = _make_runner(
                buffer,
                SlowFirstTokenService(answer, ttft=1.6),
                conversations,
                turn_id,
                settings,
            )
            task = asyncio.create_task(runner.run())

            events: list[str] = []
            stream = stream_frames(
                buffer=buffer,
                context=TurnContext(conversation_id=1, assistant_message_id=turn_id),
                after=STREAM_START,
                settings=settings,
            )
            async for frame in stream:
                events.append(frame.event)
                if frame.event == "done":
                    break
            await stream.aclose()

            await asyncio.wait_for(task, timeout=10)
            return events, conversations.completed
        finally:
            await _cleanup_sse_keys(redis, turn_id)
            await redis.aclose()

    events, completed = asyncio.run(scenario())

    assert "error" not in events, f"心跳断了，消费端误判成生产者已死：{events}"
    assert events[-2:] == ["meta", "done"]
    assert len(completed) == 1
    assert completed[0]["status"] is MessageStatus.COMPLETE
    assert completed[0]["answer"] == answer
