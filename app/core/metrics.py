"""进程内指标：Prometheus 文本格式，供 `/metrics` 抓取。

为什么必须有
------------
trace_id 只能事后查单条请求，回答不了三个运营问题：
现在有多少条流在跑、平均耗时多少、错误率多少。这三个数决定要不要扩容、
要不要回滚、告警阈值定在哪里。没有它们，"服务很慢"只能靠用户投诉得知。

为什么用 prometheus_client 而不是自己攒计数器
-------------------------------------------
直方图的分位计算、文本格式转义、并发写入的线程安全，都不是"顺手写两行"
能写对的。这个库纯 Python、无传递依赖，比自研可靠得多。

为什么用独立 Registry
--------------------
默认 REGISTRY 会带上 process_ / python_ 采集器，任何第三方库也能往里塞指标。
这里的指标集合应当是显式的：出现哪些名字，就是本项目承诺的可观测面。

标签基数（cardinality）是唯一需要克制的地方
------------------------------------------
HTTP 指标用**路由模板**（`/api/v1/conversations/{conversation_id}`）而不是原始
路径，否则每个会话 ID 都会生成一条时间序列，几天就能撑爆 Prometheus。
"""

from collections.abc import Iterator
from contextlib import contextmanager
from time import perf_counter

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

REGISTRY = CollectorRegistry()

HTTP_REQUESTS = Counter(
    "http_requests_total",
    "HTTP 请求数",
    ["method", "path", "status"],
    registry=REGISTRY,
)

# 桶按"人感知的慢"划分：0.1s 以内无感，1s 开始明显，10s 以上基本会离开。
HTTP_DURATION = Histogram(
    "http_request_duration_seconds",
    "HTTP 请求耗时（SSE 记录的是整条流的生命周期，不是首字节）",
    ["method", "path"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60),
    registry=REGISTRY,
)

CHAT_TURNS = Counter(
    "chat_turns_total",
    "对话轮次，按终态统计",
    ["outcome"],  # complete / interrupted / failed / abandoned / cancelled
    registry=REGISTRY,
)

CHAT_TURN_DURATION = Histogram(
    "chat_turn_duration_seconds",
    "单轮生成耗时（从开工到落库收尾）",
    buckets=(0.5, 1, 2, 5, 10, 20, 40, 60, 120, 300),
    registry=REGISTRY,
)

# 首字延迟单独一个直方图，不和总耗时混在一起：
# 用户抱怨"卡"多半是 TTFT，而优化 TTFT（换模型、改提示词长度）与优化
# 总耗时（限 max_tokens）是两件完全不同的事。
CHAT_FIRST_TOKEN = Histogram(
    "chat_first_token_seconds",
    "首字延迟（TTFT），从开工到第一块增量",
    buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 40, 60),
    registry=REGISTRY,
)

CHAT_TOKENS = Counter(
    "chat_tokens_total",
    "累计 token 用量（网关不返回用量时不记）",
    ["direction"],  # prompt / completion
    registry=REGISTRY,
)

CHAT_STREAMS_INFLIGHT = Gauge(
    "chat_streams_inflight",
    "当前在途的生成任务数。持续偏高说明上游慢或有人在刷",
    registry=REGISTRY,
)

CHAT_ERRORS = Counter(
    "chat_errors_total",
    "对话失败次数，按业务错误码统计",
    ["code"],
    registry=REGISTRY,
)

CHAT_REJECTED = Counter(
    "chat_rejected_total",
    "被成本闸门拦下的对话请求",
    ["reason"],  # rate / concurrency / quota
    registry=REGISTRY,
)


def record_http(*, method: str, path: str, status: int, seconds: float) -> None:
    HTTP_REQUESTS.labels(method=method, path=path, status=str(status)).inc()
    HTTP_DURATION.labels(method=method, path=path).observe(seconds)


def record_turn(
    *,
    outcome: str,
    seconds: float,
    first_token_seconds: float | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
) -> None:
    """一轮结束时的统一记账。

    token 只在网关真的返回用量时才记：把"不知道"记成 0 会让
    "今日消耗"这种面板永远偏低，而偏低比缺失更危险 —— 它看起来是对的。
    """
    CHAT_TURNS.labels(outcome=outcome).inc()
    CHAT_TURN_DURATION.observe(seconds)
    if first_token_seconds is not None:
        CHAT_FIRST_TOKEN.observe(first_token_seconds)
    if prompt_tokens is not None:
        CHAT_TOKENS.labels(direction="prompt").inc(prompt_tokens)
    if completion_tokens is not None:
        CHAT_TOKENS.labels(direction="completion").inc(completion_tokens)


def record_chat_error(code: int) -> None:
    CHAT_ERRORS.labels(code=str(code)).inc()


def record_rejected(reason: str) -> None:
    CHAT_REJECTED.labels(reason=reason).inc()


@contextmanager
def track_inflight() -> Iterator[None]:
    """在途生成数 ±1。异常路径也要减回去，否则 gauge 只增不减。"""
    CHAT_STREAMS_INFLIGHT.inc()
    try:
        yield
    finally:
        CHAT_STREAMS_INFLIGHT.dec()


def render() -> bytes:
    return generate_latest(REGISTRY)


def timer() -> float:
    return perf_counter()
