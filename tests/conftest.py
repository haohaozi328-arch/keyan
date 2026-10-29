"""pytest 公共夹具：客户端 + LLM 桩（成功/失败两种）+ 鉴权测试环境。

桩只实现被调用到的方法（ainvoke / astream_events），与 LCEL 链同构，
通过 dependency_overrides 注入，生产代码零改动。

**故意不提供同步 invoke**：一次性对话走的是 `await chain.ainvoke`，
桩若同时提供 invoke，某个调用点退回同步实现时测试照样能过，
等于把"阻塞事件循环"这个 bug 放过去了。
"""

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any
from urllib.parse import quote_plus

import pytest
from app.core.config import Settings, get_settings
from app.main import app
from app.services.llm_service import LLMService, get_llm_service
from app.services.mail_service import get_mail_service
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, AIMessageChunk
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine

USAGE = {"input_tokens": 11, "output_tokens": 22, "total_tokens": 33}


class StubChain:
    """逐字产出的桩链，事件结构与 LangChain 的 astream_events 一致。

    `delay` 是每块之间的间隔。断线续传的用例需要"生成还在跑"的窗口 ——
    瞬间吐完的桩里根本插不进断线这个动作，测出来的东西也就没有意义。
    """

    def __init__(self, answer: str = "这是桩回答", delay: float = 0.0) -> None:
        self.answer = answer
        self.delay = delay
        self.calls: list[dict[str, Any]] = []

    async def ainvoke(self, inputs: dict[str, Any]) -> AIMessage:
        self.calls.append(inputs)
        return AIMessage(content=self.answer, usage_metadata=USAGE)

    async def astream_events(
        self, inputs: dict[str, Any], version: str = "v2"
    ) -> AsyncIterator[dict[str, Any]]:
        self.calls.append(inputs)
        for piece in self.answer:
            if self.delay:
                await asyncio.sleep(self.delay)
            yield {
                "event": "on_chat_model_stream",
                "name": "ChatOpenAI",
                "data": {"chunk": AIMessageChunk(content=piece)},
            }
        yield {
            "event": "on_chat_model_end",
            "name": "ChatOpenAI",
            "data": {"output": AIMessage(content=self.answer, usage_metadata=USAGE)},
        }


class ExplodingChain:
    """模拟上游故障：调用即抛异常。"""

    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    async def ainvoke(self, inputs: dict[str, Any]) -> AIMessage:
        raise self.exc

    async def astream_events(
        self, inputs: dict[str, Any], version: str = "v2"
    ) -> AsyncIterator[dict[str, Any]]:
        # 空块会被服务层过滤，等价于"还没产出就失败"
        yield {"event": "on_chat_model_stream", "data": {"chunk": AIMessageChunk(content="")}}
        raise self.exc


def _make_service(chain: Any, configured: bool) -> LLMService:
    """用真实配置构造服务，再替换掉链 —— 提示词解析等逻辑保持真实行为。"""
    service = LLMService(get_settings())
    service._chain = chain  # noqa: SLF001
    service.configured = configured
    service.model = "stub-model"
    service.base_url = ""
    return service


@pytest.fixture()
def client() -> TestClient:
    """测试客户端：with 语句触发 lifespan，结束后清理依赖覆盖。"""
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture()
def stub_llm() -> Callable[..., StubChain]:
    """安装成功的桩：stub_llm(answer=..., configured=..., delay=...) → StubChain。"""

    def install(
        answer: str = "这是桩回答", configured: bool = True, delay: float = 0.0
    ) -> StubChain:
        chain = StubChain(answer, delay)
        service = _make_service(chain if configured else None, configured)
        app.dependency_overrides[get_llm_service] = lambda: service
        return chain

    return install


@pytest.fixture()
def failing_llm() -> Callable[..., None]:
    """安装失败的桩：上游异常模拟。"""

    def install(exc: Exception | None = None) -> None:
        chain = ExplodingChain(exc or RuntimeError("upstream boom"))
        app.dependency_overrides[get_llm_service] = lambda: _make_service(chain, True)

    return install


# --------------------------------------------------------------- 鉴权测试环境

# 测试专用 Redis 库。用独立 db 索引，避免 flush 时清掉开发环境里的东西。
TEST_REDIS_DB = 15
# 测试用户邮箱前缀。收尾时按这个前缀清理，不碰开发库里的真实数据。
TEST_EMAIL_PREFIX = "sr-test-"


