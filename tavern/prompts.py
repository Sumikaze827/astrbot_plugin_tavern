from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from .constants import CORE_NARRATOR_RULES
from .world_contract import world_contract
from .chat_experience import narrator_directives
from .security import clean_text


def _json(value: Any) -> str:
    # Prompt payloads are machine-readable; whitespace only consumes context.
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


RUNTIME_FACTS_LIMIT = 80


def _world_state_projection(value: Any) -> dict[str, Any]:
    """Bound only the prompt working set; never mutate stored facts or other channels."""
    state = dict(value) if isinstance(value, Mapping) else {}
    if isinstance(state.get('facts'), list):
        state['facts'] = state['facts'][-RUNTIME_FACTS_LIMIT:]
    return state


RESOLUTION_SCHEMA = {
    "mode": "resolve | check",
    "narrative": "最终叙事；check 阶段留空",
    "check": {
        "stat": "检定维度",
        "reason": "为什么结果存在不确定性",
        "difficulty": "5-25 的整数",
        "modifier": "填 0；插件会按角色卡与世界查表覆盖为权威加值",
        "risk": "safe | controlled | dangerous | desperate | lethal",
        "check_type": "standard | leader | group | resistance | opposed",
        "advantage_sources": ["只能引用角色卡、装备、状态、协助或场景中已存在的有利事实"],
        "disadvantage_sources": ["只能引用伤势、环境、时间压力或场景中已存在的不利事实"],
        "known_consequences": "玩家当前能够预见的失败后果；致命风险必须明确",
        "visibility": "public | immersive | hidden",
        "participant_ids": ["集体检定或独立抵抗时的参与角色 ID"],
        "opponent_modifier": "对抗检定时填 0；插件会覆盖为权威值",
    },
    "state_patch": {
        "location": "可选，全体活跃玩家共同转场后的共享场景；个人或小队移动不得填写",
        "time": "可选，新时间",
        "scene_summary": "可选，当前场景简要状态",
        "facts_add": ["影响后续判断的已定事实；不写家具、感官、过场、未执行计划；同一事不重复记"],
        "facts_remove": ["精确匹配已失效事实"],
        "inventory_ops": [
            {
                "owner_id": "玩家ID或角色标识",
                "item": "物品名",
                "delta": "有符号整数",
            }
        ],
        "relationship_ops": [
            {
                "source": "关系发起方（优先用插件提供的 participant_id / NPC stable_key / 名称）",
                "target": "关系对象（同上）",
                "dimension": "信任/亲近/敬畏/敌意等",
                "delta": "-20 到 20 的整数",
            }
        ],
        "economy_ops": [
            {
                "operation_id": "唯一幂等键（必填，避免重复扣款）",
                "kind": "credit | debit | transfer | reward | fine | purchase | sale | adjust",
                "currency_id": "世界包声明的货币稳定 ID",
                "amount": "主单位金额",
                "from_owner_type": "player | party | npc | shop | faction | 世界包自定义",
                "from_owner_ref": "所有者稳定 ID",
                "to_owner_type": "同上",
                "to_owner_ref": "同上",
                "reason": "变动原因（写入交易日志）",
            }
        ],
    },
    "location_ops": [
        {
            "target_id": "实际发生移动的玩家 participant_id、角色名或角色代号",
            "location": "该角色移动完成后的当前位置",
        }
    ],
    "participants": [
        "本轮参与者的participant_id；普通移动延续travel_groups，漏人不拆队"
    ],
    "travel_change": {"mode": "split|join；无变化省略", "members": ["participant_id"], "reason": "分开或会合依据"},
    "memories": [
        {
            "scope": "world | player | npc",
            "scope_id": "对应ID；world 可留空",
            "kind": "fact | promise | relationship | discovery | injury",
            "content": "值得跨回合保留的简短事实",
            "importance": "1-5",
            "tags": ["检索标签"],
            "visibility": "public | host | private",
            "locked": "只有确定且不可被普通摘要淘汰的重要事实才为 true",
            "pinned": "需要优先进入上下文时为 true",
            "supersedes_id": "明确取代旧记忆时填写旧记忆 ID",
        }
    ],
    "next_choices": [
        {
            "key": "A | B | C | D，必须恰好四项且不重复",
            "actor_id": "必须等于插件提供的 next_actor.participant_id",
            "text": "下一位玩家可选择的行动意图，不预设结果",
            "danger_id": "世界声明的危险度 ID",
            "check": {
                "required": "布尔值",
                "attribute_id": "attribute 模式填写世界属性 ID；其他模式留空",
                "type": "standard | leader | group | resistance | opposed",
                "difficulty": "5-25",
                "known_consequences": "玩家可预见的风险；不能泄露隐藏真相",
                "advantage_sources": ["选项生成时已经成立的优势来源"],
                "disadvantage_sources": ["选项生成时已经成立的劣势来源"],
            },
            "collective": "布尔值；影响全队时为 true",
            "vote_scope": "party（默认全队）或 local（仅影响当前位置相同的现场小队）",
        }
    ],
    "group_decision": {
        "vote_scope": "party 或 local；local 不得决定远处队员的行动或全队主线抉择",
        "question": "只有遇到影响全队的关键节点时填写",
        "options": [
            {
                "key": "A-D，2 至 4 项",
                "text": "互斥且不提前保证结果的集体方案",
            }
        ],
    },
    "return_progress": {
        "request_id": "仅在已存在返场任务且本轮产生真实推进时填写",
        "evidence": "本轮如何推进或完成返场条件",
        "completed": "只有条件已经在剧情中实际完成时才为 true",
    },
    "npc_ops": [
        {
            "op": "create | update | archive | depart | kill（实际死亡必须 kill；update/create 不得复活已死亡NPC）",
            "npc_id": "更新既有 NPC 时填写稳定 ID",
            "name": "姓名；每回合最多创建 3 名",
            "aliases": ["别名"],
            "role_type": "npc | creature | faction",
            "persistent": "需要跨回合保留时为 true",
            "registration_reasons": [
                "direct_interaction | important_clue | long_term_memory"
            ],
            "public_profile": {
                "identity": "公开身份",
                "appearance": "外貌",
                "personality": "可观察到的性格",
            },
            "runtime_state": {
                "location": "当前位置",
                "faction": "阵营",
                "status": "active | departed | dead | archived",
            },
            "known_facts": ["该 NPC 确实知道的事实"],
            "misconceptions": ["误解、谣言或错误认知"],
        }
    ],
    "clock_ops": [
        {
            "op": "create | advance | set | complete | archive",
            "clock_id": "既有时钟 ID",
            "title": "时钟名称",
            "segments": "4 | 6 | 8",
            "delta": "推进格数",
            "value": "set 时的新值",
            "visibility": "public | vague | hidden",
            "trigger": "填满时只触发一次的事件",
        }
    ],
    "ledger_ops": [
        {
            "op": "create | update | complete | fail | archive",
            "entry_id": "既有条目 ID",
            "stable_key": "milestone 必填：current_chapter.milestones[*].id",
            "kind": "main | side | objective | milestone",
            "title": "标题",
            "description": "当前已确认的信息",
            "visibility": "public | host",
        }
    ],
    "status_ops": [
        {
            "op": "add | update | remove",
            "target_id": "角色或参与者 ID",
            "name": "伤势或状态名",
            "severity": "minor | serious | critical",
            "affects": ["受影响的具体行动"],
            "effect": "通常为相关检定劣势，不得无差别影响全部行动",
            "removal": "明确解除条件",
            "note": "治疗/净化/驱散只能 remove（彻底解除）或 update（减轻，severity 降级）既有状态；禁止用 add 新增状态来表达治疗",
        }
    ],
    "assist_ops": [
        {
            "target_id": "被协助角色 ID",
            "stat": "适用检定维度",
            "method": "本回合实际采取的协助方式",
            "expires_round": "默认本轮结束失效",
        }
    ],
    "director_note": "仅供审计的简短裁定依据，不得泄露隐藏剧情",
}


_NON_NARRATIVE_RULE_KEYS = {
    "capabilities",
    "character_card",
    "context_budget",
    "danger_levels",
    "default_difficulty",
    "difficulty_max",
    "difficulty_min",
    "event_pool",
    "entity_registry",
    "opening_choices",
    "option_presentation",
    # 全部章节会在每次叙事/选项请求中重复注入；当前章已有独立快照，
    # 其余章节只会增加 token、诱发抢跑与导演信息泄露。
    "progress",
    "world_schema_version",
}

_CARD_ONLY_SETTING_KEYS = {
    "attribute_progression",
    "factions",
    "origin_regions",
    "power_systems",
    "professions",
    "regions",
    "social_identities",
    "species_and_identities",
}


def compact_world_rules(world: Mapping[str, Any]) -> dict[str, Any]:
    """Compile only rules useful to narration; omit authoring/card payloads."""
    raw = world.get("rules", {})
    rules = raw if isinstance(raw, Mapping) else {}
    result = {
        key: value
        for key, value in rules.items()
        if key not in _NON_NARRATIVE_RULE_KEYS and key != "setting_modules"
    }
    setting_modules = rules.get("setting_modules")
    if isinstance(setting_modules, Mapping):
        compact_modules = {
            key: value
            for key, value in setting_modules.items()
            if key not in _CARD_ONLY_SETTING_KEYS
        }
        if compact_modules:
            result["setting_modules"] = compact_modules
    return result


def _schema_for(*, allow_check: bool) -> dict[str, Any]:
    schema = json.loads(json.dumps(RESOLUTION_SCHEMA, ensure_ascii=False))
    if not allow_check:
        schema["mode"] = "resolve"
        schema.pop("check", None)
        schema["next_choices"][0]["check"] = (
            "null 或下一回合预先声明的检定；safe 必须为 null"
        )
    return schema


def _milestone_done(mid: str, completed: set[str]) -> bool:
    """Return whether the exact authoritative milestone ID is completed."""
    return bool(mid) and mid in completed


def _completed_milestone_keys(
    story_ledger: Sequence[Mapping[str, Any]] | None,
) -> set[str]:
    keys: set[str] = set()
    for entry in story_ledger or ():
        if not isinstance(entry, Mapping):
            continue
        if entry.get("kind") == "milestone" and entry.get("status") == "completed":
            k = str(entry.get("stable_key") or entry.get("entry_id") or "").strip()
            if k:
                keys.add(k)
    return keys


def _current_chapter_block(
    world: Mapping[str, Any],
    progress: Mapping[str, Any] | None,
    completed_milestones: set[str] | None = None,
) -> str:
    """Highlight the player's current chapter for the system prompt."""
    if not isinstance(progress, Mapping):
        progress = {}
    cur_id = progress.get("current_chapter_id") or ""
    if not cur_id:
        return ""
    w_progress = world.get("rules", {}).get("progress", {}) if isinstance(
        world.get("rules"), Mapping
    ) else {}
    chapters = w_progress.get("chapters") if isinstance(w_progress, Mapping) else None
    cur = next(
        (c for c in (chapters or [])
         if isinstance(c, Mapping) and c.get("id") == cur_id),
        None,
    )
    if not cur:
        return ""
    snap = {
        "id": cur.get("id"),
        "title": cur.get("title"),
        "current_objective": progress.get("current_objective")
            or cur.get("current_objective"),
        "narrative_length_band": cur.get("narrative_length_band"),
        "pacing_directive": cur.get("pacing_directive"),
        "milestones": [
            {
                "id": m.get("id"),
                "label": m.get("label"),
                "status": "completed"
                if (completed_milestones is not None
                    and _milestone_done(str(m.get("id") or ""), completed_milestones))
                else "pending",
            }
            for m in (cur.get("milestones") or [])
            if isinstance(m, Mapping) and m.get("id")
        ],
        "next_chapter_id": cur.get("next_chapter_id"),
        "exits_when": cur.get("exits_when"),
    }
    return (
        "<current_chapter trust=\"world-authoritative\">\n"
        f"{_json(snap)}\n"
        "</current_chapter>\n\n"
    )


def system_prompt(
    world: Mapping[str, Any],
    *,
    allow_check: bool = True,
    capability_projection: Sequence[Mapping[str, Any]] | None = None,
    current_progress: Mapping[str, Any] | None = None,
    runtime_directive: str = "",
    story_ledger: Sequence[Mapping[str, Any]] | None = None,
) -> str:
    """Purpose-built narrative system prompt without card-authoring duplication."""
    contract = world_contract(world)
    effective_allow_check = allow_check and contract["resolution"]["mode"] in {
        "dice_only",
        "attribute",
    }
    projection = [
        {
            "capability_ref": item.get("capability_ref"),
            "available": bool(item.get("available", True)),
            "state": item.get("state", {}),
        }
        for item in (capability_projection or ())
        if isinstance(item, Mapping) and item.get("capability_ref")
    ]
    capability_block = ""
    if projection:
        capability_block = (
            "<available_capabilities>\n"
            f"{_json(projection)}\n"
            "Only these projected capabilities may be narrated as currently usable. "
            "The plugin remains authoritative for costs, targets, constraints and effects.\n"
            "</available_capabilities>\n\n"
        )
    experience = narrator_directives(world)
    experience_block = (
        "<multiplayer_experience>\n"
        f"{_json(experience)}\n"
        "</multiplayer_experience>\n\n"
        if experience
        else ""
    )
    current_chapter = _current_chapter_block(
        world,
        current_progress,
        _completed_milestone_keys(story_ledger),
    )
    pacing_directive_text = ""
    if isinstance(current_progress, Mapping):
        w_progress = world.get("rules", {}).get("progress", {}) if isinstance(
            world.get("rules"), Mapping
        ) else {}
        chapters = w_progress.get("chapters") if isinstance(w_progress, Mapping) else None
        cur_id = current_progress.get("current_chapter_id") or ""
        cur = next(
            (c for c in (chapters or [])
             if isinstance(c, Mapping) and c.get("id") == cur_id),
            None,
        )
        if cur and cur.get("pacing_directive"):
            pacing_directive_text = (
                "本章 pacing_directive：" + str(cur["pacing_directive"]) + "\n\n"
            )
    runtime_text = (
        f"<runtime_directive trust=\"plugin-authoritative\">\n{runtime_directive}\n</runtime_directive>\n\n"
        if runtime_directive
        else ""
    )
    return (
        f"{CORE_NARRATOR_RULES}\n\n"
        "<world_definition>\n"
        f"{str(world.get('system_prompt', '')).strip()}\n"
        "</world_definition>\n\n"
        "<narrative_world_rules>\n"
        f"{_json(compact_world_rules(world))}\n"
        "</narrative_world_rules>\n\n"
        f"{current_chapter}"
        f"{capability_block}"
        f"{experience_block}"
        f"<pacing_directive_text trust=\"world-authoritative\">\n{pacing_directive_text}\n</pacing_directive_text>\n\n"
        f"{runtime_text}"
        "<required_output_schema>\n"
        f"{_json(_schema_for(allow_check=effective_allow_check))}\n"
        "</required_output_schema>\n"
    )


