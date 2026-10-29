"""会话管理：列表、详情、历史消息、改名、删除。

所有端点都要求登录（`CurrentUser`），且**每个查询都把 user_id 写进 WHERE**：
对话内容是最私密的一类数据，归属判断绝不能靠"取出来再比一下"这种
可以被复制粘贴漏掉的写法。查不到就 404 —— 不用 403，
否则"这个 ID 存在但不是你的"和"这个 ID 不存在"的差别就泄露了。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.deps import CurrentUser, get_conversation_service
from app.core.response import ApiResponse, ok
from app.db.enums import MessageFeedback, MessageStatus
from app.db.models import Conversation, Message
from app.schemas.conversation import (
    ConversationListResponse,
    ConversationSummary,
    MessageItem,
    MessageListResponse,
    RenameConversationRequest,
)
from app.services.conversation_service import ConversationService
from app.utils.ids import parse_snowflake

router = APIRouter()

# 枚举 → 对外字符串。数据库里存数字是为了索引和 CHECK，
# 但前端 `status === 1` 这种判断没人看得懂，也会在加状态时静默错位。
_STATUS_NAMES = {
    MessageStatus.STREAMING: "streaming",
    MessageStatus.COMPLETE: "complete",
    MessageStatus.INTERRUPTED: "interrupted",
    MessageStatus.FAILED: "failed",
}

_FEEDBACK_NAMES = {MessageFeedback.UP: "up", MessageFeedback.DOWN: "down"}


@router.get(
    "",
    response_model=ApiResponse[ConversationListResponse],
    summary="我的会话列表",
    description="按最近活跃倒序。游标分页：把上一页的 next_cursor 原样回传即可。",
)
async def list_conversations(
    user: CurrentUser,
    service: Annotated[ConversationService, Depends(get_conversation_service)],
    limit: Annotated[int, Query(ge=1, le=50, description="每页条数")] = 20,
    cursor: Annotated[str | None, Query(max_length=128, description="上一页的 next_cursor")] = None,
) -> ApiResponse[ConversationListResponse]:
    rows, next_cursor = await service.list_conversations(
        user_id=user.id, limit=limit, cursor=cursor
    )
    return ok(
        ConversationListResponse(
            items=[_summary(row) for row in rows],
            next_cursor=next_cursor,
        )
    )


@router.get(
    "/{conversation_id}",
    response_model=ApiResponse[ConversationSummary],
    summary="会话详情",
    description="列表页点进去时用；消息正文走 /messages，避免详情接口返回大字段。",
)
async def get_conversation(
    conversation_id: str,
    user: CurrentUser,
    service: Annotated[ConversationService, Depends(get_conversation_service)],
) -> ApiResponse[ConversationSummary]:
    row = await service.get_conversation(
        user_id=user.id, conversation_id=parse_snowflake(conversation_id, field="conversation_id")
    )
    return ok(_summary(row))


@router.get(
    "/{conversation_id}/messages",
    response_model=ApiResponse[MessageListResponse],
    summary="历史消息（分页）",
    description=(
        "按 seq 升序返回，前端可直接从上往下渲染。"
        "打开会话时取最后 N 条；往上滚时把 next_before_seq 作为 before_seq 再取一页。"
    ),
)
async def list_messages(
    conversation_id: str,
    user: CurrentUser,
    service: Annotated[ConversationService, Depends(get_conversation_service)],
    limit: Annotated[int, Query(ge=1, le=100, description="每页条数")] = 50,
    before_seq: Annotated[int | None, Query(ge=1, description="取 seq 小于该值的消息")] = None,
) -> ApiResponse[MessageListResponse]:
    rows, next_before_seq = await service.list_messages(
        user_id=user.id,
        conversation_id=parse_snowflake(conversation_id, field="conversation_id"),
        limit=limit,
        before_seq=before_seq,
    )
    return ok(
        MessageListResponse(
            items=[_message_item(row) for row in rows],
            next_before_seq=next_before_seq,
        )
    )


@router.patch(
    "/{conversation_id}",
    response_model=ApiResponse[ConversationSummary],
    summary="重命名会话",
    description="只改标题。首轮提问自动生成的标题通常太长，前端让用户改一下很常见。",
)
async def rename_conversation(
    conversation_id: str,
    payload: RenameConversationRequest,
    user: CurrentUser,
    service: Annotated[ConversationService, Depends(get_conversation_service)],
) -> ApiResponse[ConversationSummary]:
    row = await service.rename(
        user_id=user.id,
        conversation_id=parse_snowflake(conversation_id, field="conversation_id"),
        title=payload.title.strip(),
    )
    return ok(_summary(row))


@router.delete(
    "/{conversation_id}",
    response_model=ApiResponse[None],
    summary="删除会话",
    description="软删除：会话从列表消失，消息记录保留（排查模型答歪时要用）。重复删除不报错。",
)
async def delete_conversation(
    conversation_id: str,
    user: CurrentUser,
    service: Annotated[ConversationService, Depends(get_conversation_service)],
) -> ApiResponse[None]:
    await service.delete(
        user_id=user.id, conversation_id=parse_snowflake(conversation_id, field="conversation_id")
    )
    return ok(message="已删除")


def _summary(conversation: Conversation) -> ConversationSummary:
    return ConversationSummary(
        id=str(conversation.id),
        title=conversation.title,
        message_count=conversation.message_count,
        model=conversation.model,
        last_message_at=conversation.last_message_at,
        created_at=conversation.created_at,
    )


def _message_item(message: Message) -> MessageItem:
    return MessageItem(
        id=str(message.id),
        seq=message.seq,
        role=message.role.value,
        # 流式中/失败的记录 content 可能是 NULL，对外统一成空串：
        # 前端拿到 null 再去 .length 就是一个白屏级别的 TypeError
        content=message.content or "",
        status=_STATUS_NAMES.get(message.status, "complete"),
        model=message.model,
        # 雪花 ID 一律转字符串，理由同其他地方：JS 的数字精度只有 2^53
        parent_message_id=str(message.parent_message_id) if message.parent_message_id else None,
        feedback=_FEEDBACK_NAMES.get(message.feedback),
        feedback_reason=message.feedback_reason,
        feedback_at=message.feedback_at,
        created_at=message.created_at,
    )
