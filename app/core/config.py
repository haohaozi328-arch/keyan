"""全局配置：环境相关的值集中在此。

优先级：真实环境变量 > .env 文件 > 本文件默认值。
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import quote_plus

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.paths import resolve_path
from app.prompts import PromptKey


class Settings(BaseSettings):
    """项目配置项，字段名即环境变量名（区分大小写）。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # 应用元信息
    PROJECT_NAME: str = "learn-fastapi"
    VERSION: str = "0.1.0"
    DEBUG: bool = False

    # ---- 日志 ----
    # 必须显式配置：Python 的 root logger 默认 WARNING 且没有 handler，
    # 项目里成片的 logger.info 会被静默丢掉。
    # 用 Literal 而不是 str：级别名拼错时在启动阶段就报错，
    # 而不是等到线上发现"日志怎么没了"
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    # json：一行一个 JSON，供 Loki / ELK 按字段检索；text：本地肉眼可读
    LOG_FORMAT: Literal["json", "text"] = "json"

    # ---- 指标 ----
    # /metrics 暴露在根路径（不进 /api/v1）：Prometheus 的抓取路径是约定值，
    # 通常由反向代理或内网限定来源，不适合对外网开放。
    METRICS_ENABLED: bool = True

    # 接口与文档
    # 版本只体现在 URL 契约里，目录结构不再嵌套 v1
    API_PREFIX: str = "/api/v1"
    DOCS_URL: str = "/docs"
    OPENAPI_URL: str = "/api/v1/openapi.json"

    # 跨域：环境变量里用 JSON 数组写法，如 CORS_ORIGINS=["http://localhost:5173"]
    CORS_ORIGINS: list[str] = ["*"]

    # 服务监听
    HOST: str = "127.0.0.1"
    PORT: int = 8000

    # SSE：ping 注释的间隔（秒）。要小于网关/负载均衡的空闲超时（常见 60s）
    SSE_PING_SECONDS: float = 15.0

    # 断线续传：一轮生成结束后，Redis 里的分片缓冲还能留多久（秒）。
    # 太短 = 手机切个后台回来就续不上了；太长 = 白占内存。
    # 过期后续传接口返回 410，客户端改用 GET 消息列表拿全文。
    SSE_RESUME_TTL_SECONDS: int = 600

    # 断线续传：**所有**客户端都断开多久之后停止生成（秒）。
    # 续传要求生成活过连接，所以断线不能立刻停 —— 但一直开着就是白烧 token。
    # 这个值就是"离开多久算放弃"的界线，客户端在窗口内重连都能无缝接上。
    SSE_RESUME_GRACE_SECONDS: int = 30

    # 就绪探针里单个依赖检查的超时（秒）。
    # 必须远小于编排系统的探针超时（k8s 默认 1s/次、失败 3 次摘流量），
    # 否则"探针在等 MySQL"会被当成"探针挂了"，把所有实例一起摘掉。
    HEALTH_CHECK_TIMEOUT_SECONDS: float = 2.0

    # LLM（LangChain，OpenAI 兼容网关）
    # 密钥为空 = 功能关闭，服务照常启动，/api/v1/chat/* 返回 503
    LLM_API_KEY: str = ""
    LLM_BASE_URL: str = ""
    LLM_MODEL: str = "deepseek-flash"
    LLM_TIMEOUT_SECONDS: float = 60.0
    LLM_MAX_RETRIES: int = 2
    LLM_TEMPERATURE: float = 0.7
    # 单次回答的输出上限。**不设等于把账单的控制权交给模型**：
    # 一个跑飞的长回答能吃掉几万 token，而用户往往看不到一半就离开了。
    # 0 = 不限制（不推荐，仅在网关自身有硬上限时使用）。
    LLM_MAX_TOKENS: int = 2048

    # 每轮对话给模型喂多少条历史消息（从数据库按会话加载）。
    # 这是**兜底**，真正决定成本的是下面那个 token 预算：
    # 单条消息上限 8000 字符，20 条最坏就是 4 万 token。
    CHAT_CONTEXT_MESSAGES: int = 20

    # 历史上下文的总 token 预算（不含本轮提问）。超出就从最旧的开始丢。
    # 预算的作用是让"最坏情况"可计算：无论用户怎么聊，喂进去的历史
    # 都不会超过这个量，成本上限因此是已知的。
    CHAT_CONTEXT_MAX_TOKENS: int = 3000

    # ---- 对话成本闸门 ----
    # 只校验 JWT 是不够的：一个账号可以无限并发、无限长输出。
    # LLM 服务最典型的被打爆方式不是 DDoS，是账单。
    # 每用户每分钟最多发起几轮对话（含流式与非流式）
    CHAT_RATE_LIMIT_PER_MINUTE: int = 10
    # 每用户同时在途的生成数。超过就 429，而不是排队 ——
    # 排队会让用户以为"卡住了"，而他其实只是开了太多标签页。
    CHAT_MAX_CONCURRENT_STREAMS: int = 2
    # 单条流最长占用一个并发名额多久（秒）。进程崩溃时名额靠它自然释放，
    # 否则每崩一次就永久少一个名额，"重启才能恢复"。
    CHAT_INFLIGHT_TTL_SECONDS: int = 900
    # 每用户每日 token 预算（输入 + 输出）。0 = 不限制。
    # 请求前检查、完成后累加：这是"防止跑飞"的护栏，不是精确计费 ——
    # 精确计费必须走数据库用量表，Redis 计数会在重启/过期时丢。
    CHAT_DAILY_TOKEN_BUDGET: int = 200_000

    # ---- 提示词 ----
    # 正文是内容、不是代码，所以放在仓库根的 prompts/ 而不是 app/ 包里：
    # 改一句话不该触发重新打包，运维/产品也不该进源码目录才能改。
    # 相对路径按仓库根解析；容器里挂载到别处时填绝对路径（如 /app/prompts）。
    PROMPTS_DIR: str = "prompts"
    # 配置里只存"用哪个 id"，这样切提示词变体（灰度/租户）只改环境变量。
    PROMPT_CHAT_SYSTEM_ID: str = PromptKey.CHAT_SYSTEM.value

    # 雪花 ID 机器号（0-1023）。单实例填 0 即可；
    # 多实例部署时每台必须不同，否则会生成完全相同的 ID。
    # 纪元写死在 app/core/snowflake.py，不开放配置 —— 改纪元会让新旧 ID 撞车。
    SNOWFLAKE_WORKER_ID: int = 0

    # ---- 数据库（MySQL 8.4，见 deploy/）----
    DB_HOST: str = "127.0.0.1"
    DB_PORT: int = 3306
    DB_USER: str = "app"
    DB_PASSWORD: str = ""
    DB_NAME: str = "agent_chat"
    DB_ECHO: bool = False

    # 迁移专用账号（DDL 权限）。运行时账号 DB_USER 只给 DML，
    # 这样即使某天出现注入、拿到了 SQL 执行权，也删不掉表、改不了结构。
    # 本地开发可以不配，不配时迁移沿用 DB_USER。
    DB_MIGRATION_USER: str = "migrator"
    DB_MIGRATION_PASSWORD: str = ""

    # 连接池：单实例并发上限 ≈ POOL_SIZE + MAX_OVERFLOW
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    # 小于 MySQL 的 wait_timeout（默认 8 小时），主动换掉可能被服务端关闭的连接
    DB_POOL_RECYCLE_SECONDS: int = 3600

    # ---- Redis（验证码 / 限流；见 deploy/）----
    REDIS_HOST: str = "127.0.0.1"
    REDIS_PORT: int = 6379
    REDIS_PASSWORD: str = ""
    REDIS_DB: int = 0
    REDIS_SOCKET_TIMEOUT_SECONDS: float = 3.0

    # ---- 鉴权 ----
    # 密钥为空 = 鉴权功能关闭：/api/v1/auth/* 返回 503，其余接口不受影响。
    # 生产必须显式配置，绝不能给默认值 —— 默认密钥等于没有密钥。
    # 生成方式：python -c "import secrets; print(secrets.token_urlsafe(48))"
    JWT_SECRET: str = ""
    JWT_ALGORITHM: str = "HS256"
    # access 短、refresh 长。access 不落库，泄露后最多被用 15 分钟；
    # 想立刻踢人只能靠 refresh 撤销，所以 access 不能设成几天。
    ACCESS_TOKEN_TTL_SECONDS: int = 15 * 60
    REFRESH_TOKEN_TTL_SECONDS: int = 30 * 24 * 3600

    # refresh token 复用检测的宽限期（秒）。
    # 轮换后旧 token 再次出现，只有两种可能：并发/重试，或者 token 被盗用。
    # 宽限期内视为前者（不撤销、不告警），超过则按盗用处理：
    # 撤销整条链路并告警。窗口越短越安全，但也越容易误伤网络重试。
    REFRESH_REUSE_GRACE_SECONDS: int = 30

    # 登录失败限流。必须在验密之前拦：argon2id 单次约 50ms CPU，
    # 没有计数时登录接口就是个免认证的 CPU 放大器。
    # 按账号防定点爆破，按 IP 防一台机器换账号刷。
    LOGIN_MAX_ATTEMPTS_PER_EMAIL: int = 5
    LOGIN_MAX_ATTEMPTS_PER_IP: int = 20
    LOGIN_FAILURE_WINDOW_SECONDS: int = 15 * 60

    # ---- 邮件（QQ SMTP）----
    # 465 走隐式 SSL，587 走 STARTTLS，由端口自动判定，不再单独开关。
    SMTP_HOST: str = "smtp.qq.com"
    SMTP_PORT: int = 465
    SMTP_USER: str = ""  # 完整发件地址，如 123456@qq.com
    SMTP_PASSWORD: str = ""  # QQ 邮箱的"授权码"，不是登录密码
    SMTP_FROM_NAME: str = "AI 助手"
    SMTP_TIMEOUT_SECONDS: float = 10.0

    # ---- 人机验证（VAPTCHA V4）----
    # VID / VKey 都来自 VAPTCHA 控制台的验证单元。**VKey 只能放服务端**，
    # 写进前端等于把验签能力公开（拿到 VKey 就能伪造验签通过）。
    # 两者任一为空 = 人机验证关闭，发码照常、票据不校验 ——
    # 与 LLM_API_KEY / JWT_SECRET 同一套开关语义，本地开发不必人人申请一套。
    #
    # 注意：V2 时代的 api.vaptcha.com / 0.vaptcha.com 已 NXDOMAIN，
    # V4 的验签端点是 v41.vaptcha.com/api/verify（JSON 提交，非 form）。
    VAPTCHA_VID: str = ""
    VAPTCHA_VKEY: str = ""
    # remote：调官方接口（默认）。IP 漂移判定与票据复用检测由官方兜底，集成最省心。
    # local ：本地 HMAC-SHA256 校验（少一次外网往返），用 Redis 保证票据一次性。
    VAPTCHA_VERIFY_MODE: str = "remote"
    VAPTCHA_VERIFY_URL: str = "https://v41.vaptcha.com/api/verify"
    VAPTCHA_TIMEOUT_SECONDS: float = 5.0
    # 本地校验的票据有效期（秒）。token 里带签发时间戳，超过这个窗口视为过期。
    # 官方文档：token 本身的校验窗口是 30 秒，并**建议本地验签取 3 秒**
    # （"本地验签建议使用 3 秒有效期，并对已通过的 token 做防复用"）。
    # 窗口就是重放空间，所以默认跟官方建议一致，不要图省事设成分钟级。
    VAPTCHA_LOCAL_TTL_SECONDS: int = 3
    # 官方接口返回 data.code=1（"鉴权不通过但 token 存在"）时是否放行。
    # 默认严格拒绝；政企老宽带的 IP 会随机漂移，需要兼容时再打开。
    VAPTCHA_ALLOW_CODE_1: bool = False

    # ---- 邮箱验证码 ----
    # TTL 与重发冷却分开：TTL 是"这个码多久失效"，冷却是"多久能再要一个"。
    VERIFICATION_CODE_TTL_SECONDS: int = 5 * 60
    VERIFICATION_CODE_RESEND_SECONDS: int = 60
    # 同一验证码允许试错次数，超了直接作废 —— 否则 6 位数字可以在有效期内穷举
    VERIFICATION_CODE_MAX_ATTEMPTS: int = 5
    # 两层配额：按邮箱防单点轰炸，按 IP 防换邮箱刷
    VERIFICATION_DAILY_LIMIT_PER_EMAIL: int = 10
    VERIFICATION_HOURLY_LIMIT_PER_IP: int = 20

    @property
    def prompts_dir(self) -> Path:
        """提示词目录的绝对路径（相对 PROMPTS_DIR 按仓库根解析）。"""
        return resolve_path(self.PROMPTS_DIR)

    @property
    def db_configured(self) -> bool:
        """没配密码视为"数据库功能关闭"，应用照常启动。"""
        return bool(self.DB_PASSWORD)

    def _dsn(self, user: str, password: str) -> str:
        """拼异步 DSN。

        账号密码必须 quote_plus：密码里出现 @ / : / # 会把 DSN 拆坏，
        报出来的错还跟密码无关（通常是一句看不懂的 "invalid port"）。
        """
        return (
            f"mysql+aiomysql://{quote_plus(user)}:{quote_plus(password)}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}?charset=utf8mb4"
        )

    @property
    def database_url(self) -> str:
        """运行时 DSN：只有 DML 权限。"""
        return self._dsn(self.DB_USER, self.DB_PASSWORD)

    @property
    def migration_url(self) -> str:
        """迁移 DSN：需要 DDL 权限。

        没配迁移账号时退回运行时账号 —— 本地开发图方便，
        但那种情况下 app 账号必然带着 DDL 权限，生产不要这么干。
        """
        if not self.DB_MIGRATION_PASSWORD:
            return self.database_url
        return self._dsn(self.DB_MIGRATION_USER, self.DB_MIGRATION_PASSWORD)

    @property
    def redis_configured(self) -> bool:
        return bool(self.REDIS_PASSWORD)

    @property
    def redis_url(self) -> str:
        """redis:// 形式。密码同样要 quote_plus，理由同 database_url。"""
        auth = f":{quote_plus(self.REDIS_PASSWORD)}@" if self.REDIS_PASSWORD else ""
        return f"redis://{auth}{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"

    @property
    def jwt_configured(self) -> bool:
        return bool(self.JWT_SECRET)

    @property
    def mail_configured(self) -> bool:
        """发件账号和授权码都齐了才算可用。"""
        return bool(self.SMTP_USER and self.SMTP_PASSWORD)

    @property
    def vaptcha_configured(self) -> bool:
        """VID 和 VKey 都配了才启用人机验证，缺一个都算关闭。"""
        return bool(self.VAPTCHA_VID and self.VAPTCHA_VKEY)

    @property
    def smtp_use_ssl(self) -> bool:
        """465 是隐式 SSL（连上就加密），587 是 STARTTLS（先明文再升级）。"""
        return self.SMTP_PORT == 465


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """配置工厂：全局单例，只解析一次。"""
    return Settings()


settings = get_settings()
