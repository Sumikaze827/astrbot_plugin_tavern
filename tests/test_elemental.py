"""元素反应系统独立单测（无需 AstrBot）。"""
import unittest

from tavern.elemental import (
    affinity,
    parse,
    resolve,
    table,
    validate,
)

SAMPLE = {
    "elemental": {
        "elements": ["火", "水", "雷", "冰", "风", "土", "光", "暗"],
        "affinities": {
            "npc:炎魔": {"水": 0.5, "冰": 1.0},
            "item:木盾": {"火": -1.0},
        },
        "reactions": [
            {"a": "火", "b": "冰", "result": "融化", "effect": {"op": "emit_event", "text": "冰层融化。"}},
            {"a": "雷", "b": "水", "result": "感电"},
        ],
    }
}


class ElementalTest(unittest.TestCase):
    def test_parse_normalizes(self):
        t = parse(SAMPLE)
        self.assertEqual(t["elements"], ["火", "水", "雷", "冰", "风", "土", "光", "暗"])
        self.assertEqual(t["affinities"]["npc:炎魔"]["水"], 0.5)
        self.assertEqual(len(t["reactions"]), 2)
        self.assertEqual(t["resolver"], "")

    def test_affinity_default_zero(self):
        t = parse(SAMPLE)
        self.assertEqual(affinity(t, "npc:炎魔", "火"), 0.0)
        self.assertEqual(affinity(t, "npc:炎魔", "冰"), 1.0)
        self.assertEqual(affinity(t, "unknown", "水"), 0.0)

    def test_resolve_affinity_only(self):
        t = parse(SAMPLE)
        result = resolve(t, "冰", "npc:炎魔")
        self.assertIsNotNone(result)
        self.assertEqual(result["affinity"], 1.0)
        self.assertIsNone(result["reaction"])

    def test_resolve_reaction(self):
        t = parse(SAMPLE)
        result = resolve(t, "火", "npc:炎魔", target_element="冰")
        self.assertIsNotNone(result)
        self.assertEqual(result["reaction"]["result"], "融化")
        self.assertEqual(result["effects"], {"op": "emit_event", "text": "冰层融化。"})

    def test_resolve_none_when_no_match(self):
        t = parse(SAMPLE)
        result = resolve(t, "光", "item:木盾", target_element="土")
        self.assertIsNone(result)

    def test_resolve_custom_resolver(self):
        t = parse(SAMPLE)
        def resolver(source, target, context, table):
            if source == "暗":
                return {"matched": True, "reaction": {"result": "暗蚀"}, "effects": []}
            return None
        result = resolve(t, "暗", "npc:炎魔", resolver=resolver)
        self.assertIsNotNone(result)
        self.assertEqual(result["resolver"], "custom")
        self.assertEqual(result["reaction"]["result"], "暗蚀")

    def test_resolver_fallback_on_throw(self):
        t = parse(SAMPLE)
        def bad(*_args):
            raise RuntimeError("boom")
        result = resolve(t, "冰", "npc:炎魔", resolver=bad)
        self.assertIsNotNone(result)
        self.assertEqual(result["affinity"], 1.0)

    def test_validate(self):
        self.assertEqual(validate({}), [])
        self.assertEqual(validate({"elemental": {"elements": ["火", "火"]}})[0]["code"], "elemental.duplicate_element")
        self.assertEqual(validate({"elemental": "x"})[0]["code"], "elemental.not_object")

    def test_table_summary(self):
        t = table(SAMPLE)
        self.assertEqual(len(t["elements"]), 8)
        self.assertEqual(t["version"], "1.0")
        self.assertEqual(t["issues"], [])


if __name__ == "__main__":
    unittest.main()
