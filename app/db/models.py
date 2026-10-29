"""ORM 模型：与 alembic/versions 里的迁移一一对应。

表结构由 Alembic 管理，不由 `Base.metadata.create_all()` 创建 ——
生产改结构要走迁移，不能靠应用启动时偷偷建表。
模型在这里的作用是：查询、写入、以及给 autogenerate 提供比对基准。

因此**两边的约束名必须逐字一致**（uk_users_username、fk_msg_conv ...），
否则 Alembic 会认为约束被改过，反复生成 drop/create。
tests/test_db_contract.py 会拿真实库的 information_schema 做交叉验证。
"""

from datetime import datetime

from sqlalchemy import (
    CHAR,
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, utcnow
from app.db.enums import (
    ConversationStatus,
    MessageFeedback,
    MessageRole,
    MessageStatus,
    UserStatus,
)
from app.db.types import (
    BinaryIP,
    IntEnumValue,
    MediumText,
    SnowflakeID,
    StrEnumValue,
    Timestamp,
    UnsignedBigInt,
    UnsignedInt,
)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("username", name="uk_users_username"),
        UniqueConstraint("email", name="uk_users_email"),
        Index("idx_users_status", "status", "deleted_at"),
        CheckConstraint("status IN (0, 1, 2)", name="ck_users_status"),
        {"comment": "账号"},
    )

    id: Mapped[int] = mapped_column(
        SnowflakeID, primary_key=True, autoincrement=False, comment="雪花 ID"
    )
    # 邮箱注册不产生 username，所以允许为空。
    # MySQL 的唯一索引允许多个 NULL，不会因为"都是 NULL"而互相冲突 ——
    # 换成空字符串就不行了：第二个空串用户会直接撞 uk_users_username。
    username: Mapped[str | None] = mapped_column(
        String(64), default=None, comment="登录账号；邮箱注册的用户为 NULL"
    )
    email: Mapped[str | None] = mapped_column(String(255), default=None, comment="可选登录方式")
    # 255 而不是 60：bcrypt 是 60 字符，argon2id 约 100。
    # 留宽度是为了将来换算法不用改表，登录时按 $2b$ / $argon2id$ 前缀分流。
    password_hash: Mapped[str] = mapped_column(String(255), comment="bcrypt/argon2id 密文")
    nickname: Mapped[str] = mapped_column(
        String(64), default="", server_default=text("''"), comment="展示名"
    )
    status: Mapped[UserStatus] = mapped_column(
        IntEnumValue(UserStatus), default=UserStatus.ACTIVE, server_default=text("1")
    )
    last_login_at: Mapped[datetime | None] = mapped_column(Timestamp, default=None)
    # VARBINARY(16)：INET6_ATON 写入，同时兼容 IPv4 与 IPv6。
    # 存字符串的话 IPv6 最长 45 字符，索引又宽又慢。
    last_login_ip: Mapped[bytes | None] = mapped_column(BinaryIP, default=None)
    created_at: Mapped[datetime] = mapped_column(
        Timestamp, default=utcnow, server_default=text("CURRENT_TIMESTAMP(3)")
    )
    updated_at: Mapped[datetime] = mapped_column(
        Timestamp,
        default=utcnow,
        onupdate=utcnow,
        server_default=text("CURRENT_TIMESTAMP(3)"),
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        Timestamp, default=None, comment="软删除标记，NULL 表示有效"
    )

    conversations: Mapped[list["Conversation"]] = relationship(
        back_populates="user", lazy="raise", passive_deletes=True
    )
    auth_sessions: Mapped[list["AuthSession"]] = relationship(
        back_populates="user", lazy="raise", passive_deletes=True
    )


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        # 会话列表的主查询：
        #   WHERE user_id=? AND deleted_at IS NULL
        #   ORDER BY last_message_at DESC, id DESC
        # 四列都进索引：前两列是等值条件，后两列直接喂给排序。
        # 少了 deleted_at 就要回表过滤软删（翻 20 条可能扫几百行），
        # 少了 id 则同毫秒的会话要靠 filesort 兜底。
        Index(
            "idx_conv_user_recent",
            "user_id",
            "deleted_at",
            text("last_message_at DESC"),
            text("id DESC"),
        ),
        Index("idx_conv_status", "status", "last_message_at"),
        CheckConstraint("status IN (0, 1)", name="ck_conv_status"),
        {"comment": "对话"},
    )

    id: Mapped[int] = mapped_column(
        SnowflakeID, primary_key=True, autoincrement=False, comment="雪花 ID，即对外的对话 ID"
    )
    user_id: Mapped[int] = mapped_column(
        SnowflakeID,
        ForeignKey("users.id", name="fk_conv_user", ondelete="RESTRICT", onupdate="RESTRICT"),
        comment="归属用户",
    )
    title: Mapped[str] = mapped_column(
        String(255), default="", server_default=text("''"), comment="首轮内容摘要，异步回填"
    )
    model: Mapped[str] = mapped_column(
        String(64), default="", server_default=text("''"), comment="本会话使用的模型"
    )
    prompt_id: Mapped[str] = mapped_column(
        String(64),
        default="chat_system",
        server_default=text("'chat_system'"),
        comment="提示词 id，对齐 app/prompts",
    )
    prompt_version: Mapped[int] = mapped_column(
        UnsignedInt, default=1, server_default=text("1"), comment="提示词版本，答歪时回溯用"
    )
    # 冗余计数与累计用量：列表页显示"共 N 条"、账单按会话汇总时，
    # 不必回表 COUNT/SUM 全表，读路径从 O(消息数) 降到 O(1)。
    message_count: Mapped[int] = mapped_column(
        UnsignedInt, default=0, server_default=text("0"), comment="消息条数"
    )
    total_prompt_tokens: Mapped[int] = mapped_column(
        UnsignedBigInt, default=0, server_default=text("0"), comment="累计输入令牌"
    )
    total_completion_tokens: Mapped[int] = mapped_column(
        UnsignedBigInt, default=0, server_default=text("0"), comment="累计输出令牌"
    )
    status: Mapped[ConversationStatus] = mapped_column(
        IntEnumValue(ConversationStatus),
        default=ConversationStatus.ACTIVE,
        server_default=text("0"),
    )
    last_message_at: Mapped[datetime] = mapped_column(
        Timestamp,
        default=utcnow,
        server_default=text("CURRENT_TIMESTAMP(3)"),
        comment="列表排序键",
    )
    created_at: Mapped[datetime] = mapped_column(
        Timestamp, default=utcnow, server_default=text("CURRENT_TIMESTAMP(3)")
    )
    updated_at: Mapped[datetime] = mapped_column(
        Timestamp,
        default=utcnow,
        onupdate=utcnow,
        server_default=text("CURRENT_TIMESTAMP(3)"),
    )
    deleted_at: Mapped[datetime | None] = mapped_column(Timestamp, default=None)

    user: Mapped["User"] = relationship(back_populates="conversations", lazy="raise")
    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation", lazy="raise", passive_deletes=True
    )


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("conversation_id", "seq", name="uk_msg_conv_seq"),
        UniqueConstraint("user_id", "client_message_id", name="uk_msg_client_idem"),
        Index("idx_msg_user_created", "user_id", "created_at"),
        Index("idx_msg_status_updated", "status", "updated_at"),
        CheckConstraint("role IN ('user', 'assistant', 'system', 'tool')", name="ck_msg_role"),
        CheckConstraint("status IN (0, 1, 2, 3)", name="ck_msg_status"),
        CheckConstraint("feedback IN (0, 1, 2)", name="ck_msg_feedback"),
        # 差评巡检：WHERE feedback=2 ORDER BY feedback_at DESC 是运营侧最常用的查询
        Index("idx_msg_feedback", "feedback", "feedback_at"),
        # 重新生成要问"这条回答有没有被替代"（相关 EXISTS 子查询），
        # 没有索引就是每轮对话扫一遍消息表
        Index("idx_msg_parent", "parent_message_id"),
        {"comment": "消息"},
    )

    id: Mapped[int] = mapped_column(
        SnowflakeID,
        primary_key=True,
        autoincrement=False,
        comment="雪花 ID，兼作 SSE Last-Event-ID",
    )
    conversation_id: Mapped[int] = mapped_column(
        SnowflakeID,
        ForeignKey(
            "conversations.id", name="fk_msg_conv", ondelete="RESTRICT", onupdate="RESTRICT"
        ),
        comment="所属对话",
    )
    # 冗余 user_id：按用户审计/隔离时免 JOIN，代价是每行多 8 字节
    user_id: Mapped[int] = mapped_column(SnowflakeID, comment="冗余用户 ID")
    # 会话内递增序号。不能靠 created_at 排序 ——
    # 同一毫秒可能插入两条，顺序不确定，翻页会跳行或重复。
    seq: Mapped[int] = mapped_column(UnsignedInt, comment="会话内递增序号，从 1 开始")
    role: Mapped[MessageRole] = mapped_column(StrEnumValue(MessageRole, 16))
    content: Mapped[str | None] = mapped_column(MediumText, default=None, comment="正文")
    status: Mapped[MessageStatus] = mapped_column(
        IntEnumValue(MessageStatus),
        default=MessageStatus.STREAMING,
        server_default=text("0"),
    )
    model: Mapped[str | None] = mapped_column(String(64), default=None)
    prompt_tokens: Mapped[int | None] = mapped_column(UnsignedInt, default=None)
    completion_tokens: Mapped[int | None] = mapped_column(UnsignedInt, default=None)
    first_token_ms: Mapped[int | None] = mapped_column(
        UnsignedInt, default=None, comment="首字延迟"
    )
    total_ms: Mapped[int | None] = mapped_column(UnsignedInt, default=None, comment="总耗时")
    finish_reason: Mapped[str | None] = mapped_column(String(32), default=None)
    # 上游报错原文可能带密钥或内部地址，落库前必须脱敏
    error_message: Mapped[str | None] = mapped_column(String(512), default=None)
    # 评价放在消息行上而不是独立表：一条消息只有一个评价，列表页要回显"赞过没"
    # 就必须回查，独立表意味着每页多一次 JOIN 或一次 IN 查询。
    # 代价是没有评价历史（改主意就覆盖），需要审计时再加 event 表。
    feedback: Mapped[MessageFeedback] = mapped_column(
        IntEnumValue(MessageFeedback),
        default=MessageFeedback.NONE,
        server_default=text("0"),
        comment="用户评价：0 未评价 / 1 赞 / 2 踩",
    )
    feedback_reason: Mapped[str | None] = mapped_column(
        String(255), default=None, comment="点踩原因，可选"
    )
    feedback_at: Mapped[datetime | None] = mapped_column(
        Timestamp, default=None, comment="评价时间，取消评价时清空"
    )
    client_message_id: Mapped[str | None] = mapped_column(
        String(64), default=None, comment="客户端幂等键"
    )
    parent_message_id: Mapped[int | None] = mapped_column(
        SnowflakeID, default=None, comment="被本条替代的回答 ID（重新生成时写入）"
    )
    created_at: Mapped[datetime] = mapped_column(
        Timestamp, default=utcnow, server_default=text("CURRENT_TIMESTAMP(3)")
    )
    updated_at: Mapped[datetime] = mapped_column(
        Timestamp,
        default=utcnow,
        onupdate=utcnow,
        server_default=text("CURRENT_TIMESTAMP(3)"),
    )

    conversation: Mapped["Conversation"] = relationship(back_populates="messages", lazy="raise")


