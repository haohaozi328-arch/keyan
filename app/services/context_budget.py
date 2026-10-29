"""上下文预算：按 token 裁剪历史，条数只作兜底。

为什么不能只按条数截断
----------------------
`CHAT_CONTEXT_MESSAGES = 20` 配上单条 8000 字符的上限，最坏情况是
20 × 8000 ≈ 16 万字符，中文约 1:1 折成 token，就是 4 万 token 直接怼进模型。
两个后果：账单按最坏情况走，以及超过上下文窗口时上游直接 502 ——
用户看到的是"服务异常"，日志里只有一句网关报错。

为什么不用 tiktoken 之类的精确分词
--------------------------------
1. 每个模型的分词器不同，网关背后换了模型，tokenizer 就对不上了；
2. tiktoken 第一次用要下载词表，离线环境会直接失败；
3. 这里要的是**成本上限**，不是精确计费 —— 估算偏差 20% 完全够用，
   因为预算本身还留了余量（输出上限另算）。

估算规则（保守取大，宁可贵一点也不要超出窗口）：
    中日韩字符  按 1 字符 = 1 token（多数分词器在 0.6~1.5 之间）
    其他字符    按 4 字符 = 1 token（英文/代码的常见比例）
    每条消息    +4 token 的对话框架开销（role、分隔符）
"""

import math

from app.schemas.chat import ChatMessage

# 每条消息的角色标记与分隔符开销。OpenAI 的文档口径是每条 3~4 token，
# 取 4 是为了在"很多短消息"的场景下也不低估。
_MESSAGE_OVERHEAD = 4

# 中日韩统一表意文字（含扩展 A、兼容区）、日文假名、韩文音节。
# 用区间判断而不是 unicodedata.category：后者只能判断"是否是字母"，
# 判断不出宽度，而拉丁字母与汉字的 token 密度差 4 倍。
_CJK_RANGES = (
    (0x3040, 0x30FF),  # 平假名 / 片假名
    (0x3400, 0x4DBF),  # CJK 扩展 A
    (0x4E00, 0x9FFF),  # CJK 基本区
    (0xAC00, 0xD7AF),  # 韩文音节
    (0xF900, 0xFAFF),  # CJK 兼容表意文字
    (0xFF00, 0xFFEF),  # 全角符号（中文标点在这里）
)


def _is_cjk(char: str) -> bool:
    code = ord(char)
    return any(start <= code <= end for start, end in _CJK_RANGES)


def estimate_tokens(text: str) -> int:
    """粗略估算一段文本的 token 数。空串为 0。"""
    if not text:
        return 0
    cjk = sum(1 for char in text if _is_cjk(char))
    rest = len(text) - cjk
    return cjk + math.ceil(rest / 4)


def message_tokens(message: ChatMessage) -> int:
    return estimate_tokens(message.content) + _MESSAGE_OVERHEAD


def fit_history(history: list[ChatMessage], *, budget_tokens: int) -> tuple[list[ChatMessage], int]:
    """从**最新**往前保留，直到预算用完。返回 (保留的消息, 丢弃条数)。

    为什么从最新往前：聊天是"最近几轮最相关"，丢开头比丢结尾合理得多。
    反过来的话，模型会拿着十轮之前的话题回答当前问题。

    为什么最近一条**无论如何**都保留：预算再小，上下文为空也比"超一点"糟 ——
    模型会完全脱离上下文；而刚好一条消息超预算，说明是用户自己贴了超长文本，
    这种情况该由输入长度校验去管，不该在裁剪这层静默截断用户的问题。
    """
    kept: list[ChatMessage] = []
    used = 0
    for message in reversed(history):
        cost = message_tokens(message)
        if kept and used + cost > budget_tokens:
            break
        kept.append(message)
        used += cost
    kept.reverse()
    return kept, len(history) - len(kept)
