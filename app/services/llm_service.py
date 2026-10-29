"""LLM 服务：LangChain LCEL 链（prompt | model）的封装。

客户端是进程级单例：内部持有 httpx 连接池，每请求新建会导致连接风暴。
未配置密钥时不构建链，调用方拿到 LLMNotConfiguredError，由路由层映射 503。

提示词以运行时变量传入链（`{system_prompt}`），不在建链时固化 ——
这样改提示词文件只影响后续请求，不必重建客户端、也不会丢连接池。

链尾**不挂 StrOutputParser**：解析器会把 AIMessage 压成 str，
usage_metadata / response_metadata 一起丢掉，`ChatResponse.usage`
就只能永远为 null。文本自己取（`_chunk_text`），元信息也拿得到。
"""

import logging
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableSerializable
from langchain_openai import ChatOpenAI

from app.core.config import Settings, get_settings
from app.prompts import Prompt, PromptKey, get_prompt
from app.schemas.chat import ChatMessage, ChatRequest
from app.services.exceptions import LLMNotConfiguredError, LLMUpstreamError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TokenDelta:
    """一段增量文本。"""

    text: str


@dataclass(frozen=True)
class ChatResult:
    """一次性对话的结果：回答 + 实际模型 + 令牌用量。"""

    answer: str
    model: str
    usage: dict[str, int] | None = None


@dataclass(frozen=True)
class StreamFinished:
    """流结束时的元信息：实际模型与令牌用量。"""

    model: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


# 服务层对外的流式事件：路由层据此映射成 SSE 帧，不暴露 LangChain 的事件字典
StreamEvent = TokenDelta | StreamFinished


