"""Redis 连接被服务端悄悄断开时，业务命令要能自愈。

起因：线上出现 `redis.exceptions.ConnectionError: Connection closed by server`，
抛在 `stream_hub` 的 `buffer.start()` 里 —— 也就是对话刚开始写心跳占位时，
整轮对话被打成 10500「服务内部异常」。

根因不是 Redis 挂了，而是 redis-py 的默认重试是 `Retry(NoBackoff(), 0)`：
探活 PING 失败时 `_ping_failed` 确实会 disconnect，但 `_retries == 0` 让
`failures > self._retries` 立刻成立，异常直接被抛出去 —— 重连代码没有机会执行。
睡眠唤醒、网络切换、服务端 idle 超时之后，连接池里全是这种「客户端以为还活着」
的僵尸连接，必然踩到。
"""

import asyncio
from typing import Any

import pytest
from app.db.redis import get_redis
from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import ExponentialWithJitterBackoff
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

_PROBE_KEY = "probe:resilience"


def _retry_config() -> dict[str, Any]:
    """与 `app.db.redis.get_redis()` 保持一致的重试配置。"""
    return {
        "retry": Retry(ExponentialWithJitterBackoff(base=0.05, cap=0.5), retries=2),
        "retry_on_error": [RedisConnectionError, RedisTimeoutError],
    }


def test_client_retries_transient_connection_errors() -> None:
    """生产客户端的重试配置不能被删掉。

    少了这两个参数，一条死连接就能打死整轮对话，而且日志里看起来像
    「Redis 挂了」，实际 Redis 好好的。
    """
    kwargs = get_redis().connection_pool.connection_kwargs

    retry = kwargs.get("retry")
    assert retry is not None, "未配置 retry，探活失败会直接抛给业务层"
    assert retry._retries >= 1, "retries 为 0 等于不重试"

    retried_on = kwargs.get("retry_on_error") or []
    assert RedisConnectionError in retried_on, "连接被关闭不会触发重试"
    assert RedisTimeoutError in retried_on, "读写超时不会触发重试"


def test_command_survives_dropped_connection(test_redis_url: str) -> None:
    """底层连接被掐断后，下一条命令应当自愈而不是抛错。

    手工 abort transport 而不是调 disconnect()，是为了模拟「服务端已经没了、
    客户端还不知道」——正是睡眠唤醒和 idle 超时之后的真实状态。
    """

    async def scenario() -> None:
        client = Redis.from_url(
            test_redis_url,
            decode_responses=True,
            socket_timeout=3.0,
            socket_connect_timeout=3.0,
            health_check_interval=0.05,  # 压低，让下次取连接时必然触发探活
            **_retry_config(),
        )
        try:
            await client.set(_PROBE_KEY, "1")

            connection = await client.connection_pool.get_connection()
            writer = getattr(connection, "_writer", None)
            if writer is None or getattr(writer, "transport", None) is None:
                pytest.skip("redis-py 内部结构变化，无法模拟断连")
            writer.transport.abort()
            await client.connection_pool.release(connection)
            await asyncio.sleep(0.1)

            # 修复前这里会抛 ConnectionError，修复后探活失败即重连
            await client.set(_PROBE_KEY, "2")
            assert await client.get(_PROBE_KEY) == "2"
        finally:
            try:
                await client.delete(_PROBE_KEY)
            except Exception:  # noqa: BLE001 - 清理失败不该掩盖真正的断言
                pass
            await client.aclose()

    asyncio.run(scenario())
