from __future__ import annotations

import hashlib
import json
import secrets
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping

from .lifecycle import normalize_choices


def _text(value: Any, maximum: int = 1000) -> str:
    text = str(value or "").strip()
    return text[:maximum]


def _int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _attribute_value(value: Any) -> int | None:
    """把世界给定的基础属性值归一化为非负整数；缺失/非法时返回 None。"""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return max(0, min(10000, parsed))


def extract_json_object(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("模型未返回有效 JSON 对象")


def salvage_json_objects(raw: str) -> list[dict[str, Any]]:
    """收集文本里所有**完整**的 JSON 对象，即使外层文档被截断。

    2026-09-20 玩家反馈「当前检查点一直没触发」：里程碑裁判的输出上限只有
    800 token，待判定里程碑一多就会被 max_tokens 截断，外层对象永远解析
    失败，引擎静默当成「没有判定」——章节因此可以卡住几十个回合。
    截断只毁掉最后一条；前面的条目仍然完整可解析，这里把它们抢救出来，
    由调用方按 id 过滤后交给 ``_parse_milestone_judge``。
    """
    text = (raw or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    decoder = json.JSONDecoder()
    found: list[dict[str, Any]] = []
    index = 0
    length = len(text)
    while index < length:
        pos = text.find("{", index)
        if pos < 0:
            break
        try:
            value, end = decoder.raw_decode(text[pos:])
        except json.JSONDecodeError:
            index = pos + 1
            continue
        if isinstance(value, dict):
            found.append(value)
        index = pos + max(1, int(end))
    return found


@dataclass(frozen=True, slots=True)
class CheckRequest:
    stat: str
    reason: str
    difficulty: int
    modifier: int
    attribute_value: int | None = None
    risk: str = "controlled"
    check_type: str = "standard"
    advantage_sources: tuple[str, ...] = ()
    disadvantage_sources: tuple[str, ...] = ()
    known_consequences: str = ""
    visibility: str = "public"
    inspiration_mode: str = ""
    participant_ids: tuple[str, ...] = ()
    opponent_modifier: int = 0
    # 2026-08-24 八次修正：自由演绎非法 check_stat 走玩家最高属性兜底
    # 时，stat 字段存最高属性 key（让 authoritative_modifier 查到修正），
    # display_stat 存「通用」（让 _format_dice_result 报「【通用检定】」）。
    # 其他路径 display_stat="" 时下游 fallback 用 stat 自身。
    display_stat: str = ""


@dataclass(frozen=True, slots=True)
class DiceResult:
    die: int
    modifier: int
    total: int
    difficulty: int
    outcome: str
    critical: str | None
    attribute_value: int | None = None
    rolls: tuple[int, ...] = ()
    kept: int = 0
    dice_mode: str = "standard"
    margin: int = 0
    risk: str = "controlled"
    check_type: str = "standard"
    advantage_sources: tuple[str, ...] = ()
    disadvantage_sources: tuple[str, ...] = ()
    advantages_cancelled: bool = False
    original_rolls: tuple[int, ...] = ()
    rerolled: bool = False
    visibility: str = "public"
    members: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class Resolution:
    mode: str
    narrative: str
    check: CheckRequest | None
    state_patch: dict[str, Any]
    memories: tuple[dict[str, Any], ...]
    next_choices: tuple[dict[str, Any], ...]
    group_decision: dict[str, Any] | None
    return_progress: dict[str, Any] | None
    npc_ops: tuple[dict[str, Any], ...]
    clock_ops: tuple[dict[str, Any], ...]
    ledger_ops: tuple[dict[str, Any], ...]
    location_ops: tuple[dict[str, Any], ...]
    status_ops: tuple[dict[str, Any], ...]
    assist_ops: tuple[dict[str, Any], ...]
    director_note: str
    raw: dict[str, Any]
    # 本轮这条行动实际由哪些玩家一起完成（participant_id）。模型按现场
    # 背景判断：同场的例行行动把同行者一并写进来，只有行动者自己做就
    # 只写他自己。移动还须遵守持续 travel_groups；不跳过同行者的行动轮次。
    participants: tuple[str, ...] = ()


def validate_resolution(payload: Mapping[str, Any]) -> Resolution:
    mode = str(payload.get("mode", "resolve")).strip().lower()
    if mode not in {"resolve", "check"}:
        raise ValueError("mode 必须为 resolve 或 check")

    narrative = _text(payload.get("narrative"), 6000)
    check: CheckRequest | None = None
    if mode == "check":
        raw_check = payload.get("check")
        if not isinstance(raw_check, Mapping):
            raise ValueError("check 模式缺少检定参数")
        risk_aliases = {
            "low": "safe",
            "standard": "controlled",
            "high": "dangerous",
            "safe": "safe",
            "controlled": "controlled",
            "dangerous": "dangerous",
            "desperate": "desperate",
            "lethal": "lethal",
        }
        check_type = str(
            raw_check.get("check_type", "standard")
        ).strip().lower()
        if check_type not in {
            "standard",
            "leader",
            "group",
            "resistance",
            "opposed",
        }:
            check_type = "standard"
        visibility = str(
            raw_check.get("visibility", "public")
        ).strip().lower()
        if visibility not in {"public", "immersive", "hidden"}:
            visibility = "public"
        inspiration_mode = str(
            raw_check.get("inspiration_mode", "")
        ).strip().lower()
        if inspiration_mode not in {"", "advantage", "reroll"}:
            inspiration_mode = ""
        check = CheckRequest(
            stat=_text(raw_check.get("stat"), 40) or "通用",
            reason=_text(raw_check.get("reason"), 240) or "行动存在不确定性",
            difficulty=_int(raw_check.get("difficulty"), 12, 5, 25),
            modifier=_int(raw_check.get("modifier"), 0, -10, 10),
            risk=risk_aliases.get(
                str(raw_check.get("risk", "controlled")).strip().lower(),
                "controlled",
            ),
            check_type=check_type,
            advantage_sources=tuple(
                _list_of_text(
                    raw_check.get("advantage_sources"),
                    8,
                    120,
                )
            ),
            disadvantage_sources=tuple(
                _list_of_text(
                    raw_check.get("disadvantage_sources"),
                    8,
                    120,
                )
            ),
            known_consequences=_text(
                raw_check.get("known_consequences"),
                300,
            ),
            visibility=visibility,
            inspiration_mode=inspiration_mode,
            participant_ids=tuple(
                _list_of_text(
                    raw_check.get("participant_ids"),
                    32,
                    128,
                )
            ),
            opponent_modifier=_int(
                raw_check.get("opponent_modifier"),
                0,
                -10,
                10,
            ),
        )
    elif not narrative:
        raise ValueError("resolve 模式必须包含 narrative")

    raw_patch = payload.get("state_patch", {})
    state_patch = (
        dict(raw_patch) if isinstance(raw_patch, Mapping) else {}
    )

    memories: list[dict[str, Any]] = []
    raw_memories = payload.get("memories", [])
    if isinstance(raw_memories, list):
        for entry in raw_memories[:12]:
            if not isinstance(entry, Mapping):
                continue
            content = _text(entry.get("content"), 600)
            if not content:
                continue
            scope = str(entry.get("scope", "world")).lower()
            if scope not in {"world", "player", "npc"}:
                scope = "world"
            memories.append(
                {
                    "scope": scope,
                    "scope_id": _text(entry.get("scope_id"), 128),
                    "kind": _text(entry.get("kind"), 32) or "fact",
                    "content": content,
                    "importance": _int(
                        entry.get("importance"), 3, 1, 5
                    ),
                    "tags": [
                        _text(tag, 32)
                        for tag in (
                            entry.get("tags")
                            if isinstance(entry.get("tags"), list)
                            else []
                        )[:8]
                        if _text(tag, 32)
                    ],
                    "visibility": (
                        str(entry.get("visibility") or "public").lower()
                        if str(entry.get("visibility") or "public").lower()
                        in {"public", "host", "private"}
                        else "public"
                    ),
                    "locked": bool(entry.get("locked", False)),
                    "pinned": bool(entry.get("pinned", False)),
                    "supersedes_id": _text(
                        entry.get("supersedes_id"),
                        128,
                    ),
                }
            )

    next_choices: tuple[dict[str, Any], ...] = ()
    if "next_choices" in payload:
        next_choices = tuple(
            normalize_choices(payload.get("next_choices"))
        )

    group_decision: dict[str, Any] | None = None
    raw_group_decision = payload.get("group_decision")
    if raw_group_decision is not None:
        if not isinstance(raw_group_decision, Mapping):
            raise ValueError("group_decision 必须为对象或 null")
        question = _text(raw_group_decision.get("question"), 500)
        raw_options = raw_group_decision.get("options")
        options: list[dict[str, str]] = []
        if isinstance(raw_options, list):
            seen: set[str] = set()
            for index, item in enumerate(raw_options[:4]):
                if not isinstance(item, Mapping):
                    continue
                key = _text(
                    item.get("key") or chr(ord("A") + index),
                    1,
                ).upper()
                text = _text(item.get("text"), 240)
                if key not in {"A", "B", "C", "D"} or key in seen or not text:
                    continue
                seen.add(key)
                options.append({"key": key, "text": text})
        if question and len(options) >= 2:
            group_decision = {
                "question": question,
                "options": options,
                "vote_scope": "local" if raw_group_decision.get("vote_scope") == "local" else "party",
            }
        elif question or raw_options:
            raise ValueError("集体决策必须包含问题和 2-4 个有效选项")

    return_progress: dict[str, Any] | None = None
    raw_return_progress = payload.get("return_progress")
    if raw_return_progress is not None:
        if not isinstance(raw_return_progress, Mapping):
            raise ValueError("return_progress 必须为对象或 null")
        request_id = _text(raw_return_progress.get("request_id"), 128)
        evidence = _text(raw_return_progress.get("evidence"), 500)
        if request_id and evidence:
            return_progress = {
                "request_id": request_id,
                "evidence": evidence,
                "completed": bool(
                    raw_return_progress.get("completed", False)
                ),
            }

    npc_ops: list[dict[str, Any]] = []
    raw_npc_ops = payload.get("npc_ops")
    if isinstance(raw_npc_ops, list):
        create_count = 0
        for item in raw_npc_ops[:12]:
            if not isinstance(item, Mapping):
                continue
            operation = str(item.get("op") or "").strip().lower()
            if operation not in {"create", "update", "archive", "depart", "kill"}:
                continue
            if operation == "create":
                create_count += 1
                if create_count > 3:
                    continue
            name = _text(item.get("name"), 80)
            npc_id = _text(item.get("npc_id"), 128)
            if operation == "create" and not name:
                continue
            if operation != "create" and not npc_id and not name:
                continue
            npc_ops.append(
                {
                    "op": operation,
                    "npc_id": npc_id,
                    "name": name,
                    "aliases": _list_of_text(
                        item.get("aliases"),
                        12,
                        80,
                    ),
                    "role_type": _text(
                        item.get("role_type"),
                        40,
                    ) or "npc",
                    "persistent": bool(item.get("persistent", True)),
                    "public_profile": (
                        dict(item.get("public_profile"))
                        if isinstance(item.get("public_profile"), Mapping)
                        else {}
                    ),
                    "runtime_state": (
                        dict(item.get("runtime_state"))
                        if isinstance(item.get("runtime_state"), Mapping)
                        else {}
                    ),
                    "known_facts": _list_of_text(
                        item.get("known_facts"),
                        30,
                        400,
                    ),
                    "misconceptions": _list_of_text(
                        item.get("misconceptions"),
                        20,
                        400,
                    ),
                    "registration_reasons": [
                        reason
                        for reason in _list_of_text(
                            item.get("registration_reasons"),
                            3,
                            40,
                        )
                        if reason
                        in {
                            "direct_interaction",
                            "important_clue",
                            "long_term_memory",
                        }
                    ],
                }
            )

    clock_ops: list[dict[str, Any]] = []
    raw_clock_ops = payload.get("clock_ops")
    if isinstance(raw_clock_ops, list):
        for item in raw_clock_ops[:12]:
            if not isinstance(item, Mapping):
                continue
            operation = str(item.get("op") or "advance").strip().lower()
            if operation not in {"create", "advance", "set", "complete", "archive"}:
                continue
            clock_id = _text(item.get("clock_id"), 128)
            title = _text(item.get("title"), 100)
            if operation == "create" and not title:
                continue
            if operation != "create" and not clock_id and not title:
                continue
            visibility = str(item.get("visibility") or "public").lower()
            if visibility not in {"public", "vague", "hidden"}:
                visibility = "public"
            segments = _int(item.get("segments"), 4, 4, 8)
            if segments not in {4, 6, 8}:
                segments = 4
            clock_ops.append(
                {
                    "op": operation,
                    "clock_id": clock_id,
                    "title": title,
                    "segments": segments,
                    "delta": _int(item.get("delta"), 1, -8, 8),
                    "value": _int(item.get("value"), 0, 0, 8),
                    "visibility": visibility,
                    "trigger": _text(item.get("trigger"), 500),
                }
            )

    ledger_ops: list[dict[str, Any]] = []
    raw_ledger_ops = payload.get("ledger_ops")
    if isinstance(raw_ledger_ops, list):
        for item in raw_ledger_ops[:16]:
            if not isinstance(item, Mapping):
                continue
            operation = str(item.get("op") or "update").strip().lower()
            if operation not in {"create", "update", "complete", "fail", "archive"}:
                continue
            entry_id = _text(item.get("entry_id"), 128)
            stable_key = _text(item.get("stable_key"), 128)
            title = _text(item.get("title"), 160)
            if operation == "create" and not title:
                continue
            if operation != "create" and not entry_id and not title:
                continue
            # 2026-09-20：clue 条目不再接受。
            #
            # 它原本是里程碑判定的输入——引擎用关键词子串匹配线索标题里的
            # evidence_required 词（见 milestone_judge_prompt 的说明）。2026-08-23
            # 用户要求「不要靠正则」后那条路径被删除，改用模型裁判 + 已提交正文
            # 举证；但生成侧一直没跟着删，于是模型持续按一份作废的格式写记录：
            # 某局 270+ 轮攒了 126 条，只有 2 条被结清，叙事循环里谁都不读它，
            # 反而把 context_budget.ledger_items 的 8 格占满、把 completed
            # 里程碑全部挤出上下文。
            #
            # 注意不能只把它从下面的白名单里拿掉：那会让它落到 `kind = "objective"`
            # 兜底，换个名字继续写进去，所以这里显式丢弃整条操作。
            #
            # 已有历史行保留（续作继承仍会带它们），只是新的一局不再积攒。
            if str(item.get("kind") or "").strip().lower() == "clue":
                continue
            kind = str(item.get("kind") or "objective").strip().lower()
            if kind not in {
                "main",
                "side",
                "objective",
                "milestone",
                "failed",
            }:
                kind = "objective"
            ledger_ops.append(
                {
                    "op": operation,
                    "entry_id": entry_id,
                    "stable_key": stable_key,
                    "kind": kind,
                    "title": title,
                    "description": _text(item.get("description"), 800),
                    "visibility": (
                        "host"
                        if str(item.get("visibility") or "").lower() == "host"
                        else "public"
                    ),
                }
            )

    location_ops: list[dict[str, Any]] = []
    raw_location_ops = payload.get("location_ops")
    if isinstance(raw_location_ops, list):
        for item in raw_location_ops[:16]:
            if not isinstance(item, Mapping):
                continue
            target_id = _text(item.get("target_id"), 128)
            location = _text(item.get("location"), 160)
            if not target_id or not location:
                continue
            location_ops.append(
                {
                    "target_id": target_id,
                    "location": location,
                }
            )

    status_ops: list[dict[str, Any]] = []
    raw_status_ops = payload.get("status_ops")
    if isinstance(raw_status_ops, list):
        for item in raw_status_ops[:16]:
            if not isinstance(item, Mapping):
                continue
            operation = str(item.get("op") or "add").strip().lower()
            if operation not in {"add", "update", "remove"}:
                continue
            target_id = _text(item.get("target_id"), 128)
            name = _text(item.get("name"), 100)
            if not target_id or not name:
                continue
            severity = str(item.get("severity") or "minor").strip().lower()
            if severity not in {"minor", "serious", "critical"}:
                severity = "minor"
            status_ops.append(
                {
                    "op": operation,
                    "target_id": target_id,
                    "name": name,
                    "severity": severity,
                    "affects": _list_of_text(item.get("affects"), 12, 80),
                    "effect": _text(item.get("effect"), 300),
                    "removal": _text(item.get("removal"), 300),
                }
            )

    assist_ops: list[dict[str, Any]] = []
    raw_assist_ops = payload.get("assist_ops")
    if isinstance(raw_assist_ops, list):
        for item in raw_assist_ops[:4]:
            if not isinstance(item, Mapping):
                continue
            target_id = _text(item.get("target_id"), 128)
            method = _text(item.get("method"), 300)
            if not target_id or not method:
                continue
            assist_ops.append(
                {
                    "target_id": target_id,
                    "stat": _text(item.get("stat"), 40),
                    "method": method,
                    "expires_round": _int(
                        item.get("expires_round"),
                        0,
                        0,
                        1_000_000,
                    ),
                }
            )

    return Resolution(
        mode=mode,
        narrative=narrative,
        check=check,
        state_patch=state_patch,
        memories=tuple(memories),
        next_choices=next_choices,
        group_decision=group_decision,
        return_progress=return_progress,
        npc_ops=tuple(npc_ops),
        clock_ops=tuple(clock_ops),
        ledger_ops=tuple(ledger_ops),
        location_ops=tuple(location_ops),
        status_ops=tuple(status_ops),
        assist_ops=tuple(assist_ops),
        director_note=_text(payload.get("director_note"), 500),
        raw=dict(payload),
        participants=tuple(
            _list_of_text(payload.get("participants"), 32, 128)
        ),
    )


def _outcome_for_roll(
    die: int,
    margin: int,
    policy: Mapping[str, Any] | None = None,
) -> tuple[str, str | None]:
    policy = policy if isinstance(policy, Mapping) else {}
    critical_margin = _int(
        policy.get("critical_success_margin"), 10, 1, 100
    )
    failure_floor = _int(
        policy.get("failure_min_margin"), -9, -100, -1
    )
    # Total minus DC is authoritative, including worlds carrying legacy flags.
    if margin >= critical_margin:
        return "critical_success", None
    if margin >= 0:
        return "success", None
    if margin >= failure_floor:
        return "failure", None
    return "critical_failure", None


def roll_check(
    check: CheckRequest,
    outcome_policy: Mapping[str, Any] | None = None,
) -> DiceResult:
    advantages = tuple(dict.fromkeys(check.advantage_sources))
    disadvantages = tuple(dict.fromkeys(check.disadvantage_sources))
    if check.inspiration_mode == "advantage":
        advantages = (*advantages, "灵感点")
    cancelled = bool(advantages and disadvantages)
    if cancelled:
        dice_mode = "standard"
    elif advantages:
        dice_mode = "advantage"
    elif disadvantages:
        dice_mode = "disadvantage"
    else:
        dice_mode = "standard"

    def make_pool() -> tuple[int, ...]:
        count = 2 if dice_mode in {"advantage", "disadvantage"} else 1
        return tuple(secrets.randbelow(20) + 1 for _ in range(count))

    original_rolls = make_pool()
    rolls = original_rolls
    rerolled = check.inspiration_mode == "reroll"
    if rerolled:
        rolls = make_pool()
    if dice_mode == "advantage":
        die = max(rolls)
    elif dice_mode == "disadvantage":
        die = min(rolls)
    else:
        die = rolls[0]
    total = die + check.modifier
    margin = total - check.difficulty
    outcome, critical = _outcome_for_roll(die, margin, outcome_policy)
    return DiceResult(
        die=die,
        modifier=check.modifier,
        attribute_value=check.attribute_value,
        total=total,
        difficulty=check.difficulty,
        outcome=outcome,
        critical=critical,
        rolls=rolls,
        kept=die,
        dice_mode=dice_mode,
        margin=margin,
        risk=check.risk,
        check_type=check.check_type,
        advantage_sources=advantages,
        disadvantage_sources=disadvantages,
        advantages_cancelled=cancelled,
        original_rolls=original_rolls if rerolled else (),
        rerolled=rerolled,
        visibility=check.visibility,
    )


def roll_group_check(
    check: CheckRequest,
    actors: list[Mapping[str, Any]],
    outcome_policy: Mapping[str, Any] | None = None,
) -> DiceResult:
    """Resolve a majority group check without waiting for manual roll commands."""

    members: list[dict[str, Any]] = []
    success_count = 0
    for actor in actors[:32]:
        member_check = CheckRequest(
            stat=check.stat,
            reason=check.reason,
            difficulty=check.difficulty,
            modifier=_int(actor.get("modifier"), 0, -10, 10),
            attribute_value=_attribute_value(actor.get("attribute_value")),
            risk=check.risk,
            check_type=check.check_type,
            advantage_sources=tuple(
                _list_of_text(actor.get("advantage_sources"), 8, 120)
            ),
            disadvantage_sources=tuple(
                _list_of_text(actor.get("disadvantage_sources"), 8, 120)
            ),
            visibility=check.visibility,
        )
        rolled = roll_check(member_check, outcome_policy)
        succeeded = rolled.outcome in {
            "critical_success",
            "success",
            "success_with_cost",
        }
        success_count += int(succeeded)
        members.append(
            {
                "actor_id": _text(actor.get("actor_id"), 128),
                "name": _text(actor.get("name"), 100),
                "rolls": list(rolled.rolls),
                "kept": rolled.kept,
                "modifier": rolled.modifier,
                "attribute_value": rolled.attribute_value,
                "total": rolled.total,
                "outcome": rolled.outcome,
            }
        )
    required = len(members) // 2 + 1
    group_success = bool(members) and success_count >= required
    outcome = "success" if group_success else "failure"
    if members and success_count == len(members):
        outcome = "critical_success"
    elif members and success_count == 0:
        outcome = "critical_failure"
    return DiceResult(
        die=0,
        modifier=0,
        total=success_count,
        difficulty=required,
        outcome=outcome,
        critical=None,
        rolls=(),
        kept=0,
        dice_mode="group",
        margin=success_count - required,
        risk=check.risk,
        check_type=check.check_type,
        visibility=check.visibility,
        members=tuple(members),
    )


def roll_opposed_check(
    check: CheckRequest,
    *,
    defender_id: str = "",
    defender_name: str = "防守方",
    outcome_policy: Mapping[str, Any] | None = None,
) -> DiceResult:
    """Resolve an active opposed check; ties favor the defender."""

    attacker = roll_check(check, outcome_policy)
    defender_die = secrets.randbelow(20) + 1
    defender_total = defender_die + check.opponent_modifier
    margin = attacker.total - defender_total
    outcome, _ = _outcome_for_roll(attacker.die, margin, outcome_policy)
    if margin == 0:
        outcome = "failure"  # Opposed ties still favor the defender.
    return DiceResult(
        die=attacker.die,
        modifier=attacker.modifier,
        attribute_value=attacker.attribute_value,
        total=attacker.total,
        difficulty=defender_total,
        outcome=outcome,
        critical=None,
        rolls=attacker.rolls,
        kept=attacker.kept,
        dice_mode=attacker.dice_mode,
        margin=margin,
        risk=check.risk,
        check_type="opposed",
        advantage_sources=attacker.advantage_sources,
        disadvantage_sources=attacker.disadvantage_sources,
        advantages_cancelled=attacker.advantages_cancelled,
        original_rolls=attacker.original_rolls,
        rerolled=attacker.rerolled,
        visibility=check.visibility,
        members=(
            {
                "actor_id": defender_id,
                "name": defender_name,
                "rolls": [defender_die],
                "kept": defender_die,
                "modifier": check.opponent_modifier,
                "total": defender_total,
                "outcome": "defender",
            },
        ),
    )


def _list_of_text(value: Any, maximum_items: int, maximum_chars: int) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value[:maximum_items]:
        text = _text(item, maximum_chars)
        if text and text not in result:
            result.append(text)
    return result


def apply_state_patch(
    current: Mapping[str, Any] | None,
    patch: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Apply only explicitly allowed world-state fields."""

    state: dict[str, Any] = deepcopy(dict(current or {}))
    update = dict(patch or {})

    for key, limit in (
        ("location", 160),
        ("time", 160),
        ("scene_summary", 1200),
    ):
        if key in update:
            value = _text(update.get(key), limit)
            if value:
                state[key] = value

    facts = _list_of_text(state.get("facts"), 200, 400)
    remove = set(_list_of_text(update.get("facts_remove"), 30, 400))
    if remove:
        facts = [fact for fact in facts if fact not in remove]
    for fact in _list_of_text(update.get("facts_add"), 30, 400):
        if fact not in facts:
            facts.append(fact)
    state["facts"] = facts[-200:]

    inventory = state.get("inventory")
    inventory = deepcopy(inventory) if isinstance(inventory, dict) else {}
    operations = update.get("inventory_ops")
    if isinstance(operations, list):
        for operation in operations[:30]:
            if not isinstance(operation, Mapping):
                continue
            owner = _text(operation.get("owner_id"), 128)
            item = _text(operation.get("item"), 100)
            if not owner or not item:
                continue
            delta = _int(operation.get("delta"), 0, -100, 100)
            owner_items = inventory.get(owner)
            owner_items = (
                deepcopy(owner_items)
                if isinstance(owner_items, dict)
                else {}
            )
            old_value = _int(owner_items.get(item), 0, 0, 1_000_000)
            new_value = max(0, old_value + delta)
            if new_value:
                owner_items[item] = new_value
            else:
                owner_items.pop(item, None)
            inventory[owner] = owner_items
    state["inventory"] = inventory

    relationships = state.get("relationships")
    relationships = (
        deepcopy(relationships) if isinstance(relationships, dict) else {}
    )
    operations = update.get("relationship_ops")
    if isinstance(operations, list):
        for operation in operations[:30]:
            if not isinstance(operation, Mapping):
                continue
            source = _text(operation.get("source"), 128)
            target = _text(operation.get("target"), 128)
            dimension = _text(operation.get("dimension"), 40) or "信任"
            if not source or not target:
                continue
            key = f"{source}→{target}"
            dimensions = relationships.get(key)
            dimensions = (
                deepcopy(dimensions)
                if isinstance(dimensions, dict)
                else {}
            )
            old_value = _int(dimensions.get(dimension), 0, -100, 100)
            delta = _int(operation.get("delta"), 0, -20, 20)
            dimensions[dimension] = max(-100, min(100, old_value + delta))
            relationships[key] = dimensions
    state["relationships"] = relationships

    return state


def memory_fingerprint(
    session_id: str,
    scope: str,
    scope_id: str,
    kind: str,
    content: str,
) -> str:
    material = "\x1f".join(
        [session_id, scope, scope_id, kind, " ".join(content.split()).lower()]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()
