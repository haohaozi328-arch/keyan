"""健康检查测试：存活探针与就绪探针的语义差异。

探针函数用 monkeypatch 替换，不依赖真实 MySQL/Redis：
这里要验的是**状态码与结论的映射规则**（未配置 → 200 降级；
配了连不上 → 503），而不是驱动能不能连库 —— 后者由鉴权用例真连真写覆盖。
"""

import asyncio
from collections.abc import Callable
from typing import Any

import pytest
from app.core.config import get_settings
from app.main import app
from fastapi.testclient import TestClient

LIVE = "/api/v1/health"
READY = "/api/v1/health/ready"

MODULE = "app.api.routes.health"

# 哨兵：表示"这个探针别替换，用真实实现"
REAL = object()


@pytest.fixture()
def stub_probes(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """把探针换掉：stub_probes(db=..., redis=..., llm=...)。

    取值含义：
        结果字符串  —— 直接返回该结果
        异常实例    —— 抛出，模拟连不上
        浮点数      —— 先睡这么多秒再返回，用来测超时
        REAL        —— 保留真实实现
    """

    def install(db: Any = "ok", redis: Any = "ok", llm: Any = "ok") -> None:
        for name, value in (("db", db), ("redis", redis), ("llm", llm)):
            if value is REAL:
                continue

            def make(value: Any = value) -> Callable[..., Any]:
                # 真实探针都收一个位置参数（config 或 service），
                # 桩必须保持同签名，否则路由传进来的实参会被当成"返回值"本身
                async def probe(_dependency: Any = None) -> str:
                    if isinstance(value, Exception):
                        raise value
                    if isinstance(value, float):
                        await asyncio.sleep(value)
                        return "ok"
                    return str(value)

                return probe

            monkeypatch.setattr(f"{MODULE}._probe_{name}", make())

    return install


def test_liveness_ok(client: TestClient) -> None:
    response = client.get(LIVE)

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    assert body["data"]["status"] == "ok"
    assert "version" in body["data"]


def test_readiness_ready(
    client: TestClient, stub_llm: Callable[..., Any], stub_probes: Callable[..., None]
) -> None:
    stub_llm()
    stub_probes()

    response = client.get(READY)

    assert response.status_code == 200
    assert response.json()["data"] == {
        "status": "ready",
        "checks": {"db": "ok", "redis": "ok", "llm": "ok"},
    }


def test_readiness_degraded_when_llm_missing(
    client: TestClient, stub_llm: Callable[..., Any], stub_probes: Callable[..., None]
) -> None:
    """未配置密钥：标记 degraded 但仍返回 200，不触发摘流量/重启。"""
    stub_llm(configured=False)
    stub_probes(llm="not_configured")

    response = client.get(READY)

    assert response.status_code == 200
    assert response.json()["data"] == {
        "status": "degraded",
        "checks": {"db": "ok", "redis": "ok", "llm": "not_configured"},
    }


def test_readiness_degraded_when_backends_not_configured(
    client: TestClient, stub_llm: Callable[..., Any], stub_probes: Callable[..., None]
) -> None:
    """本地只配了一部分：仍然 200。

    这条是防"一刀切 503"的回归 —— 没起 Redis 的开发机上
    不该连健康检查都过不去。
    """
    stub_llm()
    stub_probes(db="not_configured", redis="not_configured")

    response = client.get(READY)

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "degraded"


@pytest.mark.parametrize("broken", ["db", "redis"])
def test_readiness_503_when_configured_backend_unreachable(
    client: TestClient,
    stub_llm: Callable[..., Any],
    stub_probes: Callable[..., None],
    broken: str,
) -> None:
    """配了却连不上 → 503。

    必须是 503 而不是 200：DB/Redis 是登录态的硬依赖，
    留着流量只会让用户看到一堆 500，不如让负载均衡把请求转走。
    也不能是 500 —— 500 会让编排系统认为"应用崩了"并丢掉 checks 明细。
    """
    stub_llm()
    stub_probes(**{broken: ConnectionError("connection refused")})

    response = client.get(READY)

    assert response.status_code == 503
    body = response.json()
    assert body["data"]["status"] == "not_ready"
    assert body["data"]["checks"][broken] == "error"
    # 统一外壳不能破：503 也要有 code / trace_id
    assert body["code"] != 0
    assert body["trace_id"]


def test_readiness_503_on_probe_timeout(
    client: TestClient, stub_llm: Callable[..., Any], stub_probes: Callable[..., None]
) -> None:
    """探针卡住也要收敛成 error，不能让请求一直挂着。

    把超时调到 50ms、让探针睡 5 秒：如果超时没生效，这个用例会明显变慢
    而不是变红 —— 所以断言里同时带上"结果正确"，两者缺一不可。
    """
    stub_llm()
    stub_probes(db=5.0)
    app.dependency_overrides[get_settings] = lambda: get_settings().model_copy(
        update={"HEALTH_CHECK_TIMEOUT_SECONDS": 0.05}
    )

    response = client.get(READY)

    assert response.status_code == 503
    assert response.json()["data"]["checks"]["db"] == "error"


def test_readiness_never_checks_llm_by_calling_upstream(
    client: TestClient, stub_llm: Callable[..., Any], stub_probes: Callable[..., None]
) -> None:
    """就绪探针不能真调上游模型。

    真调一次要几百毫秒到几秒、还产生费用；上游抖动时探针会跟着失败，
    结果把所有实例一起摘掉，把"模型慢"放大成"全站不可用"。
    所以这里保留真实的 llm 探针，只断言它没碰模型。
    """
    chain = stub_llm()
    stub_probes(db="ok", redis="ok", llm=REAL)

    response = client.get(READY)

    assert response.json()["data"]["checks"]["llm"] == "ok"
    assert chain.calls == [], "就绪探针不应触发模型调用"


# ------------------------------------------------------------------ 指标


def test_metrics_endpoint_serves_prometheus_text(client: TestClient) -> None:
    """`/metrics` 走标准文本格式，**不套**统一响应外壳。

    它挂在根路径而不是 /api/v1：抓取路径是运维侧的约定值，
    改 API 版本不该让所有 scrape 配置一起失效。
    """
    response = client.get("/metrics")

    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    body = response.text

    # 业务指标：在途流与首字延迟是这套系统最该被盯的两个数
    assert "chat_streams_inflight" in body
    assert "chat_first_token_seconds" in body
    assert "chat_turn_duration_seconds" in body
    # HTTP 指标：标签是路由模板，不是原始路径（否则会话 ID 会撑爆序列数）
    assert "http_requests_total" in body
