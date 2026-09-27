"""Persistent travel cohorts; participation in dialogue is not travel consent."""
from dataclasses import replace
from .party_scope import _match_member


POLICY = ('travel_groups 是持续同行小队，不是本轮谁发言的名单。普通赶路、进门、休整默认延续小队同行，'
    '不因没重复说“我们”或 participants 漏人而拆队。只有明确独行、留守、分头任务或已发生的强制隔离才拆队。'
    '用 travel_change={"mode":"split|join","members":[participant_id],"reason":"具体依据"} 提出变化；'
    'split 将 members 与原小队其余成员分开，join 只合并实际同场且明确会合的成员。没有变化省略。'
    '不得用同行代替发言、消耗资源、接受危险或不可逆决定；这些仍须本人行动或表决。'
    '普通移动小队所有成员的位置应一致；已分队的人不能跟随另一队移动；位置未知不推定在场。'
    '小队记录与权威位置不符时按位置拆开（只拆不并），不自动把人拉回一起。')


def _active_map(roster):
    active = {}
    for p in roster:
        pid = str(p.get('id') or p.get('participant_id') or '')
        if pid and p.get('participation_status') == 'active':
            active[pid] = p
    return active


def _place(pid, active):
    return str((active[pid].get('runtime_state') or {}).get('current_location') or '').strip()


def _by_place(members, active):
    """按权威位置切分成员；位置未知者各自单列（未知不等于同场）。"""
    buckets = {}
    for pid in members:
        buckets.setdefault(_place(pid, active) or 'solo:' + pid, []).append(pid)
    return list(buckets.values())


def cohorts(roster, saved=None):
    """当前同行小队，并按权威位置自愈。

    ``saved`` 为空表示尚未初始化：只按**完全相同且非空**的位置建队，散在
    不同地点的人不会被自动合并（旧存档首次加载走这条）。

    已初始化时保留记录成员，但成员位置已经分叉的小队会被就地拆开。这一步
    只拆不并，所以永远安全；不拆的话，任何绕过同行结算的移动（例如主持推进
    单独把人挪走）都会留下一支与事实不符的小队，之后该成员每次普通移动都会
    撞上「同行记录与实际位置冲突」，而模型无从修复——整轮会反复失败。
    """
    active = _active_map(roster)
    groups, seen = [], set()
    for group in (saved or []):
        if not isinstance(group, list):
            continue
        members = [p for p in group if isinstance(p, str) and p in active and p not in seen]
        if members:
            seen.update(members)
            groups.extend(_by_place(list(dict.fromkeys(members)), active))
    rest = [pid for pid in active if pid not in seen]
    if saved:
        # 已初始化过：没登记的成员按独行者处理，不因位置标签相同而被并队。
        groups.extend([[pid] for pid in rest])
    else:
        groups.extend(_by_place(rest, active))
    return groups


