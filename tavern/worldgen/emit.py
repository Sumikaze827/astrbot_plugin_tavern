"""把已批准的检查点装配成可导入的 v5 世界包 + NPC 包，并过闸门交付。

装配原则：**结构由 Python 拼，内容由模型给。**

模型已经提供了章节的标题/目标/节奏/里程碑/NPC 名录，这里负责把所有标识符、计数、
引用闭合、属性预算、协议信封都**确定性地**拼出来，再交给两道闸门：

1. :func:`tavern.worldgen.lint.lint_world_package` —— 补 ``world_preflight`` 的缺口
   （里程碑 id 格式、悬空 ``next_chapter_id``、``total_milestones`` 计数、NPC 引用）
2. :func:`tavern.world_preflight.inspect_world_package` —— 插件自带的体检门

**两道都必须过**。lint 通过不代表 preflight 通过，反之亦然：前者管剧情线路，
后者管协议契约与建卡算术。

产物一律只落文件（``worlds/<slug>.json`` 与 ``worlds/<slug>-npcs.json``），
不直接写运行库——这样才有回滚余地，也能被 ``world_market`` 自动发现。
"""

from __future__ import annotations

import copy
import json
import re
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .lint import lint_world_package
from .models import CheckpointProposal, ContinuityBrief, ProposalChapter
from .steps import replaced_name_hits

WORLD_SCHEMA_VERSION = 5
MINIMUM_PLUGIN_VERSION = "v0.12.0"
NPC_TEMPLATE_VERSION = 2

#: 与王都第一日一致的 7 属性。改编本沿用同一套，玩家角色卡才能直接搬过来。
DEFAULT_ATTRIBUTES: tuple[tuple[str, str, str], ...] = (
    ("strength", "力量", "近身发力、破门、搬运、压制和承受冲击。"),
    ("agility", "敏捷", "闪避、攀爬、潜行、追逐与精细操作。"),
    ("vitality", "体质", "抵抗疲劳、失血、毒物与身体损伤。"),
    ("intellect", "智力", "医学常识、器物分析、逻辑推理与技术判断。"),
    ("willpower", "意志", "忍痛、抗恐惧、抵抗神经干扰、保持专注和守住立场。"),
    ("perception", "感知", "发现埋伏、搜索线索、识别伪造、追踪和判断异常。"),
    ("charisma", "魅力", "谈判、威慑、欺骗、领导和争取信任。"),
)

