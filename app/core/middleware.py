"""请求上下文中间件：给每个请求分配 trace_id。

为什么是纯 ASGI 中间件，而不是 Starlette 的 BaseHTTPMiddleware：
    BaseHTTPMiddleware 会把下游包进一个新 task，并且把流式响应
    包一层 anyio 内存对象 —— 对 SSE 这种长连接，
    轻则缓冲掉所有分块（前端一个字都收不到），
    重则在客户端断开时吞掉 CancelledError。
    纯 ASGI 只包 send，一个字节都不缓冲，对 SSE 完全透明。

trace_id 的取值优先级：
    客户端带了 X-Request-ID 就沿用（便于前端/网关串联同一条链路），
    没带就生成一个。回写到响应头，前端报障时直接提供这个值。
"""

import logging
import time
from uuid import uuid4

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.context import reset_trace_id, set_trace_id
from app.core.metrics import record_http

logger = logging.getLogger(__name__)

TRACE_ID_HEADER = "X-Request-ID"


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            # websocket / lifespan 不参与
            await self.app(scope, receive, send)
            return

        incoming = Headers(scope=scope).get(TRACE_ID_HEADER)
        trace_id = incoming or uuid4().hex
        token = set_trace_id(trace_id)

        async def send_with_trace_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[TRACE_ID_HEADER] = trace_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_trace_id)
        finally:
            reset_trace_id(token)


class MetricsMiddleware:
    """HTTP 指标采集。同样是纯 ASGI —— 理由与 trace_id 中间件一致。

    两个细节：

    1. **标签用路由模板，不用原始路径。** `/api/v1/conversations/373814368841`
       这样的路径每个会话一条时间序列，几天就能把 Prometheus 撑爆。
       Starlette 在路由匹配后把 `route` 写进 scope，这里直接取它的 path 模板；
       没匹配上（404）统一记成 `unmatched`。
    2. **SSE 的耗时就该很长。** 它记的是整条流的生命周期，不是首字节；
       首字延迟另有 `chat_first_token_seconds`，两者混在一起看会误判。
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        # 默认 500：万一在响响应之前就抛了，记 500 比记 200 更接近事实
        status = 500

        async def capture(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, capture)
        finally:
            route = scope.get("route")
            path = getattr(route, "path", None) or "unmatched"
            # 抓取本身不计入，否则 Prometheus 每抓一次就污染一次请求指标
            if path != "/metrics":
                record_http(
                    method=scope["method"],
                    path=path,
                    status=status,
                    seconds=time.perf_counter() - started,
                )
