"""输出上限截断的识别（2026-09-20 线上回归）。

现场时间线（token_usage）：

    14:57:24  huoshan story_checked         out=6000  截断
    15:00:24  huoshan story_checked_repair  out=6000  截断   ← 白花 180 秒
    15:03:25  deepseek story_checked        out=1675  正常   ← 白花 181 秒

`huoshan/deepseek-v4-flash-260425` 的 provider 条目没关 thinking，模型把整个
max_tokens=6000 预算烧在 reasoning 上，JSON 永远闭合不了，报「模型未返回有效
JSON 对象」。同一输入、同一上限再「修复」一次结果必然相同——所以引擎必须识别
出截断，跳过该模型的修复重试直接换下一个，而不是再花一次完整调用。

本测试只覆盖纯判定函数，不依赖数据库。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from tavern.engine import TavernEngine


def _response(*, finish_reason: str | None = None, output_tokens: int | None = None,
              text: str = "{}"):
    usage = None
    if output_tokens is not None:
        usage = SimpleNamespace(output_tokens=output_tokens)
    choices = [] if finish_reason is None else [
        SimpleNamespace(finish_reason=finish_reason)
    ]
    return SimpleNamespace(
        completion_text=text,
        usage=usage,
        raw_completion=SimpleNamespace(choices=choices),
    )


class TokenCapDetectionTests(unittest.TestCase):
    def test_finish_reason_length_is_truncation(self) -> None:
        self.assertTrue(
            TavernEngine._response_hit_token_cap(
                _response(finish_reason="length"), 6000
            )
        )

    def test_output_tokens_at_cap_is_truncation(self) -> None:
        """火山这次就是这种情况：usage 输出正好等于上限。"""
        self.assertTrue(
            TavernEngine._response_hit_token_cap(
                _response(output_tokens=6000), 6000
            )
        )

    def test_normal_completion_is_not_truncation(self) -> None:
        """真正干活的 deepseek 用了 1675 输出 token，不能被误判。"""
        self.assertFalse(
            TavernEngine._response_hit_token_cap(
                _response(finish_reason="stop", output_tokens=1675), 6000
            )
        )

    def test_output_above_cap_still_counts(self) -> None:
        self.assertTrue(
            TavernEngine._response_hit_token_cap(
                _response(output_tokens=7000), 6000
            )
        )

    def test_missing_usage_or_cap_is_not_truncation(self) -> None:
        for response, cap in (
            (_response(), 6000),
            (_response(finish_reason="stop"), 0),
            (None, 6000),
        ):
            with self.subTest(cap=cap, response=response):
                self.assertFalse(
                    TavernEngine._response_hit_token_cap(response, cap)
                )


class TruncationSkipsRepairTests(unittest.TestCase):
    """截断时必须跳过同模型的修复重试。

    行为发生在 _generate_resolution 里，走数据库难以端到端复现，所以沿用
    test_config_attributes.py 的做法做静态断言：防止这段被无声改回去。
    """

    def test_resolution_loop_breaks_on_cap_before_repair(self) -> None:
        from pathlib import Path

        source = (
            Path(__file__).resolve().parent.parent / "tavern" / "engine.py"
        ).read_text(encoding="utf-8")
        self.assertIn("last_hit_cap = self._response_hit_token_cap(", source)
        marker = "if last_hit_cap:"
        self.assertIn(marker, source)
        window = source[source.index(marker) : source.index(marker) + 700]
        self.assertIn("break", window)
        self.assertIn("max_tokens 上限被截断", window)


if __name__ == "__main__":
    unittest.main()
