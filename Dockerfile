# 应用镜像：多阶段构建，运行镜像里不带编译工具链。
#
# 为什么分两段：只编译 wheel 时需要 gcc，而 gcc + headers 会让镜像大 300MB 左右，
# 还会把编译器一起交付到线上 —— 多出来的攻击面没有任何用处。
#
# 用法：
#   docker build -t agent-chat .
#   docker run --env-file .env -p 8000:8000 agent-chat
# 或者直接 `docker compose --profile app up -d`（见 docker-compose.yml）。

# syntax=docker/dockerfile:1

FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 少数依赖在缺少 wheel 时需要现场编译；装在这里不会进最终镜像
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
# 镜像装的是**锁定的**版本（requirements.lock），不是 ranges：
# 生产环境要的是"每次构建出来的东西完全一样"，
# 而 ranges 是给 CI 用的 —— 让上游的新版本先在那里暴露问题。
COPY requirements.lock .
COPY requirements.txt .
RUN pip install --prefix=/install -r requirements.lock


FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    # 生产默认 JSON 日志：一行一个 JSON 才能被 Loki / ELK 按字段检索
    LOG_FORMAT=json \
    LOG_LEVEL=INFO \
    PROMPTS_DIR=/app/prompts

# tini 接管 PID 1：uvicorn 收到 SIGTERM 才会走优雅停机（等 SSE 收尾），
# 没有它时 PID 1 是 python，信号处理容易变成"直接 kill -9 掉整组"
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 appuser

WORKDIR /app

COPY --from=builder /install /usr/local
COPY --chown=appuser:appuser app ./app
COPY --chown=appuser:appuser alembic ./alembic
COPY --chown=appuser:appuser alembic.ini pyproject.toml ./
COPY --chown=appuser:appuser prompts ./prompts

# 不用 root 跑：容器逃逸的第一道成本就是拿到一个非特权 uid
USER appuser

EXPOSE 8000

# 只探活，不查依赖：liveness 失败会触发重启，依赖抖动时不该重启应用
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=2).status == 200 else 1)"

ENTRYPOINT ["/usr/bin/tini", "--"]
# 0.0.0.0 是容器里唯一正确的监听地址：绑 127.0.0.1 时端口映射不通
# --proxy-headers 让 request.client.host 取到真实来源（限流按 IP 计数依赖它）
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
