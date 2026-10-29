"""上下文预算：token 估算与按预算裁剪。

这两个函数是纯函数，跑得飞快，但它们决定"喂给模型多少内容" ——
算错了不会抛异常，只会让账单悄悄变高、或者让模型忘掉上文，
所以值得单独锁住。
"""

from app.schemas.chat import ChatMessage
from app.services.context_budget import estimate_tokens, fit_history


def _msg(content: str, role: str = "user") -> ChatMessage:
    return ChatMessage(role=role, content=content)


def test_estimate_counts_cjk_per_char() -> None:
    """汉字按 1 字符 ≈ 1 token。低估中文是最常见的成本估算错误。"""
    assert estimate_tokens("你好世界") == 4
    assert estimate_tokens("") == 0


def test_estimate_counts_latin_per_four_chars() -> None:
    """英文按 4 字符 ≈ 1 token，比中文便宜 4 倍。"""
    assert estimate_tokens("abcdefgh") == 2
    assert estimate_tokens("abcde") == 2  # 向上取整，宁可高估


def test_estimate_mixes_cjk_and_latin() -> None:
    # 4 个汉字 + 4 个 ASCII = 4 + 1
    assert estimate_tokens("你好世界abcd") == 5


def test_fit_keeps_newest_within_budget() -> None:
    """预算不够时丢最旧的，保留最近的几轮。"""
    history = [_msg("a" * 100) for _ in range(3)]  # 每条 ≈ 29 token

    kept, dropped = fit_history(history, budget_tokens=70)

    assert len(kept) == 2
    assert dropped == 1
    # 保留的是后两条
    assert kept == history[-2:]


def test_fit_always_keeps_the_latest_message() -> None:
    """预算再小也留一条：上下文为空时模型会完全脱离话题。

    真实场景是用户自己贴了一段超长文本，那该由输入长度校验去管，
    不该在这里静默截断他的问题。
    """
    history = [_msg("a" * 100), _msg("b" * 100)]

    kept, dropped = fit_history(history, budget_tokens=0)

    assert kept == [history[-1]]
    assert dropped == 1


def test_fit_with_empty_history() -> None:
    assert fit_history([], budget_tokens=1000) == ([], 0)


def test_fit_keeps_everything_when_budget_is_generous() -> None:
    history = [_msg("短句"), _msg("另一个短句", role="assistant")]

    kept, dropped = fit_history(history, budget_tokens=1000)

    assert kept == history
    assert dropped == 0