class StubMailService:
    """捕获验证码，不真的发信。

    为什么必须从这里取码：`/auth/verification-code` 刻意**不返回**验证码
    （返回等于把"邮箱所有权"白送）。测试要拿到码，只能从发信这一侧截获 ——
    这恰好证明了"邮件服务可替换"这个设计是有用的。
    """

    configured = True

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_verification_code(
        self, *, to: str, code: str, scene: str, ttl_seconds: int
    ) -> None:
        self.sent.append({"to": to, "code": code, "scene": scene, "ttl": ttl_seconds})

    @property
    def last_code(self) -> str:
        assert self.sent, "还没有发出任何验证码"
        return str(self.sent[-1]["code"])


@pytest.fixture()
def stub_mail() -> StubMailService:
    stub = StubMailService()
    # get_verification_service 依赖 get_mail_service，覆盖上游即可自动传导
    app.dependency_overrides[get_mail_service] = lambda: stub
    return stub


@pytest.fixture()
def make_email() -> Callable[[], str]:
    """每个测试用独立邮箱，测试之间不共享状态。"""

    def build() -> str:
        return f"{TEST_EMAIL_PREFIX}{uuid.uuid4().hex[:12]}@example.com"

    return build


def _test_settings() -> Settings:
    return get_settings().model_copy(
        update={
            "REDIS_DB": TEST_REDIS_DB,
            # 测试会反复发码，把 IP 配额放大，免得配额错误盖住真正的断言。
            # 配额本身在 test_verification.py 里单独测。
            "VERIFICATION_HOURLY_LIMIT_PER_IP": 1000,
            # 冷却缩到 1 秒：有些用例（比如"重复注册"）必须给同一个邮箱发两次码，
            # 保持 60 秒会让每个这类用例都卡一分钟。
            "VERIFICATION_CODE_RESEND_SECONDS": 1,
            # 登录失败也按 IP 计数，而所有用例都来自同一个测试客户端 IP。
            # 不放大就会"单跑能过、整套跑 429"。按账号的阈值保持 5，
            # 那才是 test_auth.py 里要断言的东西。
            "LOGIN_MAX_ATTEMPTS_PER_IP": 1000,
            # 测试环境固定关闭人机验证：开发机 .env 里可能配了 VAPTCHA_VID/VKey，
            # 不清掉的话所有发码用例都会撞 11400。
            # 人机验证自身的行为契约只在 test_vaptcha.py 里测，那里自建配置。
            "VAPTCHA_VID": "",
            "VAPTCHA_VKEY": "",
        }
    )


def _patch_test_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """把应用侧的 Redis 指向测试库。

    两条路都要打：
    - `dependency_overrides[get_settings]` —— 覆盖所有 `Depends(get_settings)`。
      `Depends` 在**定义时**就捕获了函数对象，改模块属性对它无效。
    - `app.db.redis.get_settings` —— 那是直接调用，不走依赖注入。

    另一个好处：`get_redis()` 仍然在**应用的事件循环里**创建客户端。
    如果由夹具在外部线程建好再注入，连接会绑在另一个 loop 上，
    报错是 `attached to a different loop`，而且往往出现在毫不相干的测试里。
    """
    patched = _test_settings()
    app.dependency_overrides[get_settings] = lambda: patched
    monkeypatch.setattr("app.db.redis.get_settings", lambda: patched)
    return patched


@pytest.fixture()
def test_redis_url() -> str:
    """测试库的 DSN，供不经 HTTP 的用例自己建客户端。"""
    settings = get_settings()
    if not settings.redis_configured:
        pytest.skip("未配置 REDIS_PASSWORD，跳过验证码测试")
    password = quote_plus(settings.REDIS_PASSWORD)
    return f"redis://:{password}@{settings.REDIS_HOST}:{settings.REDIS_PORT}/{TEST_REDIS_DB}"


async def _purge_test_users() -> None:
    """删除测试用户及其对话数据。

    auth_sessions 对 users 是 ON DELETE CASCADE，删用户会级联清掉会话，
    不用手动删两张表。

    **但对话数据不是**：`conversations.user_id` 和 `messages.conversation_id`
    都是 `ON DELETE RESTRICT`（对话内容是排查问题的证据，不能被级联误删）。
    所以先按邮箱前缀删消息、再删会话，最后才轮到用户 —— 顺序反了会直接
    撞上外键约束，而清理失败一旦被静默吞掉，测试用户就会一直堆在库里。
    """
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    params = {"pattern": f"{TEST_EMAIL_PREFIX}%@example.com"}
    try:
        async with engine.begin() as conn:
            # LIKE 的模式串作为**绑定参数**传入，不是拼进 SQL 文本 ——
            # 值里的 % 是字面量，不会被当成通配符以外的东西。
            await conn.execute(
                text(
                    "DELETE m FROM messages m JOIN users u ON m.user_id = u.id "
                    "WHERE u.email LIKE :pattern"
                ),
                params,
            )
            await conn.execute(
                text(
                    "DELETE c FROM conversations c JOIN users u ON c.user_id = u.id "
                    "WHERE u.email LIKE :pattern"
                ),
                params,
            )
            await conn.execute(
                text("DELETE FROM users WHERE email LIKE :pattern"),
                params,
            )
    finally:
        await engine.dispose()