def _history(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for event in events:
        result.append(
            {
                "id": event.get("id"),
                "turn": event.get("turn_no"),
                "role": event.get("role"),
                "actor_id": event.get("actor_id"),
                "actor_name": event.get("actor_name"),
                "content": event.get("content"),
            }
        )
    return result


def _party(roster: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in roster:
        if not isinstance(item, Mapping) or item.get(
            "participation_status"
        ) not in {"active", "standby", "away"}:
            continue
        profile = item.get("card_profile")
        profile = profile if isinstance(profile, Mapping) else {}
        runtime = item.get("runtime_state")
        runtime = runtime if isinstance(runtime, Mapping) else {}
        # 非行动角色只暴露现场可观察信息，避免模型把其性格、秘密、
        # 专长或决定权混入下一位玩家的行动选项。
        result.append(
            {
                "participant_id": item.get("id"),
                "character_name": item.get("character_name"),
                "character_code": item.get("character_code"),
                "participation_status": item.get("participation_status"),
                "public_appearance": profile.get("appearance", ""),
                "visible_location": runtime.get("current_location", ""),
                "visible_statuses": runtime.get("statuses", []),
            }
        )
    return result


def _character_projection(value: Mapping[str, Any]) -> dict[str, Any]:
    profile = value.get("profile")
    if not isinstance(profile, Mapping):
        profile = value.get("card_profile")
    stats = value.get("stats")
    if not isinstance(stats, Mapping):
        stats = value.get("card_stats")
    runtime = value.get("runtime_state")
    runtime = runtime if isinstance(runtime, Mapping) else {}
    return {
        "participant_id": value.get("participant_id") or value.get("id"),
        "character_name": value.get("character_name"),
        "character_code": value.get("character_code"),
        "display_name": value.get("display_name"),
        "profile": dict(profile) if isinstance(profile, Mapping) else {},
        "stats": dict(stats) if isinstance(stats, Mapping) else {},
        "runtime_state": dict(runtime),
        "participation_status": value.get("participation_status"),
    }


def compact_character(value: Mapping[str, Any]) -> dict[str, Any]:
    """Public helper for dedicated option prompts and context-size tests."""
    return _character_projection(value)


def _npc_knowledge_gate(direction: Any) -> str:
    """把「谁有感知依据」写成显式约束，避免模型反复写越界 known_facts。

    2026-09-20：本地图未覆盖新场景时，模型给在场 NPC 写 known_facts 会被旧逻辑
    判为越界并作废整轮。现在引擎只剥离条目，但仍在提示里点名允许对象，减少
    无效轮次与「正文写了、状态没记」的落差。
    """
    if not isinstance(direction, Mapping):
        return ""
    scope = direction.get("scope")
    if not isinstance(scope, Mapping) or not scope.get("enforce_knowledge"):
        return ""
    observers = [
        str(item.get("observer_id"))
        for item in (scope.get("perception_candidates") or [])
        if isinstance(item, Mapping) and item.get("observer_id")
    ]
    listed = _json(observers)
    if observers:
        rule = (
            "npc_ops.known_facts / misconceptions 只能写给这些 NPC；"
            "写给别人会被引擎剥离（不会作废本轮，但正文也不得让未列出的 NPC "
            "偷听、回应或表现出知情）。引擎另有感知回执，无需替它补记。"
        )
    else:
        # 空列表是「本轮无法认证感知范围」（例如玩家走进了作者 location_map
        # 未登记的场景），不等于「现场没有别人」。禁止记账可以，禁止在场人物
        # 正常回应就是把 NPC 写成一排木头——那正是玩家抱怨的 OOC。
        rule = (
            "本轮无法认证感知范围，因此不要写 known_facts / misconceptions"
            "（引擎只在有感知依据时记账，写了会被剥离）。正文仍按玩家行动与"
            "现场人物正常回应书写；只是不得让明显不在场或远处的 NPC 得知本次"
            "发言，也不得把这次发言当成他们本来就知道的事。"
        )
    return (
        '<npc_knowledge_gate trust="plugin-authoritative">\n'
        f"本轮有感知依据的 NPC（active_npcs 的 npc_id）：{listed}。{rule}\n"
        "</npc_knowledge_gate>\n\n"
    )


def _npc_projection(
    characters: Sequence[Mapping[str, Any]],
    world: Mapping[str, Any],
) -> list[dict[str, Any]]:
    presets: dict[str, Mapping[str, Any]] = {}
    for item in world.get("characters", []):
        if not isinstance(item, Mapping) or not item.get("enabled", True):
            continue
        for key in (item.get("id"), item.get("slug"), item.get("name")):
            if key:
                presets[str(key)] = item
    result: list[dict[str, Any]] = []
    for item in characters:
        if not isinstance(item, Mapping):
            continue
        preset = presets.get(str(item.get("stable_key") or "")) or presets.get(
            str(item.get("name") or "")
        )
        from .npc_direction import canonical_profile
        profile = canonical_profile(item, world)
        row = {
            "npc_id": item.get("id"),
            "stable_key": item.get("stable_key"),
            "name": item.get("name"),
            "aliases": item.get("aliases", []),
            "role_type": item.get("role_type"),
            "public_profile": dict(profile) if isinstance(profile, Mapping) else {},
            "known_facts": item.get("known_facts", []),
            "misconceptions": item.get("misconceptions", []),
            "runtime_state": {k: v for k, v in (item.get("state") or {}).items()
                              if k != "duplicate_proposals"},
        }
        if profile.get("private_direction"):
            row["private_direction"] = profile["private_direction"]
        result.append(row)
    return result


def _memory_projection(memories: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    keys = (
        "id",
        "scope",
        "scope_id",
        "kind",
        "content",
        "importance",
        "tags",
        "visibility",
        "locked",
        "pinned",
    )
    return [{key: item.get(key) for key in keys if key in item} for item in memories]


def _ledger_projection(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """把账本投影成提示词块（截断由 context_budget.ledger_items 在取数时完成）。"""
    keys = ("id", "stable_key", "kind", "title", "description", "status", "visibility")
    return [{key: item.get(key) for key in keys if key in item} for item in items]


def _dialogue_voice_rules() -> str:
    """对白人声规则（2026-09-20 玩家反馈「人物发言僵硬、命令式、满口限制」）。

    病因不是模型不会写对白，而是它的上下文里**全是**命令式约束（12 条核心
    规则、7 条清晰度硬约束、章节 paces 指令、≤50 字的行动选项），却没有一句
    告诉它「这些是对你的写作要求，不是角色说话的方式」。于是模型把自己的
    指令腔照搬进台词，NPC 变成了发号施令的任务派发员。这一段就是那道界线。
    """
    return (
        "\n对白与人声（硬约束）：\n"
        "1. 上面所有的「不得 / 禁止 / 必须 / 不许」都是对**你**的写作要求，"
        "不是角色说话的方式。正文对白必须像人在说话，不能像任务简报、流程"
        "说明或系统提示——禁止让任何角色用条目式口吻逐条发布要求"
        "（「不许加…」「必须一次…」「别加判断」「到此为止，谁也别…」）。\n"
        "2. 每个人按自己的身份、性格、当下情绪和与对方的亲疏说话：可以有"
        "犹豫、反问、改口、岔开、说半句、带情绪的词、口头禅和固定的称呼"
        "习惯。同一句话换个人说，效果应该完全不同。\n"
        "3. 对白只承载「这个人在这个当口会说的话」，不承载规则条文。禁止把"
        "行动选项、行动说明、系统约束原样搬进台词：玩家的行动意图要写成"
        "这个人会说的句子，而不是把「原话递上去、一次递清、不加判断」这类"
        "执行细则念出来。<npc_direction> 里的 goal / intent / condition 是"
        "**给你看的意图摘要**，不是台词脚本；不要让它变成 NPC 嘴里的条件清单。"
        "expression 是态度与表达指导，voice_profile 是人物声音依据，也不要逐字念出这些说明。\n"
        "4. 一次说话只推进一件事。角色不需要在一句台词里把条件、限制、例外"
        "列全；把「我要求你照实说」写成带立场和后果的活话，例如「你要是"
        "添油加醋，回头我不好替你说话」。\n"
        "5. 上位者、长辈、主君下要求时同样说人话：主君不会用行政口吻派活，"
        "老人不会讲流程，街头少年不会讲条例。确实需要立规矩时，让角色给出"
        "**理由或情绪**，而不是只给规定。\n"
        "6. 与硬约束第 4 条并存：事实与结果照样要交代清楚——把规矩写进叙述"
        "或动机里，把「人话」留给台词。若一段对白读起来像你在向模型复述"
        "要求，就重写它。\n"
    )


def _runtime_sections(
    *,
    world: Mapping[str, Any],
    session: Mapping[str, Any],
    player: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
    memories: Sequence[Mapping[str, Any]],
) -> str:
    from .continuity import previous_action, CONTINUITY_POLICY
    personal = previous_action(player, session.get("personal_history_events", events), int(session.get("turn_no") or 0))
    focal_scene = {
        "current_location": (player.get("runtime_state") or {}).get("current_location") or "未确认，不推定同场",
        "rule": "以此为本次行动起点；全局 scene_summary 可能属于另一玩家，只作背景，不替代本人的现场。相同地点的成员也不自动参加同一对话。",
    }
    acting = _character_projection(player)
    next_actor = _character_projection(
        session.get("next_actor", {})
        if isinstance(session.get("next_actor"), Mapping)
        else {}
    )
    if (
        acting.get("participant_id")
        and acting.get("participant_id") == next_actor.get("participant_id")
    ):
        next_actor = {
            "participant_id": acting["participant_id"],
            "same_as_acting_player": True,
        }
    return (
        f'<personal_continuity_policy>{CONTINUITY_POLICY}</personal_continuity_policy>\n'
        '<previous_personal_action trust="untrusted-data" temporal_scope="past-settled-only">\n'
        f'{_json(personal)}\n</previous_personal_action>\n'
        f'<acting_scene trust="untrusted-data">{_json(focal_scene)}</acting_scene>\n'
        '<runtime_state trust="untrusted-data">\n'
        f"{_json(_world_state_projection(session.get('world_state', {})))}\n"
        "</runtime_state>\n\n"
        '<turn_context trust="untrusted-data">\n'
        f"{_json(session.get('turn_status', {}))}\n"
        "</turn_context>\n\n"
        '<active_party trust="untrusted-data">\n'
        f"{_json(_party(session.get('roster', [])))}\n"
        "</active_party>\n\n"
        '<split_party_knowledge_policy>旁白可见的全局历史不等于角色已知。'
        '严格使用每人的 visible_location；分队时 runtime_state.location 仅是上次共享场景。'
        '只有角色亲历或正文已明确交流的信息才能用于其对白、判断和选项；'
        '另一队的线索、NPC 对话和危险不得自动共享。位置未知时不得推定同场。'
        '会合只代表同场，不代表已分享全部发现；可以提供交流发现的个人行动。'
        '远处成员不能直接治疗、接应、听见密谈或被本地危险波及，除非有既定远程能力或传播证据。'
        '</split_party_knowledge_policy>\n\n'
        '<acting_player trust="untrusted-data">\n'
        f"{_json(acting)}\n"
        "</acting_player>\n\n"
        '<next_actor trust="plugin-authoritative">\n'
        f"{_json(next_actor)}\n"
        "</next_actor>\n\n"
        '<relevant_memories trust="untrusted-data">\n'
        f"{_json(_memory_projection(memories))}\n"
        "</relevant_memories>\n\n"
        '<recent_history trust="untrusted-data">\n'
        f"{_json(_history(events))}\n"
        "</recent_history>\n\n"
        '<active_return_requests trust="untrusted-data">\n'
        f"{_json(session.get('return_requests', []))}\n"
        "</active_return_requests>\n\n"
        '<active_npcs trust="untrusted-data">\n'
        f"{_json(_npc_projection(session.get('session_characters', []), world))}\n"
        "</active_npcs>\n\n"
        '<npc_direction trust="untrusted-data" temporal_scope="conditional-intent-not-result">\n'
        f"{_json(session.get('npc_direction', {}))}\n"
        "</npc_direction>\n\n"
        f"{_npc_knowledge_gate(session.get('npc_direction'))}"
        '<story_ledger trust="untrusted-data">\n'
        f"{_json(_ledger_projection(session.get('story_ledger', [])))}\n"
        "</story_ledger>\n\n"
        '<scene_clocks trust="untrusted-data">\n'
        f"{_json(session.get('scene_clocks', []))}\n"
        "</scene_clocks>\n\n"
        '<content_boundaries trust="trusted-policy">\n'
        f"{_json(session.get('content_boundaries', {}))}\n"
        "</content_boundaries>\n"
    )


def story_length_bounds(
    story_complete: bool,
    band: str = "",
) -> tuple[int, int]:
    """故事正文长度下限（与 _validate_mobile_resolution 共用）。

    返回 (下限, 上限)，**上限 0 表示不设上限**。

    2026-09-20 取消上限：玩家反馈模型的正文写到 755 字时被判
    「结构校验失败（故事正文必须为 150—500 字）」——两个 provider 都因此
    失败，整轮裁定作废、世界状态一个字没动。正文写长不是错误，不该拿整轮
    进度去罚它：模型对篇幅的判断（该交代的设定、该写的场景）本身就是叙事
    质量的一部分，硬砍回 500 字反而制造了「设定介绍不全」的老问题。

    下限保留（150 字）：太短的正文确实无法锚定 NPC 身份、场景与动作结果。
    band 仍由世界包提供并传入，但不再影响结果——留参数是为了不动调用链。
    """
    return 150, 0


def _outcome_fulfillment_hint(outcome: str) -> str:
    """按 dice.outcome 给叙事模型一个兑现强度倾向（软指令，2026-08-24
    玩家反馈「自由演绎的摇点结果应有不同程度影响叙事模型的裁定」后落地）。

    玩家原话：
    「大成功就要求模型尽力去圆，成功和失败就一个给一点影响，一个完全
    不给影响；大失败的死亡判定可以不动」。

    四档兑现强度（写入 <freeform_acceptance> 块，仅自由演绎路径生效）：
    - critical_success → "尽力去圆 + 充分兑现"：找到场景内最有力
      的解读、给足与这一手好牌相称的回报，不得以未声明的副作用为由
      打折扣。
    - success → "克制放水"：结果应成立，但不得放大成决定性胜利或
      完美压制；成功不等同于万事顺遂。
    - failure → "行动落空，不嘴硬圆"：如实呈现行动被挡回/落空/被
      反制，不得用"虽然没有……但是……"式的伪成功掩盖失败。
    - critical_failure → 返回空（死亡由引擎 _lethal_death_verdict 硬
      落实 / freeform 路径走 <freeform_critical_failure> 块），prompt
      不再加任何兑现引导。
    - success_with_cost → 返回空（已在 checked_resolution_prompt
      顶部明文要求"代价必须相称"，不再叠加）。
    - 其它/空 outcome → 返回空（无 outcome 信息时不强行注入）。
    """
    normalized = str(outcome or "").strip().lower()
    if normalized == "critical_success":
        return (
            "本次骰点为 critical_success（大成功），叙事兑现应倾向'尽力"
            "去圆'——在尊重世界事实与场景内已确立约束的前提下，优先"
            "采用对玩家最有利的解读、给足与这一手好牌相称的回报；"
            "不得以未声明的物理规则/未发生的副作用为由打折扣。"
        )
    if normalized == "success":
        return (
            "本次骰点为 success（成功），叙事兑现应倾向'克制放水'"
            "——结果应成立，但不得放大成决定性胜利或完美压制；"
            "成功不等同于万事顺遂，仍需留出场景内合理阻力与代价空间。"
        )
    if normalized == "failure":
        return (
            "本次骰点为 failure（失败），叙事兑现应如实呈现行动被挡"
            "回/落空/被反制——不得嘴硬圆成'虽然没有……但是……'式"
            "的伪成功；失败的代价必须可追踪，且与风险等级相称。"
        )
    # critical_failure / success_with_cost / 其它：不动
    return ""


def _option_attribute_rule(world: Mapping[str, Any]) -> str:
    """在 planning_prompt 里注入「检定属性只能用本世界属性表的稳定 ID /
    label，禁止自造」的硬约束（2026-08-24 玩家反馈「检定属性 strength
    不属于当前世界」——九州借命局世界的属性是 body/agility/sword/spell/
    insight/array/guile/presence（体魄/身法/兵道/术法/神识/阵理/机变/心辩），
    模型却在 option 的 attribute_id 里填了 DND 风的标准 key "strength"，
    导致 authoritative_modifier 找不到匹配、整轮被拒）。"""
    contract = world_contract(world)
    resolution_mode = str(contract["resolution"]["mode"])
    if resolution_mode in {"none", "narrative", "dice_only"}:
        return ""
    attributes = contract.get("attributes") or []
    if not attributes:
        return ""
    allowed_text = "、".join(
        f"{item.get('key')}（{item.get('label')}）"
        for item in attributes
        if isinstance(item, Mapping) and item.get("key")
    )
    return (
        "check.attribute_id 必须从世界属性表的稳定 key 里选，不得自造"
        "任何不在列表里的属性名（哪怕是常见叫法如「力量/敏捷/智力」的"
        "英文「strength/agility/intelligence」也不行，本世界的属性以"
        "下面为准）：\n"
        f"世界属性表：{allowed_text}。\n"
        "若选项的检定属性吃不准，宁可留空让引擎按行动自动推断，也不要"
        "填一个不在列表里的 ID 导致整轮作废。\n"
    )


def _ending_length_rule(
    story_complete: bool,
    band: str,
    objective: str = "",
) -> str:
    """渲染正文长度要求；结局点只要求一次性主线收束。

    objective：当前章节 current_objective（含世界自己声明的收尾意象），
    结局收尾时引用它避免硬编码别的主线本 NPC/事件（2026-08-23 修复）。
    """
    low, high = story_length_bounds(story_complete, band)
    # 上限 0 = 不做结构拒绝（2026-09-20：写长不判废整轮）。但 2026-09-20 玩家
    # 反馈「叙事全是无意义的琐碎信息，又装得像都有用」——所以提示词里给出
    # 软篇幅与反流水账清单：长度不拦，但明确告诉模型注水在哪、要删什么。
    if high > 0:
        length_rule = f"本回合故事正文必须为 {low}—{high} 个中文可见字符"
    else:
        length_rule = (
            f"本回合故事正文不少于 {low} 个中文可见字符；"
            "建议落在 250—700 个中文可见字符"
        )
    if story_complete:
        note = f"。收尾必须落回本章要求：{objective}" if objective else ""
        return (
            f"{length_rule}"
            "。用一个紧凑尾声写清主线核心冲突的结果、全队最终选择的"
            "直接后果，以及队伍整体离场或去向；必要的关键 NPC 只需一句"
            "概括。除非玩家明确要求，不得逐一安排每名玩家角色的私人结局，"
            f"也不要穷举所有 NPC 或支线钩子{note}。尾声后不得再抛新的"
            "待办、表决或行动选项拖延结尾。"
        )
    return (
        f"{length_rule}。"
        "篇幅由内容决定：该交代的设定、场景与动作结果要写清楚，但**一次行动"
        "只写一个拍子**——玩家做了一件事，就写这件事的结果和现场反应。\n"
        "以下都属于注水，必须删掉：出发/准备的过程流水（上嚼子、穿皮扣、"
        "点干粮、数水囊、翻检行李）；把登记簿、清单、册子逐条念出来；"
        "只为凑气氛的路人闲笔（削木楔的老头、趴着的狗、路边的野花）；"
        "已经交代过的景物与重复确认；以及角色独自把已知信息再复盘一遍。\n"
        "同一章里**已经写过的地点再次出现时，不要重走一遍路线或重新建立场景**"
        "（「从深绿木门出去、拐上青石窄街」这类路径复述，或又一次「日头已经偏西」"
        "的开场）；直接写到了之后发生了什么。最近 30 条正文里有 13 条在复述同一个"
        "住处、12 条复述同一处门口——这种路径描写不提供任何新信息。\n"
        "每个细节都必须**要么改变局面、要么给出可操作的下一步**，否则不留。"
        "一次行动里跨越多日或多站的旅途，用一到三句概括；只把真正出事的那"
        "一段写开，不要按「到达 A—问 A—到达 B—问 B」逐站直播。"
        "线索要写成**可行动的结论**（谁在威胁、缺什么、下一步去哪），"
        "不要堆一串谜语式的异常现象让玩家自己猜。"
    )


# 里程碑"达成条件属于哪一类"的关键词表。只用于生成给叙事模型的推进指令
# （信息型＝把事实送到玩家手上；行动型＝让这件事真的发生），**不参与任何
# 达成判定**——里程碑永远只由证据裁判落账。
_MILESTONE_STRONG_INFO_WORDS = (
    "获知", "得知", "查明", "了解到", "明白", "识破", "看破", "意识到",
)
_MILESTONE_INFO_WORDS = (
    "了解", "确认", "掌握", "清楚", "听到", "知道", "发现", "看到",
    "获报", "判断", "解释", "证据", "情报",
)
_MILESTONE_ACTION_WORDS = (
    "发出", "执行", "落实", "达成", "开始", "安排", "提交", "建立",
    "保护", "疏散", "送达", "会面", "结盟", "出发", "交出", "完成",
    "兑现", "救援", "接应", "撤离", "宣布", "公布", "签署", "同意",
    "拒绝", "流传", "处置", "行动", "施展", "击退", "押送",
)


def milestone_requirement_kind(label: str) -> str:
    """把里程碑达成条件粗分为 ``"info"`` / ``"action"`` / ``""``。

    2026-09-20 玩家反馈「信息获取太慢」：很多里程碑判定的是"某件事已经
    发生"（预警已发出、同盟已达成），叙事却把它当成"再多给一条线索"，
    于是玩家一轮轮打听、里程碑一动不动。分类只用来决定给模型写什么推进
    指令，不参与达成判定。
    """
    text = str(label or "")
    if not text:
        return ""
    if any(w in text for w in _MILESTONE_STRONG_INFO_WORDS):
        return "info"
    has_action = any(w in text for w in _MILESTONE_ACTION_WORDS)
    if has_action:
        return "action"
    if any(w in text for w in _MILESTONE_INFO_WORDS):
        return "info"
    return ""


def _information_delivery_rules() -> str:
    """情报交付硬规则（2026-09-20 玩家反馈「信息获取太慢」后落地）。

    提速的关键不是把答案白送给玩家，而是禁止模型把一个来源已经掌握的
    答案拆成多轮挤牙膏：一次打听必须交付可行动的完整结论，来源确实不知道
    就当场交出下一跳。这里只约束叙事写法，不改变任何达成判定。
    """
    return (
        "情报交付硬规则（2026-09-20 玩家反馈「信息获取太慢」后落地，"
        "与上面的清晰度硬约束同级，必须遵守）：\n"
        "1. 一次打听给全。玩家向可能知情的个人、机构或渠道询问时，本轮必须"
        "把该来源与这个问题有关的**全部结论**一次交付完：发生了什么、谁在"
        "背后、现在缺什么、下一步该找谁或去哪，并说明他凭什么知道。禁止"
        "只给一层，把同一来源已经掌握的另一半留到下一轮再问出来。\n"
        "2. 给结论，不给猜谜。情报要写成可直接据以行动的句子——「这半个月"
        "往北去的车队有三拨没按时到，车和地龙丢在路边，人和货没影」；不要"
        "只写「地上有奇怪的痕迹、空气里有股腥味」这类现象清单让玩家自己猜。\n"
        "3. 来源不知道就当场给下一跳。必须马上说清知情者是谁、在哪、凭什么"
        "愿意说、或需要什么凭证，并尽量当场安排引见、捎信、同行或交出文书，"
        "让玩家这一次行动就能接上；不得让玩家空手回去再开一轮打听。\n"
        "4. 禁止多跳情报链。真相确实要经手多人时，本轮行动就要能打通整条链"
        "（同行、当场引见、带信、拿到可自证的文书）；不得出现「找到人之后"
        "他也不知道」的空跑，也不得按「登门A—没结果—再登门B」逐轮推进。\n"
        "5. 已知的不重复。正文已经交付过的结论不得复述充数；玩家重复问同一"
        "件事时，本轮必须给出**更深一层**的新事实（新的名字、地点、条件、"
        "代价或时间限制），或直接把场面推到需要作决定的节点。\n"
        "6. 一次行动可同时推进多件事。玩家打听的同时顺手发出预警、捎带口信、"
        "约定会面或谈妥条件时，本轮要一并写成已经发生的事实，不得因为"
        "「本轮主题是调查」就把这些已经说出口的动作推迟到下一轮。\n"
        "7. 同处一地、能互相听见的玩家角色之间，一次明确交流（包括一句概括"
        "性的「他把刚才的见闻说了一遍」）即视为情报已共享；不必为每名玩家"
        "重演同一场打听，也不得让后来者因为「没亲自去」而一无所知。\n"
        "8. 情报可以要价（时间、人情、筹码、暴露风险），但要价不跨轮：当场"
        "谈成或当场谈崩，都要在本轮给出可行动的结论。\n"
    )


def _ending_phase_contract(active: bool, objective: str = "") -> str:
    """Authoritative output contract for the one and only ending turn."""
    if not active:
        return ""
    objective_rule = (
        f"收尾必须落实本章目标：{objective}。" if objective else ""
    )
    return (
        "<authoritative_ending_contract>"
        "引擎已进入一次性收尾阶段；本块优先于所有普通回合、终章、"
        "选项和表决规则。先结算玩家本次行动，然后在同一篇正文里直接"
        "写完紧凑结尾：明确主线核心冲突的结果、全队最终选择造成的"
        "直接后果、队伍整体离场或共同去向；必要的关键 NPC 状态各用"
        "一句概括。除非玩家本轮明确要求，不得逐一安排每名玩家角色的"
        "私人生活、情感落点或个人后日谈；不得提出是否续接下一卷，"
        "不得开启新任务、支线、旅途步骤、悬念、待办或再次表决。"
        f"{objective_rule}"
        "最终 JSON 必须是 mode=resolve、check=null、next_choices=[]、"
        "group_decision=null，且 director_note 必须精确填写"
        "story_ending_complete。尾声完成即停止，不给任何下一步行动。"
        "</authoritative_ending_contract>"
    )


def planning_prompt(
    *,
    world: Mapping[str, Any],
    session: Mapping[str, Any],
    player: Mapping[str, Any],
    player_input: str,
    events: Sequence[Mapping[str, Any]],
    memories: Sequence[Mapping[str, Any]],
    allow_checks: bool,
    workflow: Mapping[str, Any] | None = None,
) -> str:
    selected_choice = (
        dict(workflow.get("selected_choice") or {})
        if workflow
        and isinstance(workflow.get("selected_choice"), Mapping)
        else {}
    )
    choice_contract: dict[str, Any] = {}
    if workflow:
        choice_contract = {
            "choice_set_id": workflow.get("choice_set_id"),
            "selected_key": workflow.get("selected_key"),
            "requires_check": bool(workflow.get("requires_check")),
            "collective": bool(workflow.get("collective")),
            "check_type": selected_choice.get("check_type"),
            "check_stat": selected_choice.get("check_stat"),
            "difficulty": selected_choice.get("difficulty"),
            "risk": selected_choice.get("risk"),
            "known_consequences": selected_choice.get(
                "known_consequences"
            ),
            "advantage_sources": selected_choice.get(
                "advantage_sources",
                [],
            ),
            "disadvantage_sources": selected_choice.get(
                "disadvantage_sources",
                [],
            ),
        }
    if choice_contract.get("requires_check"):
        mode_rule = (
            "本条行动来自插件已锁定的必检选项，必须返回 mode=check；"
            "不得直接返回 resolve。check 阶段 narrative 留空、"
            "state_patch={}、memories=[]，且不要输出 next_choices "
            "与 group_decision；选项中已有的检定属性、难度、风险、"
            "类型、已知后果与优劣势不得弱化或改写。"
        )
    elif workflow and not workflow.get("freeform"):
        mode_rule = (
            "本条行动来自插件已锁定的免检选项，必须返回 mode=resolve；"
            "不得临时追加检定或隐藏加码。"
        )
    else:
        # 自由演绎（未选择任何 A-D 选项）也走这里：不锁定模式，
        # 由模型按行动本身的风险自行决定 mode=check 或 mode=resolve。
        mode_rule = (
            "若行动结果存在风险、对抗或显著不确定性，返回 mode=check；"
            "check 阶段 narrative 为空，state_patch 与 memories 必须为空。"
            if allow_checks
            else (
                "本轮不启用随机检定；请依据已有事实保守裁定并"
                "返回 mode=resolve。"
            )
        )
    # 接收程度必须注入到**所有**自由演绎路径。2026-09-21：这段原来写在
    # 上面的 else 分支里，但自由演绎恒有 requires_check=True（引擎强制摇点），
    # 必定走第一个分支，于是这段注入对自由演绎从来没生效过——死代码。
    if workflow and workflow.get("freeform") and workflow.get(
        "freeform_acceptance_guidance"
    ):
        mode_rule += (
            "\n<freeform_acceptance>"
            f"{workflow.get('freeform_acceptance_guidance')}"
            f"{_outcome_fulfillment_hint('')}"
            "只按接受范围内的动作裁定，越界部分（代写结果、操控他人、"
            "违背世界事实）不得兑现；即使检定或叙事结果有利，"
            "也不能把被砍掉的内容当作已发生事实。"
            "该指令对所有自由演绎都生效，包括照单全收（full）。"
            "接受程度已由插件单独公告，正文不要重复评定结论、"
            "不要写「【演绎结果评定】」之类的判定标签，直接写正文推进剧情。"
            "</freeform_acceptance>"
        )
    progress: dict[str, Any] = {}
    if isinstance(session, Mapping):
        _session_progress = session.get("progress")
        progress = (
            _session_progress
            if isinstance(_session_progress, Mapping)
            else {}
        )
    story_complete = bool(progress.get("story_complete"))
    ending_phase = bool(story_complete or progress.get("_ending_phase"))
    length_band = str(progress.get("narrative_length_band") or "compact")
    _ending_objective = str(
        progress.get("_ending_objective")
        or progress.get("current_objective")
        or ""
    )
    ending_contract = _ending_phase_contract(
        ending_phase, _ending_objective
    )
    next_choice_rule = (
        "收尾阶段不得生成任何 next_choices 或 group_decision。"
        if ending_phase
        else (
            "mode=resolve 时必须给出恰好四个 next_choices；风险梯度由当前"
            "局势决定，不强制塞入 safe 选项。选项只描述行动意图，不保证结果。"
        )
    )
    return (
        "裁定下面这一条玩家行动。"
        f"{mode_rule}\n"
        "若无需检定，直接给出 mode=resolve 的完整结果。"
        "叙事应具体、克制，并给其他玩家留下行动空间。每轮只选择一至两项"
        "与本次行动有关、且相较上轮发生变化的感官或环境细节；不要机械"
        "重复光线、温度、声响、气味和触感清单。正文还应写清在场 NPC/敌人"
        "本轮可见的反应、本次行动造成的变化，以及至少一个玩家可据以行动"
        "的具体细节。细节服务当前行动，不得为了凑描写离题扩写背景。\n"
        "清晰度硬约束（2026-08-24 玩家反馈「太谜语人 + 设定缺少介绍和铺垫」"
        "后落地，禁止以任何「神秘感」「伏笔」「克制」为名写成谜语或纯暗示）：\n"
        "1. 首次登场的 NPC 必须用一句话锚定身份 / 外貌 / 与场景的关联"
        "——不得只丢名字、称号或代称就推进动作。「铁面人」必须先写出"
        "「黑铁面具覆面、自称亡龙守护者的黑袍剑士」之类的具体定位；\n"
        "2. 首次进入的场景必须用一两句话写清空间感、当前氛围与至少一个"
        "可观察的具体物（地形、建筑特征、符号、残留物、声响、味道），"
        "不得只丢「你来到裂谷城」就让玩家脑补整个场景；\n"
        "3. 世界专有名词（组织、法阵、物品、典仪、势力口号、历史事件）"
        "首次出现时必须给出在场角色能观察到的具体线索或简短解释——可"
        "由 NPC 嘴里说出、由场景描述带出、或由玩家角色根据记忆联想到"
        "——不得让玩家看着专有名词发愣不知何意；\n"
        "4. 关键动作结果必须直接陈述（玩家成功 / 失败 / NPC 反应如何），"
        "不得用「气氛变了」「他的眼神松动」「某种东西出现了」这类模糊"
        "暗示代替明确交代。气氛与表情描写可以保留，但不能取代事实陈述。\n"
        "5. 禁止在正文里泄露判定/结果分类标签——不得出现「success 下」「"
        "success_with_cost 下」「critical_failure 下」「大成功」「大失败」"
        "「代价成功」「骰面 + 修正」「判定 = ...」「DC = ...」「骰池 "
        "... 取高/取低」「优势/劣势 抵消」等任何把插件内部机制或检定"
        "元信息照搬进玩家文面的字眼。这些是写给审计的，不是写给玩家"
        "的；正文只能写「发生了什么、谁做了什么、结果怎样」。\n"
        "6. 正文语言必须用直白白话（2026-08-24 玩家反馈「模型输出莫名"
        "奇妙的深奥难以理解」后落地）：避免诗意化、隐喻化、文言化、"
        "华丽辞藻堆砌；感官与动作尽量用「看到 X」「闻到 Y」「她后退"
        "一步、刀从腰间抽出」这种一望即知的具体陈述。允许保留必要的"
        "修辞张力（气氛、表情、声响），但优先服务事实传达——任何让"
        "读者「看不懂这句话在讲什么」的句子都得改写。\n"
        "7. 陌生物体必须先用日常名称建立画面，再补古称、专业名或世界"
        "专名。建筑、门窗、台阶、平台、通道、城墙部件与宗教设施，不得"
        "只写「丹墀、甬道、雉堞、券门、飞扶壁、须弥座」等术语让玩家"
        "猜；应写成「殿前的高台和石阶（丹墀）」「只能容两人并行的狭长"
        "通道」「城墙顶齐胸高的矮墙与射击缺口」「上端圆拱的石门洞」"
        "「支撑外墙的斜石架」「托住神像的方形石台」。若必须保留风味词，"
        "在同一句补充物体类别，并写出外形、位置或用途中的至少一项。"
        "每个新场景先让玩家分清主要空间、出口或障碍和一个可互动对象，"
        "再补纹样、材质和气氛；不得用一串名词充当空间说明。\n"
        "伏笔与揭示：关键真相、身份反转、立场摊牌可分阶段呈现，但每一"
        "阶段都必须用清晰的句子交代——「他的表情显出动摇」改为「他退了"
        "一步、拳头在袖口收紧」，「他似乎在准备什么」改为「他从腰带里"
        "抽出一柄小刀」。转折发生前应有可回看的铺垫（器物细节、反常言"
        "行、环境征兆），但不得为了保持神秘感把已经写清的事实改用模糊"
        "暗示重述，也不得为了凑伏笔拖延玩家已经合理查明的真相。当章节"
        "pacing 指令、玩家关键行动或检定结果要求转折发生时，转折照常"
        "发生，伏笔用于呼应而非延期。禁止主动揭穿由其他玩家未掌握的"
        "剧情真相（NPC 私下的对话、隐藏组织从未公开过的意图等）。\n"
        f"{_information_delivery_rules()}"
        f"{_dialogue_voice_rules()}"
        "失败也必须推进局势：可以带来代价、残缺线索、威胁时钟推进"
        "或新的选择，但关键主线线索不能因一次失败永久消失。"
        "同一个原因只能影响一次裁定：属性提供固定加值，情境提供优劣势，"
        "行动本身决定 DC，失败严重度由风险等级决定。"
        "没有风险和不确定性的动作直接成功；明确不可能的动作直接说明边界，"
        "不能用自然 20 突破世界事实。"
        "不得替玩家决定内心、未经选择的对白、情感或对其他玩家角色的伤害。"
        f"{_ending_length_rule(ending_phase, length_band, _ending_objective)}"
        f"{ending_contract}"
        "每个 next_choices.text 正文"
        "不得超过 50 字，正文禁止自带风险或检定括号。四个选项的 actor_id 必须全部等于 next_actor 的"
        " participant_id；选项只能描述该角色本人可尝试的行动，不能替其他"
        "玩家角色说话、移动、使用能力、消耗物品、同意或作决定。"
        "角色位置必须逐人记录：本轮任何玩家角色实际移动后，都必须只为"
        "实际移动者填写 location_ops，target_id 优先使用 active_party 中的"
        " participant_id；有 travel_groups 时普通移动延续原小队，不因 participants 漏人拆队；"
        "其他小队不得被带走。无持续同行记录时仅记录有现场依据的实际移动者。"
        "state_patch.location 只表示全体活跃玩家共同完成转场后的共享场景；"
        "个人行动、分头行动或部分小队移动时不得更新它。全队共同转场时，"
        "必须为每名实际同行的活跃玩家分别填写 location_ops，并可同时更新"
        " state_patch.location。若行动者的 runtime_state.current_location 为"
        "空，则本轮即使没有移动，也要按近期叙事中已确认的位置为该行动者补"
        "一条 location_ops；不得借机推断或改写其他角色的位置。"
        "participants 描述本轮参与者，不是改写持续同行小队的权威，按现场背景判断，"
        "不按句子措辞判断：先看 active_party 每人的 visible_location 与"
        " recent_history 里谁和谁同行、有没有分开行动——地名写法不同但指"
        "同一处算在一起，已经分开行动的人即使位置字段相同也不算在一起。"
        "再判断这条行动是只有行动者一个人做（只填他自己）、还是在场这几个"
        "人默认一起做（一起赶路、进门、落座、共同听讲、集体休整——把同行者"
        "都填进来），或是需要他们同意才能替他们决定（不可逆、有代价、拿别人"
        "的安危或资源去赌——这种情况必须走 group_decision，不得用"
        " participants 代替同意）。同一句「我们走」：目的地是全队已定目标、"
        "或行动者正领着大家做上一轮已在做的事 → 把同行者填进去；是行动者"
        "单方面把别人带去陌生危险 → 走 group_decision；只有他一个人动 → "
        "只填他自己。但个人发言不意味着脱离同行小队；移动按持续 travel_groups 处理。名单只影响这段叙事里谁跟着做"
        "了这件事、以及谁的位置随之改变，不改变行动顺序：被带上的角色轮到"
        "他时照常行动。因此不得为了省事把不在场或已分头行动的人填进来。"
        "新 NPC 必须有名字，并至少满足直接互动、掌握重要线索或写入长期记忆"
        "之一；create 时用 registration_reasons 的固定值说明依据。"
        "已登记 NPC 必须使用 active_npcs 中的稳定 npc_id 更新，"
        "不能凭相似名称静默合并。"
        f"{next_choice_rule}"
        "每个 next_choices 的 check 配置：safe 选项 check.required 必须为"
        " false（check 为 null）；controlled/dangerous/desperate/lethal "
        "选项若结果确有不确定性，check.required 必须为 true，并填写"
        " attribute_id、type 与 known_consequences。\n"
        f"{_option_attribute_rule(world)}"
        "只有存在真实阻力、结果不确定且失败会改变局势"
        "时才配置检定；例行交谈、出示既有证据、无阻碍旅行和确定成功的"
        "动作无需检定。战斗、潜行或正在运作的危险机关不得漏配检定。"
        "已成立的优劣势必须写入 check.advantage_sources / "
        "disadvantage_sources：角色 runtime_state.statuses 中带劣势/增益的"
        "状态（如灼伤、暴露、跟踪）、掩护/伏击/地形/协助加成等；"
        "有劣势状态却漏填会让检定失真。"
        "禁止零行动选项：每个 next_choices 都必须能改变局面、产出新信息"
        "或明确下一步。针对新问题的调查、验证新证据、制定并立即执行撤离"
        "或旅行计划都属于有效行动；只有重复检查已确认事实、纯盘点、原地"
        "等待或整理思绪等不产生任何进展的内容才算零行动。"
        "状态标记：角色获得/失去持续的伤势、暴露、跟踪、禁制等状态时，"
        "必须用 status_ops.add/remove 记录；移除时 name 复用 <party> 或"
        " runtime_state.statuses 中现存的名字（或取其前缀），并在该状态的"
        " removal 条件达成时立刻移除，不要遗留过期状态。"
        "治疗/净化/驱散类行动：彻底解除 → status_ops.remove（name 复用存量"
        "状态名或其前缀，本轮立即删除）；仅减轻 → status_ops.update（复用"
        "存量 name，severity 下调或保持、effect 写减轻后的描述）。禁止用"
        " status_ops.add 表达『治疗/减轻』——治疗绝不产生新状态、绝不让"
        "状态名或 severity 因治疗而更重；同一角色的同一类状态（如灼伤）"
        "同一时刻只能存在一条，反复治疗只应 update 或 remove 它。检定为"
        "success、critical_success 或 success_with_cost 时，玩家明确要求治愈/"
        "拔除/净化的普通状态必须 remove；success_with_cost 的代价放在资源、"
        "暴露或新局势上，不能把目标伤势继续保留。只有 runtime_state 中已由"
        "剧本明确以 policy_source=world 标记 healing_policy=story_locked/"
        "permanent 或"
        " removable_by_healing=false 的状态才可拒绝普通治疗；不得临时编造"
        "主线条件把普通 debuff 锁死。普通debuff默认有可及的治疗、净化、急救或休息路径，"
        "不频繁设计稀有材料、多次仪式或主线任务作为解除门槛。治疗必须明确对象与所治状态；"
        "只提出治疗意愿、询问方法或尚未执行，不能记作治疗成功。"
        "若局面涉及全队转场、主线分支、共有资源、不可逆契约、"
        "全队撤退或返场支线，必须填写 group_decision，"
        "不要让个人选项直接替全队决定。生成 group_decision 时，"
        "state_patch 不得提前写入尚未表决通过的集体结果。"
        "若 <pacing_directive_text> 含本章 pacing_directive，在不跳过本轮行动与现场连续性的前提下遵守。"
        "若 <current_chapter> 中存在 status='pending' 的里程碑，保留当前"
        "章节进度，但章节不是地点锁：玩家可前往、折返或探索任何世界内"
        "合理可达的地点；必须承接其旅行和目的地当前局势，不得以章节为由"
        "封路、强制传送、否定行动或强迫折返。玩家提前到达后续地点时，不得"
        "仅凭地点提前泄露或宣告完成尚无行动证据的后章内容；主线只通过世界"
        "后果和非强制选项自然提示。\n"
        "里程碑是本章结束前的方向清单，不是本轮目标：剧情重心是展开当前"
        "场景、回应玩家行动、给下一位玩家留下可行动的局面；不要为完成"
        "里程碑而催赶剧情。只有当本轮玩家行动亲手完成了里程碑描述的"
        "行为（亲手取得信物、亲手开启大门等）时，才可用 ledger_ops.create"
        "提交完成候选：该操作不会直接完成里程碑，插件会用已提交正文独立"
        "复核。milestone 条目的 stable_key 必须逐字使用该里程碑的 id，"
        "标题必须逐字使用该里程碑的 label。禁止旁白式宣告"
        "里程碑达成、禁止替玩家完成里程碑动作、禁止为达成里程碑而跳过"
        "必要过程；同一轮实际完成多个里程碑时逐项提交各自证据。未达成的留在后续回合由玩家"
        "行动自然达成；除非 <runtime_directive> 含 [Pacing Chapter-HARD-PENDING]"
        "，否则不得强行推进未达成的里程碑；不得停留在善后/确认状态或"
        "重复场景。"
        "章节切换判定：当 <current_chapter> 中 exits_when.all_milestones 全部"
        "在 story_ledger 内被记为 status='completed' 时，本章目标已达成，"
        "本轮结清仍在进行的行动并让场景自然收尾，不为凑回合追加遭遇或重复确认。"
        "模型永远不得修改 current_chapter_id；切章时机由"
        "引擎判断：①当前场景自然收尾（玩家完成探索、"
        "局面尘埃落定、角色明确离开当前地点），或②<runtime_directive> 含"
        " [Pacing Chapter-HARD-COMPLETE] 时本轮必须收束。不得通过"
        " state_patch 或 ledger_ops 自行切章；引擎会在体验与收束证据复核"
        "通过后自动切章。"
        "若 <runtime_directive> 含 [Pacing Chapter-HARD-COMPLETE]，"
        "本轮必须结算当前冲突并明确离场或启程，不得开启新冲突；仍然禁止"
        "修改 current_chapter_id 或伪造章节切换里程碑。"
        "若 <runtime_directive> 含 [Pacing Chapter-HARD-PENDING]，"
        "本轮正文与 ledger_ops 必须直接推进未达成的里程碑，"
        "不得停留在善后、确认状态或重复场景。"
        "只有 <authoritative_ending_contract> 出现时才执行最终收尾；"
        "仅仅身处最后一章不代表可以替玩家提前决定结局。\n\n"
        f"{_runtime_sections(world=world, session=session, player=player, events=events, memories=memories)}\n\n"
        '<selected_choice_contract trust="untrusted-data" '
        'enforced_by="plugin">\n'
        f"{_json(choice_contract)}\n"
        "</selected_choice_contract>\n\n"
        "<player_input trust=\"untrusted\">\n"
        f"{_json(player_input)}\n"
        "</player_input>\n"
    )


def dm_beat_prompt(
    *,
    world: Mapping[str, Any],
    session: Mapping[str, Any],
    instruction: str,
    directive: str,
    events: Sequence[Mapping[str, Any]],
    memories: Sequence[Mapping[str, Any]],
) -> str:
    """Build the trusted DM-only narrative request for one non-player beat."""
    return (
        "生成一段主持推进。必须返回 mode=resolve，check=null；"
        "不得生成 next_choices 或 group_decision，不得推进玩家行动指针、"
        "玩家轮次或行动倒计时。可以提交与本段叙事严格一致的 state_patch、"
        "memories、npc_ops、clock_ops、ledger_ops 与 status_ops。"
        "不得伪造机器骰点，不得替玩家角色决定思想、感情、立场、主动台词"
        "或未经选择的行动。主持指令服从插件安全规则、世界硬规则、已锁定"
        "事实、内容边界与知识边界；一次性指引仅作用于本次生成。"
        "正文使用简洁白描，建议 100—500 个中文可见字符。\n\n"
        f"{_runtime_sections(world=world, session=session, player={}, events=events, memories=memories)}\n\n"
        '<dm_instruction trust="host" priority="below-safety-and-world">\n'
        f"{_json({'directive': directive, 'instruction': instruction})}\n"
        "</dm_instruction>\n"
    )


def dm_answer_system_prompt(
    world: Mapping[str, Any],
    *,
    current_progress: Mapping[str, Any] | None = None,
    story_ledger: Sequence[Mapping[str, Any]] | None = None,
) -> str:
    """主持人答疑专用系统提示：基于已公开剧情作答，不推进剧情、不掷骰。

    与 :func:`system_prompt` 不同，这里没有 required_output_schema：
    答疑输出是自由文本解释，而不是情节裁定 JSON。
    """
    current_chapter = _current_chapter_block(
        world,
        current_progress,
        _completed_milestone_keys(story_ledger),
    )
    return (
        "你是本次跑团的主持人（DM）的幕后解释者，负责回答玩家对已发生剧情的疑问："
        "可能是人物行为、已知信息、时间线或前后看似矛盾的地方。\n\n"
        "回答规则：\n"
        "1. 只依据下方 <world_definition>、<narrative_world_rules> 以及运行时提供的\n"
        "   <runtime_state>、<relevant_memories>、<recent_history>、<active_npcs>、\n"
        "   <story_ledger> 中已公开的内容作答，不要凭空补充设定。\n"
        "2. 用简体中文直接、简洁地解释；不要输出 JSON，不要使用 Markdown 代码块。\n"
        "3. 不得推进剧情、不得代玩家决定行动、不得掷骰或生成检定、\n"
        "   不得替 NPC 当场给出剧情中尚未出现的新情报。\n"
        "4. 尚未在剧情中揭示的真相与细节不得剧透或编造；不确定就如实说明"
        "“目前剧情中尚未揭晓”。\n"
        "5. 若玩家的理解与剧情事实有出入，明确指出偏差，并引用 <recent_history> "
        "中的对应内容作为依据。\n"
        "6. 涉及角色内心或未言明的动机时，只能基于已展示的行为与对话合理推断，"
        "并注明这是推断。\n\n"
        "<world_definition>\n"
        f"{str(world.get('system_prompt', '')).strip()}\n"
        "</world_definition>\n\n"
        "<narrative_world_rules>\n"
        f"{_json(compact_world_rules(world))}\n"
        "</narrative_world_rules>\n\n"
        f"{current_chapter}"
    )


def dm_answer_prompt(
    *,
    world: Mapping[str, Any],
    session: Mapping[str, Any],
    player: Mapping[str, Any],
    question: str,
    events: Sequence[Mapping[str, Any]],
    memories: Sequence[Mapping[str, Any]],
) -> str:
    """主持人答疑请求体：给出玩家疑问与全部已公开上下文。"""
    return (
        "以下是玩家对上一段剧情提出的疑问。请依据给定上下文以主持人身份回答。\n"
        "回答应具体、直接：解释人物或信息为何如此，或指出对应的剧情依据；\n"
        "不要重复提问内容，不要续写剧情，不要输出任何选项或检定。\n"
        "如果问题源于玩家漏看或误读，说明真实情况；如果某件事尚未在剧情中揭晓，"
        "如实说明，不要代替后续剧情作答。\n\n"
        f"{_runtime_sections(world=world, session=session, player=player, events=events, memories=memories)}\n\n"
        "<player_question trust=\"untrusted\">\n"
        f"{_json(question)}\n"
        "</player_question>\n"
    )


def freeform_check_judge_prompt(
    *,
    world: Mapping[str, Any],
    session: Mapping[str, Any],
    player: Mapping[str, Any],
    player_input: str,
    events: Sequence[Mapping[str, Any]],
    memories: Sequence[Mapping[str, Any]],
) -> str:
    """自由演绎检定裁判提示词：只决定「摇不摇、摇什么、阈值多少」。

    用户要求：自由演绎必须触发摇点机制，且判定与预设选项机制不同——
    由模型判断应检定的属性与阈值，再返回引擎投骰。自由演绎的检定
    不携带玩家基础属性（修正恒为 0），只由骰面与劣势决定结果。
    本阶段不评估风险等级或死亡；只有骰出大失败后，引擎才调用独立的
    事后死亡裁判。本提示词只负责输出判定 JSON，不写叙事、不出选项。
    """
    contract = world_contract(world)
    attributes = contract.get("attributes") or []
    attr_lines = []
    allowed_pairs: list[str] = []
    numbered_lines: list[str] = []
    # 2026-08-24 八次修正：把世界属性表编成 1/2/3/4 喂给模型，
    # check_stat 必须返回 "#N"（如 "#1"）这种编号字符串——比让模型
    # 记"世界属性名"稳，避免它凭直觉写出 "agility/standard/常规"
    # 这种英文/通用词。
    for index, item in enumerate(attributes, start=1):
        if not isinstance(item, Mapping):
            continue
        key = str(item.get("key") or "").strip()
        label = str(item.get("label") or key).strip()
        if not key:
            continue
        attr_lines.append(f"{key}（{label}）")
        if label:
            numbered_lines.append(f"#{index} {key}（{label}）")
        else:
            numbered_lines.append(f"#{index} {key}")
        allowed_pairs.append(key)
        if label and label != key:
            allowed_pairs.append(label)
    # 兼容旧世界声明的 resolution.allowed_attributes（白名单 key）
    legacy_allowed = (
        contract.get("resolution", {}).get("allowed_attributes") or []
    )
    for item in legacy_allowed:
        text = str(item).strip()
        if text and text not in allowed_pairs:
            allowed_pairs.append(text)
    if numbered_lines:
        # 世界属性表非空时强制按编号选
        attribute_block = "\n".join(numbered_lines)
        check_stat_rule = (
            "2. check_stat：必须从下面世界属性表里选——按编号返回字符串"
            "「#N」（N 是下面对应的序号），例如「#1」表示第 1 项，"
            "「#3」表示第 3 项。不得自造——不得返回任何 key 名字、label "
            "名字或英文/通用词（agility、standard、常规等）；返回非法值"
            "（例如 #0、超出范围的 #N）引擎会直接走兜底按角色最高属性"
            "检定、公告显示「【通用检定】」，意味着你这次判定没派上用"
            f"场。\n世界属性表（编号）：\n{attribute_block}\n"
        )
    else:
        # 世界未声明属性表——引擎兜底按角色最高属性检定，公告显示
        # 「【通用检定】」。这条 path 模型返回任何 check_stat 都会被
        # 视为非法并走兜底，模型可省略 check_stat 或填空串。
        check_stat_rule = (
            "2. check_stat：世界未声明属性表，本轮无论填什么都会被引擎"
            "判定为非法、走「角色最高属性兜底」+「通用检定」公告。可以"
            "省略该字段或填空串。\n"
        )
    return (
        "你是一名规则裁判，只做一件事：判断下面这条自由演绎行动是否需要"
        "投骰检定，以及检定哪个属性、阈值（DC）多少。\n"
        "你不是叙事者：不写剧情、不写结局、不生成选项、不替玩家决定成败。\n\n"
        "判定规则：\n"
        "1. 只要行动存在结果的不确定性（战斗、潜行、攀爬、追逃、硬闯、"
        "施法、开锁、说服、探查机关、跳跃、闪避等），必须 should_roll=true；"
        "只有纯粹的闲聊寒暄、无风险观察、已知事实陈述这类零风险行动才允许"
        "should_roll=false。拿不准时一律 should_roll=true。\n"
        f"{check_stat_rule}"
        "3. difficulty：与行动实际难度匹配的 DC——轻松 8-10、常规 12-14、"
        "困难 16-18、极难 20；DC 不得超过 20（高于 20 视为不可能，引擎会"
        "clamp 到 20）。给极难 18-20 之外的情况，需要在 reason 字段解释为"
        "什么需要更高难度——基本都需要把演绎拆细或重新评估。\n"
        "4. 本阶段禁止评估 risk、lethal 或 fatal_consequence。自由演绎的"
        "死亡只在骰出 critical_failure 后，由另一个裁判结合已经发生的大"
        "失败与现场事实事后判断；不要预判、保护或强行安排死亡。\n"
        "5. 本检定不携带玩家基础属性：引擎投骰时修正恒为 0，只看骰面与"
        "劣势/负面状态。因此你只需要给出检定维度的名字与 DC，不需要参考"
        "角色属性值，也不要因此降低 DC 或拒绝检定。\n"
        "6. 不要用宽松 DC 保护玩家，也不要为了制造死亡抬高 DC；这里只按"
        "动作本身的不确定性决定属性与难度。\n"
        "7. 同时判定这条演绎的「接收程度」acceptance——玩家在自由演绎里"
        "经常把动作和结果混在一起写，你要把两者分开：\n"
        "   - full（照单全收）：内容是能力与世界逻辑内的『尝试』（如"
        "『我爬上崖壁寻找裂缝』），全部尊重，正常检定；\n"
        "   - partial（部分接受）：内容夹带了越界成分——代写结果（『我一剑"
        "斩下龙头』『我说服了他』『我毫发无伤地躲过攻击』）、替其他角色/"
        "玩家做决定、使用角色根本没有的能力或物品、与已揭示事实冲突——"
        "只接受其中合理的动作部分作为尝试，越界部分在 acceptance_note "
        "里写明接受什么、砍掉什么；\n"
        "   - reduced（降格接受）：动作本身明显离谱或不可能（一拳碎城门、"
        "瞬移、当场说降国王、召唤陨石），把它降格为能力范围内最接近的"
        "合理尝试（如『一拳碎城门』→『一拳砸向城门』），或在 "
        "acceptance_note 里说明该动作不可行、只能作为无效尝试；"
        "不能被自然 20 突破世界事实。\n"
        "   原则：玩家的演绎意图、风格、角色个性必须被尊重，判定针对的是"
        "『动作』不是『结果』——结果只能由检定与叙事兑现。"
        "但不要把执行既有决定误判成代写结果：若 recent history、"
        "runtime state 或正式集体表决已经确认了目的地、交易、撤离路线或"
        "终局方案，玩家说『前往』『继续』『完成交接』『执行方案』是在"
        "执行已经作出的决定，应把这整段行动意图按 full 接受；只有其中"
        "夹带新的未决选择、不可能效果或替他人作出新决定时，才裁掉对应"
        "部分。不得仅因行动包含完整转场、交接或收尾，就以『需要后续逐步"
        "推进』『单次动作不能涵盖』为由降成 partial。是否成功仍由真实"
        "阻力和检定结果决定；没有现实阻力的既定流程不得凭空制造检定。"
        "空间、沟通和同行条件也在这里通过接收程度处理，不交给后置叙事校验作废整轮："
        "人物不同场且没有已成立的远程能力或渠道时，接受求助/治疗/会合意图，"
        "不接受对方已听见、已施法、已治好或已经会合的结果，通常判partial；"
        "连可执行的联系或移动方式都没有时判reduced，说明本轮尝试未能落实及原因。"
        "若已有渠道、同场证据或普通可达路线，不要凭地点文字不同增加障碍；"
        "合理赶路并会合可在一轮完成，不强制拆成抵达、确认、会合三轮。"
        "acceptance_note 写清接受的动作、暂不成立的效果、当前可执行的部分；"
        "后续正文兑现这个范围，不编造传送、联络渠道或替另一玩家同意治疗。"
        "不可行尝试也应正常结束本轮，不要求为推进而强行成功。"
        "acceptance_note 必填（2026-08-24 玩家反馈「自由演绎有的时候叙事"
        "模型又不给说明了」）：无论 full/partial/reduced 都必须用一句话说明"
        "接受什么、砍掉或降格什么以及原因。full 时简要写出接受了哪些"
        "行动及依据（避免一句话了事），让玩家知道这条演绎被怎样处理；"
        "partial/reduced 时更要把砍掉/降格的部分具体列出。\n\n"
        "只输出一个 JSON 对象，不要输出任何其他文字：\n"
        "{\n"
        '  "should_roll": true,\n'
        '  "check_stat": "#1",\n'
        '  "difficulty": 14,\n'
        '  "acceptance": "partial",\n'
        '  "acceptance_note": "接受「冲向亡龙挥剑」，不接受「一剑斩下龙头」'
        '——那是结果不是动作",\n'
        '  "reason": "一句话说明为什么存在不确定性"\n'
        "}\n\n"
        f"{_runtime_sections(world=world, session=session, player=player, events=events, memories=memories)}\n\n"
        "<player_freeform_action trust=\"untrusted\">\n"
        f"{_json(player_input)}\n"
        "</player_freeform_action>\n"
    )


def freeform_death_judge_prompt(
    *,
    world: Mapping[str, Any],
    session: Mapping[str, Any],
    player: Mapping[str, Any],
    player_input: str,
    events: Sequence[Mapping[str, Any]],
    memories: Sequence[Mapping[str, Any]],
    check: Mapping[str, Any],
    dice: Mapping[str, Any],
) -> str:
    """Judge death causality after a freeform critical failure."""

    return (
        "你是自由演绎大失败后的死亡裁判。检定结果已经权威确定为 "
        "critical_failure；你只判断这个已经发生的大失败，能否依据当前现场"
        "事实自然、完整地导向行动角色死亡。\n\n"
        "裁定规则：\n"
        "1. 只能使用玩家本次实际尝试、当前地点与地形、已经在场且已建立"
        "能力的敌人/NPC、角色已有伤势状态和最近剧情中已确认的威胁。\n"
        "2. 不得新增敌人、伏兵、灾害、疾病、机关、武器能力、场外袭击或"
        "巧合；不得把普通失误夸张成无来源的突然死亡。\n"
        "3. 若从『动作如何失败』到『伤害如何发生』再到『为何足以致死』"
        "能够形成连续、具体、无需补造事实的因果链，death=true，并在 "
        "fatal_consequence 中用一句话写清该链。\n"
        "4. 只要中间需要补造关键危险、现有威胁强度不足，或场景本身无法"
        "承载死亡，就必须 death=false；fatal_consequence 留空，reason "
        "说明为什么最多只能重伤、被俘、暴露、失去资源或彻底失败。\n"
        "5. 不考虑事前 risk 标签，也不因为玩家主动求死或骰出自然 1 就偏向"
        "死亡；只做事后因果判断。\n\n"
        "只输出一个 JSON 对象，不要输出其他文字：\n"
        '{"death":false,"fatal_consequence":"",'
        '"reason":"现场没有足以致死的既有危险，最多造成重伤"}\n\n'
        f"{_runtime_sections(world=world, session=session, player=player, events=events, memories=memories)}\n\n"
        '<player_freeform_action trust="untrusted">\n'
        f"{_json(player_input)}\n"
        "</player_freeform_action>\n\n"
        "<authoritative_check_result>\n"
        f"{_json({'check': dict(check), 'dice': dict(dice)})}\n"
        "</authoritative_check_result>\n"
    )


def milestone_judge_prompt(
    *,
    world: Mapping[str, Any],
    session: Mapping[str, Any],
    chapter: Mapping[str, Any],
    milestones: Sequence[Mapping[str, Any]],
    events: Sequence[Mapping[str, Any]],
) -> str:
    """章节里程碑达成裁判提示词：让模型判定剩余里程碑哪些已被故事兑现。

    用户 2026-08-23 反馈「不要靠正则，没有用的」：此前引擎用关键词子串
    匹配 story_ledger 线索标题来判断里程碑，导致故事明明完结（裂缝闭合、
    裁决落定）但账本没记录、章节/结局永远不触发。这里改为模型裁判——按
    最近叙事严格判定每个待达成里程碑是否真的被兑现，引擎按判定落账。
    裁判只输出判定 JSON，不写叙事、不出选项、不编造已发生的事。
    """
    raw = world.get("rules", {}) if isinstance(world, Mapping) else {}
    raw = raw if isinstance(raw, Mapping) else {}
    world_name = clean_text(str(world.get("name") or ""), max_chars=80)
    chapter_title = clean_text(
        str(chapter.get("title") or ""), max_chars=80
    )
    objective = clean_text(
        str(chapter.get("current_objective") or ""), max_chars=400
    )
    lines: list[str] = []
    for m in milestones:
        if not isinstance(m, Mapping) or not m.get("id"):
            continue
        mid = str(m["id"])
        label = str(m.get("label") or m.get("title") or "")
        words: list[str] = []
        for er in (m.get("evidence_required") or []):
            if isinstance(er, Mapping):
                _mw = er.get("match")
                if isinstance(_mw, list):
                    words.extend(str(x) for x in _mw if x)
        hint = "、".join(words)
        lines.append(
            f"- {mid}｜{label}"
            + (f"（达成信号参考：{hint}）" if hint else "")
        )
    milestone_block = "\n".join(lines) or "（无待判定里程碑）"
    return (
        "你是章节里程碑裁判，只做一件事：判断下面这些里程碑在最近的故事里"
        "是否已经被真正兑现。\n"
        "你不是叙事者：不写剧情、不生成结局、不替故事编造已发生的事件。\n\n"
        f"世界：{world_name}｜当前章节：{chapter_title}\n"
        f"章节目标：{objective}\n\n"
        f"待判定的里程碑：\n{milestone_block}\n\n"
        "判定规则：\n"
        "1. achieved=true 的唯一标准：最近的叙事里明确出现了该里程碑的兑现"
        "场景——事件被实际完成，不是口头承诺、不是计划、不是接近而未成、"
        "不是只提了一嘴。\n"
        "2. 严格保守：证据不足、只有铺垫或计划、或叙事自相矛盾时，判 "
        "achieved=false。宁可少判一个，也不要提前宣告完成、跳过应有的战斗"
        "或抉择。\n"
        "3. 「达成信号参考」只是作者给的外观提示，不是硬条件：叙事用同义"
        "表达真实完成了同一件事也算达成（如信号写「裂缝闭合」，正文写"
        "『死潮退散、裂口弥合』同样算）；反之只出现信号词但事情并未实际"
        "完成，不算达成。\n"
        "4. 终章/结局收尾时，如果故事已经走到自然收尾而里程碑还没全部达成，"
        "请按叙事如实判定——已兑现的判达成，不要因为『想让它结束』就多判，"
        "也不要因为『还没写完整』就漏判。\n"
        "5. （2026-08-24 玩家反馈「里程碑触发太早，章节根本没满足对应条件」"
        "后落地）以下叙事字眼一律不算兑现证据，不得据此判 achieved=true：\n"
        "   - 「可推知 / 推测 / 似乎 / 可能 / 看起来 / 认为 / 觉得 / 应该 / "
        "大概」——这些是推断与猜测，不是事实陈述。\n"
        "   - 「计划 / 准备 / 打算 / 即将 / 快要」——这些都是未发生。\n"
        "   - 「差不多 / 接近 / 即将完成 / 大致」——模糊近似也算没完成。\n"
        "   「看到 / 确认 / 拿出 / 拿到 / 取到 / 带回 / 杀死 / 治愈 / 打开 / "
        "关闭 / 识破 / 记录在案 / 写进账本」这类直接动作或在场物证是最典型的"
        "证据，但它们**只是举例，不是白名单**。由他人执行、由规则或程序确认、"
        "或正文明确写出的既成结果同样构成证据，例如「记档」「按例」「名册上"
        "有你」「手铐解开」「放回去」「费用由内廷出」「议定/通过/驳回/放行」"
        "「人已押在廊下」等。判定看的是**这件事有没有在正文里实际发生**，"
        "不是看它用了哪个动词。\n"
        "   反过来，上列推断词只要出现在引用句里就必须改判 "
        "achieved=false，哪怕你觉得「差不多够了」。\n"
        "6. 每条里程碑必须返回 criteria 数组。把里程碑 label 的全部必要条件"
        "拆成可核验的子条件，各项可引用不同回合，不要求同一句话证明全部条件。"
        "分头调查可累计各人的证据，带代价成功仍须承认已实际获得的成果；"
        "但不得漏掉 label 明确要求的人数、对象或结果。只有所有必要条件都已在正文中"
        "完成才可 achieved=true。每项 passed=true 都必须填写真实 event_id"
        "和该事件 content 中连续出现的逐字 quote。quote 不得改写、概括、"
        "拼接多处文字；拿不出正文原句就必须 passed=false。\n"
        "   （2026-09-20 补充）label 里写着「已发生的结果直接计入」「不要求"
        "重新…」的，按这个口径判：把正文里**已经落地**的那些结果算数，"
        "不要因为「没有单独为它演一场」而判否。\n"
        "7. （同上反馈）禁止「顺手给齐」——章末/章初/章节即将结束时不得"
        "为推动节奏批量给出 achieved=true；宁可整章都判 false 也不"
        "要因「都差最后一步了」而放水。\n"
        "8. （2026-09-20 落地）输出必须紧凑：criteria 只列 label 真正要求的"
        "必要条件（每条约 1—3 项，不要展开解释）；quote 只取 8—40 字连续原文，"
        "不得抄整段；reason 一句话、不超过 40 字；不要输出任何 JSON 之外的"
        "文字。输出被截断会导致整批判定作废。\n\n"
        "只输出一个 JSON 对象，不要输出任何其他文字：\n"
        "{\n"
        '  "milestones": [\n'
        '    {"id": "m_04_02_rift_anchor_felled", "achieved": true, '
        '"criteria":[{"name":"砍断锚链并使裂缝闭合",'
        '"passed":true,"event_id":"event_abc",'
        '"quote":"锚链应声断裂，裂缝随死潮一同闭合"}],'
        '"reason": "引用事件直接记录了完成结果"},\n'
        '    {"id": "m_04_03_verdict_made", "achieved": false, '
        '"criteria":[{"name":"作出并执行裁决",'
        '"passed":false,"event_id":"","quote":""}],'
        '"reason": "裁决仍在商议"}\n'
        "  ]\n"
        "}\n\n"
        f"<recent_story_events>\n{_json(_history(events))}\n</recent_story_events>\n"
    )


def chapter_closure_prompt(
    *,
    world: Mapping[str, Any],
    chapter: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
    terminal: bool,
) -> str:
    """Require evidence that a completed chapter has actually reached closure."""
    chapter_contract = {
        "id": chapter.get("id"),
        "milestones": chapter.get("milestones") or [],
        "exits_when": chapter.get("exits_when") or {},
        "pacing_directive": chapter.get("pacing_directive") or "",
    }
    rules = world.get("rules") or {}
    chapters = (rules.get("progress") or {}).get("chapters") or []
    successor = next((c for c in chapters if isinstance(c, Mapping)
                      and c.get("id") == chapter.get("next_chapter_id")), {})
    successor_scope = {
        "id": successor.get("id"),
        "current_objective": successor.get("current_objective"),
        "milestones": successor.get("milestones") or [],
    }
    title = clean_text(str(chapter.get("title") or ""), max_chars=100)
    objective = clean_text(
        str(chapter.get("current_objective") or ""), max_chars=400
    )
    if terminal:
        requirement = (
            "这是终章。closed=true 仅当最终方案已经实际执行，且正文已经交代"
            "主线结果、直接后果和队伍整体去向；必要 NPC 用一句概括，"
            "不要求逐个玩家私人结局。仅完成表决、开始仪式、"
            "宣称即将落幕或停在余波画面都不算。"
        )
    else:
        requirement = (
            "只判断本章目标，不得把后续章节的新冲突当成本章未结束。"
            "以本章目标、里程碑和退出条件确定完成边界，不添加统一的离场或全员会合门槛。"
            "若本章目标就是取得可靠路线、找到人物或建立联系，实际得到这些结果即可"
            "证明该阶段收束；不要求随后交易、战斗或整段主线也结束。若目标要求实际"
            "到达、交付或脱险，则仅知道路线、提出方案仍不算。"
            "依据下一章目标辨别后续事件：人物已获准进入交涉场所、已开始下一阶段"
            "活动，是上一阶段结束的证据；后续谈判未谈成不能阻挡寻路章节完成。"
            "不要求每个队员重复敲门、自我介绍或走完同一段路。"
            "只有仍属于本章目标的未结算冲突或待执行决定才能阻止收束。"
            "已完成的历史阶段不会因后来返回旧地点、队员暂留门外或新冲突出现而失效。"
        )
    return (
        "你是章节收束裁判，不写剧情，只判断当前章节是否已经在已提交正文中"
        "自然收束。严格保守，不能为推进节奏补全事实。\n\n"
        f"章节：{title}\n目标：{objective}\n"
        f"<chapter_completion_contract>\n{_json(chapter_contract)}\n</chapter_completion_contract>\n"
        f"<next_chapter_scope>\n{_json(successor_scope)}\n</next_chapter_scope>\n"
        f"{requirement}\n"
        "若 closed=true，event_id 必须来自下方事件，quote 必须是该事件"
        "content 中连续出现的逐字原句，且原句本身直接证明收束；不得改写、"
        "概括或引用计划性文字。否则 closed=false。\n\n"
        "只输出 JSON："
        '{"closed":true,"event_id":"event_xxx",'
        '"quote":"队伍带着证据离开已经平息的问剑台",'
        '"reason":"核心冲突已结算且队伍明确离场"}\n\n'
        f"<chapter_events>\n{_json(_history(events))}\n</chapter_events>\n"
    )


def _lethal_death_directive(
    world: Mapping[str, Any],
    check: Mapping[str, Any],
    dice: Mapping[str, Any],
) -> str:
    """致命失败（lethal + failure/critical_failure）的死亡硬约束。

    用户反馈「几次致死风险判定输了却一直没死」：模型（doubao）常在 lethal
    失败后用「重伤但无碍/奇迹生还/被及时救回」糊弄。此前权威裁定 prompt 只写
    「允许死亡」，软约束模型不照做。这里读取世界死亡策略
    （content_boundaries.character_death 与 death_requires_confirmation），
    把「允许」升级为「必须」：
    - 世界允许死亡且无需二次确认 → lethal 失败必须当场兑现死亡/永久退场；
    - 世界禁止死亡或需二次确认 → 落实最接近死亡的永久重创，保留致死机会；
    - 非 lethal 或检定成功 → 维持原措辞，防误杀。
    注意：引擎 `_lethal_death_verdict` 会对命中策略的角色做硬落实（退场 +
    死亡宣告），本段只是叙事侧配合。
    """
    raw = world.get("rules", {}) if isinstance(world, Mapping) else {}
    raw = raw if isinstance(raw, Mapping) else {}
    boundaries = raw.get("content_boundaries") or {}
    boundaries = boundaries if isinstance(boundaries, Mapping) else {}
    character_death = str(
        boundaries.get("character_death") or ""
    ).strip().lower()
    requires_confirmation = bool(raw.get("death_requires_confirmation", False))
    risk = str(check.get("risk") or "").lower()
    outcome = str(dice.get("outcome") or "").lower()
    failed = outcome in {"failure", "critical_failure"}
    if risk != "lethal" or not failed:
        # 非致命 / 检定成功（含 success_with_cost）：维持防误杀措辞
        return (
            "critical_failure 不等于角色突然变蠢，也不能仅凭自然 1 直接杀死"
            "角色。只有世界允许死亡、选项已明确标记 lethal 且结果成立时，"
            "才允许产生死亡或永久退场。"
        )
    if character_death == "no" or requires_confirmation:
        return (
            "本检定是 lethal 且失败。但本世界死亡策略为"
            f"character_death={character_death or 'yes'}"
            f"{'、死亡需二次确认' if requires_confirmation else ''}："
            "不得当场判死，改为落实最接近死亡的永久重创/被俘，并保留后续"
            "致死机会。"
        )
    return (
        "本检定是 lethal 且失败：本世界允许角色死亡且无需二次确认，"
        "必须当场兑现致死后果——该角色死亡或永久退场（被碾碎/枭首/冻裂/"
        "吞噬等，按 known_consequences 兑现）。不得用「重伤但无碍」「奇迹"
        "生还」「被及时救回」「只是晕倒」糊弄 lethal 失败；只有该 lethal "
        "行动被明确抵抗、结果未成立时才豁免。"
    )


def checked_resolution_prompt(
    *,
    world: Mapping[str, Any],
    session: Mapping[str, Any],
    player: Mapping[str, Any],
    player_input: str,
    events: Sequence[Mapping[str, Any]],
    memories: Sequence[Mapping[str, Any]],
    check: Mapping[str, Any],
    dice: Mapping[str, Any],
    death_verdict: Mapping[str, Any] | None = None,
    freeform: bool = False,
    acceptance_guidance: str | None = None,
) -> str:
    progress: dict[str, Any] = {}
    if isinstance(session, Mapping):
        _session_progress = session.get("progress")
        progress = (
            _session_progress
            if isinstance(_session_progress, Mapping)
            else {}
        )
    story_complete = bool(progress.get("story_complete"))
    ending_phase = bool(story_complete or progress.get("_ending_phase"))
    length_band = str(progress.get("narrative_length_band") or "compact")
    _ending_objective = str(
        progress.get("_ending_objective")
        or progress.get("current_objective")
        or ""
    )
    ending_contract = _ending_phase_contract(
        ending_phase, _ending_objective
    )
    next_choice_rule = (
        "收尾阶段不得生成任何 next_choices 或 group_decision。"
        if ending_phase
        else (
            "同时必须生成恰好四个合规 next_choices；关键集体节点使用 "
            "group_decision；生成表决时，state_patch 不得提前写入尚未"
            "通过的集体结果。"
        )
    )
    outcome = str(dice.get("outcome") or "")
    if death_verdict:
        # 引擎已硬落实死亡：这是不可更改的事实，模型只负责把它写进叙事。
        # （_lethal_death_verdict 已按世界策略判定过；此处不再给模型
        # 「豁免/救场」空间，软指令时代模型仍会逃避，故由引擎兜底。）
        death_rule = (
            "<authoritative_death_verdict>"
            f"{death_verdict.get('reason') or ''}"
            "该角色的死亡已是既定事实，不是建议：正文必须明确写出死亡本身"
            "（倒下、断气、被碾碎、被吞噬、炸裂、熄灭等，按上面已定代价），"
            "不得写成「重伤昏迷」「奇迹生还」「被及时救回」「只是晕倒」，"
            "不得安排任何角色救场或复活。"
            "</authoritative_death_verdict>"
        )
    else:
        if freeform:
            # 自由演绎不做事前 lethal 判断。大失败后独立死亡裁判没有给出
            # 权威死亡事实，就只能落实场景内最大的非致死后果。
            death_rule = (
                "自由演绎只有在事后因果裁判生成 "
                "<authoritative_death_verdict> 时才允许角色死亡或永久退场；"
                "本次没有该裁定，不得自行判死。"
            )
        else:
            death_rule = _lethal_death_directive(world, check, dice)
        if freeform and outcome == "critical_failure":
            death_rule += (
                "<freeform_critical_failure>"
                "这是自由演绎大失败，但事后裁判没有确认可成立的致死链，"
                "因此不得宣告角色死亡或永久退场。你仍须尽力落实当前场景"
                "中最严重、最不利且可追踪的后果，例如重伤、丢失机会/资源、"
                "暴露、关系破裂、被俘或目标彻底失败；具体强度不得超过当前"
                "环境、在场人物能力与已建立事实。不得为杀死角色而凭空加入"
                "敌人、灾害、疾病、事故或场外袭击。如果连重伤或被俘也无法"
                "从场景中圆回来，就改用该场景真实能成立的最大代价。"
                "</freeform_critical_failure>"
            )
    if acceptance_guidance:
        # 2026-09-21：评定改成由插件单独公告（engine.process_freeform），
        # 正文不再要求以「【演绎结果评定】」开头。原来那条要求与本函数靠后的
        # 「禁止在正文里泄露判定/结果分类标签」直接冲突，靠后的指令通常获胜，
        # 模型于是干脆整段不写——玩家侧看到的就是"接受程度判定没了"。
        # 这里只保留**作用域约束**：正文按接受范围兑现，越界部分不兑现。
        # 按 dice.outcome 的兑现强度倾向（critical_success 尽力去圆 /
        # success 克制放水 / failure 不嘴硬圆）继续保留；critical_failure 走
        # 引擎硬落实 + <freeform_critical_failure> 块，hint 返回空串。
        death_rule += (
            "\n<freeform_acceptance>"
            f"{acceptance_guidance}"
            f"{_outcome_fulfillment_hint(outcome)}"
            "只按接受范围内的动作兑现结果，越界部分（代写结果、操控他人、"
            "违背世界事实）不得兑现；即使检定有利，也不能把被砍掉的内容"
            "当作已发生事实。"
            "该指令对所有自由演绎都生效，包括照单全收（full）。"
            "接受程度、砍掉或降格了什么已由插件单独公告，正文不要重复"
            "评定结论、不要写「【演绎结果评定】」之类的判定标签，直接写正文。"
            "</freeform_acceptance>"
        )
    # 0.13.x：锁定选项的兑现程度由模型自己判断——成功不等于必须全兑现，
    # 但效果只能部分兑现或暂不兑现时，正文必须说明原因（与自由演绎
    # 「接收/半接收/不接受要说明理由」同原则）。不设硬性成功约束。
    if freeform:
        # 自由演绎：接收程度已由裁判裁定，走 <freeform_acceptance> 块。
        fulfillment_rule = ""
    else:
        fulfillment_rule = (
            "<result_explanation>"
            f"本次权威检定结果是 outcome={outcome or 'unknown'}。"
            "效果的兑现程度由你按世界事实判断：可完全兑现、只部分兑现、"
            "或受更深阻碍限制而暂不兑现。无论哪种，正文都必须如实呈现"
            "检定结果与实际效果之间的关系；当效果只能部分兑现或暂不兑现时，"
            "必须明确写出阻碍来源与程度（如「冻创化开一角，深层仍被死亡"
            "锚链锁住」），让玩家看出检定结果与效果之间的对应关系，"
            "不得只写结果不说明原因，也不得让检定成功显得毫无作用。"
            "</result_explanation>"
        )
    return (
        "依据下面由插件生成的权威检定结果完成叙事。"
        "不得重投、修改骰池、难度、风险、加值、优劣势来源或结果档位。"
        "必须返回 mode=resolve；check 设为 null。"
        "outcome=success_with_cost 时必须让目标达成，同时落实一项与风险等级"
        "相称且可追踪的代价。"
        + fulfillment_rule
        + death_rule
        + "清晰度硬约束（2026-08-24 玩家反馈「太谜语人 + 设定缺少介绍和铺垫」"
        "后落地，禁止以「克制」「伏笔」为名写成谜语或纯暗示）：首次登场的"
        "NPC 必须用一句话锚定身份 / 外貌 / 与场景关联；首次进入的场景必须"
        "用一两句话写清空间感与至少一个可观察物；世界专有名词首次出现时"
        "必须给在场角色能观察到的具体线索或简短解释；关键动作结果必须"
        "直接陈述，不得用「气氛变了」「他的眼神松动」之类模糊暗示代替"
        "明确交代。禁止在正文里泄露判定/结果分类标签（不得出现「success "
        "下」「success_with_cost 下」「critical_failure 下」「大成功」「"
        "大失败」「骰面 + 修正」「判定 = ...」「DC = ...」等任何把插件"
        "内部机制或检定元信息照搬进玩家文面的字眼）。正文语言必须用"
        "直白白话，避免诗意化、隐喻化、文言化、华丽辞藻堆砌——任何让"
        "读者「看不懂这句话在讲什么」的句子都得改写。"
        "陌生物体必须先用日常名称建立画面，再补古称、专业名或世界专名。"
        "建筑、门窗、台阶、平台、通道和城防设施不得只靠「丹墀、甬道、"
        "雉堞、券门、飞扶壁、须弥座」等术语传达画面；同一句必须说明它"
        "是高台、石阶、狭长通道、矮墙、圆拱门洞、斜撑石架或神像底座"
        "之类的什么东西，并补充外形、位置或用途中的至少一项。"
        f"{_dialogue_voice_rules()}"
        + "新 NPC 必须有名字，并至少满足直接互动、掌握重要线索或写入长期记忆"
        "之一；create 时用 registration_reasons 的固定值说明依据。"
        "已登记 NPC 必须使用稳定 npc_id 更新，不能凭相似名称静默合并。"
        "只写本次行动直接造成且能被当前场景确认的变化。"
        f"{_ending_length_rule(ending_phase, length_band, _ending_objective)}"
        f"{ending_contract}"
        "每个 next_choices.text 连同括号内容不得超过 50 字。"
        "四个选项的 actor_id 必须全部等于 next_actor 的 participant_id，"
        "并且只能描述该角色本人可尝试的行动，不得操控其他玩家角色。"
        f"{next_choice_rule}\n\n"
        f"{_runtime_sections(world=world, session=session, player=player, events=events, memories=memories)}\n\n"
        "<player_input trust=\"untrusted\">\n"
        f"{_json(player_input)}\n"
        "</player_input>\n\n"
        "<authoritative_check>\n"
        f"{_json({'request': dict(check), 'result': dict(dice)})}\n"
        "</authoritative_check>\n"
    )


def repair_prompt(
    raw_output: str,
    error: str,
    original_prompt: str,
) -> str:
    from .action_contract import turn_contract, ACTION_POLICY
    context = turn_contract(original_prompt)
    return (
        "上一份输出是未提交草稿，不是已发生事实。根据校验错误修复 JSON 结构、字段类型、正文长度、"
        "选项长度与行动角色归属；可以在不改变已发生事实的前提下压缩或"
        "补足叙事，并重写越权选项。不得改变检定结论、世界状态变化、代价"
        "或记忆事实。若错误指出行动遗漏、无依据跳场或虚构结果，须同时修正正文及对应状态操作，"
        "不能保留草稿中虚构的事实；权威骰果和原始现场不能改。修复后的玩家文面仍须使用直白白话；生僻专名或建筑"
        "术语首次出现时，先说明它是什么或有什么用，不得为了缩短文字删掉"
        "已有的通俗解释。不要解释错误。返回单个 JSON 对象。\n\n"
        f"{ACTION_POLICY}\n"
        f"<original_turn_context trust=\"untrusted-data\">{_json(context)}</original_turn_context>\n"
        f"<validation_error>{json.dumps(error, ensure_ascii=False)}</validation_error>\n"
        "<invalid_output>\n"
        f"{raw_output[:12000]}\n"
        "</invalid_output>\n"
    )


_CHOICE_SCHEMA = {
    "choices": [
        {
            "key": "A | B | C | D",
            "actor_id": "插件提供的 participant_id",
            "text": "不预设结果的行动意图，最多 50 字",
            "danger_id": "safe | controlled | dangerous | desperate | lethal",
            "check": {
                "required": True,
                "attribute_id": "世界属性稳定 ID；纯骰或免检留空",
                "type": "standard | leader | group | resistance | opposed",
                "difficulty": "由插件按 danger_id 覆盖",
                "known_consequences": "玩家当前可预见的失败后果",
                "advantage_sources": ["已经成立的优势来源"],
                "disadvantage_sources": ["已经成立的劣势来源"],
            },
            "collective": False,
        }
    ]
}


def choice_system_prompt(world: Mapping[str, Any]) -> str:
    """Small system prompt used only for A-D generation and repair."""
    return (
        f"{CORE_NARRATOR_RULES}\n\n"
        "你的当前任务仅是生成下一位角色的 A、B、C、D 四个行动选项。"
        "不要续写故事，不要输出状态补丁、记忆或骰点结果。\n\n"
        "<required_output_schema>\n"
        f"{_json(_CHOICE_SCHEMA)}\n"
        "</required_output_schema>\n"
    )


def choice_generation_prompt(
    *,
    world: Mapping[str, Any],
    session: Mapping[str, Any],
    participant: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
    avoid: Sequence[Mapping[str, Any]] = (),
    validation_error: str = "",
    story_context: str = "",
    pacing_directive: str = "",
) -> str:
    contract = world_contract(world)
    resolution_mode = str(contract["resolution"]["mode"])
    risk_policy = contract["resolution"]["difficulty_policy"]
    if resolution_mode in {"none", "narrative"}:
        check_rule = "当前世界不启用检定，四项 check 必须全部为 null。"
    elif resolution_mode == "dice_only":
        check_rule = (
            "当前世界使用纯骰检定；需要检定时 attribute_id 留空。"
        )
    else:
        attributes = "、".join(
            f"{item.get('key')}={item.get('label')}"
            for item in contract["attributes"]
        )
        check_rule = f"需要检定时 attribute_id 只能使用：{attributes}。"
    ws = session.get("world_state")
    ws = ws if isinstance(ws, Mapping) else {}
    current_scene = {
        "location": (participant.get("runtime_state") or {}).get("current_location") or "角色位置未确认，不能推定与其他队员同场",
        "scene_summary": "仅使用该角色所在地的可观察事实；其他地点的历史是旁白背景，不是角色已知",
        "time": ws.get("time") or "",
        "weather": ws.get("weather") or "",
    }
    choice_state = {**_world_state_projection(ws), "location": current_scene["location"],
                    "scene_summary": current_scene["scene_summary"]}
    from .continuity import previous_action, CONTINUITY_POLICY
    scene_block = (
        '<personal_continuity_policy>'
        + CONTINUITY_POLICY
        + '</personal_continuity_policy>\n'
        + '<previous_personal_action trust="untrusted-data" temporal_scope="past-settled-only">\n'
        + _json(previous_action(
            participant, session.get("personal_history_events", events), int(session.get("turn_no") or 0)))
        + '</previous_personal_action>\n'
        +
        "<current_scene trust=\"world-authoritative\">\n"
        f"{_json({k: v for k, v in current_scene.items() if v})}\n"
        "</current_scene>\n\n"
    )
    pacing_block = (
        "<pacing_directive trust=\"plugin-authoritative\">\n"
        f"{pacing_directive}\n"
        "</pacing_directive>\n\n"
        if pacing_directive
        else ""
    )
    return (
        "只生成当前角色接下来可选择的四个行动意图。默认以 <current_scene>"
        "为行动起点，不得凭空把角色写成已经抵达别处；但章节和当前场景都"
        "不是地点锁。只要目的地已被提及、属于世界内合理常识，或玩家已表达"
        "旅行意图，选项可以写『启程前往/返回某地』、选择路线或旅行方式。"
        "旅行选项只承诺出发与意图，不得预设平安抵达、自动完成后章目标或"
        "凭空生成目的地人物。若玩家已经明确目的地，不得用四个原地选项"
        "拖住玩家。"
        "先定位 acting_character，再从历史中选择属于其所在地的局面；"
        "轮到下一名玩家不代表他跟随上一名玩家移动。<resolved_story> 是上一行动"
        "的全局结果，不是当前角色的所在场景。历史提到的外地人物不能直接对话、"
        "协助或同行，除非先明确移动或有已成立的远程联系；不得把外地行动"
        "写成当前角色已参与。室内与室外可依据已发生的开门、呼喊获取有限信息，"
        "但不能自动共享另一条街的求援对话。"
        "严禁凭空捏造互动对象：人物不仅须在历史中出现，还须有证据表明当前"
        "可与行动者接触。若当前场景内没有任何"
        "对手或可疑人物在场，选项只能围绕当前场景实际可做的事（调查物证、"
        "核对线索、通讯联络、准备行动、整理装备等）展开；不得假定存在"
        "埋伏、对峙、被困或需要逃离的险境。"
        "必须恰好包含 A、B、C、D；actor_id 必须等于"
        " acting_character.participant_id。每项正文最多 50 字且不得自带"
        "风险或检定括号；不得预设成功，不得添加角色没有的能力、物品或知识。"
        "选项只能引用**当前角色已经知道**的信息：亲历、已成立的联络或物证。"
        "章节目标与里程碑是主持方向，不是角色已知情报；禁止照抄其中未公开的"
        "人物、势力和地点。未知事物应通过已知线索调查，查明后才引入专名。\n"
        "safe 代表没有显著风险与不确定性，check 必须为 null；"
        "controlled、dangerous、desperate、lethal 若结果确有不确定性，"
        "check 必须配置 required=true 并填写 attribute_id、type 与"
        " known_consequences。只有存在真实阻力、结果不确定且失败会改变局势"
        "时才配置检定；例行交谈、出示既有证据、无阻碍旅行和确定成功的"
        "动作无需检定。战斗、潜行或正在运作的危险机关不得漏配检定。"
        "已成立的优劣势（状态、掩护、伏击、地形、协助等）"
        "必须写入 check.advantage_sources / disadvantage_sources，"
        "不得漏填。局面确有致命威胁时如实给出 lethal 选项，不必为回避"
        "致死而压制其出现，也不必每组保留 safe。"
        "DC 由插件按风险映射，模型填写值不具权威性。"
        f"{check_rule}"
        "致命风险必须明确已知后果；同一原因不能同时提高 DC 和造成劣势。"
        "只能描述当前角色本人能够尝试的行为，不得替其他玩家角色行动或决定。"
        "选项使用直白动作与常见物体名；陌生专名需补明是门、平台、机关等什么东西。"
        "不得让玩家先查词才能知道自己在选择什么。"
        "影响全队的转场、主线分支、共有资源或不可逆决定只能标记"
        " collective=true。现场小队行动可加 vote_scope=local，必须只影响当前位置完全相同的成员；"
        "全队利益、主线抉择、远处队员的行动仍用 vote_scope=party，不得缩小范围。"
        "位置未知或不足两名同场队员时不要生成小队表决。返回单个 JSON 对象，不要解释。\n"
        "禁止零行动选项：每项须改变局面、产出新信息、明确下一步或开始合理旅行。"
        "允许调查新问题、验证新证据、制定后立即执行计划；禁止只盘点物品、"
        "重读已确认条款、原地等待或整理思绪。无即时事务就提供合理的接触、决定或离开，"
        "不要写零行动选项。\n"
        "已得到的答案不再重复打听，应据此决定或行动；同场公开交流可使用，远处分队不自动共享。"
        "新调查须写清问谁、在哪、弄清什么，不能泛泛打听。目标要求事件实际发生时，"
        "至少一项是现在可执行的实质动作，不再增加一层情报中转。\n"
        "章节已推进不代表人物已转场。选项必须承接该玩家实际现场和本轮已兑现结果；"
        "已谈定的事不再包装成重新谈判的必做任务，尚未完成的互动也不能因章节切换被省略。"
        "前往下一场景需有已知动机和可执行的衔接，不把原作战场当作人物当前位置。"
        "若 <pacing_directive> 声明了未达成的章节里程碑，本组 A/B/C/D 中"
        "至少一项必须是直接推进该里程碑的决定性行动（作出决定、确认模型"
        "代拟的短条款、拒绝/翻脸、公布证据、采取行动、推进调查或启程前往"
        "目标地点等），或为达成"
        "目标作出实质进展；四个选项不得全部停留在检查、询问、核对、观察、"
        "等待或拖延。\n"
        "若 <pacing_directive> 含 [Choice-Pacing-ENDING]，剧情已到结局点，"
        "本组 A/B/C/D 必须推进收尾：作出最终决定、与在场角色完成交代/道别、"
        "明确各自去向与后续安排等；不得停留在检查、确认、观望、整理或等待。\n\n"
        f"{scene_block}"
        f"{pacing_block}"
        "<world_definition>\n"
        f"{str(world.get('system_prompt', '')).strip()}\n"
        "</world_definition>\n\n"
        "<relevant_world_rules>\n"
        f"{_json(compact_world_rules(world))}\n"
        "</relevant_world_rules>\n\n"
        "<risk_dc_policy trust=\"plugin-authoritative\">\n"
        f"{_json(risk_policy)}\n"
        "</risk_dc_policy>\n\n"
        "<runtime_state>\n"
        f"{_json(choice_state)}\n"
        "</runtime_state>\n\n"
        "<acting_character>\n"
        f"{_json(_character_projection(participant))}\n"
        "</acting_character>\n\n"
        "<recent_history>\n"
        f"{_json(_history(list(events)[-8:]))}\n"
        "</recent_history>\n\n"
        "<avoid_repeating>\n"
        f"{_json([dict(item) for item in list(avoid)[-4:]])}\n"
        "</avoid_repeating>\n\n"
        "<resolved_story>\n"
        f"{str(story_context or '')[:3000]}\n"
        "</resolved_story>\n\n"
        "<previous_choice_error>\n"
        f"{str(validation_error or '')[:500]}\n"
        "</previous_choice_error>\n"
    )


def choice_repair_prompt(
    raw_output: str,
    error: str,
    *,
    world: Mapping[str, Any],
    participant: Mapping[str, Any],
    pacing_directive: str = "",
) -> str:
    contract = world_contract(world)
    attributes = [
        {"id": item.get("key"), "label": item.get("label")}
        for item in contract["attributes"]
    ]
    pacing_block = (
        "<pacing_directive trust=\"plugin-authoritative\">\n"
        f"{pacing_directive}\n"
        "</pacing_directive>\n"
        if pacing_directive
        else ""
    )
    return (
        "上一组选项未通过校验。只修复四个选项，不续写故事。"
        "必须保留 A、B、C、D；safe 的 check 必须为 null；"
        "actor_id 必须使用下面的权威 ID；属性只用稳定 ID。"
        "行动起点必须使用下面 current_location；其他队员在外地的互动不能转给该角色。"
        "返回单个 JSON 对象，不要解释。\n"
        "禁止零行动选项：修复后每个选项都必须是能推进剧情或产出新信息的"
        "具体行动；不得保留只检查/盘点/清点手上已有物品、重读/核对协议条款、"
        "原地等待、整理思绪这类零行动选项。\n"
        "若 <pacing_directive> 声明了未达成的章节里程碑，修复后的选项中"
        "必须至少一项是直接推进该里程碑的决定性行动；不得四个全部停留在"
        "检查、询问、核对、观察、等待或拖延。\n"
        "若 <pacing_directive> 含 [Choice-Pacing-ENDING]，修复后的选项必须"
        "推进收尾（作出最终决定、与角色交代/道别、明确各自去向等），"
        "不得停留在检查、确认、观望、整理或等待。\n\n"
        f"{pacing_block}"
        "<actor_id>"
        f"{_character_projection(participant).get('participant_id')}"
        "</actor_id>\n"
        "<current_location>"
        f"{_json((participant.get('runtime_state') or {}).get('current_location') or '位置未确认')}"
        "</current_location>\n"
        "<allowed_attributes>"
        f"{_json(attributes)}"
        "</allowed_attributes>\n"
        "<risk_dc_policy>"
        f"{_json(contract['resolution']['difficulty_policy'])}"
        "</risk_dc_policy>\n"
        f"<validation_error>{json.dumps(error, ensure_ascii=False)}</validation_error>\n"
        "<invalid_output>\n"
        f"{raw_output[:8000]}\n"
        "</invalid_output>\n"
    )
