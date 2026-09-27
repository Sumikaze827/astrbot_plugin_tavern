"""引擎引用的 TavernConfig 属性必须真实存在（2026-09-20 线上事故回归）。

事故：新增的「章节题材补写」调用块里把 `config.enforce_mobile_output` 写成了
`config.enforce_mobile_limits`，触发 AttributeError，被最外层兜底变成
「叙事引擎出现内部错误，世界状态没有改变」，白白废掉两轮。
这类拼写错误不产生 import/语法错误，只有真跑那一行才会炸——所以用静态
扫描把 `config.<attr>` 与 TavernConfig 的字段对齐。
"""

from __future__ import annotations

import dataclasses
import re
import unittest
from pathlib import Path

from tavern.config import TavernConfig

ROOT = Path(__file__).resolve().parent.parent
CONFIG_ATTR = re.compile(r"\bconfig\.([A-Za-z_][A-Za-z0-9_]*)")


class ConfigAttributeTests(unittest.TestCase):
    def test_engine_only_uses_real_tavern_config_attributes(self) -> None:
        """只扫 engine.py：那里的 `config` 一定是 TavernConfig。

        其它模块的 `config` 可能是 dict（`config.get(...)`）或类引用，
        混进来只会制造误报。
        """
        known = {item.name for item in dataclasses.fields(TavernConfig)}
        known.update(dir(TavernConfig))
        text = (ROOT / "tavern" / "engine.py").read_text(
            encoding="utf-8", errors="replace"
        )
        offenders: list[str] = []
        for match in CONFIG_ATTR.finditer(text):
            attr = match.group(1)
            if attr in known:
                continue
            line_no = text.count("\n", 0, match.start()) + 1
            offenders.append(f"engine.py:{line_no} config.{attr}")
        self.assertEqual(offenders, [], "\n".join(offenders))

    def test_mobile_limit_attribute_name(self) -> None:
        """锁住正确的属性名，避免再次写成 enforce_mobile_limits。"""
        fields = {item.name for item in dataclasses.fields(TavernConfig)}
        self.assertIn("enforce_mobile_output", fields)
        self.assertNotIn("enforce_mobile_limits", fields)

    def test_stale_operation_seconds(self) -> None:
        """过期事务判定：解析不出来时不接手（保持原「正在处理中」行为）。"""
        from datetime import datetime, timedelta, timezone

        from tavern.engine import STALE_TURN_OPERATION_SECONDS, _operation_stale_seconds

        fresh = datetime.now(timezone.utc).isoformat()
        self.assertLess(_operation_stale_seconds(fresh), 5)
        old = (
            datetime.now(timezone.utc) - timedelta(seconds=1000)
        ).isoformat()
        self.assertGreaterEqual(
            _operation_stale_seconds(old), STALE_TURN_OPERATION_SECONDS
        )
        self.assertIsNone(_operation_stale_seconds(""))
        self.assertIsNone(_operation_stale_seconds("not-a-date"))


if __name__ == "__main__":
    unittest.main()
