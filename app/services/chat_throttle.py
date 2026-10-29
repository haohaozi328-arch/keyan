"""对话成本闸门：按用户的请求频率、在途并发、每日 token 预算。

为什么必须有
------------
`/chat` 与 `/chat/stream` 此前只校验 JWT。一个账号可以无限并发、无限长输出 ——
LLM 服务最典型的被打爆方式不是 DDoS，是账单：攻击者不需要让服务挂掉，
只需要让你的账单翻十倍，而监控上一切正常（请求都 200）。

三道闸的分工
------------
    频率    每人每分钟 N 次：挡住脚本连发，也挡住手抖连点
    并发    每人在途 M 条：一条流可能跑几十秒，只靠频率挡不住 ——
            每分钟 10 次的重叠，足够同时烧掉 10 条流
    预算    每人每天 N token：前两道数的是"次数"，只有它直接对应钱

为什么并发名额用 ZSET 而不是 INCR
--------------------------------
INCR 的计数没有"谁占着"，进程崩溃后名额永不释放（只能重启或写对账任务）。
ZSET 里每个成员带过期时刻，取名额前先按分数清掉过期的 ——
崩溃的槽位过一会儿自动回收，代价只是"崩后短时间内限流偏松"。

已知取舍
--------
1. `ZADD` 与 `ZCARD` 不是原子的：两个请求同时挤最后一个名额时，
   可能**两个都被拒**（都看到 limit+1）。宁可偶尔误拒一次，
   也不要为了精确去上 Lua 脚本 —— 成本闸门不需要精确，只需要上限。
2. Redis 计数会在重启或过期时丢。所以它是"防跑飞的护栏"，不是计费依据；
   精确账单必须看数据库里的 messages.prompt_tokens / completion_tokens。
"""

import logging
import time
from dataclasses import dataclass
from uuid import uuid4

from redis.asyncio import Redis

from app.core import metrics
from app.core.config import Settings
from app.services.exceptions import ChatRateLimitedError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChatTicket:
    """一次对话占用的名额。必须在结束时 release，否则要等 TTL 自然回收。"""

    user_id: int
    slot: str


class ChatThrottle:
    def __init__(self, redis: Redis, settings: Settings) -> None:
        self._redis = redis
        self._settings = settings

    # ------------------------------------------------------------------ 取名额

    async def acquire(self, *, user_id: int) -> ChatTicket:
        """三道闸依次检查。任一不过就抛 429，此时**还没写任何消息**。

        顺序有讲究：预算（最贵的那道）→ 频率 → 并发。
        先查便宜的会让人用频率额度去撞预算，白白消耗计数。
        """
        await self._ensure_quota(user_id)
        await self._ensure_rate(user_id)
        slot = uuid4().hex
        await self._take_slot(user_id=user_id, slot=slot)
        return ChatTicket(user_id=user_id, slot=slot)

    async def release(self, ticket: ChatTicket) -> None:
        """归还名额。幂等：重复 release 只是再删一次不存在的成员。"""
        await self._redis.zrem(self._inflight_key(ticket.user_id), ticket.slot)

    # ------------------------------------------------------------------ 记账

    async def record_tokens(self, user_id: int, tokens: int) -> None:
        """累加当日用量。网关没返回用量时不记 —— 记 0 会让额度面板永远偏低。"""
        if tokens <= 0 or self._settings.CHAT_DAILY_TOKEN_BUDGET <= 0:
            return
        key = self._quota_key(user_id)
        pipe = self._redis.pipeline()
        pipe.incrby(key, tokens)
        # nx=True：只在第一次设置过期，避免每条流都把 TTL 往后推，
        # 那会让"昨天的用量"永远不过期，额度就再也回不来了
        pipe.expire(key, 2 * 86_400, nx=True)
        await pipe.execute()

    # ------------------------------------------------------------------ 内部

    async def _ensure_quota(self, user_id: int) -> None:
        budget = self._settings.CHAT_DAILY_TOKEN_BUDGET
        if budget <= 0:
            return
        used = int(await self._redis.get(self._quota_key(user_id)) or 0)
        if used >= budget:
            logger.warning("对话额度用尽 user=%s used=%s budget=%s", user_id, used, budget)
            metrics.record_rejected("quota")
            raise ChatRateLimitedError("今日对话额度已用完，请明天再试")

    async def _ensure_rate(self, user_id: int) -> None:
        limit = self._settings.CHAT_RATE_LIMIT_PER_MINUTE
        if limit <= 0:
            return
        # 固定窗口（自然分钟）而不是滑动窗口：实现只有 INCR，代价是
        # 跨分钟边界时最多能发出 2×limit 次。对"防账单"这个目的完全够用。
        bucket = int(time.time() // 60)
        key = f"chat:rate:{user_id}:{bucket}"
        pipe = self._redis.pipeline()
        pipe.incr(key)
        pipe.expire(key, 120, nx=True)
        count, _ = await pipe.execute()
        if int(count) > limit:
            logger.warning("对话频率超限 user=%s count=%s limit=%s", user_id, count, limit)
            metrics.record_rejected("rate")
            raise ChatRateLimitedError(f"每分钟最多发起 {limit} 轮对话，请稍后再试")

    async def _take_slot(self, *, user_id: int, slot: str) -> None:
        limit = self._settings.CHAT_MAX_CONCURRENT_STREAMS
        if limit <= 0:
            return
        key = self._inflight_key(user_id)
        now = time.time()
        deadline = now + self._settings.CHAT_INFLIGHT_TTL_SECONDS
        pipe = self._redis.pipeline()
        pipe.zremrangebyscore(key, "-inf", now)  # 回收崩溃进程留下的槽位
        pipe.zadd(key, {slot: deadline})
        pipe.zcard(key)
        pipe.expire(key, self._settings.CHAT_INFLIGHT_TTL_SECONDS + 60)
        _, _, count, _ = await pipe.execute()
        if int(count) > limit:
            await self._redis.zrem(key, slot)
            logger.warning("并发超限 user=%s count=%s limit=%s", user_id, count, limit)
            metrics.record_rejected("concurrency")
            raise ChatRateLimitedError(f"同时进行的对话不能超过 {limit} 条，请等待当前回答完成")

    def _quota_key(self, user_id: int) -> str:
        # 按 UTC 日切分：跨时区的用户不会在本地零点重置，但"额度何时恢复"
        # 有个确定答案，比按用户时区存一份配置简单可靠
        day = time.strftime("%Y%m%d", time.gmtime())
        return f"chat:tokens:{user_id}:{day}"

    @staticmethod
    def _inflight_key(user_id: int) -> str:
        return f"chat:inflight:{user_id}"
