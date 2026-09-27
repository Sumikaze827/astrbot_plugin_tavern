"""v0.12.0-A18 回归测试。

覆盖：
1. 副本实时仪表盘数据聚合（squad / npcs / ledger / clocks / party_relations /
   current_choice / economy / return_requests）。
2. 小队列表字段整合（数值 / 背包 / 关系 / 行动状态）。
3. 队伍关系提取（party_relations 只取队伍级关系）。
4. WebUI 权限降级：PolicyRejection 映射为 403（不触发 AstrBot 登录跳转）。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tavern.constants import DEFAULT_WORLD_SLUG
from tavern.dashboard import (
    _party_relations,
    _squad,
    session_dashboard,
)
from tavern.database import TavernDatabase
from tavern.database_support import json_dump, new_id, utc_now
from tavern.errors import PolicyRejection


def _sample_world_state() -> dict:
    return {
        "location": "鸦渡镇",
        "relationships": {
            "队伍": {"信任": 80, "声望": "知名"},
            "party": {"信任": 60},
            "npc:晨辉教会鸦渡礼拜": {"信任": 40},
            "participant_abc": {"信任": 50},
        },
        "inventory": {"公共物资": ["干粮"]},
    }


class A18PartyRelationTests(unittest.TestCase):
    def test_party_relations_only_party_level(self) -> None:
        rows = _party_relations(_sample_world_state())
        targets = [row["target"] for row in rows]
        self.assertIn("队伍", targets)
        self.assertIn("party", targets)
        # 个人关系不属于队伍级
        self.assertNotIn("participant_abc", targets)
        by_target = {row["target"]: row for row in rows}
        self.assertIn("fields", by_target["队伍"])
        self.assertEqual(by_target["队伍"]["fields"].get("声望"), "知名")

    def test_party_relations_parse_source_target(self) -> None:
        world_state = {
            "relationships": {
                "队伍→npc:晨辉教会鸦渡礼拜": {"信任": 70, "好感": 60},
                "队伍→鸦渡镇守备队": {"信任": 40},
                "participant_abc→npc:某人": {"信任": 50},
                "npc:某人→队伍": {"信任": 30},
            }
        }
        rows = _party_relations(world_state)
        by_target = {row["target"]: row for row in rows}
        self.assertIn("npc:晨辉教会鸦渡礼拜", by_target)
        self.assertEqual(by_target["npc:晨辉教会鸦渡礼拜"]["favor"], 60)
        self.assertIn("鸦渡镇守备队", by_target)
        # 角色级关系不进入队伍板块
        self.assertNotIn("npc:某人", by_target)

    def test_squad_enriches_resources_inventory_relations(self) -> None:
        turn = {
            "current_user_id": "user-1",
            "order": [
                {"position": 1, "user_id": "user-1", "name": "白鸦"},
            ],
        }
        roster = [
            {
                "id": "participant_abc",
                "group_user_id": "user-1",
                "character_name": "白鸦",
                "character_code": "BY",
                "display_name": "玩家一",
                "participation_status": "active",
                "card_status": "approved",
                "ready": True,
                "card_profile": {"class": "游侠"},
                "card_stats": {
                    "raw": {"hp": 12, "mp": 5},
                    "labels": {"hp": "生命值", "mp": "魔力值"},
                },
                "runtime_state": {
                    "statuses": ["中毒", "祝福"],
                    "current_location": "鸦渡镇",
                    "reputation": {"声望": "知名"},
                },
            }
        ]
        # A19: 关系与背包以 world_state（引擎权威数据）为准。
        world_state = {
            "relationships": {
                "user-1→npc:某人": {"信任": 70, "好感": 80},
                "队伍→npc:某人": {"信任": 50},
            },
            "inventory": {
                "user-1": {
                    "短弓": {"count": 2, "category": "武器"},
                    "干粮": 5,
                },
                "队伍": {"公共物资": {"count": 3, "category": "补给"}},
            },
        }
        squad = _squad(roster, turn, world_state)
        self.assertEqual(len(squad), 1)
        member = squad[0]
        self.assertTrue(member["is_current"])
        self.assertEqual(member["turn_position"], 1)
        self.assertEqual(member["role"], "游侠")
        self.assertEqual(member["resources"].get("hp"), 12)
        # 中文资源标签（问题 3）
        self.assertEqual(member["resource_labels"].get("hp"), "生命值")
        # 背包来自 world_state.inventory 按角色 owner 键（问题 1）
        self.assertEqual(len(member["inventory"]), 2)
        bow = next(item for item in member["inventory"] if item["name"] == "短弓")
        self.assertEqual(bow["count"], 2)
        self.assertEqual(bow["category"], "武器")
        # 关系来自 world_state.relationships 中 source 为该角色的条目（问题 1）
        self.assertEqual(len(member["relationships"]), 1)
        rel = member["relationships"][0]
        self.assertEqual(rel["target"], "npc:某人")
        self.assertEqual(rel["fields"].get("信任"), 70)
        # 状态来自 runtime_state.statuses（问题 4）
        self.assertEqual(member["statuses"], ["中毒", "祝福"])

    def test_party_relations_collects_org_info(self) -> None:
        """A23: _party_relations 收集组织/势力关系（字符串值条目，kind=info）。"""
        world_state = {
            "relationships": {
                "北境王国": "合作但受监管",
                "晨辉教会": "共同处理异常",
                "队伍→npc:某人": {"信任": 50},
            }
        }
        rows = _party_relations(world_state)
        info = [r for r in rows if r.get("kind") == "info"]
        self.assertEqual(len(info), 2)
        by_label = {r["label"]: r for r in info}
        self.assertEqual(by_label["北境王国"]["summary"], "合作但受监管")
        self.assertEqual(by_label["晨辉教会"]["summary"], "共同处理异常")
        party = [r for r in rows if r.get("kind") == "party"]
        self.assertEqual(len(party), 1)
        self.assertEqual(party[0]["target"], "npc:某人")

    def test_squad_runtime_fallback_and_reputation_normalization(self) -> None:
        turn = {"current_user_id": "user-2", "order": []}
        roster = [
            {
                "id": "participant_xyz",
                "group_user_id": "user-2",
                "character_name": "艾达",
                "card_profile": {},
                "card_stats": {},
                "runtime_state": {
                    "equipment": ["旧匕首"],
                    "npc_relationships": {
                        "npc:守卫": {"favor": 40, "stage": "相识"},
                    },
                    "reputation": {"声望": "知名"},
                },
            }
        ]
        # world_state 中没有该角色的数据 → 回退到 runtime_state 补充来源
        squad = _squad(roster, turn, {"relationships": {}, "inventory": {}})
        member = squad[0]
        self.assertEqual(len(member["inventory"]), 1)
        self.assertEqual(member["inventory"][0]["name"], "旧匕首")
        self.assertEqual(member["relationships"][0]["target"], "npc:守卫")
        self.assertEqual(member["relationships"][0]["favor"], 40)
        # reputation dict 归一为文本，不再输出 {..}
        self.assertNotIn("{", member["reputation"])
        self.assertIn("知名", member["reputation"])


class A18DashboardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.database = TavernDatabase(Path(self.temp.name))
        self.session = await self.database.ensure_session(
            "qq", "group-a18", "qq:group-a18", DEFAULT_WORLD_SLUG, "admin-1"
        )
        await self.database.transition_session(
            self.session["id"], "preparing", "admin-1"
        )
        await self.database.join_turn_order(
            self.session["id"], "user-1", "玩家一", "user-1"
        )

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    async def test_session_dashboard_has_a18_keys(self) -> None:
        result = await session_dashboard(self.database, self.session["id"])
        self.assertIsInstance(result.get("squad"), list)
        self.assertIsInstance(result.get("npcs"), list)
        self.assertIsInstance(result.get("ledger"), list)
        self.assertIsInstance(result.get("clocks"), list)
        self.assertIsInstance(result.get("party_relations"), list)
        self.assertIsInstance(result.get("economy"), dict)
        self.assertIsInstance(result.get("return_requests"), list)
        self.assertIsInstance(result.get("party_inventory"), list)
        self.assertIsInstance(result.get("quest_items"), list)
        # turn.order 保留 user_id（供前端上移/下移/指定）
        order = result["turn"]["order"]
        self.assertTrue(order)
        self.assertIn("user_id", order[0])
        # 无活跃选项时 current_choice 为 None
        self.assertIsNone(result.get("current_choice"))

    async def test_policy_rejection_is_403_semantics(self) -> None:
        # PolicyRejection 是预期内策略拒绝，不进入 401（避免前端桥跳登录页）。
        self.assertIsInstance(PolicyRejection("x"), Exception)
        self.assertIsInstance(PolicyRejection("x"), PolicyRejection)

    async def test_dashboard_reads_active_choices(self) -> None:
        """A20: session_dashboard 正确读取 active_choice_set 的 choices（修复 choices_json 键）。"""
        sid = self.session["id"]
        participant = await self.database.reserve_participant(sid, "chooser-1", "选择者")
        await self.database.join_turn_order(sid, "chooser-1", "选择者", "chooser-1")
        await self.database.transition_session(sid, "running", "admin-1")
        now = utc_now()
        choices = [
            {"key": "A", "text": "调查房间中的异常痕迹", "risk": "safe"},
            {"key": "B", "text": "询问门口的信使", "risk": "medium"},
            {"key": "C", "text": "沿走廊搜索线索", "risk": "high"},
            {"key": "D", "text": "原地等待观察", "risk": "safe"},
        ]
        with self.database._connect() as connection:
            connection.execute(
                """
                INSERT INTO choice_sets(
                    id, session_id, participant_id, round_no, session_revision,
                    choices_json, status, reroll_count, idempotency_key,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'active', 0, ?, ?, ?)
                """,
                (
                    new_id("choices"),
                    sid,
                    participant["id"],
                    1,
                    1,
                    json_dump(choices),
                    f"test:{sid}:1",
                    now,
                    now,
                ),
            )
        result = await session_dashboard(self.database, sid)
        self.assertEqual(len(result["active_choices"]), 4)
        self.assertEqual(result["current_choice"]["choices"][0]["key"], "A")
        self.assertEqual(
            result["current_choice"]["participant"]["group_user_id"], "chooser-1"
        )
