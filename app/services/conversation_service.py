"""对话与会话的业务编排：上下文加载、落库、评价。

为什么这个服务收的是 session factory，而不是像 AuthService 那样收 AsyncSession
----------------------------------------------------------------------------
其他服务是"一个请求一个 session"，因为请求内的读写是连续的、短暂的。
对话不一样：**SSE 流的生命周期可能有几分钟**。整条流共用一个 session
就等于把一条数据库连接占住几分钟 —— 连接池默认 5 + 10，同时开几条流就满了，
其他人连长会话列表都刷不出来。

所以这里把写库拆成两次短事务，中间不持有任何连接：
    流开始前 → 写用户消息 + 助手占位（STREAMING），提交，释放连接
    流结束后 → 回填助手内容 + 用量 + 会话统计，提交

代价是"流进行中"的消息在库里就是 STREAMING 状态，别人查历史会看到半截内容。
这是真实状态，不是脏数据：进程若在流中途挂掉，那条记录会一直是 STREAMING，
由对账任务捞出来标成 INTERRUPTED（这也是 MessageStatus 里保留该值的理由）。

只服务登录用户
--------------
`prepare_turn` 要求一个真实的 user：没有 user_id 就没有归属，
"匿名会话"只会变成一堆无法清理的孤儿数据。鉴权在路由层（`CurrentUser`）
就拦掉了，这里不做 user is None 的兼容分支 —— 留一个"其实永远不会走到"
的匿名路径，只会让人误以为还有不落库的用法。
"""

import base64
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.errors import InvalidParamError, NotFoundError
from app.db.enums import MessageFeedback, MessageRole, MessageStatus
from app.db.models import Conversation, Message, User
from app.repositories import ConversationRepository, MessageRepository
from app.schemas.chat import ChatMessage
from app.services.context_budget import estimate_tokens, fit_history

logger = logging.getLogger(__name__)

# 列表标题的落库长度。列表页再长也显示不下，存整段提问只是浪费行宽；
# 用户想看的完整内容在消息里。
_TITLE_MAX = 30


@dataclass(frozen=True)
class TurnContext:
    """一轮对话在**调用模型之前**就确定下来的东西。

    流式响应开始后就没机会再纠正了：这时候已经在往客户端写字节，
    状态码也发出去了。所以归属校验、上下文加载、消息占位必须全部前移到这里。
    """

    conversation_id: int | None = None
    user_message_id: int | None = None
    assistant_message_id: int | None = None
    history: list[ChatMessage] = field(default_factory=list)
    title: str = ""

    @property
    def persisted(self) -> bool:
        """这一轮是否落了库。

        登录是硬性要求，理论上恒为 true；收尾方法仍据它做防御 ——
        将来若允许"临时会话"之类的例外，收尾逻辑不必再改一遍。
        """
        return self.conversation_id is not None


