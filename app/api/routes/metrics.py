"""Prometheus 抓取端点。

为什么放在根路径 `/metrics` 而不是 `/api/v1` 下
--------------------------------------------
抓取路径是运维侧的约定值（Prometheus 配置、Grafana Agent、k8s PodMonitor 里
都写死了 `/metrics`）。把它挂在版本号下面，意味着改 API 版本要让所有抓取配置
一起失效 —— 而指标与 API 契约本来就没有关系。

为什么**不套**统一响应外壳
------------------------
Prometheus 只认文本格式（`text/plain; version=0.0.4`），包成
`{code, message, data}` 会让所有抓取直接失败。这是全项目唯一的例外，
理由和探针不同：探针只看状态码，所以可以包；这个连 body 都要被解析。

为什么默认不鉴权
----------------
它只暴露聚合计数（没有用户内容、没有 ID），行业标准做法是靠网络隔离：
只监听内网，由反向代理限制来源。若要更强的隔离，把 `METRICS_ENABLED`
关掉，改由 sidecar `curl localhost:8000/metrics` 转发。
"""

from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST

from app.core.metrics import render

router = APIRouter()


@router.get(
    "/metrics",
    summary="Prometheus 指标",
    description="Prometheus 文本格式。不套统一响应外壳，抓取端按标准格式解析。",
    response_class=Response,
    include_in_schema=False,
)
async def prometheus_metrics() -> Response:
    return Response(content=render(), media_type=CONTENT_TYPE_LATEST)
