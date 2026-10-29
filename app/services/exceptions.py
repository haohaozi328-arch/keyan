"""业务异常：service 层只表达"发生了什么"，不关心 HTTP。

所有异常都继承 AppError，由全局处理器翻译成统一响应 ——
service 里看不到一个 HTTPException，换框架也不用重写业务。
"""

from app.core.errors import AppError, ErrorCode


class ServiceError(AppError):
    """业务异常基类。

    子类必须用 default_code 声明自己的错误码；
    没声明就落到 500，等于告诉调用方"这是 bug"，属于有意为之的默认值。
    """

    default_code: ErrorCode = ErrorCode.INTERNAL_ERROR

    def __init__(
        self,
        message: str | None = None,
        *,
        code: ErrorCode | None = None,
        data: object | None = None,
    ) -> None:
        super().__init__(code or self.default_code, message, data=data)


class LLMNotConfiguredError(ServiceError):
    """LLM 未配置（密钥为空）：功能整体关闭，映射 503。"""

    default_code = ErrorCode.LLM_NOT_CONFIGURED

    def __init__(self) -> None:
        super().__init__("LLM 未配置：请设置 LLM_API_KEY 后重启服务")


class LLMUpstreamError(ServiceError):
    """上游网关故障（密钥无效 / 限流 / 超时 / 网络不可达）。

    只暴露中性摘要（这里给的是异常**类名**，不是异常内容），
    原始异常挂在 __cause__ 上供日志溯源 ——
    上游报错原文常带密钥前缀、内部地址，绝不能进响应体。
    """

    default_code = ErrorCode.LLM_UPSTREAM_ERROR

    def __init__(self, reason: str) -> None:
        super().__init__(f"LLM 上游服务异常：{reason}")


class StreamExpiredError(ServiceError):
    """续传时缓冲已不在（TTL 到期 / 进程重启过）。

    「过期」和「不存在」必须分开：前者消息还在库里，客户端应该去拉全文；
    后者是真的没有这条消息（或不属于当前用户），拉也拉不到。
    都回 404 会让前端误以为消息被删了。
    """

    default_code = ErrorCode.CHAT_STREAM_EXPIRED


class StreamNotConfiguredError(ServiceError):
    """没配 Redis：流式对话的续传缓冲无处可放。

    不静默降级成"不可续传的流"—— 那会让客户端以为拿到了可续传的 id，
    断线后才发现接不上，且行为随部署环境漂移，很难排查。
    """

    default_code = ErrorCode.SERVICE_UNAVAILABLE

    def __init__(self) -> None:
        super().__init__("流式对话需要 Redis：请设置 REDIS_PASSWORD 后重启服务")


class ChatRateLimitedError(ServiceError):
    """成本闸门拦下的请求：频率超限 / 在途流太多 / 当日 token 预算用尽。

    一律 429。三种情况的客户端动作完全一样（等一会儿再来），
    区别只写进 message，让人看得懂"是刷太快还是额度用完了"。
    """

    default_code = ErrorCode.CHAT_RATE_LIMITED

    def __init__(self, message: str = "对话请求过于频繁，请稍后再试") -> None:
        super().__init__(message)


class ChatGateUnavailableError(ServiceError):
    """没配 Redis：成本闸门的计数器无处可放。

    **fail-closed**，不降级成"不限制" —— 一个"Redis 没配就没有限流"的系统，
    出问题时第一反应就是把 Redis 摘掉，限流随之静默消失，而这不会报任何错。
    """

    default_code = ErrorCode.SERVICE_UNAVAILABLE

    def __init__(self) -> None:
        super().__init__("对话需要 Redis（限流与额度计数）：请设置 REDIS_PASSWORD 后重启服务")


# --------------------------------------------------------------------- 鉴权


class AuthNotConfiguredError(ServiceError):
    """JWT 密钥 / 邮件账号没配：功能整体关闭，映射 503。"""

    default_code = ErrorCode.AUTH_NOT_CONFIGURED

    def __init__(self, missing: str) -> None:
        super().__init__(f"鉴权未配置：请设置 {missing} 后重启服务")


class InvalidCredentialsError(ServiceError):
    """邮箱或密码不正确。

    文案**故意不区分**"邮箱不存在"和"密码错"：
    区分了就等于送出一个账号枚举接口，攻击者可以先批量确认哪些邮箱注册过。
    """

    default_code = ErrorCode.UNAUTHORIZED

    def __init__(self) -> None:
        super().__init__("邮箱或密码不正确")


class SessionInvalidError(ServiceError):
    """登录态不存在 / 已撤销 / 已过期，三者对外是同一个结果。

    不区分是刻意的：客户端对这三种情况的处理完全一样（回登录页），
    而区分会告诉攻击者"这个 refresh token 曾经是真的"。
    """

    default_code = ErrorCode.UNAUTHORIZED

    def __init__(self, message: str = "登录已失效，请重新登录") -> None:
        super().__init__(message)


class AccountDisabledError(ServiceError):
    """账号被停用 / 锁定。403 而不是 401：凭证是对的，是权限不够。"""

    default_code = ErrorCode.FORBIDDEN

    def __init__(self) -> None:
        super().__init__("账号已被停用，请联系管理员")


class EmailAlreadyRegisteredError(ServiceError):
    default_code = ErrorCode.AUTH_EMAIL_TAKEN

    def __init__(self) -> None:
        super().__init__("该邮箱已被注册")


class VerificationCodeError(ServiceError):
    """验证码错误 / 过期 / 试错次数用尽，三者对外是同一个错误码。

    内部区分只写进日志：对外区分会告诉攻击者"这个码存在，只是不对"。
    """

    default_code = ErrorCode.AUTH_CODE_INVALID

    def __init__(self, message: str = "验证码不正确或已过期") -> None:
        super().__init__(message)


class CaptchaInvalidError(ServiceError):
    """人机验证票据缺失 / 无效 / 已被使用。

    与邮箱验证码共用 11400：票据缺失和验签不通过也返回同一个错误码，
    区分了等于告诉攻击者"服务端开了人机验证、但你的请求格式不对"。
    文案在这里覆盖默认值，让用户看到的是"重新做人机验证"而不是"查邮件"。
    """

    default_code = ErrorCode.AUTH_CODE_INVALID

    def __init__(self, message: str = "人机验证未通过，请重新验证") -> None:
        super().__init__(message)


class CaptchaUpstreamError(ServiceError):
    """VAPTCHA Verify 接口超时 / 不可达 / 非 200。

    fail-closed：验签服务挂了就拒绝发码，而不是放行。
    开着人机验证却在上游故障时放行，等于给攻击者一个"打挂验签就免验证"的按钮。
    与 LLM_TIMEOUT（22504）同构取 504；原始异常挂 __cause__，响应只给中性文案。
    """

    default_code = ErrorCode.CAPTCHA_UPSTREAM_ERROR

    def __init__(self, reason: str) -> None:
        super().__init__(f"人机验证服务异常：{reason}")


class AuthRateLimitedError(ServiceError):
    default_code = ErrorCode.AUTH_RATE_LIMITED

    def __init__(self, message: str = "操作过于频繁，请稍后再试") -> None:
        super().__init__(message)


class MailSendError(ServiceError):
    """SMTP 发信失败。原始异常挂 __cause__，响应里只给中性文案。"""

    default_code = ErrorCode.AUTH_MAIL_SEND_FAILED

    def __init__(self, reason: str) -> None:
        super().__init__(f"验证码邮件发送失败：{reason}")