class ConversationService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings

    # ------------------------------------------------------------------ 准备

    async def prepare_turn(
        self,
        *,
        user: User,
        conversation_id: int | None,
        message: str,
        model: str,
        prompt_id: str,
        prompt_version: int,
    ) -> TurnContext:
        """建/取会话、加载历史、写两条消息占位。

        历史**只从数据库读**：客户端没有任何途径指定喂给模型的上下文。
        让浏览器决定历史，等于把提示注入的入口开在客户端 ——
        伪造几条 assistant 消息就能把模型带跑偏。
        """
        async with self._session_factory() as session:
            conversations = ConversationRepository(session)
            messages = MessageRepository(session)

            if conversation_id is None:
                conv = conversations.add(
                    user_id=user.id,
                    title=_title_from(message),
                    model=model,
                    prompt_id=prompt_id,
                    prompt_version=prompt_version,
                )
                # flush 只为把 INSERT 发出去、拿回主键；提交在结尾统一做
                await session.flush()
                history: list[ChatMessage] = []
            else:
                # for_update：同一会话的并发提问要串行分配 seq，
                # 否则两条消息算出同一个 seq，撞 uk_msg_conv_seq 直接 500
                conv = await conversations.get(
                    user_id=user.id, conversation_id=conversation_id, for_update=True
                )
                if conv is None:
                    # 不存在与不属于当前用户返回同一个错误：区分了就能被用来
                    # 探测"某个 ID 是否存在"，而对话 ID 是连续可猜的雪花值
                    raise NotFoundError("会话不存在")
                rows = await messages.recent_for_context(
                    conversation_id=conv.id, limit=self._settings.CHAT_CONTEXT_MESSAGES
                )
                history = self._fit(_to_chat_messages(rows), question=message)

            seq = await messages.next_seq(conv.id)
            user_message = messages.add(
                conversation_id=conv.id,
                user_id=user.id,
                seq=seq,
                role=MessageRole.USER,
                content=message,
                status=MessageStatus.COMPLETE,
            )
            assistant = messages.add(
                conversation_id=conv.id,
                user_id=user.id,
                seq=seq + 1,
                role=MessageRole.ASSISTANT,
                content="",
                status=MessageStatus.STREAMING,
                model=model,
            )
            # 两条消息一起计入 message_count：列表页显示"共 N 条"时，
            # 用户刚发出去的那条也应该算进去，而不是等回答完才 +1
            conversations.touch(conv, new_messages=2)

            context = TurnContext(
                conversation_id=conv.id,
                user_message_id=user_message.id,
                assistant_message_id=assistant.id,
                history=history,
                title=conv.title,
            )
            await session.commit()
            return context

    def _fit(self, history: list[ChatMessage], *, question: str) -> list[ChatMessage]:
        """按 token 预算裁历史，本轮提问先占掉自己那份。

        条数上限（`CHAT_CONTEXT_MESSAGES`）只是兜底：单条消息就能到 8000 字符，
        20 条最坏是 4 万 token，既贵又可能超窗口。真正决定成本上限的是预算。
        """
        budget = max(0, self._settings.CHAT_CONTEXT_MAX_TOKENS - estimate_tokens(question))
        kept, dropped = fit_history(history, budget_tokens=budget)
        if dropped:
            logger.info("上下文超预算，丢弃最旧的 %d 条（可用预算 %d token）", dropped, budget)
        return kept

    async def prepare_regeneration(
        self,
        *,
        user: User,
        message_id: int,
        model: str,
        prompt_id: str,
        prompt_version: int,
    ) -> tuple[TurnContext, str]:
        """重新生成：**不重复写用户消息**，只在末尾追加一条新的助手消息。

        为什么保留旧回答而不是覆盖：
            用户点"重新生成"是想比较，不是想销毁证据；旧回答还是评价与 badcase
            的载体。新消息用 `parent_message_id` 指向被替代的那条，
            上下文加载时据此把旧回答排除 —— 同一个问题只留一份答案进 prompt，
            否则模型会看到"问一次答两次"，越聊越偏。

        为什么归档查找上一条用户消息而不是 `seq - 1`：
            重新生成之后，被替代的回答还留在原 seq 上，"上一条"就成了另一份回答。
        """
        async with self._session_factory() as session:
            messages = MessageRepository(session)
            conversations = ConversationRepository(session)

            target = await messages.get_owned(user_id=user.id, message_id=message_id)
            if target is None or target.role is not MessageRole.ASSISTANT:
                # 只允许重答助手消息：用户消息没有对应的生成过程
                raise NotFoundError("消息不存在")

            # for_update：和普通一轮一样，seq 必须在锁内分配
            conv = await conversations.get(
                user_id=user.id, conversation_id=target.conversation_id, for_update=True
            )
            if conv is None:
                raise NotFoundError("会话不存在")

            question = await messages.last_user_before(conversation_id=conv.id, seq=target.seq)
            if question is None or not question.content:
                raise InvalidParamError("找不到对应的提问，无法重新生成")

            rows = await messages.context_before(
                conversation_id=conv.id,
                seq=question.seq,
                limit=self._settings.CHAT_CONTEXT_MESSAGES,
            )
            history = self._fit(_to_chat_messages(rows), question=question.content)

            seq = await messages.next_seq(conv.id)
            assistant = messages.add(
                conversation_id=conv.id,
                user_id=user.id,
                seq=seq,
                role=MessageRole.ASSISTANT,
                content="",
                status=MessageStatus.STREAMING,
                model=model,
                parent_message_id=target.id,
            )
            # 只 +1：问题没有重写，用户看到的还是同一句话
            conversations.touch(conv, new_messages=1)

            context = TurnContext(
                conversation_id=conv.id,
                assistant_message_id=assistant.id,
                history=history,
                title=conv.title,
            )
            await session.commit()
            return context, question.content

    # ------------------------------------------------------------------ 收尾

    async def complete_turn(
        self,
        context: TurnContext,
        *,
        answer: str,
        status: MessageStatus = MessageStatus.COMPLETE,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        finish_reason: str | None = None,
    ) -> None:
        """回填助手消息。

        `status` 允许是 INTERRUPTED：客户端中途断流时，已经生成的那部分
        仍然要存下来 —— 用户下次打开会话看到的应该是"聊到哪儿了"，
        而不是一条空白消息。
        """
        if not context.persisted or context.assistant_message_id is None:
            return

        async with self._session_factory() as session:
            message = await session.get(Message, context.assistant_message_id)
            if message is None:  # 会话被并发的删除请求清掉了
                return
            message.content = answer
            message.status = status
            message.prompt_tokens = prompt_tokens
            message.completion_tokens = completion_tokens
            message.finish_reason = finish_reason

            conversation = await session.get(Conversation, context.conversation_id)
            if conversation is not None:
                ConversationRepository(session).touch(
                    conversation,
                    prompt_tokens=prompt_tokens or 0,
                    completion_tokens=completion_tokens or 0,
                )
            await session.commit()

    async def fail_turn(self, context: TurnContext, *, reason: str, answer: str = "") -> None:
        """上游失败：助手消息标 FAILED。

        `answer` 是失败前已经吐出来的片段。存下来而不是丢掉：
        "说到一半断了"和"一个字都没说"对用户是两种完全不同的情况。

        只存异常类名（调用方传入的就是类名或中性摘要），不存原文 ——
        上游报错里可能带密钥、内部地址或请求体，落库就等于把泄露面扩大了。
        """
        if not context.persisted or context.assistant_message_id is None:
            return

        async with self._session_factory() as session:
            message = await session.get(Message, context.assistant_message_id)
            if message is None:
                return
            message.content = answer or None
            message.status = MessageStatus.FAILED
            message.error_message = reason[:512]
            await session.commit()

    # ------------------------------------------------------------------ 读取

    async def list_conversations(
        self,
        *,
        user_id: int,
        limit: int,
        cursor: str | None = None,
    ) -> tuple[list[Conversation], str | None]:
        """会话列表，按最近活跃倒序。

        多取一条来判断"还有没有下一页"，比再发一次 `COUNT(*)` 便宜得多，
        也不会因为并发新增而出现"算的时候 20 条、翻页时变 21 条"的错位。
        """
        before = _decode_cursor(cursor) if cursor else None
        async with self._session_factory() as session:
            rows = await ConversationRepository(session).list_page(
                user_id=user_id, limit=limit + 1, before=before
            )
        has_more = len(rows) > limit
        items = rows[:limit]
        next_cursor = _encode_cursor(items[-1]) if has_more and items else None
        return items, next_cursor

    async def get_conversation(self, *, user_id: int, conversation_id: int) -> Conversation:
        async with self._session_factory() as session:
            conversation = await ConversationRepository(session).get(
                user_id=user_id, conversation_id=conversation_id
            )
        if conversation is None:
            raise NotFoundError("会话不存在")
        return conversation

    async def resume_context(self, *, user_id: int, message_id: int) -> TurnContext:
        """续传用：确认这条助手消息属于当前用户，还原这一轮的身份。

        这里**不看 status**：正在 STREAMING 的当然要能续，已经 COMPLETE 的
        也可能需要重放（客户端把最后几帧丢了，或者刷新了页面）。
        缓冲还在就照发，缓冲没了由调用方判成 410 并让客户端去拉消息全文。

        只接受助手消息：用户那条消息没有流式响应，拿它来续没有意义，
        放过去只会变成"续了一个永远不会有下文的流"。
        """
        async with self._session_factory() as session:
            message = await MessageRepository(session).get_owned(
                user_id=user_id, message_id=message_id
            )
            if message is None or message.role is not MessageRole.ASSISTANT:
                # 不存在与不属于当前用户给同一个 404：区分了就能拿来探测消息是否存在
                raise NotFoundError("消息不存在")
            return TurnContext(
                conversation_id=message.conversation_id,
                assistant_message_id=message.id,
            )

    async def list_messages(
        self,
        *,
        user_id: int,
        conversation_id: int,
        limit: int,
        before_seq: int | None = None,
    ) -> tuple[list[Message], int | None]:
        # 先确认会话归属，再读消息：少这一句就是"知道 ID 就能读别人对话"
        await self.get_conversation(user_id=user_id, conversation_id=conversation_id)
        async with self._session_factory() as session:
            rows = await MessageRepository(session).list_page(
                conversation_id=conversation_id, limit=limit + 1, before_seq=before_seq
            )
        has_more = len(rows) > limit
        items = rows[:limit]
        next_before_seq = items[0].seq if has_more and items else None
        return items, next_before_seq

    # ------------------------------------------------------------------ 修改

    async def rename(self, *, user_id: int, conversation_id: int, title: str) -> Conversation:
        async with self._session_factory() as session:
            repo = ConversationRepository(session)
            conversation = await repo.get(user_id=user_id, conversation_id=conversation_id)
            if conversation is None:
                raise NotFoundError("会话不存在")
            repo.rename(conversation, title)
            await session.commit()
            return conversation

    async def delete(self, *, user_id: int, conversation_id: int) -> None:
        async with self._session_factory() as session:
            repo = ConversationRepository(session)
            conversation = await repo.get(user_id=user_id, conversation_id=conversation_id)
            if conversation is None:
                # 幂等：已经删掉的再删一次不算错，前端重复点不会弹错误
                return
            repo.soft_delete(conversation)
            await session.commit()

    async def set_feedback(
        self,
        *,
        user_id: int,
        message_id: int,
        rating: str | None,
        reason: str | None = None,
    ) -> Message:
        """写评价。`rating=None` 表示取消之前的评价。

        只允许评价 assistant 消息：给用户自己的提问点赞/点踩没有语义，
        放过去只会让统计里混进一堆无意义的样本。
        """
        async with self._session_factory() as session:
            repo = MessageRepository(session)
            message = await repo.get_owned(user_id=user_id, message_id=message_id)
            if message is None:
                raise NotFoundError("消息不存在")
            if message.role is not MessageRole.ASSISTANT:
                raise InvalidParamError("只能评价助手回复")
            repo.set_feedback(message, rating=_to_feedback(rating), reason=reason)
            await session.commit()
            return message


