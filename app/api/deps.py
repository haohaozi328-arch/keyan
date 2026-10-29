"""接口层依赖：鉴权、服务装配。

为什么依赖写在这里而不是 service 里：
    `Depends` 是 FastAPI 的概念，service 层不应该知道它存在。
    这样 AuthService 可以直接被脚本 / 任务 / 测试调用，不必先造一个 Request。
"""

import logging
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.errors import UnauthorizedError
from app.core.security import decode_access_token
from app.db import get_redis, get_session, get_session_factory
from app.db.enums import UserStatus
from app.db.models import User
from app.repositories import UserRepository
from app.services.auth_service import AuthService
from app.services.chat_throttle import ChatThrottle
from app.services.conversation_service import ConversationService
from app.services.exceptions import (
    AuthNotConfiguredError,
    ChatGateUnavailableError,
    StreamNotConfiguredError,
)
from app.services.login_throttle import LoginThrottle
from app.services.mail_service import MailService, get_mail_service
from app.services.stream_hub import StreamBuffer
from app.services.vaptcha_service import VaptchaService
from app.services.verification_service import VerificationService

logger = logging.getLogger(__name__)

# auto_error=False：交给全局异常处理器统一出响应。
# 用默认的 auto_error=True 的话，缺 token 时 FastAPI 会自己抛 HTTPException，
# 响应体是 {"detail": "Not authenticated"} —— 全站唯一的响应格式就此破功，
# 而且状态码是 403（规范上应该是 401）。
_bearer_scheme = HTTPBearer(
    auto_error=False,
    description="Bearer <access_token>，由 /api/v1/auth/login 签发",
)


def client_ip(request: Request) -> str | None:
    """取调用方 IP。

    只用 `request.client.host`，**不读 X-Forwarded-For**。
    XFF 是客户端可任意伪造的头，拿它做 IP 限流等于没做限流 ——
    攻击者每次请求换一个假 IP 就能绕过配额。
    部署在反向代理后面时，正确做法是让 uvicorn 处理：
        uvicorn app.main:app --proxy-headers --forwarded-allow-ips=<代理IP>
    此时 request.client.host 会被 uvicorn 替换成真实来源地址。
    """
    return request.client.host if request.client else None


def user_agent(request: Request) -> str:
    return request.headers.get("user-agent", "")


def get_verification_service(
    mail: Annotated[MailService, Depends(get_mail_service)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> VerificationService:
    if not settings.redis_configured:
        raise AuthNotConfiguredError("REDIS_PASSWORD")
    return VerificationService(get_redis(), mail, settings)


def get_login_throttle(
    settings: Annotated[Settings, Depends(get_settings)],
) -> LoginThrottle:
    if not settings.redis_configured:
        raise AuthNotConfiguredError("REDIS_PASSWORD")
    return LoginThrottle(get_redis(), settings)


def get_vaptcha_service(
    settings: Annotated[Settings, Depends(get_settings)],
) -> VaptchaService:
    """人机验证服务。

    未配置 VID/VKey 时**不报错**，返回 disabled 实例由 verify() 放行 ——
    人机验证是可选加固，不该让"没申请 VAPTCHA 的开发者"连服务都起不来。
    Redis 只在 local 模式用（票据一次性标记），没配降级为重放检测关闭。
    """
    return VaptchaService(settings, redis=get_redis() if settings.redis_configured else None)


def get_auth_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    verification: Annotated[VerificationService, Depends(get_verification_service)],
    throttle: Annotated[LoginThrottle, Depends(get_login_throttle)],
    vaptcha: Annotated[VaptchaService, Depends(get_vaptcha_service)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> AuthService:
    return AuthService(session, verification, throttle, vaptcha, settings)


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> User:
    """从 Authorization 头解出当前用户。

    这里**每请求查一次库**，而不是只信 token 里的 sub。
    代价是一次主键查询（走连接池，成本可忽略），换来的是：
    封号、注销、软删除能立刻生效，而不是等 token 自然过期。
    access token 只有 15 分钟，所以"每请求查库"并没有把 JWT 的无状态
    优势完全丢掉 —— 真正高频的读接口将来可以加本地缓存。
    """
    if credentials is None or not credentials.credentials:
        raise UnauthorizedError("缺少访问令牌")

    claims = decode_access_token(credentials.credentials)
    user = await UserRepository(session).get_by_id(claims.user_id)
    if user is None or user.status != UserStatus.ACTIVE:
        raise UnauthorizedError("登录已失效，请重新登录")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def get_conversation_service(
    settings: Annotated[Settings, Depends(get_settings)],
) -> ConversationService:
    """会话服务。

    注意它拿的是 **session factory** 而不是当前请求的 session：
    流式对话要跨越整条 SSE 流，用请求级 session 会把一条数据库连接
    占住几分钟。理由写在 ConversationService 的模块注释里。
    """
    return ConversationService(get_session_factory(), settings)


def get_stream_buffer(
    settings: Annotated[Settings, Depends(get_settings)],
) -> StreamBuffer:
    """流式分片缓冲。

    Redis 是硬依赖：续传要求"生成活过连接"，分片必须存在一个
    **跨进程可达**的地方。没配 Redis 就直接 503 说清楚 ——
    悄悄降级成"不可续传的流"会让客户端拿着续不上的 id 去重连，
    而且行为随部署环境漂移，比直接报错难查得多。
    """
    if not settings.redis_configured:
        raise StreamNotConfiguredError()
    return StreamBuffer(get_redis(), settings.SSE_RESUME_TTL_SECONDS)


def get_chat_throttle(
    settings: Annotated[Settings, Depends(get_settings)],
) -> ChatThrottle:
    """对话成本闸门。

    没配 Redis 时**不降级成"不限制"**：那样一来，出故障时把 Redis 摘掉
    就等于静默关掉了限流，而没有任何报警。宁可 503 说清楚。
    """
    if not settings.redis_configured:
        raise ChatGateUnavailableError()
    return ChatThrottle(get_redis(), settings)