#: 规范建卡配置：**逐字**取自《宅邸长夜》（v6），只差 `profession_presets`。
#:
#: 为什么生成器必须逐字照抄而不是自己拼一套：跨副本导入角色卡时，
#: `repositories/characters.py:_prepare_card_import` 会比较来源与目标**两边世界的
#: `card_template`**（`fields` / `stats` / `preset_dimensions`），只要有一项不同就
#: 拒绝导入（「来源与当前本的建卡模板不兼容」）。生成器原先自己拼了一套 v2 形状的卡
#: ——字段标签、属性描述、修正表、`preset_selector` / `bonus_choices` / `total_validation`
#: 全不一样——于是每个生成出来的世界都是一座孤岛：玩家在同一部作品的两卷之间
#: 搬不过去角色卡，而原因藏在三个 JSON 子树的差异里，肉眼根本看不出来。
#:
#: 所以这里锚定成常量，并由 `tests/test_worldgen_emit.py` 拿真实世界包逐字回验：
#: 改了这份常量却与参照世界对不上，测试会红。
CANONICAL_CHARACTER_CARD: dict[str, Any] = {
    "version": 6,
    "auto_approve": False,
    "edit_requires_review": True,
    "fields": [
        {"key": "name", "label": "角色姓名", "type": "text", "required": True,
         "private": False, "max_chars": 12},
        {"key": "code", "label": "副本代号", "type": "text", "required": True,
         "private": False, "max_chars": 12},
        {"key": "profession", "label": "选择穿越前职业（固定50点基础）",
         "type": "preset_select", "preset_source": "profession_presets", "page_size": 8,
         "required": True, "private": False, "max_chars": 30,
         "clear_on_change": ["primary_attribute", "secondary_attribute"]},
        {"key": "primary_attribute", "label": "选择主属性（固定+7）",
         "type": "preset_select", "required": True, "private": False, "max_chars": 8,
         "options": ["力量", "敏捷", "体质", "智力", "意志", "感知", "魅力"],
         "page_size": 7, "bonus_value": 7,
         "clear_on_change": ["secondary_attribute"]},
        {"key": "secondary_attribute", "label": "选择副属性（固定+3）",
         "type": "preset_select", "required": True, "private": False, "max_chars": 8,
         "options": ["力量", "敏捷", "体质", "智力", "意志", "感知", "魅力"],
         "page_size": 7, "bonus_value": 3,
         "must_differ_from": "primary_attribute"},
        {"key": "supplement", "label": "补充说明（可留空，不授予额外能力）",
         "type": "text", "required": False, "private": False, "max_chars": 300},
    ],
    "stats": {
        "allocation_mode": "profession_base_plus_primary7_secondary3",
        "input_mode": "automatic_profession_base_plus_two_fixed_bonus_choices",
        "calculate_after_field": "secondary_attribute",
        "reset_scope": "primary_and_secondary_only",
        "mode": "preset",
        "budget": 60,
        "base_budget": 50,
        "bonus_budget": 10,
        "effective_total": 60,
        "attributes": [
            {"key": key, "label": label, "minimum": 0, "maximum": 20,
             "default": 5, "description": description}
            for key, label, description in DEFAULT_ATTRIBUTES
        ],
        "modifier_table": {
            "0": -3, "1": -2, "2": -2, "3": -1, "4": -1, "5": 0, "6": 0, "7": 1,
            "8": 1, "9": 2, "10": 2, "11": 3, "12": 3, "13": 4, "14": 4, "15": 5,
            "16": 5, "17": 6, "18": 6, "19": 7, "20": 7,
        },
        "primary_bonus": 7,
        "secondary_bonus": 3,
        "require_distinct_bonus_attributes": True,
        "player_selects_only": ["primary_attribute", "secondary_attribute"],
        "defaults_are_schema_fallback_only": True,
        "manual_entry_enabled": False,
        "auto_apply_profession_base": True,
        "profession_base_locked": True,
        "show_profession_values_at_profession_step": True,
        "profession_step": 2,
        "skip_generated_attribute_questions": True,
        "calculation_formula": (
            "effective_attribute = profession.base_attribute + 7 if primary"
            " + 3 if secondary"
        ),
        "preserve_base_on_reset": True,
        "preset_selector": {
            "field": "profession", "source": "profession_presets", "match_by": "id",
        },
        "bonus_choices": [
            {"field": "primary_attribute", "label": "主属性", "bonus": 7, "count": 1},
            {"field": "secondary_attribute", "label": "副属性", "bonus": 3, "count": 1,
             "exclude_selected_by": ["primary_attribute"]},
        ],
        "total_validation": {"base_total": 50, "final_total": 60},
        "allocation": {"rule": "exact", "total": 60},
    },
}

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

#: 人数上限。**必须显式声明**，否则 ``lifecycle.player_limits`` 会退回内置默认
#: ``maximum=4``——生成出来的世界因此永远只能坐 4 个人，而操作者在面板上改
#: 「强制人数上限」也没用：他改的是世界，副本用的是**建本那一刻冻结的快照**；
#: 一旦重新导入世界包，改动又被文件里那个不存在的值覆盖回去。
#: 取《宅邸长夜》《王都第一日》里同一部作品的声明值。
DEFAULT_PLAYER_LIMITS: dict[str, int] = {
    "minimum_start": 1,
    "maximum": 6,
    "recommended_min": 3,
    "recommended_max": 5,
}


