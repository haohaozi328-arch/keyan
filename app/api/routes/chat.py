"""对话端点：一次性响应、SSE 流式响应（可断线续传）、配置状态查询。

分层：路由只做参数校验、协议适配与响应组装；链路细节在 LLMService，
会话归属与落库在 ConversationService，流式缓冲与生产者任务在 StreamHub。

**对话必须登录。** 没有令牌一律 401，不存在"匿名试用"分支：

    历史上下文只能来自服务端。让客户端传 `history` 等于把喂给模型的
    上下文交给浏览器决定 —— 伪造几条 assistant 消息就能把模型带偏，
    而这类注入在服务端日志里看不出来（请求参数是合法的）。
    没有 user_id 也就没有归属，"匿名会话"只会变成一堆无法清理的孤儿数据。

流式对话的两段式：
    POST /chat/stream              开工：建会话 + 写消息占位 + 起生产者任务
    GET  /chat/stream/{message_id}  续读：从 Last-Event-ID 之后接着拿帧

为什么要拆成两个接口：断线重连时**绝不能重发 POST** —— 那会新建一轮、
再扣一次 token，用户看到的还是从头开始的答案。GET 只读缓冲，天然幂等，
重连十次也只花一次生成的钱。

SSE 仍有两种错误通道：
    流开始前 → 抛 AppError，走全局处理器，客户端拿到真实状态码
    流开始后 → 响应头已发出，只能往流里写 event=error 帧

落库时机：`event=done` **之前**。客户端看到 done 通常会立刻刷新会话列表，
先发 done 再写库会出现"回答完了但列表里没有这条"。这条规则现在由生产者
保证（它先写库、再追加 done 帧），与连接是否还在无关。

成本闸门
--------
四个对话入口（一次性 / 流式 / 续传 / 重新生成）里，会真正产生 token 成本的
是前两个和重新生成；续传只读缓冲，不重新生成，所以不占名额。
闸门在**写库之前**拦：被限流的请求不该在会话里留下两条空消息。
"""

import asyncio
import logging
import re
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from sse_starlette import EventSourceResponse

