"""错误码与异常体系。

错误码是 5 位十进制：X M SSS

    X    错误归属：1=调用方（客户端的问题）  2=服务端/依赖
    M    模块：0=通用  1=鉴权  2=对话  3=数据
    SSS  HTTP 状态码

把 HTTP 状态码编进错误码里，是为了排障时一眼看懂：
看到 22502 就知道"对话模块、服务端问题、上游 502"，
不用翻对照表。前端也能只看首位决定"该提示用户还是该重试"。

异常分两类，处理方式完全不同：
    AppError 及其子类  → 可预期，翻译成统一响应，不记 error 日志
    其他 Exception     → 未预期，兜底 500，记 error 日志并带堆栈
所以"这个错误该不该让用户看见具体原因"，由继承关系决定，不靠手写 if。
"""

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ErrorSpec:
    """错误码的三要素：业务码、HTTP 状态、默认文案。"""

    code: int
    http_status: int
    message: str


class ErrorCode(Enum):
    OK = ErrorSpec(0, 200, "ok")

    # ---- 通用（1 0 xxx）----
    INVALID_PARAM = ErrorSpec(10400, 400, "请求参数不合法")
    NOT_FOUND = ErrorSpec(10404, 404, "资源不存在")
    METHOD_NOT_ALLOWED = ErrorSpec(10405, 405, "请求方法不支持")
    CONFLICT = ErrorSpec(10409, 409, "资源冲突")
    VALIDATION_FAILED = ErrorSpec(10422, 422, "请求参数校验失败")
    RATE_LIMITED = ErrorSpec(10429, 429, "请求过于频繁，请稍后再试")
    INTERNAL_ERROR = ErrorSpec(10500, 500, "服务内部异常")
    BAD_GATEWAY = ErrorSpec(10502, 502, "上游服务异常")
    SERVICE_UNAVAILABLE = ErrorSpec(10503, 503, "服务暂时不可用")

    # ---- 鉴权（1 1 xxx / 2 1 xxx）----
    # X=1 是调用方能自己纠正的（密码错、码错、重复注册）；
    # X=2 是服务端依赖的问题（邮件发不出去、功能没配）。
    # 两类分开，前端才知道该让用户改输入、还是提示"稍后重试"。
    AUTH_CODE_INVALID = ErrorSpec(11400, 400, "验证码不正确或已过期")
    # 11400 是鉴权模块 400 的唯一编码，邮箱验证码与人机验证票据共用：
    # 客户端动作一样（重新获取 / 重新验证），区分只会给绕过尝试免费指导。
    UNAUTHORIZED = ErrorSpec(11401, 401, "未登录或登录已失效")
    FORBIDDEN = ErrorSpec(11403, 403, "没有权限执行该操作")
    AUTH_EMAIL_TAKEN = ErrorSpec(11409, 409, "该邮箱已被注册")
    AUTH_RATE_LIMITED = ErrorSpec(11429, 429, "操作过于频繁，请稍后再试")
    AUTH_MAIL_SEND_FAILED = ErrorSpec(21502, 502, "验证码邮件发送失败")
    # 与 22504（LLM 上游响应超时）同构：验签上游超时 / 不可达，fail-closed
    CAPTCHA_UPSTREAM_ERROR = ErrorSpec(21504, 504, "人机验证服务异常")
    AUTH_NOT_CONFIGURED = ErrorSpec(21503, 503, "鉴权功能未配置")

    # ---- 对话（2 2 xxx）----
    LLM_UPSTREAM_ERROR = ErrorSpec(22502, 502, "LLM 上游服务异常")
    LLM_NOT_CONFIGURED = ErrorSpec(22503, 503, "LLM 未配置")
    LLM_TIMEOUT = ErrorSpec(22504, 504, "LLM 上游响应超时")
    # 成本闸门：请求太频 / 在途流太多 / 当日 token 预算用尽。
    # 三者共用 429：客户端动作完全一样（等一会儿再来），
    # 而区分成 403 或 402 只会让前端为"同一个处理"写三套分支。
    CHAT_RATE_LIMITED = ErrorSpec(22429, 429, "对话请求过于频繁，请稍后再试")
    # 流式生成期间长时间没有任何客户端接收，服务端主动停掉（省 token）
    CHAT_STREAM_ABANDONED = ErrorSpec(22408, 408, "长时间没有客户端接收，生成已中止")
    # 分片缓冲已过期（TTL 到了 / 进程重启）：续传失败，客户端应改拉消息全文
    CHAT_STREAM_EXPIRED = ErrorSpec(22410, 410, "流式响应已过期，请重新读取消息")

    @property
    def code(self) -> int:
        return self.value.code

    @property
    def http_status(self) -> int:
        return self.value.http_status

    @property
    def message(self) -> str:
        return self.value.message


class AppError(Exception):
    """可预期异常的基类。

    继承它 = 由全局处理器翻译成统一响应，文案可以给用户看。
    不继承 = 走到兜底 500，日志里留堆栈，响应里只说"服务内部异常"。

    message 允许覆盖默认文案，但**不要把上游原始报错拼进去** ——
    上游的错误信息可能带密钥、内部地址、SQL 片段。
    需要溯源就把原始异常挂在 __cause__ 上，让日志去承载。
    """

    def __init__(
        self,
        error_code: ErrorCode,
        message: str | None = None,
        *,
        data: Any = None,
    ) -> None:
        self.error_code = error_code
        self.message = message or error_code.message
        self.data = data
        super().__init__(self.message)

    def __str__(self) -> str:
        return f"[{self.error_code.code}] {self.message}"


class InvalidParamError(AppError):
    def __init__(self, message: str | None = None, *, data: Any = None) -> None:
        super().__init__(ErrorCode.INVALID_PARAM, message, data=data)


class NotFoundError(AppError):
    def __init__(self, message: str | None = None, *, data: Any = None) -> None:
        super().__init__(ErrorCode.NOT_FOUND, message, data=data)


class ConflictError(AppError):
    def __init__(self, message: str | None = None, *, data: Any = None) -> None:
        super().__init__(ErrorCode.CONFLICT, message, data=data)


class UnauthorizedError(AppError):
    def __init__(self, message: str | None = None, *, data: Any = None) -> None:
        super().__init__(ErrorCode.UNAUTHORIZED, message, data=data)


class ForbiddenError(AppError):
    def __init__(self, message: str | None = None, *, data: Any = None) -> None:
        super().__init__(ErrorCode.FORBIDDEN, message, data=data)
