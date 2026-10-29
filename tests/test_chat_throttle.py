"""成本闸门：并发名额、频率窗口、每日额度。

跑真 Redis —— 计数就在 Redis 里，用假客户端测不出 INCR/EXPIRE/ZSET
的真实语义（尤其是过期回收）。

每个用例用独立的 user_id，互不干扰；收尾删掉自己的 key。
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable

import pytest
from app.api.deps import get_chat_throttle
from app.core.config import Settings
from app.db import get_redis
from app.main import app
from app.services.chat_throttle import ChatThrottle
from app.services.exceptions import ChatRateLimitedError
from fastapi.testclient import TestClient
from redis.asyncio import Redis

CHAT = "/api/v1/chat"


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "CHAT_RATE_LIMIT_PER_MINUTE": 100,
        "CHAT_MAX_CONCURRENT_STREAMS": 2,
        "CHAT_DAILY_TOKEN_BUDGET": 1000,
        "CHAT_INFLIGHT_TTL_SECONDS": 60,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[arg-type]


async def _with_throttle(
    redis_url: str,
    user_id: int,
    scenario: Callable[[ChatThrottle], Awaitable[None]],
    **overrides: object,
) -> None:
    redis = Redis.from_url(redis_url, decode_responses=True)
    try:
        await scenario(ChatThrottle(redis, _settings(**overrides)))
    finally:
        stale = [key async for key in redis.scan_iter(match=f"chat:*:{user_id}*")]
        if stale:
            await redis.delete(*stale)
        await redis.aclose()


def _uid() -> int:
    # 随机而不是递增：并行跑用例时不会撞到同一份计数
    return uuid.uuid4().int % 10**15


def test_limits_concurrent_streams(test_redis_url: str) -> None:
    """同一用户最多 N 条在途流，第三条直接被拒。"""

    async def scenario(throttle: ChatThrottle) -> None:
        first = await throttle.acquire(user_id=user_id)
        await throttle.acquire(user_id=user_id)
        with pytest.raises(ChatRateLimitedError):
            await throttle.acquire(user_id=user_id)
        await throttle.release(first)

    user_id = _uid()
    asyncio.run(_with_throttle(test_redis_url, user_id, scenario))


def test_release_frees_the_slot(test_redis_url: str) -> None:
    """归还名额之后立刻能再取，不必等 TTL。"""

    async def scenario(throttle: ChatThrottle) -> None:
        ticket = await throttle.acquire(user_id=user_id)
        await throttle.release(ticket)
        # 名额回到上限内，这次不该抛
        await throttle.acquire(user_id=user_id)

    user_id = _uid()
    asyncio.run(_with_throttle(test_redis_url, user_id, scenario))


def test_expired_slots_are_reclaimed(test_redis_url: str) -> None:
    """进程崩溃留下的槽位不能永久占名额，靠过期时间回收。

    TTL 设成 0 等价于"上一轮早已崩溃并过期"：每次取名额前都会先把过期成员
    清掉，所以上限 2 的账号可以连取 4 次。少了这步清理，第 3 次就会撞上限。
    """

    async def scenario(throttle: ChatThrottle) -> None:
        for _ in range(4):
            await throttle.acquire(user_id=user_id)

    user_id = _uid()
    asyncio.run(_with_throttle(test_redis_url, user_id, scenario, CHAT_INFLIGHT_TTL_SECONDS=0))


def test_rate_limit_within_window(test_redis_url: str) -> None:
    """每分钟次数按窗口累计，超了就拒。"""

    async def scenario(throttle: ChatThrottle) -> None:
        for _ in range(3):
            ticket = await throttle.acquire(user_id=user_id)
            await throttle.release(ticket)
        with pytest.raises(ChatRateLimitedError) as excinfo:
            await throttle.acquire(user_id=user_id)
        assert "每分钟" in str(excinfo.value)

    user_id = _uid()
    asyncio.run(_with_throttle(test_redis_url, user_id, scenario, CHAT_RATE_LIMIT_PER_MINUTE=3))


def test_daily_token_budget(test_redis_url: str) -> None:
    """当日额度用尽后直接拒，且额度按用户隔离。"""

    async def scenario(throttle: ChatThrottle) -> None:
        await throttle.record_tokens(user_id, 1200)
        with pytest.raises(ChatRateLimitedError) as excinfo:
            await throttle.acquire(user_id=user_id)
        assert "额度" in str(excinfo.value)

    user_id = _uid()
    asyncio.run(_with_throttle(test_redis_url, user_id, scenario))


def test_budget_disabled_means_no_limit(test_redis_url: str) -> None:
    """预算配 0 = 不限制。这是给"网关自身有硬上限"的部署留的口子。"""

    async def scenario(throttle: ChatThrottle) -> None:
        await throttle.record_tokens(user_id, 10**9)
        await throttle.acquire(user_id=user_id)

    user_id = _uid()
    asyncio.run(_with_throttle(test_redis_url, user_id, scenario, CHAT_DAILY_TOKEN_BUDGET=0))


def test_http_returns_429_when_rate_limited(
    auth_client: TestClient, auth_headers: dict[str, str], stub_llm
) -> None:
    """接口层：被闸门拦下时是 429 + 22429，且**不写消息**。

    被限流的请求在会话里留下两条空记录的话，用户会看到一堆"发出去没有回答"
    的消息，而它们其实是根本没被处理过的。
    """
    stub_llm(answer="回答")
    tight = _settings(CHAT_RATE_LIMIT_PER_MINUTE=1)
    app.dependency_overrides[get_chat_throttle] = lambda: ChatThrottle(get_redis(), tight)

    assert (
        auth_client.post(CHAT, json={"message": "第一次"}, headers=auth_headers).status_code == 200
    )

    response = auth_client.post(CHAT, json={"message": "第二次"}, headers=auth_headers)

    assert response.status_code == 429
    assert response.json()["code"] == 22429
    # 只落下了第一轮的两条消息
    conversations = auth_client.get("/api/v1/conversations", headers=auth_headers).json()["data"]
    assert conversations["items"][0]["message_count"] == 2
