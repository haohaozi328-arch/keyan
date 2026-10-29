"""会话与消息的对外契约。

两个刻意的取舍：

1. **雪花 ID 一律是字符串**。19 位整数超过 JS 的 `Number.MAX_SAFE_INTEGER`，
   给数字前端 `JSON.parse` 后末几位被静默改写，拿到的 id 查不到任何东西。
2. **评价用 `up` / `down` / `null`，不用 0 / 1 / 2**。数字是数据库的存储形态，
   不是接口形态 —— 前端 `if (feedback === 1)` 这种写法没人记得住 1 是赞还是踩，
   而且哪天后端加了第三种评价，数字含义会悄悄变。
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

# 只有"赞"和"踩"两种表态；取消评价用 null，不新增第三个字面量
FeedbackRating = Literal["up", "down"]


class ConversationSummary(BaseModel):
    """会话列表项。不含消息正文 —— 列表页不需要，带上就是白传大字段。"""

    id: str = Field(description="会话 ID（雪花 ID 字符串）")
    title: str = Field(description="标题，首轮提问自动生成，可改名")
    message_count: int = Field(description="消息条数（含用户与助手）")
    model: str = Field(description="会话使用的模型")
    last_message_at: datetime = Field(description="最后一条消息时间，列表按它倒序")
    created_at: datetime


class ConversationListResponse(BaseModel):
    items: list[ConversationSummary]
    next_cursor: str | None = Field(
        None,
        description="下一页游标，原样回传即可；为 null 表示没有更多",
        examples=["MjAyNi0xMC0yOFQxMjoyMDowMHwzNzM4MTQzNjg4NDE3NjA3Njg="],
    )


class MessageItem(BaseModel):
    id: str = Field(description="消息 ID（雪花 ID 字符串），评价接口要用的就是它")
    seq: int = Field(description="会话内序号，从 1 开始，排序以它为准")
    role: str = Field(description="user / assistant / system / tool", examples=["assistant"])
    content: str = Field(description="正文；流式中断时是已经生成的部分")
    status: str = Field(
        description="streaming / complete / interrupted / failed",
        examples=["complete"],
    )
    model: str | None = Field(None, description="本消息使用的模型")
    parent_message_id: str | None = Field(
        None,
        description=(
            "重新生成的来源消息 ID。非空表示这条回答替代了哪一条 —— "
            "前端据此把旧回答标成「已被重新生成」，而不是当成两轮对话。"
        ),
    )
    feedback: FeedbackRating | None = Field(
        None, description="当前用户的评价：up / down；null 表示没评价过"
    )
    feedback_reason: str | None = Field(None, description="点踩原因")
    feedback_at: datetime | None = Field(None, description="评价时间")
    created_at: datetime


class MessageListResponse(BaseModel):
    items: list[MessageItem] = Field(description="按 seq 升序，前端可直接从上往下渲染")
    next_before_seq: int | None = Field(
        None,
        description="继续往前翻时把这个值传给 before_seq；为 null 表示已到最早",
    )


class FeedbackRequest(BaseModel):
    rating: FeedbackRating | None = Field(
        None,
        description="up=赞，down=踩，null=取消之前的评价",
        examples=["up"],
    )
    reason: str | None = Field(
        None,
        max_length=255,
        description="点踩原因，可选。点赞时传了也会存下来（用户想夸也可以写）",
    )


class FeedbackResponse(BaseModel):
    message_id: str
    rating: FeedbackRating | None
    feedback_reason: str | None
    feedback_at: datetime | None


class RenameConversationRequest(BaseModel):
    title: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="新标题，1–100 字；后端不会自动截断，超长直接 422",
        examples=["LangChain 入门"],
    )
