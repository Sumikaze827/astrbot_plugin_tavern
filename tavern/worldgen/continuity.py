"""前作继承：把上一卷玩出来的结果，推导成可注入新卷的续卷简报。

数据来源与**刻意的取舍**
------------------------
所有事实只从这几处取：

- ``story_ledger`` —— 权威账本。里程碑用 ``kind='milestone'`` + ``status='completed'``；
  其他公开条目（线索、旗标、目标）走 ``carried_facts``。
- ``events`` —— 逐回合的权威行动日志（``role='player'`` 是玩家动作，
  ``role='narrator'`` 是叙事结果，骰点结果在 ``meta_json.check`` 里）。
- ``participants`` —— 参战名单与**退出原因**。
- ``session_characters`` —— NPC 的出现区间与生命周期状态。
- ``sessions.world_state_json`` —— 只取 ``relationships`` 与 ``inventory``。

**刻意不取 ``world_state_json.facts``**：实测该字段被引擎钳在 ``list[200]``
（``resolution.py:815`` 附近），是一段滑动窗口，超出 200 条的最旧事实会被丢弃。
用它做继承会让前 2/3 的剧情静默蒸发。事实必须从 ``events`` 反推。

**死亡检测**：实测全库 ``session_characters.lifecycle_status`` 只有 ``'active'``
（90 条），死亡**从不落这张表**；``players.enabled`` 也是陈旧值。真正的死亡信号是
``participants.exit_reason``——实测取值里有 ``'lethal_check_death'``。
因此这里以 ``exit_reason`` 词表判定，并把每一项都标上置信度，可疑项交给人工确认。
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from .models import (
    Confidence,
    ContinuityBrief,
    NpcLifecycleEntry,
    PlayerImpact,
    RosterEntry,
)

#: 确认的死亡退出原因。``lethal_check_death`` 是实测存在于生产库的取值。
DEATH_EXIT_REASONS = frozenset(
    {"lethal_check_death", "death", "killed", "killed_in_action", "dead"}
)
#: 确认的离队/退役原因。
DEPARTURE_EXIT_REASONS = frozenset(
    {"departure", "left", "retired", "cancelled_card", "withdrawn", "quit"}
)

OCCUPANCY_ALIVE = "存活"
OCCUPANCY_DEAD = "阵亡"
OCCUPANCY_DEPARTED = "离队"
OCCUPANCY_UNKNOWN = "未知"

#: 姓名/物品名之后常见的续接成分。用作非贪婪匹配的边界，避免把
#: 「救下了拉姆并把她带出宅邸」的宾语贪成「拉姆并把」。
_SUBJECT_BOUNDARY = (
    r"(?=[，。！？、；：\s」』）)]|把|被|并|和|与|的|了|着|到|在|就|才|便|又|也|都|还|后|时|后|$)"
)

#: 从叙事正文里识别「某人被救下 / 带走」这类玩家影响的候选模式。
#: 这里只产出**候选**，是否成立交给带引用的模型步骤确认。
#:
#: 宾语用**非贪婪** + 边界前瞻：非贪婪保证「拉姆并把」截成「拉姆」，
#: 前瞻保证「罗兹瓦尔，」不会被截成「罗兹」。
_IMPACT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "saved_npc",
        re.compile(
            r"(?:救下|救出|救|带出|背出|拉出|抱出|带离)(?:了)?(?P<subject>[一-鿿]{2,4}?)"
            + _SUBJECT_BOUNDARY
        ),
    ),
    (
        "killed_npc",
        re.compile(
            r"(?:杀死|击杀|斩杀|了结|除掉)(?:了)?(?P<subject>[一-鿿]{2,4}?)" + _SUBJECT_BOUNDARY
        ),
    ),
    (
        "acquired_item",
        re.compile(
            r"(?:取得|获得|拿到|夺回|捡到)(?:了)?(?P<subject>[一-鿿]{2,6}?)" + _SUBJECT_BOUNDARY
        ),
    ),
)

MAX_EVENTS_SCAN = 4000

#: 算作"这条线索/目标还立着"的账本状态。
#:
#: **``active`` 必须在内**。实测生产库：非里程碑账本行里 ``active`` 有 1312 条，
#: ``completed`` 只有 22 条——线索一经记录就是 ``active``，一直立到本子结束。
#: 早先只认 ``completed``，导致某档 59 条线索里只有 1 条被继承，
#: 等于把玩家一路攒下的全部认知丢掉了。
CARRIED_LEDGER_STATUSES = frozenset({"active", "completed"})


# --- 取数 -----------------------------------------------------------------


@contextmanager
def _connect(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    yield connection


def _rows(connection: sqlite3.Connection, sql: str, args: tuple[Any, ...]) -> list[sqlite3.Row]:
    cursor = connection.execute(sql, args)
    return list(cursor.fetchall())


def _as_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _load_json(raw: Any, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _verify_session(connection: sqlite3.Connection, session_id: str) -> dict[str, Any]:
    rows = _rows(
        connection,
        """
        SELECT s.*, w.slug AS world_slug, w.name AS world_name
        FROM sessions s LEFT JOIN worlds w ON w.id = s.world_id
        WHERE s.id = ?
        """,
        (session_id,),
    )
    if not rows:
        raise ValueError(f"session 不存在：{session_id}")
    return _as_dict(rows[0])


# --- 派生 -----------------------------------------------------------------


def _derive_roster(participants: list[dict[str, Any]]) -> tuple[list[RosterEntry], list[RosterEntry], list[RosterEntry]]:
    roster: list[RosterEntry] = []
    dead: list[RosterEntry] = []
    departed: list[RosterEntry] = []

    for row in participants:
        exit_reason = str(row.get("exit_reason") or "").strip()
        status = str(row.get("participation_status") or "").strip()
        normalized = exit_reason.lower()

        if normalized in DEATH_EXIT_REASONS:
            occupancy = OCCUPANCY_DEAD
        elif normalized in DEPARTURE_EXIT_REASONS or status in {"retired", "left"}:
            occupancy = OCCUPANCY_DEPARTED
        elif status in {"active", "ready", "joined", "playing"}:
            occupancy = OCCUPANCY_ALIVE
        elif not exit_reason and not status:
            occupancy = OCCUPANCY_UNKNOWN
        else:
            occupancy = OCCUPANCY_UNKNOWN

        entry = RosterEntry(
            player_id=str(row.get("player_id") or ""),
            display_name=str(row.get("display_name") or ""),
            character_name=str(row.get("character_name") or ""),
            character_code=str(row.get("character_code") or ""),
            participation_status=status,
            exit_reason=exit_reason,
            occupancy=occupancy,
        )
        roster.append(entry)
        if occupancy is OCCUPANCY_DEAD:
            dead.append(entry)
        elif occupancy is OCCUPANCY_DEPARTED:
            departed.append(entry)

    return roster, dead, departed


def _derive_milestones(
    rows: list[dict[str, Any]], declared: list[str]
) -> tuple[list[str], list[str], dict[str, str]]:
    """已完成 / 未完成的里程碑，外加二者的可读标签。

    ``declared`` 是当前世界包声明的全部里程碑 id（可能为空——那时只能报已完成的）。

    标签取两处：已完成的用账本 ``title``（引擎写入时就是人话），
    未完成的只能靠世界包声明。两边都没有就退回机器 key。
    """
    completed: list[str] = []
    labels: dict[str, str] = {}
    for row in rows:
        key = str(row.get("stable_key") or "").strip()
        if not key:
            continue
        # 引擎把里程碑标签写进 title，说明书式的长句也在所不惜——比 m_01_02_anomaly 强。
        title = str(row.get("title") or row.get("description") or "").strip()
        if title:
            labels.setdefault(key, title)
        if str(row.get("status") or "") == "completed":
            completed.append(key)

    completed_set = set(completed)
    uncompleted = [m for m in declared if m not in completed_set]
    return completed, uncompleted, labels


def _declared_milestones(
    connection: sqlite3.Connection, world_id: str
) -> tuple[list[str], dict[str, str]]:
    """从世界包读出它声明的全部里程碑 ``([id], {id: label})``。

    路径是 ``worlds.rules_json → rules.progress.chapters[].milestones[]``，
    每项形如 ``{"id": "m_01_01_trust", "label": "..."}``；``id`` 与账本的
    ``stable_key`` 同源，所以能直接对表。

    整行取再 ``.get``，不做列名投影：老库/测试库未必有 ``rules_json`` 这一列，
    投影会直接抛 ``OperationalError``，而"读不到声明"是完全可以接受的降级。
    """
    try:
        row = connection.execute(
            "SELECT * FROM worlds WHERE id = ?", (world_id,)
        ).fetchone()
    except sqlite3.Error:
        return [], {}
    if row is None:
        return [], {}
    data = _load_json(_as_dict(row).get("rules_json"), {})
    if not isinstance(data, dict):
        return [], {}
    # ``rules_json`` 存的就是 rules 对象本身；但世界包文件里是 ``{rules: {...}}``，
    # 两种形状都认，免得包一层就静默读成空。
    progress = data.get("progress")
    if not isinstance(progress, dict):
        progress = (data.get("rules") or {}).get("progress") or {}
    if not isinstance(progress, dict):
        return [], {}

    keys: list[str] = []
    labels: dict[str, str] = {}
    for chapter in progress.get("chapters") or []:
        if not isinstance(chapter, dict):
            continue
        for milestone in chapter.get("milestones") or []:
            if not isinstance(milestone, dict):
                continue
            key = str(milestone.get("id") or "").strip()
            if not key or key in labels:
                continue
            keys.append(key)
            label = str(milestone.get("label") or "").strip()
            labels[key] = label or key
    return keys, labels


def _derive_npc_lifecycle(rows: list[dict[str, Any]]) -> list[NpcLifecycleEntry]:
    entries: list[NpcLifecycleEntry] = []
    for row in rows:
        key = str(row.get("stable_key") or "").strip()
        if not key:
            continue
        entries.append(
            NpcLifecycleEntry(
                stable_key=key,
                name=str(row.get("name") or ""),
                lifecycle_status=str(row.get("lifecycle_status") or ""),
                persistent=bool(row.get("persistent")),
                first_turn=int(row.get("first_turn") or 0),
                last_turn=int(row.get("last_turn") or 0),
            )
        )
    return entries


def _iter_player_actions(events: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    for event in events:
        if str(event.get("role") or "") != "player":
            continue
        content = str(event.get("content") or "").strip()
        if not content:
            continue
        yield {
            "event_id": str(event.get("id") or ""),
            "turn_no": int(event.get("turn_no") or 0),
            "actor_name": str(event.get("actor_name") or ""),
            "content": content,
        }


def _derive_impact_candidates(events: list[dict[str, Any]]) -> list[PlayerImpact]:
    """从玩家动作里扫出**候选**影响。

    只做模式匹配，不判定真假——是否有依据由带原文引用的确认步骤决定。
    每条都带 ``evidence_event_ids``，因此可追溯到具体回合。
    """
    impacts: list[PlayerImpact] = []
    seen: set[tuple[str, str]] = set()
    for index, action in enumerate(_iter_player_actions(events)):
        content = action["content"]
        for kind, pattern in _IMPACT_PATTERNS:
            for match in pattern.finditer(content):
                subject = match.group("subject").strip()
                if not subject:
                    continue
                dedupe = (kind, subject)
                if dedupe in seen:
                    continue
                seen.add(dedupe)
                impacts.append(
                    PlayerImpact(
                        impact_id=f"imp_{len(impacts) + 1:02d}",
                        kind=kind,
                        subject=subject,
                        detail=content[:200],
                        actor=action["actor_name"],
                        turn_no=action["turn_no"],
                        confidence=Confidence.SUSPECTED,
                        evidence_event_ids=[action["event_id"]],
                        reason=f"玩家动作文本命中 {kind} 模式",
                    )
                )
        if len(impacts) >= 60:
            break
    return impacts


def _derive_relationship_deltas(world_state: dict[str, Any]) -> list[dict[str, Any]]:
    relationships = world_state.get("relationships")
    if not isinstance(relationships, dict):
        return []
    deltas: list[dict[str, Any]] = []
    for edge, value in relationships.items():
        if not isinstance(value, dict):
            continue
        entry = {"edge": str(edge)}
        for key in ("信任", "敌意", "好感", "戒备"):
            if key in value:
                entry[key] = value[key]
        if len(entry) > 1:
            # relationships 不是滑动窗口，但仍是聚合值而非权威日志，标记为参考。
            entry["authoritative"] = False
            deltas.append(entry)
    return deltas


def _derive_lost_items(world_state: dict[str, Any]) -> list[dict[str, Any]]:
    """从各参战者的背包里汇总物品。

    ``inventory`` 是按 ``participant_id`` 分桶的字典，不是滑动窗口。
    """
    inventory = world_state.get("inventory")
    if not isinstance(inventory, dict):
        return []
    items: list[dict[str, Any]] = []
    for owner, bucket in inventory.items():
        if isinstance(bucket, dict):
            names = list(bucket.keys())
        elif isinstance(bucket, list):
            names = [str(x) for x in bucket]
        else:
            continue
        for name in names:
            items.append({"item": str(name), "owner": str(owner), "confidence": "confirmed"})
    return items


# --- 公开入口 -------------------------------------------------------------


def derive_brief(
    connection: sqlite3.Connection,
    session_id: str,
    *,
    declared_milestones: list[str] | None = None,
) -> ContinuityBrief:
    """从一个已结束（或进行中）的对局派生续卷简报。

    Args:
        connection: 已打开的 sqlite3 连接。刻意用连接而非 database 对象，
            这样本函数是纯同步的，测试里可以直接对着临时库跑。
        session_id: 前作 session id。
        declared_milestones: 前作世界包声明的全部里程碑 id，用于算出「未完成」项。
            **留 None 就自己从世界包读**；显式传值则以此为准（测试常用）。
    """
    session = _verify_session(connection, session_id)

    declared_labels: dict[str, str] = {}
    if declared_milestones is None:
        declared_milestones, declared_labels = _declared_milestones(
            connection, str(session.get("world_id") or "")
        )
    world_state = _load_json(session.get("world_state_json"), {})
    if not isinstance(world_state, dict):
        world_state = {}

    participant_rows = [
        _as_dict(r)
        for r in _rows(connection, "SELECT * FROM participants WHERE session_id = ?", (session_id,))
    ]
    ledger_rows = [
        _as_dict(r)
        for r in _rows(connection, "SELECT * FROM story_ledger WHERE session_id = ?", (session_id,))
    ]
    npc_rows = [
        _as_dict(r)
        for r in _rows(
            connection,
            "SELECT * FROM session_characters WHERE session_id = ?",
            (session_id,),
        )
    ]
    event_rows = [
        _as_dict(r)
        for r in _rows(
            connection,
            "SELECT * FROM events WHERE session_id = ? ORDER BY seq LIMIT ?",
            (session_id, MAX_EVENTS_SCAN),
        )
    ]

    roster, dead, departed = _derive_roster(participant_rows)
    milestone_rows = [r for r in ledger_rows if str(r.get("kind") or "") == "milestone"]
    completed, uncompleted, labels = _derive_milestones(
        milestone_rows, declared_milestones or []
    )
    # 世界包声明只覆盖未完成的那些，账本 title 只覆盖已完成的——两边合并才对得上全部 key。
    for key, label in declared_labels.items():
        labels.setdefault(key, label)

    carried_facts = [
        {
            "text": str(r.get("title") or r.get("description") or "").strip(),
            "ledger_id": str(r.get("id") or ""),
            "stable_key": str(r.get("stable_key") or ""),
            "kind": str(r.get("kind") or ""),
            "source_event_id": str(r.get("source_event_id") or ""),
        }
        for r in ledger_rows
        if str(r.get("kind") or "") != "milestone"
        and str(r.get("status") or "") in CARRIED_LEDGER_STATUSES
        and str(r.get("visibility") or "public") == "public"
        and str(r.get("title") or r.get("description") or "").strip()
    ]

    impacts = _derive_impact_candidates(event_rows)

    # 阵亡玩家本身也是一条必须继承的影响——后续剧情不能当他还活着。
    for entry in dead:
        impacts.append(
            PlayerImpact(
                impact_id=f"imp_death_{len(impacts) + 1:02d}",
                kind="pc_death",
                subject=entry.character_name or entry.display_name,
                detail=f"退出原因：{entry.exit_reason}",
                actor=entry.display_name,
                confidence=Confidence.CONFIRMED,
                reason="participants.exit_reason 命中死亡词表",
                downstream_effect="该角色已阵亡，后续卷不得再让其出场或行动",
            )
        )

    warnings: list[str] = []
    if dead:
        warnings.append(
            f"有 {len(dead)} 名玩家角色阵亡（来自 participants.exit_reason）；"
            "session_characters.lifecycle_status 不记录玩家死亡，不要依赖它"
        )
    if len(world_state.get("facts") or []) >= 200:
        warnings.append(
            "world_state_json.facts 已触及 200 条上限（滑动窗口），"
            "更早的事实已被丢弃；本简报刻意不使用该字段"
        )
    if not event_rows:
        warnings.append("该对局没有 events 记录，玩家影响只能靠账本推断")

    needs_review = any(i.confidence is Confidence.SUSPECTED for i in impacts)

    turn_numbers = [int(e.get("turn_no") or 0) for e in event_rows] or [0]
    brief = ContinuityBrief(
        source_session_id=session_id,
        source_world_slug=str(session.get("world_slug") or ""),
        source_world_name=str(session.get("world_name") or ""),
        derived_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        turn_range=(min(turn_numbers), max(turn_numbers)),
        state=str(session.get("state") or ""),
        roster=roster,
        dead_pcs=dead,
        retired_pcs=departed,
        npc_lifecycle=_derive_npc_lifecycle(npc_rows),
        completed_milestones=completed,
        uncompleted_milestones=uncompleted,
        milestone_labels=labels,
        carried_facts=carried_facts,
        player_impacts=impacts,
        relationship_deltas=_derive_relationship_deltas(world_state),
        lost_or_spent_items=_derive_lost_items(world_state),
        unfinished_goals=[],
        needs_review=needs_review,
        warnings=warnings,
    )
    return brief


async def derive_brief_async(
    database: Any,
    session_id: str,
    *,
    declared_milestones: list[str] | None = None,
) -> ContinuityBrief:
    """插件内调用入口，把同步派生放到数据库的线程池里跑。"""
    return await database._run(derive_brief, database._connect, session_id, declared_milestones)


# --- 渲染与注入 -----------------------------------------------------------


def render_brief_markdown(brief: ContinuityBrief) -> str:
    """给操作者看的可读简报（落 ``50_continuity.md``，不参与导入）。"""
    lines: list[str] = []
    lines.append(f"# 续卷简报 · {brief.source_world_name or brief.source_world_slug}")
    lines.append("")
    lines.append(f"- 来源对局：`{brief.source_session_id}`（{brief.state}）")
    lines.append(f"- 回合范围：{brief.turn_range[0]} – {brief.turn_range[1]}")
    lines.append(f"- 派生时间：{brief.derived_at}")
    if brief.warnings:
        lines.append("")
        lines.append("## 需要注意")
        for warning in brief.warnings:
            lines.append(f"- ⚠️ {warning}")

    lines.append("")
    lines.append("## 参战名单")
    for entry in brief.roster:
        lines.append(
            f"- {entry.display_name}（{entry.character_name}）：{entry.occupancy}"
            + (f" · {entry.exit_reason}" if entry.exit_reason else "")
        )

    if brief.completed_milestones:
        lines.append("")
        lines.append("## 已达成里程碑")
        for key in brief.completed_milestones:
            lines.append(f"- {brief.milestone_labels.get(key, key)} `{key}`")
    if brief.uncompleted_milestones:
        lines.append("")
        lines.append("## 未达成里程碑（可续用）")
        for key in brief.uncompleted_milestones:
            lines.append(f"- {brief.milestone_labels.get(key, key)} `{key}`")

    if brief.carried_facts:
        lines.append("")
        lines.append(f"## 已结算的公开线索（{len(brief.carried_facts)} 条）")
        for fact in brief.carried_facts:
            lines.append(f"- [{fact.get('kind', '')}] {fact.get('text', '')}")

    if brief.player_impacts:
        lines.append("")
        lines.append("## 玩家影响")
        for impact in brief.player_impacts:
            flag = "✅" if impact.confidence is Confidence.CONFIRMED else "❓"
            lines.append(f"- {flag} [{impact.kind}] {impact.subject}（第 {impact.turn_no} 回合）")
            if impact.detail:
                lines.append(f"  - 依据：{impact.detail[:120]}")
            if impact.downstream_effect:
                lines.append(f"  - 影响：{impact.downstream_effect}")

    if brief.needs_review:
        lines.append("")
        lines.append("## 需要你确认的继承项")
        lines.append("以下条目为**候选**（❓），请确认后再注入新卷：")
        for impact in brief.player_impacts:
            if impact.confidence is Confidence.SUSPECTED:
                lines.append(f"- [ ] {impact.impact_id}：[{impact.kind}] {impact.subject}")

    return "\n".join(lines) + "\n"


#: 供注入使用的 system_prompt 附加段。**只写规则，不写事实**——事实进
#: initial_state，因为 system_prompt 每一回合都要花 token。
CONTINUITY_PROMPT_RULE = (
    "【续卷前提】本卷延续上一卷的既定结果，那些结果已经是这个世界的事实，"
    "不是可推翻的传言。玩家在前作造成的结果优先于原作剧情：不得重演、"
    "撤销或推翻已结算的结局，也不得让已阵亡的角色重新行动。"
)


def inject_brief(
    brief: ContinuityBrief,
    world: dict[str, Any],
    *,
    npc_names: dict[str, str] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """把续卷简报注入世界包。

    注入三个位置，各自的理由不同：

    - ``initial_state`` —— **全部硬事实**。这是会话创建时写入、之后每回合被当作
      权威世界状态读取的地方。若「拉姆已被救出」不在这里，叙事模型会在第 1 回合
      按原作设定重新推导，当场和玩家矛盾。事实一律写成**世界状态**而非历史事件
      （写「拉姆已随队行动」而不是「玩家在第 41 回合救了拉姆」）。
    - ``key_npcs[].state`` —— **只做减法**。作者指南明确要求不要预写可能与玩家
      成果冲突的固定地点/生死/立场。所以这里不是"加状态"，而是**移除或改写与
      已确认影响冲突的钉死项**，并把冲突记进返回值，强制回到审批门。
    - ``system_prompt`` —— 只追加 :data:`CONTINUITY_PROMPT_RULE` 一段规则。

    Args:
        brief: 续卷简报。
        world: 待注入的世界包（不会被修改）。
        npc_names: ``{npc_slug: 显示名}``，取自 NPC 包。世界包的 ``key_npcs``
            只有 ``ref``，中文名只存在于 NPC 包里；不传这一项，就无法把
            「npc_ram」和「拉姆」对上，冲突检测会漏报。

    Returns:
        ``(注入后的 world, 冲突列表)``。``world`` 是深拷贝，原对象不被修改。
    """
    import copy

    updated = copy.deepcopy(world)
    conflicts: list[dict[str, Any]] = []

    confirmed = brief.confirmed_impacts()
    if not confirmed and not brief.completed_milestones:
        return updated, conflicts

    # --- system_prompt：只加规则 ---
    prompt = str(updated.get("system_prompt") or "")
    if CONTINUITY_PROMPT_RULE not in prompt:
        updated["system_prompt"] = (prompt + "\n\n" + CONTINUITY_PROMPT_RULE).strip()

    # --- initial_state：硬事实 ---
    initial = updated.setdefault("initial_state", {})
    if not isinstance(initial, dict):
        initial = {}
        updated["initial_state"] = initial

    facts = initial.get("facts")
    if not isinstance(facts, list):
        facts = []
    existing = {str(f).strip() for f in facts}

    for impact in confirmed:
        statement = _impact_to_state_statement(impact)
        if statement and statement not in existing:
            facts.append(statement)
            existing.add(statement)
    if facts:
        initial["facts"] = facts

    if brief.relationship_deltas:
        relationships = initial.get("relationships")
        if not isinstance(relationships, dict):
            relationships = {}
        for delta in brief.relationship_deltas:
            edge = str(delta.get("edge") or "").strip()
            if edge and edge not in relationships:
                relationships[edge] = {
                    k: v for k, v in delta.items() if k not in {"edge", "authoritative"}
                }
        if relationships:
            initial["relationships"] = relationships

    # --- key_npcs[].state：只做减法，冲突上报 ---
    dead_subjects = {
        (e.character_name or e.display_name).strip()
        for e in brief.dead_pcs
        if (e.character_name or e.display_name).strip()
    }
    protected_subjects = {i.subject.strip() for i in confirmed if i.subject.strip()}
    protected_subjects |= dead_subjects

    rules = updated.get("rules")
    chapters = []
    if isinstance(rules, dict):
        progress = rules.get("progress")
        if isinstance(progress, dict):
            chapters = progress.get("chapters") or []

    for index, chapter in enumerate(chapters):
        if not isinstance(chapter, dict):
            continue
        chapter_id = str(chapter.get("id") or f"chapters[{index}]")
        for npc_index, npc in enumerate(chapter.get("key_npcs") or []):
            if not isinstance(npc, dict):
                continue
            state = npc.get("state")
            if not isinstance(state, dict) or not state:
                continue
            ref = str(npc.get("ref") or "")
            # 冲突判定：state 钉死了地点/生死，而该 NPC 或角色受玩家影响保护
            pinning = {k for k in state if k in {"location", "alive", "dead", "status", "health"}}
            if not pinning:
                continue

            # 比对线索有三条，缺一不可：
            #   1. state 正文里直接写了受影响角色的名字；
            #   2. ref 本身含中文名（有些包的 slug 是中英混排）；
            #   3. ref 对应的**显示名**（NPC 包里 slug→中文名）命中——
            #      世界包的 key_npcs 只有 ref，中文名只存在于 NPC 包，
            #      不传 npc_names 时这一条最关键的线索就断了。
            state_json = json.dumps(state, ensure_ascii=False)
            display_name = str((npc_names or {}).get(ref) or "")
            haystack = f"{ref} {display_name} {state_json}"
            if any(subject and subject in haystack for subject in protected_subjects):
                conflicts.append(
                    {
                        "chapter_id": chapter_id,
                        "npc_index": npc_index,
                        "ref": ref,
                        "pinned": sorted(pinning),
                        "state": dict(state),
                        "reason": "该章节钉死了与玩家已确认成果冲突的 NPC 状态",
                    }
                )
                chapter["inheritance_conflict"] = True

    return updated, conflicts


def _impact_to_state_statement(impact: PlayerImpact) -> str:
    """把影响写成**世界状态**语句，而不是历史事件叙述。"""
    subject = impact.subject.strip()
    if not subject:
        return ""
    if impact.kind == "pc_death":
        return f"{subject}已经阵亡，不会再登场"
    if impact.kind == "saved_npc":
        return f"{subject}已被队伍带离险境，现随队行动"
    if impact.kind == "killed_npc":
        return f"{subject}已被击杀"
    if impact.kind == "acquired_item":
        return f"{subject}已在队伍手中"
    return f"{subject}：{impact.detail[:80]}"
