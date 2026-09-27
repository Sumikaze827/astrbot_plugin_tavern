"""Exact-location voting and explicitly authorized movement."""
import json
from typing import Any, Iterable, Mapping, Sequence


_MEMBER_KEYS = (
    "id",
    "participant_id",
    "group_user_id",
    "character_name",
    "character_code",
)


def _match_member(
    ref: Any,
    roster: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    """按稳定 ID / 角色名唯一匹配一名副本成员；匹配不到或不唯一时返回 None。"""
    needle = str(ref or "").strip().casefold()
    if not needle:
        return None
    matches = [
        member
        for member in roster
        if isinstance(member, Mapping)
        and needle in {
            str(member.get(key) or "").strip().casefold()
            for key in _MEMBER_KEYS
        }
    ]
    return matches[0] if len(matches) == 1 else None


def local_voters(connection, session_id, actor_user_id, eligible):
    rows = connection.execute(
        "SELECT p.group_user_id,r.state_json FROM participants p "
        "LEFT JOIN character_runtime_states r ON r.participant_id=p.id AND r.session_id=p.session_id "
        "WHERE p.session_id=?", (session_id,),
    ).fetchall()
    locations = {str(r['group_user_id']): str(json.loads(r['state_json'] or '{}').get('current_location') or '').strip() for r in rows}
    location = locations.get(str(actor_user_id))
    if not location:
        raise ValueError('角色位置未确认，不能发起现场小队表决')
    voters = [uid for uid in eligible if locations.get(uid) == location]
    if str(actor_user_id) not in voters or len(voters) < 2:
        raise ValueError('当前没有至少两名位置已确认的同场队员，请使用个人行动或全队表决')
    return voters


def validate_movement(operations, roster, allowed_users):
    normalized = []
    seen = set()
    for operation in operations:
        target = _match_member(operation.get('target_id'), roster)
        if target is None:
            raise ValueError('移动目标必须唯一匹配当前副本角色，请使用 participant_id')
        if str(target.get('group_user_id') or '') not in allowed_users:
            raise ValueError('不得移动未经本次行动或已通过表决授权的其他角色')
        pid = str(target.get('id') or target.get('participant_id'))
        if pid in seen:
            raise ValueError('同一角色本轮只能提交一个最终位置')
        seen.add(pid)
        normalized.append({**operation, 'target_id': pid})
    return tuple(normalized)


def resolve_companions(
    participants: Iterable[str],
    roster: Sequence[Mapping[str, Any]],
    acting_user_id: str,
) -> list[str]:
    """把模型声明的 ``participants`` 解析成本副本活跃成员的 group_user_id。

    模型点错名字（写成 NPC 名、已退场的人、或对不上任何成员）时**丢弃该条**
    而不是整轮失败——叙事正文已经写出来了，为一个人名对不上让玩家重来一次
    没有意义。行动者本人始终在结果里，且排在首位。
    """
    active = [
        member
        for member in roster
        if isinstance(member, Mapping)
        and member.get("participation_status") == "active"
        and member.get("group_user_id")
    ]
    actor = str(acting_user_id or "")
    resolved: list[str] = [actor] if actor else []
    for ref in participants:
        member = _match_member(ref, active)
        if member is None:
            continue
        user_id = str(member.get("group_user_id") or "")
        if user_id and user_id not in resolved:
            resolved.append(user_id)
    return resolved


def filter_movement(
    operations: Iterable[Mapping[str, Any]],
    roster: Sequence[Mapping[str, Any]],
    allowed_users: Iterable[str],
) -> tuple[dict[str, Any], ...]:
    """丢弃未被授权者写下的 location_ops——``validate_movement`` 的不报错版。

    个人行动路径的权威是模型的 ``participants`` 声明：没点名的人不许移动。
    这里丢弃而不是抛错，理由同上：正文已经生成，一条越界的移动记录不值得
    让整轮作废；状态以引擎为准，名单外的人保持原位。``target_id`` 统一归一化
    成 participant_id，稳定 ID 才是状态层认的键。
    """
    allowed = {str(item) for item in allowed_users if item}
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for operation in operations:
        if not isinstance(operation, Mapping):
            continue
        target = _match_member(operation.get("target_id"), roster)
        if target is None:
            continue
        if str(target.get("group_user_id") or "") not in allowed:
            continue
        pid = str(target.get("id") or target.get("participant_id") or "")
        if not pid or pid in seen:
            continue
        seen.add(pid)
        normalized.append({**dict(operation), "target_id": pid})
    return tuple(normalized)