async def _flush_test_redis() -> None:
    """清掉测试库里的验证码 / 配额键。

    不能用 FLUSHDB：它属于 Redis 的 @dangerous 分类，而 deploy 里的 ACL
    是 `+@all -@dangerous`，会直接返回 NOPERM。
    更坑的是"清理失败"如果被静默吞掉，计数器会跨轮次累加 ——
    表现为"单跑能过、连着跑就 429"，而且查不出原因。
    所以这里用 SCAN + DEL，两者都不在 @dangerous 里。
    """
    settings = get_settings()
    client = Redis(
        host=settings.REDIS_HOST,
        port=settings.REDIS_PORT,
        password=settings.REDIS_PASSWORD,
        db=TEST_REDIS_DB,
        decode_responses=True,
    )
    try:
        # 会话级清一次：验证码/配额（vc:*）与对话闸门计数（chat:*）。
        # 后者带 TTL，但额度键的 TTL 是两天 —— 不清的话测试之间会互相影响。
        for pattern in ("vc:*", "chat:*"):
            stale = [key async for key in client.scan_iter(match=pattern, count=500)]
            if stale:
                await client.delete(*stale)
    finally:
        await client.aclose()


@pytest.fixture(scope="session", autouse=True)
def _isolated_backends() -> Iterator[None]:
    """整个测试会话前后各清一次测试库。

    只在**测试专用**的 MySQL 数据（按邮箱前缀）和 Redis db 15 上操作，
    不碰开发环境的其他数据。任一后端不可达时静默跳过，
    让不依赖它们的用例照常跑。
    """
    settings = get_settings()

    def purge() -> None:
        if settings.db_configured:
            with contextlib.suppress(SQLAlchemyError, OSError):
                asyncio.run(_purge_test_users())
        if settings.redis_configured:
            # 这里故意不 suppress 所有异常：清理失败必须让测试红，
            # 否则会以"跨轮次累积配额"的形式在别处爆出来，极难定位。
            try:
                asyncio.run(_flush_test_redis())
            except Exception as exc:  # noqa: BLE001
                pytest.exit(f"测试 Redis 清理失败，拒绝在不干净的环境里跑测试：{exc}")

    purge()
    yield
    purge()


@pytest.fixture()
def auth_client(
    monkeypatch: pytest.MonkeyPatch,
    stub_mail: StubMailService,
) -> Iterator[TestClient]:
    """鉴权接口专用的 TestClient。

    注册/登录要写真实 MySQL，所以数据库不可用时直接 skip，
    而不是让一堆用例报 500 把真正的失败埋掉。
    """
    settings = get_settings()
    if not settings.db_configured:
        pytest.skip("未配置 DB_PASSWORD，跳过鉴权接口测试")
    if not settings.redis_configured:
        pytest.skip("未配置 REDIS_PASSWORD，跳过鉴权接口测试")
    if not settings.jwt_configured:
        pytest.skip("未配置 JWT_SECRET，跳过鉴权接口测试")

    _patch_test_settings(monkeypatch)
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


# 注册接口要求密码有强度，测试统一用这一串
TEST_PASSWORD = "correct horse battery staple"
CODE_PATH = "/api/v1/auth/verification-code"
REGISTER_PATH = "/api/v1/auth/register"


@pytest.fixture()
def auth_headers(
    auth_client: TestClient,
    stub_mail: StubMailService,
    make_email: Callable[[], str],
) -> dict[str, str]:
    """注册一个临时用户并返回 Authorization 头。

    对话接口要求登录，所以凡是走 `/chat`、`/chat/stream` 的用例都得先有身份。
    注册的用户走 `sr-test-` 前缀，会话级 purge 会连同对话数据一起清掉。
    """
    email = make_email()
    assert auth_client.post(CODE_PATH, json={"email": email}).status_code == 200
    response = auth_client.post(
        REGISTER_PATH,
        json={"email": email, "password": TEST_PASSWORD, "code": stub_mail.last_code},
    )
    assert response.status_code == 201, response.text
    token = response.json()["data"]["token"]["access_token"]
    return {"Authorization": f"Bearer {token}"}