def _to_feedback(rating: str | None) -> MessageFeedback:
    if rating is None:
        return MessageFeedback.NONE
    mapping = {"up": MessageFeedback.UP, "down": MessageFeedback.DOWN}
    # 字面量已由 Pydantic 的 Literal 拦过，这里只是防御：多一个漏网的
    # 非法值进来，宁可 400 也不要静默写成一个不存在的枚举
    if rating not in mapping:
        raise InvalidParamError(f"不支持的评价值：{rating!r}")
    return mapping[rating]


def _to_chat_messages(rows: Sequence[Message]) -> list[ChatMessage]:
    """库里的消息 → 链入参。

    过滤空正文：ChatMessage 要求 content 至少 1 个字符，
    而失败/中断的消息 content 可能是空串 —— 直接构造会抛校验错误，
    把一次正常的对话请求变成 500。
    """
    return [
        ChatMessage(role=message.role.value, content=message.content)
        for message in rows
        if message.content
    ]


def _title_from(text: str) -> str:
    """首轮提问 → 会话标题。

    换行压成空格：多行提问直接存进标题，列表里会撑成一大块。
    """
    flat = " ".join(text.split())
    return flat[:_TITLE_MAX]


def _encode_cursor(conversation: Conversation) -> str:
    """游标 = base64("<last_message_at>|<id>")。

    不直接用 id 当游标：列表按 last_message_at 排序，而 id 的顺序
    和活跃顺序无关 —— 老会话被续聊后会跳到最前面，但它 id 很小，
    用 id 当游标就会把它跳过。
    """
    raw = f"{conversation.last_message_at.isoformat()}|{conversation.id}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, int]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode()).decode()
        timestamp, _, conversation_id = raw.rpartition("|")
        return datetime.fromisoformat(timestamp), int(conversation_id)
    except (ValueError, UnicodeDecodeError) as exc:
        # 游标是客户端原样回传的，被改坏是正常情况，不能当 500
        raise InvalidParamError("分页游标无效") from exc
