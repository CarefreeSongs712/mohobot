"""LLM 诊断文案的标记类型。

LLMService 在未配置、调用失败、空响应等情况下, 沿用 chat()/chat_stream() 的既有返回形状,
把一段诊断文字当作"回复"交给调用方。这段文字可以展示给用户, 但它**不是助手回复**:
不能写入 contexts/ 工作记忆、conversations 表, 不能触发情感分析, 也不能被当成主动消息正文发出。

继承 str 是为了保持返回形状不变: 旧调用方照常把它当文本用; 需要区分的调用方用
isinstance 判断, 不再按 "[LLM 调用失败" 之类的前缀去猜。

注意: 任何字符串运算(拼接、strip、切片、回复过滤器处理)都会得到普通 str,
所以必须在拼接或过滤**之前**判断。
"""

from __future__ import annotations


class LLMDiagnostic(str):
    """诊断文案(非助手回复)。"""

    __slots__ = ()
