"""「不限时」必须在重启 AstrBot 之后活下来（2026-09-20 线上回归）。

现象：在酒馆控制台给「私信催办」勾上不限时，重启 AstrBot 后勾选消失，
看起来像状态没保存。**只有私信催办这两项会坏**，其它时长字段看起来正常。

根因不在保存，也不在读取，而在两者之间：

1. 控制台存「不限时」存的是 JSON ``null``；
2. AstrBot 的 ``AstrBotConfig.check_config_integrity``（astrbot/core/config/
   astrbot_config.py）把 ``conf[key] is None`` 当作「未设置」，启动时用
   ``_conf_schema.json`` 的 default 回填并回写文件：
   ``turn_dm_reminder_seconds`` → 300、``turn_dm_reminder_repeat_seconds`` → 1800；
3. 其它时长字段的 schema default 本来就是 ``-1``，而 ``-1`` 不是 ``None``，
   能活过那次检查，且读回来同样表示不限时——所以只有私信催办坏了。

修法：落盘统一写 ``-1``。本测试把第 2 步的 AstrBot 行为照抄成一个最小复现，
证明改动前会丢、改动后不会丢。
"""

from __future__ import annotations

import unittest
from pathlib import Path

from tavern.config import TavernConfig
from tavern.lifecycle import (
    OPTIONAL_SECONDS_KEYS,
    normalize_time_rules,
    time_rules_for_storage,
)

ROOT = Path(__file__).resolve().parent.parent


def _astrbot_integrity_check(reference: dict, conf: dict) -> dict:
    """照抄 AstrBotConfig.check_config_integrity 的关键分支。

    只保留与本 bug 相关的一条：``conf[key] is None`` → 用参考值（schema
    default）覆盖。真实实现见 astrbot/core/config/astrbot_config.py:181-184。
    """
    result = {}
    for key, value in reference.items():
        if key not in conf or conf[key] is None:
            result[key] = value
        elif isinstance(value, dict) and isinstance(conf.get(key), dict):
            result[key] = _astrbot_integrity_check(value, conf[key])
        else:
            result[key] = conf[key]
    for key in conf:
        if key not in reference:
            result[key] = conf[key]
    return result


# _conf_schema.json 里 time_rules 各项的 default（重启回填用的参考值）。
def _schema_defaults() -> dict:
    import json

    schema = json.loads(
        (ROOT / "_conf_schema.json").read_text(encoding="utf-8-sig")
    )
    return {
        key: item.get("default")
        for key, item in schema["runtime"]["items"]["time_rules"][
            "items"
        ].items()
    }


class UnlimitedSurvivesRestartTests(unittest.TestCase):
    def test_all_unlimited_fields_survive_a_restart(self) -> None:
        """把每个可选时长都设成不限时，过一遍重启回填，必须原样保住。"""
        unlimited = normalize_time_rules(
            {key: -1 for key in OPTIONAL_SECONDS_KEYS}
        )
        stored = time_rules_for_storage(unlimited)
        for key in OPTIONAL_SECONDS_KEYS:
            self.assertEqual(stored[key], -1, f"{key} 落盘应为 -1 而非 null")

        after_restart = _astrbot_integrity_check(_schema_defaults(), stored)
        for key in OPTIONAL_SECONDS_KEYS:
            self.assertEqual(
                after_restart[key],
                -1,
                f"{key} 重启后被 schema 默认值冲掉了",
            )

    def test_null_would_have_been_overwritten(self) -> None:
        """复现原 bug：落盘写 null 时，私信催办两项会被 default 覆盖。"""
        defaults = _schema_defaults()
        self.assertEqual(defaults["turn_dm_reminder_seconds"], 300)
        self.assertEqual(defaults["turn_dm_reminder_repeat_seconds"], 1800)

        broken = {key: None for key in OPTIONAL_SECONDS_KEYS}
        after_restart = _astrbot_integrity_check(defaults, broken)
        self.assertEqual(after_restart["turn_dm_reminder_seconds"], 300)
        self.assertEqual(after_restart["turn_dm_reminder_repeat_seconds"], 1800)

    def test_round_trip_keeps_unlimited_unlimited(self) -> None:
        """落盘 -1 再读回来仍是不限时，行为不变。"""
        original = normalize_time_rules(
            {key: -1 for key in OPTIONAL_SECONDS_KEYS}
        )
        reloaded = normalize_time_rules(time_rules_for_storage(original))
        for key in OPTIONAL_SECONDS_KEYS:
            self.assertIsNone(reloaded[key], f"{key} 读回来应仍为不限时")

    def test_explicit_numbers_are_untouched(self) -> None:
        rules = normalize_time_rules(
            {"turn_dm_reminder_seconds": 600, "turn_timeout_seconds": 180}
        )
        stored = time_rules_for_storage(rules)
        self.assertEqual(stored["turn_dm_reminder_seconds"], 600)
        self.assertEqual(stored["turn_timeout_seconds"], 180)

    def test_missing_keys_are_not_invented(self) -> None:
        """没有的键不要凭空补上，否则会覆盖「键缺失用默认值」的语义。"""
        stored = time_rules_for_storage({"turn_timeout_seconds": None})
        self.assertEqual(stored, {"turn_timeout_seconds": -1})

    def test_to_mapping_serializes_unlimited_as_minus_one(self) -> None:
        """写盘的唯一出口是 TavernConfig.to_mapping，必须已经转换。"""
        config = TavernConfig.from_mapping(
            {
                "runtime": {
                    "time_rules": {
                        "turn_dm_reminder_seconds": None,
                        "turn_dm_reminder_repeat_seconds": None,
                    }
                }
            }
        )
        mapping = config.to_mapping()["runtime"]["time_rules"]
        self.assertEqual(mapping["turn_dm_reminder_seconds"], -1)
        self.assertEqual(mapping["turn_dm_reminder_repeat_seconds"], -1)
        # 内存里的 config 仍用 None 表示不限时
        self.assertIsNone(config.time_rules["turn_dm_reminder_seconds"])

    def test_absent_dm_key_still_falls_back_to_its_default(self) -> None:
        """显式 null 与「键缺失」必须区分：后者是「用默认值」，不是「关闭」。"""
        config = TavernConfig.from_mapping(
            {"runtime": {"time_rules": {"turn_dm_reminder_seconds": None}}}
        )
        stored = config.to_mapping()["runtime"]["time_rules"]
        self.assertEqual(stored["turn_dm_reminder_seconds"], -1)
        self.assertEqual(stored["turn_dm_reminder_repeat_seconds"], 1800)

    def test_save_read_back_check_stays_consistent(self) -> None:
        """settings_save 的回读校验不能因为这次改动而开始报错。"""
        payload = {
            "runtime": {
                "time_rules": {key: None for key in OPTIONAL_SECONDS_KEYS}
            }
        }
        normalized = TavernConfig.from_mapping(payload).to_mapping()
        persisted = TavernConfig.from_mapping(normalized).to_mapping()
        self.assertEqual(persisted, normalized)


if __name__ == "__main__":
    unittest.main()