class LLMService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        # 非法 id 在这里就抛 ValueError，启动即失败，而不是等第一次请求
        self._prompt_key = PromptKey(settings.PROMPT_CHAT_SYSTEM_ID)
        self._chain: RunnableSerializable | None = None
        if settings.LLM_API_KEY:
            self._chain = self._build_chain(settings)
        self.configured = bool(settings.LLM_API_KEY)
        self.model = settings.LLM_MODEL
        self.base_url = settings.LLM_BASE_URL

    @property
    def prompt(self) -> Prompt:
        """每次调用现取：提示词文件改动后立即生效，无需重启。"""
        return get_prompt(self._prompt_key)

    @staticmethod
    def _build_chain(settings: Settings) -> RunnableSerializable:
        prompt = ChatPromptTemplate.from_messages(
            [
                # system 内容运行时注入，客户端无法覆盖（见 schemas/chat.py 的 role 白名单）
                ("system", "{system_prompt}"),
                ("placeholder", "{history}"),
                ("human", "{message}"),
            ]
        )
        model = ChatOpenAI(
            api_key=settings.LLM_API_KEY,
            model=settings.LLM_MODEL,
            temperature=settings.LLM_TEMPERATURE,
            timeout=settings.LLM_TIMEOUT_SECONDS,
            max_retries=settings.LLM_MAX_RETRIES,
            # 输出上限。不设的话，一次跑飞的长回答能吃掉几万 token，
            # 而用户通常看不到一半就离开了 —— 账单付了，价值没产生。
            max_tokens=settings.LLM_MAX_TOKENS or None,
            base_url=settings.LLM_BASE_URL or None,
        )
        # 不拼 StrOutputParser：见模块 docstring，解析器会丢掉用量元信息
        return prompt | model

    # ------------------------------------------------------------------ 校验

    def ensure_configured(self) -> None:
        """流式响应开始后无法再改状态码，所以配置检查必须前置。"""
        if self._chain is None:
            raise LLMNotConfiguredError()

    def _chain_inputs(
        self, payload: ChatRequest, prompt: Prompt, history: Sequence[ChatMessage]
    ) -> dict:
        return {
            "system_prompt": prompt.text,
            "history": [(msg.role, msg.content) for msg in history],
            "message": payload.message,
        }

    # ------------------------------------------------------------------ 调用

    async def achat(self, payload: ChatRequest, history: Sequence[ChatMessage] = ()) -> ChatResult:
        """一次性返回完整回答。

        必须是 async + `ainvoke`：这是一个 async 端点，事件循环上同时挂着
        数据库连接、Redis 和在途 SSE 流。同步 `invoke` 会把整个循环堵死
        （最长 60s × 重试次数），期间**全站**请求都停摆 ——
        包括别人的 SSE 心跳，被网关判超时后连接直接被掐。
        同步调用只在纯同步上下文（脚本、线程池）里才允许。
        """
        self.ensure_configured()
        chain = self._chain
        if chain is None:  # 仅为类型收窄，逻辑上不会走到
            raise LLMNotConfiguredError()

        prompt = self.prompt
        # 记录用了哪版提示词：线上答歪时，这是唯一能回溯的线索
        logger.info(
            "chat model=%s prompt=%s v=%s hash=%s",
            self.model,
            prompt.id,
            prompt.version,
            prompt.checksum,
        )
        try:
            message: Any = await chain.ainvoke(self._chain_inputs(payload, prompt, history))
        except Exception as exc:
            raise LLMUpstreamError(type(exc).__name__) from exc

        finished = _finished(self.model, message)
        return ChatResult(
            answer=_chunk_text(message),
            model=finished.model,
            usage=_usage_dict(finished),
        )

    async def astream_chat(
        self, payload: ChatRequest, history: Sequence[ChatMessage] = ()
    ) -> AsyncIterator[StreamEvent]:
        """流式产出结构化事件（基于 astream_events）。

        为什么用 astream_events 而不是 astream：
            astream 只给最终输出的文本块，拿不到中间过程与元信息。
            astream_events 能拿到链上每一跳（模型 / 解析器 / 子链）的事件，
            这是后续接工具调用、检索步骤、用量统计的唯一入口。
            代价是事件量更大（每个 token 会有多条事件），所以必须按
            event 名过滤，且只透传业务需要的部分。

        上游异常在流中途才可能出现，此时响应头已下发，只能由路由层转成 error 事件。
        """
        self.ensure_configured()
        chain = self._chain
        if chain is None:  # 仅为类型收窄
            raise LLMNotConfiguredError()

        prompt = self.prompt
        logger.info(
            "chat stream model=%s prompt=%s v=%s hash=%s",
            self.model,
            prompt.id,
            prompt.version,
            prompt.checksum,
        )
        try:
            async for event in chain.astream_events(
                self._chain_inputs(payload, prompt, history), version="v2"
            ):
                kind = event["event"]
                if kind == "on_chat_model_stream":
                    text = _chunk_text(event["data"].get("chunk"))
                    if text:
                        yield TokenDelta(text)
                elif kind == "on_chat_model_end":
                    yield _finished(self.model, event["data"].get("output"))
        except Exception as exc:
            raise LLMUpstreamError(type(exc).__name__) from exc


def _chunk_text(chunk: Any) -> str:
    """从模型块里取文本。

    content 可能是 str（纯文本）或 list（多模态分段），空块要丢掉 ——
    reasoning 类模型的推理增量也会产生 content 为空的事件。
    """
    content = getattr(chunk, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "") if isinstance(part, dict) else str(part) for part in content
        )
    return ""


def _finished(model: str, output: Any) -> StreamFinished:
    usage = getattr(output, "usage_metadata", None) or {}
    metadata = getattr(output, "response_metadata", None) or {}
    return StreamFinished(
        model=metadata.get("model_name", model),
        prompt_tokens=usage.get("input_tokens"),
        completion_tokens=usage.get("output_tokens"),
    )


def _usage_dict(finished: StreamFinished) -> dict[str, int] | None:
    """转成对外结构。网关不返回用量时给 None，而不是编两个 0 出来 ——
    "0 token" 和 "不知道多少 token" 是两件事，前者会污染账单统计。"""
    if finished.prompt_tokens is None and finished.completion_tokens is None:
        return None
    return {
        "prompt_tokens": finished.prompt_tokens or 0,
        "completion_tokens": finished.completion_tokens or 0,
    }


@lru_cache(maxsize=1)
def get_llm_service() -> LLMService:
    """LLM 服务工厂：带缓存的全局单例。"""
    return LLMService(get_settings())
