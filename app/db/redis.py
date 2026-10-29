"""Redis 客户端：验证码、限流计数。

和数据库引擎一样，**进程级单例**：redis-py 内部维护连接池，
每请求新建一个客户端就是每请求一个连接池，很快会把 Redis 的
maxclients（默认 10000）打满，而且报错是 "max number of clients reached"，
看起来像"Redis 挂了"，实际是自己泄漏。

decode_responses=True：验证码、计数器都是文本，
不开启的话每次都要自己 .decode()，漏一次就会拿 bytes 去比较字符串，
结果是"验证码永远不对"这种极难查的 bug。
"""

from functools import lru_cache

from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import ExponentialWithJitterBackoff
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from app.core.config import get_settings


@lru_cache(maxsize=1)
def get_redis() -> Redis:
    settings = get_settings()
    if not settings.redis_configured:
        raise RuntimeError("Redis 未配置：请在 .env 里设置 REDIS_PASSWORD")

    return Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_timeout=settings.REDIS_SOCKET_TIMEOUT_SECONDS,
        socket_connect_timeout=settings.REDIS_SOCKET_TIMEOUT_SECONDS,
        # 连接池里躺久的连接可能已被服务端/中间网络设备断开，
        # 定时探活让坏连接在取用时就被剔除，而不是在业务调用时才炸
        health_check_interval=30,
        # 只在 health_check_interval 之上补探活是不够的：redis-py 探活失败会
        # 先 disconnect（`_ping_failed`），但默认 Retry 是 `Retry(NoBackoff(), 0)`,
        # `_retries == 0` 意味着第一次失败就 `failures > self._retries` 成立、
        # 直接把异常抛出去 —— 重连代码根本没有机会跑。表现就是一条已经死掉的
        # 连接会把整轮对话打成 10500（`Connection closed by server`），
        # 哪怕 Redis 本身好好的（睡眠唤醒、网络切换后池里全是僵尸连接时必现）。
        # 这里显式给重试：失败后 disconnect 已在 fail 回调里做了，重试即重连。
        retry=Retry(ExponentialWithJitterBackoff(base=0.05, cap=0.5), retries=2),
        retry_on_error=[RedisConnectionError, RedisTimeoutError],
    )


async def close_redis() -> None:
    """停机时释放连接池，由 lifespan 调用。"""
    if get_redis.cache_info().currsize:
        await get_redis().aclose()
    get_redis.cache_clear()