from app.api.deps import (
    CurrentUser,
    get_chat_throttle,
    get_conversation_service,
    get_stream_buffer,
)
from app.core.config import Settings, get_settings
from app.core.errors import InvalidParamError, NotFoundError
from app.core.response import ApiResponse, ok
from app.db.models import User
from app.schemas.chat import ChatRequest, ChatResponse, ChatStatusResponse
from app.services.chat_throttle import ChatThrottle, ChatTicket
from app.services.conversation_service import ConversationService, TurnContext
from app.services.exceptions import LLMUpstreamError, ServiceError, StreamExpiredError
from app.services.llm_service import LLMService, get_llm_service
from app.services.stream_hub import (
    STREAM_START,
    StreamBuffer,
    TurnRegistry,
    TurnRunner,
    stream_frames,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# Redis Stream 的 entry id 形如 `1761666000000-3`。
# 只做形状校验：值本身是服务端分配的，客户端伪造一个不存在的 id 只会读到空，
# 但形状不对（比如带换行）会让 XREAD 直接抛错变成 500。
_STREAM_ID_PATTERN = re.compile(r"^\d{1,20}-\d{1,20}$")


@router.post(
    "",
    response_model=ApiResponse[ChatResponse],
    summary="对话（一次性返回）",
    description=(
        "需要登录（Bearer access_token）。等待模型生成完毕后一次性返回。"
        "返回的 conversation_id / message_id 用于继续对话和评价。"
    ),
    responses={
        401: {"description": "未登录或令牌失效"},
        429: {"description": "触发成本闸门（频率 / 并发 / 额度）"},
        503: {"description": "LLM 未配置"},
    },
)
async def chat(
    payload: ChatRequest,
    user: CurrentUser,
    service: Annotated[LLMService, Depends(get_llm_service)],
    conversations: Annotated[ConversationService, Depends(get_conversation_service)],
    throttle: Annotated[ChatThrottle, Depends(get_chat_throttle)],
) -> ApiResponse[ChatResponse]:
    # 先取名额再写库：被限流的请求不该在会话里留下两条空消息
    ticket = await throttle.acquire(user_id=user.id)
    try:
        context = await _prepare(conversations, user, payload, service)
        try:
            result = await service.achat(payload, context.history)
        except (LLMUpstreamError, ServiceError) as exc:
            # 失败也要落一条 FAILED：否则会话里只剩用户那句话，
            # 列表显示"共 2 条"却只查得到 1 条，统计和实际永远对不上
            await conversations.fail_turn(context, reason=exc.message)
            raise
    finally:
        await throttle.release(ticket)

    usage = result.usage or {}
    await conversations.complete_turn(
        context,
        answer=result.answer,
        prompt_tokens=usage.get("prompt_tokens"),
        completion_tokens=usage.get("completion_tokens"),
    )
    await throttle.record_tokens(
        user.id, (usage.get("prompt_tokens") or 0) + (usage.get("completion_tokens") or 0)
    )
    return ok(
        ChatResponse(
            answer=result.answer,
            model=result.model,
            usage=result.usage,
            conversation_id=_sid(context.conversation_id),
            message_id=_sid(context.assistant_message_id),
        )
    )


@router.post(
    "/stream",
    summary="对话（SSE 流式）",
    description=(
        "需要登录（Bearer access_token）。以 text/event-stream 逐块返回："
        "event=start 给出会话与消息 ID（续传要用的锚点），event=delta 为增量文本，"
        "event=meta 携带模型与用量，event=done 表示结束，event=error 携带错误码。"
        "**断线后不要重发本接口**，改用 GET /chat/stream/{message_id} 续读。"
    ),
    response_class=EventSourceResponse,
    responses={
        200: {"content": {"text/event-stream": {}}, "description": "SSE 事件流"},
        401: {"description": "未登录或令牌失效"},
        404: {"description": "会话不存在或不属于当前用户"},
        422: {"description": "参数校验失败"},
        503: {"description": "LLM 未配置或 Redis 未配置"},
    },
)
async def chat_stream(
    request: Request,
    payload: ChatRequest,
    user: CurrentUser,
    service: Annotated[LLMService, Depends(get_llm_service)],
    conversations: Annotated[ConversationService, Depends(get_conversation_service)],
    buffer: Annotated[StreamBuffer, Depends(get_stream_buffer)],
    settings: Annotated[Settings, Depends(get_settings)],
    throttle: Annotated[ChatThrottle, Depends(get_chat_throttle)],
) -> EventSourceResponse:
    # 配置缺失属于请求前置条件，必须在响应头下发前拦掉。
    # 这里抛出的 AppError 会被全局处理器接住 —— 此时流还没开始。
    service.ensure_configured()

    ticket = await throttle.acquire(user_id=user.id)
    try:
        # 归属校验、上下文加载、消息占位全部前移到这里：
        # 一旦开始往客户端写字节，这些错误就只能变成流里的 error 帧，
        # 前端拿不到 404/401，只知道"聊到一半失败了"
        context = await _prepare(conversations, user, payload, service)
        return await _launch_turn(
            request,
            context=context,
            payload=payload,
            service=service,
            conversations=conversations,
            buffer=buffer,
            settings=settings,
            throttle=throttle,
            ticket=ticket,
        )
    except Exception:
        # 名额在成功交给后台任务之后才归后台管；这条路径没交出去，必须自己还
        await throttle.release(ticket)
        raise


@router.get(
    "/stream/{message_id}",
    summary="对话（断线续传）",
    description=(
        "需要登录。断线后用它接着读，**不要重发 POST /chat/stream**。"
        "把上次收到的最后一个 `id:` 作为 `Last-Event-ID` 请求头回传"
        "（或 `?last_event_id=`），服务端从它之后接着发，不重新生成、不重复计费。"
        "不带该参数则从头重放。缓冲保留 10 分钟，过期返回 410。"
    ),
    response_class=EventSourceResponse,
    responses={
        200: {"content": {"text/event-stream": {}}, "description": "SSE 事件流"},
        401: {"description": "未登录或令牌失效"},
        404: {"description": "消息不存在或不属于当前用户"},
        410: {"description": "流式缓冲已过期，请改拉消息全文"},
        422: {"description": "续传位置格式不合法"},
    },
)
async def resume_stream(
    request: Request,
    message_id: str,
    user: CurrentUser,
    conversations: Annotated[ConversationService, Depends(get_conversation_service)],
    buffer: Annotated[StreamBuffer, Depends(get_stream_buffer)],
    settings: Annotated[Settings, Depends(get_settings)],
    last_event_id: Annotated[str | None, Query(alias="last_event_id")] = None,
) -> EventSourceResponse:
    context = await conversations.resume_context(
        user_id=user.id, message_id=_parse_message_id(message_id)
    )
    turn_id = context.assistant_message_id
    if turn_id is None:  # 仅类型收窄
        raise NotFoundError("消息不存在")

    if not await buffer.exists(turn_id):
        # 缓冲没了但消息还在库里：这不是 404（消息没丢），而是 410 ——
        # 客户端应该改拉 GET /conversations/{id}/messages 拿全文。
        # 都回 404 会让前端误以为消息被删了。
        raise StreamExpiredError()

    # 标准的 Last-Event-ID 请求头优先，其次是 query —— 后者是给
    # 不方便设自定义头的客户端（或手动重放）用的
    after = request.headers.get("last-event-id") or last_event_id or STREAM_START
    if not _STREAM_ID_PATTERN.match(after):
        raise InvalidParamError("Last-Event-ID 格式不合法")

    # 先续客户端心跳，让还在跑的生产者知道"人回来了"，别把生成停掉
    await buffer.touch_consumer(turn_id, settings.SSE_RESUME_GRACE_SECONDS)
    return _stream_response(request, buffer, context, settings, after=after)


@router.post(
    "/regenerate/{message_id}",
    summary="重新生成一条回答（SSE 流式）",
    description=(
        "需要登录。对指定的助手消息重新生成一次回答，帧格式与 /chat/stream 完全一致。"
        "**不会重复写入用户提问**：新回答作为一条新消息追加在末尾，"
        "原回答保留下来（可以对比，也保住了它的评价与 badcase 记录）。"
        "新消息的 parent_message_id 指向被替代的那条，之后的上下文只使用新回答。"
    ),
    response_class=EventSourceResponse,
    responses={
        200: {"content": {"text/event-stream": {}}, "description": "SSE 事件流"},
        401: {"description": "未登录或令牌失效"},
        404: {"description": "消息不存在或不属于当前用户"},
        429: {"description": "触发成本闸门（频率 / 并发 / 额度）"},
        503: {"description": "LLM 未配置或 Redis 未配置"},
    },
)
async def regenerate_stream(
    request: Request,
    message_id: str,
    user: CurrentUser,
    service: Annotated[LLMService, Depends(get_llm_service)],
    conversations: Annotated[ConversationService, Depends(get_conversation_service)],
    buffer: Annotated[StreamBuffer, Depends(get_stream_buffer)],
    settings: Annotated[Settings, Depends(get_settings)],
    throttle: Annotated[ChatThrottle, Depends(get_chat_throttle)],
) -> EventSourceResponse:
    # 重新生成同样要花一次完整的 token，闸门与 /chat/stream 一模一样
    service.ensure_configured()
    prompt = service.prompt

    ticket = await throttle.acquire(user_id=user.id)
    try:
        context, question = await conversations.prepare_regeneration(
            user=user,
            message_id=_parse_message_id(message_id),
            model=service.model,
            prompt_id=prompt.id,
            prompt_version=prompt.version,
        )
        return await _launch_turn(
            request,
            context=context,
            # 复用原来的提问：重新生成的是"同一个问题"的答案
            payload=ChatRequest(message=question),
            service=service,
            conversations=conversations,
            buffer=buffer,
            settings=settings,
            throttle=throttle,
            ticket=ticket,
        )
    except Exception:
        await throttle.release(ticket)
        raise


async def _launch_turn(
    request: Request,
    *,
    context: TurnContext,
    payload: ChatRequest,
    service: LLMService,
    conversations: ConversationService,
    buffer: StreamBuffer,
    settings: Settings,
    throttle: ChatThrottle,
    ticket: ChatTicket,
) -> EventSourceResponse:
    """起生产者任务并返回 SSE 响应。三个会花 token 的入口共用这一段。"""
    turn_id = context.assistant_message_id
    if turn_id is None:  # 仅类型收窄：prepare_* 一定写了占位
        raise RuntimeError("对话上下文缺少助手消息 ID")

    # 先把心跳立起来再起任务：消费者可能抢在生产者之前读第一帧，
    # 那时候如果没有 beat，会被判成"生产者已死"
    await buffer.start(turn_id, settings.SSE_RESUME_GRACE_SECONDS)

    runner = TurnRunner(
        service=service,
        conversations=conversations,
        context=context,
        payload=payload,
        history=context.history,
        buffer=buffer,
        settings=settings,
    )
    task = asyncio.create_task(_run_turn(runner, throttle, ticket), name=f"chat-turn-{turn_id}")
    # 必须留下强引用：asyncio 只保留弱引用，没人引用时任务会被 GC 掉，
    # 表现是"回答说到一半就停了"，且没有任何异常
    _registry(request).add(turn_id, task)
    return _stream_response(request, buffer, context, settings, after=STREAM_START)


async def _run_turn(runner: TurnRunner, throttle: ChatThrottle, ticket: ChatTicket) -> None:
    """跑生成，并在**任何**退出路径上归还名额、记当日用量。

    收尾用 shield 包住：停机时任务会被 cancel，此时直接 await 会立刻被
    再次打断，名额只能等 TTL（默认 15 分钟）自然回收 —— 这期间用户会
    一直看到"同时进行的对话不能超过 N 条"。
    """
    try:
        await runner.run()
    finally:
        try:
            await asyncio.shield(_settle_turn(throttle, ticket, runner))
        except asyncio.CancelledError:
            pass


async def _settle_turn(throttle: ChatThrottle, ticket: ChatTicket, runner: TurnRunner) -> None:
    try:
        await throttle.release(ticket)
        await throttle.record_tokens(ticket.user_id, runner.total_tokens)
    except Exception:  # noqa: BLE001
        # 记账失败不该改变这一轮的终态：消息已经写进库了，
        # 这里再抛只会让人误以为"生成失败"
        logger.exception("归还对话名额 / 记录用量失败 user=%s", ticket.user_id)


def _stream_response(
    request: Request,
    buffer: StreamBuffer,
    context: TurnContext,
    settings: Settings,
    *,
    after: str,
) -> EventSourceResponse:
    return EventSourceResponse(
        stream_frames(buffer=buffer, context=context, after=after, settings=settings),
        # ping 是 SSE 注释行，客户端忽略；用来防住代理的空闲超时
        ping=settings.SSE_PING_SECONDS,
        # 停机时通知在途流收尾，而不是被进程退出硬切断
        shutdown_event=getattr(request.app.state, "sse_shutdown", None),
    )


def _registry(request: Request) -> TurnRegistry:
    registry = getattr(request.app.state, "turn_registry", None)
    if registry is None:  # pragma: no cover - lifespan 一定会建
        registry = TurnRegistry()
        request.app.state.turn_registry = registry
    return registry


async def _prepare(
    conversations: ConversationService,
    user: User,
    payload: ChatRequest,
    service: LLMService,
) -> TurnContext:
    prompt = service.prompt
    return await conversations.prepare_turn(
        user=user,
        conversation_id=int(payload.conversation_id) if payload.conversation_id else None,
        message=payload.message,
        model=service.model,
        prompt_id=prompt.id,
        prompt_version=prompt.version,
    )


def _parse_message_id(raw: str) -> int:
    if not raw.isdigit():
        raise NotFoundError("消息不存在")
    return int(raw)


def _sid(value: int | None) -> str | None:
    """雪花 ID → 字符串。19 位整数直接给前端，`JSON.parse` 会把末几位抹掉。"""
    return None if value is None else str(value)


@router.get(
    "/status",
    response_model=ApiResponse[ChatStatusResponse],
    summary="对话功能状态",
    description="探测 LLM 是否已配置、当前使用哪版提示词，不打真网络。",
)
async def chat_status(
    service: Annotated[LLMService, Depends(get_llm_service)],
) -> ApiResponse[ChatStatusResponse]:
    prompt = service.prompt
    return ok(
        ChatStatusResponse(
            configured=service.configured,
            model=service.model,
            base_url=service.base_url,
            prompt_id=prompt.id,
            prompt_version=prompt.version,
        )
    )