class EmitError(RuntimeError):
    """装配或闸门失败。``problems`` 里是可以回炉修复的具体问题。"""

    def __init__(self, message: str, problems: Sequence[str] | None = None) -> None:
        super().__init__(message)
        self.problems = list(problems or [])


# --- 建卡 -----------------------------------------------------------------


def build_character_card(
    *,
    professions: Sequence[Mapping[str, Any]],
    attribute_labels: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """拼建卡配置：**规范卡骨架 + 这个世界自己的职业预设**。

    骨架见 :data:`CANONICAL_CHARACTER_CARD`——逐字照抄自《宅邸长夜》，
    这样同一部作品不同卷之间才能互相导入角色卡（导入会比较两侧的
    ``card_template``，见 ``repositories/characters.py:_prepare_card_import``）。
    唯一随世界变化的是 ``profession_presets``。
    """
    card = copy.deepcopy(CANONICAL_CHARACTER_CARD)
    labels = {
        str(key): str(value).strip()
        for key, value in (attribute_labels or {}).items()
        if str(value).strip()
    }
    if labels:
        attributes = card["stats"]["attributes"]
        for attribute in attributes:
            override = labels.get(str(attribute["key"]))
            if override:
                attribute["label"] = override
        # preset_select 的 options **填的是显示标签而非 key**：改了属性标签，
        # 主/副属性的选项必须跟着改，否则选出来的值对不上属性表。
        option_labels = [str(item["label"]) for item in attributes]
        for field in card["fields"]:
            if str(field.get("key") or "") in {
                "primary_attribute", "secondary_attribute"
            }:
                field["options"] = list(option_labels)

    card["profession_presets"] = [
        {
            "id": str(p.get("id") or ""),
            "name": str(p.get("name") or ""),
            "description": str(p.get("description") or ""),
            "base_attributes": {
                k: int(v)
                for k, v in (p.get("base_attributes") or {}).items()
                if isinstance(v, int) and not isinstance(v, bool)
            },
        }
        for p in professions
    ]
    return card


# --- 章节 -----------------------------------------------------------------


def _build_milestone(milestone: Any) -> dict[str, Any]:
    """拼一个里程碑。

    ``evidence_required`` 为空时**整键省略**，而不是塞一个 ``match: []`` 的空壳——
    空 match 会让裁判拿到一个永远匹配不上的信号，比没有更糟（lint 会给出
    「没有 evidence_required」的警告，那才是准确的描述）。
    """
    entry: dict[str, Any] = {"id": milestone.milestone_id, "label": milestone.label}
    evidence = [
        {
            "type": str(item.get("type") or "clue_keyword_any"),
            "match": [str(w) for w in (item.get("match") or []) if str(w).strip()],
        }
        for item in (milestone.evidence_required or [])
        if isinstance(item, Mapping)
    ]
    evidence = [item for item in evidence if item["match"]]
    if evidence:
        entry["evidence_required"] = evidence
    return entry


def build_chapters(chapters: Sequence[ProposalChapter]) -> list[dict[str, Any]]:
    """把已批准的提案拼成 ``rules.progress.chapters``。

    ``next_chapter_id`` / ``exits_when`` 这些引用闭合由这里确定性生成——
    不让模型自己写指针，就不会出现悬空引用。
    """
    built: list[dict[str, Any]] = []
    for index, chapter in enumerate(chapters):
        next_id = (
            chapters[index + 1].chapter_id if index + 1 < len(chapters) else ""
        )
        milestone_ids = [m.milestone_id for m in chapter.milestones if m.milestone_id]
        entry: dict[str, Any] = {
            "id": chapter.chapter_id,
            "title": chapter.title,
            "subtitle": chapter.subtitle,
            "min_turns": max(1, int(chapter.min_turns)),
            "max_turns": max(1, int(chapter.max_turns)),
            "narrative_length_band": chapter.narrative_length_band or "standard",
            "current_objective": chapter.current_objective,
            "pacing_directive": chapter.pacing_directive,
            "hook_pool": list(chapter.hook_pool),
            "key_npcs": [
                {
                    "ref": str(npc.get("ref") or ""),
                    "role": str(npc.get("role") or ""),
                }
                for npc in chapter.key_npcs
                if isinstance(npc, Mapping) and npc.get("ref")
            ],
            "milestones": [
                _build_milestone(m) for m in chapter.milestones if m.milestone_id
            ],
            "exits_when": {"all_milestones": milestone_ids},
        }
        if next_id:
            entry["next_chapter_id"] = next_id
        built.append(entry)
    return built


# --- 世界包 ---------------------------------------------------------------


def build_world(
    *,
    slug: str,
    name: str,
    description: str,
    chapters: Sequence[ProposalChapter],
    prose: Mapping[str, Any],
    professions: Sequence[Mapping[str, Any]],
    attribute_labels: Mapping[str, str] | None = None,
    brief: ContinuityBrief | None = None,
    initial_state: Mapping[str, Any] | None = None,
    world_content_version: str = "1.0.0",
) -> dict[str, Any]:
    """装配完整的 v5 世界包。"""
    if not SLUG_RE.match(str(slug or "")):
        raise EmitError(f"slug 非法：{slug!r}（只能用小写字母/数字/下划线/连字符，≤64 字符）")

    built_chapters = build_chapters(chapters)
    if not built_chapters:
        raise EmitError("没有任何已批准的章节，无法装配世界包")

    total_milestones = sum(len(c["milestones"]) for c in built_chapters)
    first_id = built_chapters[0]["id"]

    progress: dict[str, Any] = {
        "chapter": built_chapters[0]["title"],
        "current_chapter_id": first_id,
        "current_objective": built_chapters[0]["current_objective"],
        "completed_milestones": 0,
        "total_milestones": total_milestones,
        "design_note": (
            f"由世界包生成 agent 装配，共 {len(built_chapters)} 章 / {total_milestones} 个里程碑。"
        ),
        "chapters": built_chapters,
    }

    pacing = {
        str(item.get("chapter_id") or ""): str(item.get("pacing_directive") or "")
        for item in (prose.get("chapters") or [])
        if isinstance(item, Mapping)
    }
    for chapter in built_chapters:
        override = pacing.get(chapter["id"])
        if override:
            chapter["pacing_directive"] = override

    state = dict(initial_state or {})
    state.setdefault("location", "")
    state.setdefault("time", "")
    state.setdefault("scene_summary", str(prose.get("opening_scene") or "")[:200])
    state.setdefault("facts", [])
    state.setdefault("inventory", {})
    state.setdefault("relationships", {})

    world: dict[str, Any] = {
        "world_schema_version": WORLD_SCHEMA_VERSION,
        "minimum_plugin_version": MINIMUM_PLUGIN_VERSION,
        "world_content_version": world_content_version,
        "protocol": {
            "core_version": 5,
            "features": {"chat_experience": "1.0", "entity_registry": "1.0"},
        },
        "required_features": ["entity_registry@>=1.0"],
        "slug": slug,
        "name": name,
        "description": description,
        "system_prompt": str(prose.get("system_prompt") or ""),
        "opening_scene": str(prose.get("opening_scene") or ""),
        "rules": {
            "resolution": {
                # **必须是 RESOLUTION_MODES 里的值**（none/narrative/dice_only/
                # attribute）。这里曾经写死成 "d20"——那是骰制名，不是模式名，
                # world_contract 认不出就**静默降级成 none**，于是整包世界
                # 一次检定都不摇：章节里写好的 difficulty、dangerous 强制检定、
                # 46 条里程碑的 evidence 全都不生效，而且全程不报错。
                # 真实世界的取值一律是 attribute（角色卡有属性表）。
                "mode": "attribute",
                "dice_system": "d20",
                "default_difficulty": "controlled",
                "difficulty_min": 9,
                "difficulty_max": 19,
                "difficulty_policy": {
                    "controlled": 9,
                    "dangerous": 13,
                    "desperate": 17,
                    "lethal": 19,
                },
            },
            "player_limits": dict(DEFAULT_PLAYER_LIMITS),
            "strict_choices": True,
            "check_density": "standard",
            "allow_player_result_claims": True,
            "death_requires_confirmation": False,
            "content_boundaries": {"character_death": "yes"},
            "safety": {
                "collective_harm_requires_vote": False,
                "lethal_risk_must_be_disclosed": True,
            },
            "npc_policy": {"auto_npc_keys": "npc_*"},
            "character_card": build_character_card(
                professions=professions, attribute_labels=attribute_labels
            ),
            "opening_choices": [
                {
                    "key": str(c.get("key") or ""),
                    "text": str(c.get("text") or ""),
                    "risk": str(c.get("risk") or "safe"),
                    "requires_check": bool(c.get("requires_check")),
                    "collective": bool(c.get("collective")),
                }
                for c in (prose.get("opening_choices") or [])
                if isinstance(c, Mapping)
            ],
            "progress": progress,
        },
        "initial_state": state,
    }

    if brief is not None and not brief.is_empty:
        from .continuity import inject_brief

        world, _ = inject_brief(brief, world)

    return world


def build_npcs(
    *,
    slug: str,
    npcs: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """装配 NPC 导入包。

    NPC **不在**世界包里——``world_import_payload`` 会把 ``characters`` 键剥掉，
    必须走独立的 ``characters/import`` 通道，且要在世界包之后导入。
    """
    items: list[dict[str, Any]] = []
    for order, raw in enumerate(npcs):
        npc = dict(raw)
        npc_slug = str(npc.get("slug") or "")
        if not npc_slug:
            continue
        profile = {
            "actor_complexity": str(npc.get("actor_complexity") or "full"),
            "identity": str(npc.get("identity") or ""),
            "appearance": str(npc.get("appearance") or ""),
            "personality": str(npc.get("personality") or ""),
            "public_background": str(npc.get("public_background") or ""),
            "location": str(npc.get("location") or ""),
            "capabilities": [str(x) for x in (npc.get("capabilities") or [])],
            "limitations": [str(x) for x in (npc.get("limitations") or [])],
        }
        if npc.get("relationship_stance"):
            profile["relationship_stance"] = str(npc["relationship_stance"])
        items.append(
            {
                "slug": npc_slug,
                "name": str(npc.get("name") or ""),
                "role": str(npc.get("role") or "npc"),
                "sort_order": int(npc.get("sort_order") or (order + 1) * 10),
                "profile": profile,
                "prompt": str(npc.get("prompt") or ""),
            }
        )
    if not items:
        raise EmitError("没有任何 NPC，无法装配 NPC 包")
    return {
        "template_metadata": {
            "generated_by": "tavern.worldgen",
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        },
        "template_version": NPC_TEMPLATE_VERSION,
        "minimum_plugin_version": MINIMUM_PLUGIN_VERSION,
        "world_slug": slug,
        "items": items,
    }


# --- NPC 引用对齐 ---------------------------------------------------------


def drop_replaced_npcs(
    npcs: Sequence[Mapping[str, Any]], replaces: Sequence[str]
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """把**由玩家取代的原作角色**从 NPC 名录里剔除。返回 ``(留下的, 被剔除的)``。

    为什么要有一道机械剔除，而不是只靠提示词：``CAST`` 与时间线是两次独立的模型
    调用，时间线声明了「玩家取代菜月昴」，``CAST`` 照样可能因为原文里他戏份最多
    而给他建卡——实测 chapter030 就是这么长出一张 ``npc_subaru`` 的。提示词是劝告，
    这里是保证：被点名的角色绝不进角色表。

    剔除不是静默的：调用方把返回的 ``dropped`` 记在作业上，面板的交付产物区会列出来
    （同 :func:`reconcile_npc_refs` 的 ``dropped_refs``）。

    名字匹配走 :func:`..steps.replaced_name_hits`（包含匹配，"昴" 与 "菜月昴" 都算命中）。
    """
    names = [str(n).strip() for n in (replaces or []) if str(n).strip()]
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, str]] = []
    for raw in npcs or []:
        if not isinstance(raw, Mapping):
            continue
        item = dict(raw)
        hits = replaced_name_hits(item.get("name"), names) if names else []
        if hits:
            dropped.append(
                {
                    "slug": str(item.get("slug") or ""),
                    "name": str(item.get("name") or ""),
                    "matched": "、".join(hits),
                }
            )
            continue
        kept.append(item)
    return kept, dropped


def reconcile_npc_refs(
    world: Mapping[str, Any], npcs: Mapping[str, Any] | None
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """把章节的 ``key_npcs[].ref`` 收敛到 NPC 包**真的有**的角色上。

    返回 ``(对齐后的世界包, 被摘掉的引用)``；世界包是深拷贝，原对象不动。

    **为什么需要它**：章节抽取与角色名录是两步独立的模型调用，判据还正好相反——

    - 章节抽取照着原文给每个有名有姓的人写 ``ref``（自造 slug），
      chapter030 一轮就造了 24 个；
    - ``CAST_SYSTEM`` 明确要求「只收录在本卷实际出场且玩家可能与之交互的角色，
      **背景板人物不要收**」，于是只建了 7 张卡（拉姆/雷姆/罗兹瓦尔/爱蜜莉雅/
      帕克/贝蒂/昴）。

    两边都没错，错在**没人对账**。结果是整条流水线跑完二十来分钟、
    在最后一刻被交付闸门一句 ``key_npcs 引用了 NPC 包中不存在的 slug`` 打回，
    而 artifacts 全都好好的——纯粹白跑。

    **为什么是摘引用而不是补卡**：名录才是"这个世界里谁作为 NPC 存在"的权威，
    章节只是提到了某个人名。为一个只在旁白里出现的角色凭空捏一张卡（外形、
    性格、能力边界、扮演指示全得现编），比不建卡更糟。摘掉引用后该角色仍在
    叙事里，只是不再参与按章状态同步。
    """
    import copy

    updated = copy.deepcopy(dict(world))

    items = npcs.get("items") if isinstance(npcs, Mapping) else None
    known = {
        str(item.get("slug"))
        for item in (items or [])
        if isinstance(item, Mapping) and item.get("slug")
    }

    dropped: list[dict[str, str]] = []
    progress = (updated.get("rules") or {}).get("progress")
    chapters = progress.get("chapters") if isinstance(progress, Mapping) else None
    for chapter in chapters or []:
        if not isinstance(chapter, Mapping):
            continue
        kept: list[Any] = []
        for npc in chapter.get("key_npcs") or []:
            if not isinstance(npc, Mapping):
                continue
            ref = str(npc.get("ref") or "").strip()
            if ref and ref not in known:
                dropped.append(
                    {
                        "chapter_id": str(chapter.get("id") or ""),
                        "ref": ref,
                        "role": str(npc.get("role") or ""),
                    }
                )
                continue
            kept.append(dict(npc))
        if len(kept) != len(chapter.get("key_npcs") or []):
            chapter["key_npcs"] = kept

    return updated, dropped


def reconcile_resolution_mode(
    world: Mapping[str, Any],
) -> tuple[dict[str, Any], str]:
    """修掉认不出的 ``rules.resolution.mode``，返回 ``(世界包, 说明)``。

    只处理**运行时不认**的值（例如生成器写死的 ``"d20"``——那是 ``dice_system``
    的值，不是模式名）。``world_contract`` 遇到这种值不报错，直接当 ``none`` 用，
    于是整包世界一次检定都不摇。这里按角色卡有没有属性表补一个合法模式，
    并把改动**报出来**（同 :func:`reconcile_npc_refs`，不静默改写语义字段）。

    合法值原样返回、说明为空串。
    """
    import copy

    from ..world_contract import RESOLUTION_MODES

    updated = copy.deepcopy(dict(world))
    rules = updated.get("rules")
    if not isinstance(rules, dict):
        return updated, ""
    resolution = rules.get("resolution")
    if not isinstance(resolution, dict):
        return updated, ""

    mode = str(resolution.get("mode") or "").strip()
    if mode.lower() in RESOLUTION_MODES:
        return updated, ""

    stats = ((rules.get("character_card") or {}).get("stats") or {})
    attributes = stats.get("attributes") if isinstance(stats, dict) else None
    resolved = "attribute" if attributes else "dice_only"
    resolution["mode"] = resolved
    return updated, f"检定模式 {mode or '(空)'} → {resolved}"


# --- 闸门 -----------------------------------------------------------------


def run_gates(world: Mapping[str, Any], npcs: Mapping[str, Any]) -> dict[str, Any]:
    """跑两道闸门。任一道不过就抛 :class:`EmitError`，带上可回炉的具体问题。"""
    npc_items = npcs.get("items") if isinstance(npcs, Mapping) else None
    slugs = [
        str(item.get("slug"))
        for item in (npc_items or [])
        if isinstance(item, Mapping) and item.get("slug")
    ]

    lint_report = lint_world_package(world, npc_slugs=slugs)
    problems = [
        f"[lint] {item['path']}：{item['message']}"
        for item in lint_report["issues"]
        if item["level"] == "error"
    ]

    preflight: dict[str, Any] = {}
    try:
        from ..world_preflight import inspect_world_package

        preflight = inspect_world_package(dict(world))
        if not preflight.get("compatible"):
            for item in preflight.get("issues") or []:
                if item.get("level") == "error":
                    problems.append(f"[preflight] {item.get('path')}：{item.get('message')}")
            if not problems:
                problems.append("[preflight] 世界包体检未通过")
    except Exception as exc:  # 体检本身异常也要算失败，不能放行
        problems.append(f"[preflight] 体检执行失败：{exc}")

    if problems:
        raise EmitError(f"交付闸门未通过（{len(problems)} 项）", problems)

    return {"lint": lint_report, "preflight": preflight}


# --- 落盘 -----------------------------------------------------------------


def write_packages(
    *,
    output_dir: str | Path,
    slug: str,
    world: Mapping[str, Any],
    npcs: Mapping[str, Any],
    overwrite: bool = False,
) -> dict[str, str]:
    """把两个包写到 ``worlds/`` 下。返回 ``{"world": path, "npcs": path}``。"""
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    world_path = directory / f"{slug}.json"
    npc_path = directory / f"{slug}-npcs.json"

    if not overwrite:
        for path in (world_path, npc_path):
            if path.exists():
                raise EmitError(
                    f"目标文件已存在：{path.name}。"
                    "请换一个 slug，或先归档/删除已有世界包再重跑"
                )

    world_path.write_text(
        json.dumps(world, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    npc_path.write_text(
        json.dumps(npcs, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return {"world": str(world_path), "npcs": str(npc_path)}


def package_summary(world: Mapping[str, Any], npcs: Mapping[str, Any]) -> dict[str, Any]:
    """给面板展示的产物概览。"""
    progress = ((world.get("rules") or {}).get("progress") or {})
    chapters = progress.get("chapters") or []
    card = ((world.get("rules") or {}).get("character_card") or {})
    return {
        "slug": world.get("slug"),
        "name": world.get("name"),
        "chapters": len(chapters),
        "milestones": sum(len(c.get("milestones") or []) for c in chapters),
        "npcs": len(((npcs.get("items") if isinstance(npcs, Mapping) else None) or [])),
        "professions": len(card.get("profession_presets") or []),
        "total_turns": sum(int(c.get("max_turns") or 0) for c in chapters),
        "bytes": len(json.dumps(world, ensure_ascii=False).encode("utf-8")),
    }
