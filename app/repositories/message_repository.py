"""消息表的读写。

顺序由 `seq` 决定，不由 `created_at`：同一毫秒可能插入多条，
时间戳排序的结果不确定，翻页会跳行或重复。`seq` 是会话内递增的整数，
配合唯一约束 uk_msg_conv_seq 兜底。
"""

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.snowflake import get_snowflake
from app.db.base import utcnow
from app.db.enums import MessageFeedback, MessageRole, MessageStatus
from app.db.models import Message


class MessageRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def next_seq(self, conversation_id: int) -> int:
        """会话内下一个序号。

        调用方必须已经锁住会话行（`ConversationRepository.get(for_update=True)`），
        否则并发插入会算出同一个 seq。这里的 SELECT 不加锁 —— 那会锁住整段
        messages 扫描范围（间隙锁），把不同会话的写入也拖下水。
        """
        stmt = select(func.coalesce(func.max(Message.seq), 0)).where(
            Message.conversation_id == conversation_id
        )
        current = await self._session.scalar(stmt)
        return int(current or 0) + 1

    def add(
        self,
        *,
        conversation_id: int,
        user_id: int,
        seq: int,
        role: MessageRole,
        content: str | None,
        status: MessageStatus,
        model: str | None = None,
        parent_message_id: int | None = None,
    ) -> Message:
        message = Message(
            id=get_snowflake().next_id(),
            conversation_id=conversation_id,
            user_id=user_id,
            seq=seq,
            role=role,
            content=content,
            status=status,
            model=model,
            parent_message_id=parent_message_id,
        )
        self._session.add(message)
        return message

    async def recent_for_context(self, *, conversation_id: int, limit: int) -> list[Message]:
        """取最近 limit 条可入上下文的消息，按 seq 升序返回。

        三个容易写错的地方：

        1. **必须倒序取再反转**。正序 `LIMIT 20` 拿到的是对话开头那 20 条，
           聊到第 50 轮时喂给模型的还是最开始的内容，越聊越接不上。
        2. **只取 COMPLETE 的**。中断/失败的回答是半句话，放进上下文
           会让模型以为话说完了，接着半截继续编；status=STREAMING 的
           可能是别的请求正在写，也不能读。
        3. **排除被"重新生成"替代过的回答**。同一个问题会有两份答案，
           两份都进上下文等于告诉模型"我问了两遍、你答了两次"，
           它会顺着这个假象继续跑偏。留最新那份就够。
        """
        return await self._context(conversation_id=conversation_id, limit=limit)

    async def context_before(self, *, conversation_id: int, seq: int, limit: int) -> list[Message]:
        """取 `seq` 之前的上下文（重新生成用：不含被重答的那个问题本身）。"""
        return await self._context(conversation_id=conversation_id, limit=limit, before_seq=seq)

    async def _context(
        self, *, conversation_id: int, limit: int, before_seq: int | None = None
    ) -> list[Message]:
        # 别名给相关子查询用：判断"这条消息有没有被别的消息当作 parent"
        superseded = aliased(Message)
        stmt = (
            select(Message)
            .where(
                Message.conversation_id == conversation_id,
                Message.status == MessageStatus.COMPLETE,
                Message.role.in_((MessageRole.USER, MessageRole.ASSISTANT)),
                ~exists().where(superseded.parent_message_id == Message.id),
            )
            .order_by(Message.seq.desc())
            .limit(limit)
        )
        if before_seq is not None:
            stmt = stmt.where(Message.seq < before_seq)
        rows = list((await self._session.scalars(stmt)).all())
        rows.reverse()
        return rows

    async def last_user_before(self, *, conversation_id: int, seq: int) -> Message | None:
        """`seq` 之前最近的一条用户消息 —— 也就是这一轮回答的问题。

        不能简单地取 `seq - 1`：重新生成会在末尾追加助手消息，
        之后"上一条"就成了另一份回答，而不是提问。
        """
        stmt = (
            select(Message)
            .where(
                Message.conversation_id == conversation_id,
                Message.role == MessageRole.USER,
                Message.seq < seq,
            )
            .order_by(Message.seq.desc())
            .limit(1)
        )
        return await self._session.scalar(stmt)

    async def list_page(
        self,
        *,
        conversation_id: int,
        limit: int,
        before_seq: int | None = None,
    ) -> list[Message]:
        """历史消息分页，按 seq 升序返回，方便前端直接从上往下渲染。

        从最新往前翻（`seq < before_seq`）：前端打开会话时先看最后几轮，
        再往上滚加载更早的。反过来就得先知道总条数，而总数会随新消息变。
        """
        stmt = select(Message).where(Message.conversation_id == conversation_id)
        if before_seq is not None:
            stmt = stmt.where(Message.seq < before_seq)
        stmt = stmt.order_by(Message.seq.desc()).limit(limit)
        rows = list((await self._session.scalars(stmt)).all())
        rows.reverse()
        return rows

    async def get_owned(self, *, user_id: int, message_id: int) -> Message | None:
        """按 (归属用户, 消息 ID) 取消息。

        冗余的 messages.user_id 在这里派上用场：判断归属不用 JOIN 会话表。
        """
        stmt = select(Message).where(
            Message.id == message_id,
            Message.user_id == user_id,
        )
        return await self._session.scalar(stmt)

    def set_feedback(
        self,
        message: Message,
        *,
        rating: MessageFeedback,
        reason: str | None,
    ) -> None:
        """写入或取消评价。

        取消（rating=NONE）时把原因和时间一起清掉，而不是只改 rating：
        留着上次的差评原因，下次巡检按 reason 过滤就会捞出"其实没评价"的脏数据。
        """
        message.feedback = rating
        if rating is MessageFeedback.NONE:
            message.feedback_reason = None
            message.feedback_at = None
            return
        message.feedback_reason = reason
        message.feedback_at = utcnow()