def settle_travel(resolution, roster, groups, actor_id, *, acceptance_mode=False):
    if acceptance_mode:
        try:
            return settle_travel(resolution, roster, groups, actor_id)
        except ValueError as exc:
            # Freeform feasibility is settled by acceptance, not a turn-aborting gate.
            # Keep the established cohort; discard unconfirmed cross-party writes.
            own = next((g for g in groups if actor_id in g), [actor_id])
            actor_ops = []
            for op in resolution.location_ops:
                member = _match_member(op.get('target_id'), roster)
                pid = str((member or {}).get('id') or (member or {}).get('participant_id') or '')
                if pid == actor_id:
                    actor_ops = [dict(op, target_id=pid)]
                    break
            raw = dict(resolution.raw)
            raw.pop('travel_change', None)
            raw['_travel_adjustment'] = str(exc)
            patched = replace(resolution, raw=raw, location_ops=tuple(actor_ops))
            try:
                return settle_travel(patched, roster, groups, actor_id)
            except ValueError:
                # Conflicting legacy locations: no invented relocation to repair them.
                return replace(patched, location_ops=(), raw=dict(raw, _travel_groups=groups),
                    participants=tuple(own))
    active = {str(p.get('id') or p.get('participant_id')): p for p in roster
              if p.get('participation_status') == 'active'}
    groups = [list(g) for g in groups]
    change = resolution.raw.get('travel_change')
    if change:
        if not isinstance(change, dict) or change.get('mode') not in {'split', 'join'}:
            raise ValueError('travel_change 必须为 split 或 join')
        members = change.get('members')
        if (not isinstance(members, list) or not members or any(not isinstance(p, str) or p not in active for p in members)
                or not isinstance(change.get('reason'), str) or not change['reason'].strip()):
            raise ValueError('同行变化必须有活跃成员稳定ID和具体分开/会合依据')
        members = list(dict.fromkeys(members))
        own = next((g for g in groups if actor_id in g), [])
        if change['mode'] == 'split':
            if not set(members) < set(own):
                raise ValueError('只能拆分行动者所在小队的部分成员')
        else:
            if actor_id not in members or len(members) < 2:
                raise ValueError('会合名单必须包含行动者和其他成员')
            places = {str((active[p].get('runtime_state') or {}).get('current_location') or '').strip() for p in members}
            if len(places) != 1 or not all(places):
                # A pending reunion is not a malformed turn, on ANY input path.
                # Do not merge remote parties or let a proposed join move them.
                raw = dict(resolution.raw)
                raw.pop('travel_change', None)
                raw['_travel_adjustment'] = '会合意图尚未落实，保留原小队；只结算行动者原小队可成立的移动'
                allowed_ops = []
                for op in resolution.location_ops:
                    target = _match_member(op.get('target_id'), roster)
                    pid = str((target or {}).get('id') or (target or {}).get('participant_id') or '')
                    if pid in own:
                        allowed_ops.append(dict(op, target_id=pid))
                return settle_travel(replace(resolution, raw=raw, location_ops=tuple(allowed_ops)),
                    roster, groups, actor_id, acceptance_mode=True)
        groups = [[p for p in g if p not in members] for g in groups]
        groups = [g for g in groups if g] + [members]
    own = next((g for g in groups if actor_id in g), [actor_id])
    ops = {}
    for op in resolution.location_ops:
        member = _match_member(op.get('target_id'), roster)
        pid = str((member or {}).get('id') or (member or {}).get('participant_id') or '')
        if pid not in own:
            raise ValueError('不得移动非同行小队成员；分头后其他小队保持原位')
        ops[pid] = dict(op, target_id=pid)
    if ops:
        destinations = {op['location'] for op in ops.values()}
        if len(destinations) != 1:
            raise ValueError('同行成员目的地不一致；明确分头必须先声明travel_change')
        destination = next(iter(destinations))
        # Do not teleport a cohort member whose authoritative position has diverged.
        origins = {str((active[p].get('runtime_state') or {}).get('current_location') or '').strip() for p in own if p in active}
        if len(own) > 1 and (len(origins) != 1 or not all(origins)):
            raise ValueError('同行记录与实际位置冲突；须明确分开或先会合，不能自动拉回')
        ops = {pid: {'target_id': pid, 'location': destination} for pid in own}
    raw = dict(resolution.raw, _travel_groups=groups)
    patch = dict(resolution.state_patch)
    patch.pop('travel_groups', None)  # Only validated cohorts may persist.
    return replace(resolution, raw=raw, state_patch=patch, location_ops=tuple(ops.values()),
        participants=tuple(dict.fromkeys([*resolution.participants, *own])))


def authorized_movement_groups(roster, saved, operations):
    """Passed votes may split a cohort; coincident destinations never merge teams."""
    locations = {str(p.get('id') or p.get('participant_id')):
        str((p.get('runtime_state') or {}).get('current_location') or '') for p in roster}
    for op in operations:
        member = _match_member(op.get('target_id'), roster)
        if member:
            locations[str(member.get('id') or member.get('participant_id'))] = op.get('location') or ''
    result = []
    for group in cohorts(roster, saved):
        partitions = {}
        for pid in group:
            partitions.setdefault(locations.get(pid) or 'solo:' + pid, []).append(pid)
        result.extend(partitions.values())
    return result
