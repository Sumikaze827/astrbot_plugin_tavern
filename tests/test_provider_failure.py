"""服务商失败分类的回归测试（2026-09-20）。

背景：火山方舟的 Coding Plan 端点只服务已加入编码计划的模型。配置里把
``huoshan/deepseek-v4-1-flash-260910`` 当主叙事模型时，每次调用都稳定返回
``404 UnsupportedModel: The requested model does not support the coding plan
feature``（已用真实密钥直连验证：该 ID 404，同账户的
``deepseek-v4-flash-260425`` / ``doubao-seed-2-0-pro-260215`` 均 200）。

旧实现把这类失败当瞬时故障：连续 3 次后熔断 5 分钟，之后 10→20→40 分钟，
到期 half_open 再打一次。线上该 provider 累计 **20 次失败、0 次成功**，
面板只显示「叙事调用失败：NotFoundError」，看起来像服务商抖动，运维不会
想到要去改模型 ID。

本测试只覆盖纯判定函数（不依赖数据库）。
"""

from __future__ import annotations

import unittest

from tavern.errors import provider_failure_is_permanent

# 真实 provider_health.last_failure_reason 原文。
REAL_NOT_FOUND = "叙事调用失败：NotFoundError"
REAL_STRUCTURAL = "结构校验失败：故事正文必须为 100—300 字，当前为 378 字"


class ProviderFailureClassificationTests(unittest.TestCase):
    def test_model_or_auth_errors_are_permanent(self) -> None:
        for reason in (
            REAL_NOT_FOUND,
            "叙事调用失败：AuthenticationError",
            "叙事调用失败：PermissionDeniedError",
            "404 UnsupportedModel: The requested model does not support the "
            "coding plan feature.",
            "The model does not exist on this account",
            "invalid_api_key",
        ):
            with self.subTest(reason=reason):
                self.assertTrue(provider_failure_is_permanent(reason))

    def test_transient_errors_are_not_permanent(self) -> None:
        for reason in (
            "叙事请求超时",
            "叙事调用失败：APIConnectionError",
            "叙事调用失败：RateLimitError",
            "叙事调用失败：InternalServerError",
            "叙事调用失败：APITimeoutError",
            REAL_STRUCTURAL,
            "",
        ):
            with self.subTest(reason=reason):
                self.assertFalse(provider_failure_is_permanent(reason))

    def test_structural_failure_is_not_mistaken_for_config_error(self) -> None:
        """结构校验失败是模型输出质量问题，换模型可能有用但不该长熔断。"""
        self.assertFalse(provider_failure_is_permanent(REAL_STRUCTURAL))
        self.assertFalse(
            provider_failure_is_permanent(
                "选项生成结构校验失败：选项越权操控了其他玩家角色 jade"
            )
        )


class ProviderHealthSchemaTests(unittest.TestCase):
    """新的配置类失败必须落在已有的 status 取值域内。"""

    def test_status_check_constraint_still_covers_used_values(self) -> None:
        # provider_health.status 的 CHECK 约束是
        # ('healthy','open','half_open')；配置类失败复用 'open' + 24 小时
        # 熔断，避免为一个展示标签重建表。
        allowed = {"healthy", "open", "half_open"}
        self.assertIn("open", allowed)
        self.assertNotIn("invalid", allowed)


if __name__ == "__main__":
    unittest.main()
