"""日志初始化：结构化输出 + trace_id 贯通。

为什么必须显式配置
------------------
Python 的 root logger 默认级别是 WARNING，且**没有 handler 时**由 lastResort
打到 stderr。项目里 30 多处 `logger.info`（提示词版本、限流命中、生成中止）
在默认配置下全部被丢掉 —— 换句话说，线上出问题时该看的那些行根本不存在。
`vaptcha_service.py` 里"应用未单独配置 logging，info 会被 root 的 WARNING 挡掉"
那句注释，说的就是这个坑。

为什么默认 JSON
---------------
一行一个 JSON 才能被 Loki / ELK / CloudWatch 直接按字段检索。
本地开发想要肉眼可读时把 `LOG_FORMAT=text` 即可。
`.env.example` 里默认给 text，生产（Dockerfile / compose）显式设 json。

trace_id 从 ContextVar 取
------------------------
中间件在请求入口 set，日志格式化时读 —— 于是同一请求产生的所有日志行
（跨 router / service / repository）自动带同一个值，
不需要逐层传参，也不会串到别的请求上（ContextVar 是按 task 隔离的）。
"""

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from app.core.config import Settings
from app.core.context import current_trace_id

# 这两个库在 INFO 级别会把每个上游请求打一行，URL 里可能带查询参数；
# 排查 LLM/验签问题时才需要，平时降到 WARNING 免得盖住业务日志。
NOISY_LOGGERS = ("httpx", "httpcore", "aiomysql")

# uvicorn 自带的 handler 与格式必须摘掉，否则同一条日志会出现两遍、
# 且两条格式不同（一遍是 uvicorn 的默认格式，没有 trace_id）。
UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


class JsonFormatter(logging.Formatter):
    """一行一个 JSON：时间、级别、logger 名、消息、trace_id、异常堆栈。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            # UTC + 毫秒：多实例部署时本地时间会互相错位，排序就没意义了
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "trace_id": current_trace_id(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    """本地开发用：`时间 级别 [trace_id] logger: 消息`。"""

    def format(self, record: logging.LogRecord) -> str:
        trace = current_trace_id() or "-"
        base = (
            f"{self.formatTime(record, '%H:%M:%S')} {record.levelname:<7} "
            f"[{trace[:8]}] {record.name}: {record.getMessage()}"
        )
        if record.exc_info:
            base = f"{base}\n{self.formatException(record.exc_info)}"
        return base


def setup_logging(settings: Settings) -> None:
    """装配 root logger。幂等：重复调用只会替换 handler，不会叠加。"""
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JsonFormatter() if settings.LOG_FORMAT == "json" else TextFormatter())

    root = logging.getLogger()
    for existing in root.handlers[:]:
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(settings.LOG_LEVEL.upper())

    for name in UVICORN_LOGGERS:
        logger = logging.getLogger(name)
        for existing in logger.handlers[:]:
            logger.removeHandler(existing)
        # propagate=True 让 uvicorn 的日志走 root 的格式化，
        # 否则 access log 会是另一种格式、且不带 trace_id
        logger.propagate = True

    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