class AuthSession(Base):
    """登录态。纯无状态 JWT 的话可以整张表删掉，代价是无法主动踢人。"""

    __tablename__ = "auth_sessions"
    __table_args__ = (
        UniqueConstraint("refresh_token_hash", name="uk_sess_token"),
        Index("idx_sess_user", "user_id", "expires_at"),
        # 复用检测要按 family 批量撤销，没有索引就是全表扫
        Index("idx_sess_family", "family_id"),
        {"comment": "登录态"},
    )

    id: Mapped[int] = mapped_column(SnowflakeID, primary_key=True, autoincrement=False)
    user_id: Mapped[int] = mapped_column(
        SnowflakeID,
        ForeignKey("users.id", name="fk_sess_user", ondelete="CASCADE", onupdate="RESTRICT"),
    )
    # 同一条登录链路（登录 → 第一次轮换 → 第二次轮换 …）共用一个 family。
    # 首次签发时 family = 自己的 id；之后每代继承上一代的值。
    # 为什么要它：refresh token 是轮换的，一个**已撤销**的 token 再次出现，
    # 要么是并发重试，要么是被人复制走了。后者意味着整条链路都已泄露，
    # 必须一次性全撤 —— 没有 family 就只能"撤销该用户全部会话"，
    # 把用户其他设备也一起踢掉。
    family_id: Mapped[int] = mapped_column(SnowflakeID, comment="登录链路 ID")
    # 只存 sha256，绝不存原文：数据库被读走也无法直接冒用。
    # CHAR 而不是 VARCHAR：sha256 十六进制恒为 64 字符，定长类型不存长度前缀。
    refresh_token_hash: Mapped[str] = mapped_column(CHAR(64))
    user_agent: Mapped[str] = mapped_column(String(255), default="", server_default=text("''"))
    ip: Mapped[bytes | None] = mapped_column(BinaryIP, default=None)
    expires_at: Mapped[datetime] = mapped_column(Timestamp)
    revoked_at: Mapped[datetime | None] = mapped_column(
        Timestamp, default=None, comment="非空即已失效"
    )
    created_at: Mapped[datetime] = mapped_column(
        Timestamp, default=utcnow, server_default=text("CURRENT_TIMESTAMP(3)")
    )

    user: Mapped["User"] = relationship(back_populates="auth_sessions", lazy="raise")
