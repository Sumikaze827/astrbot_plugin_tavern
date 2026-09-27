"""Evidence-gated NPC relevance, perception and decision sets.

Author-confirmed exact location mappings are a migration bridge, not prefix
matching. Proposed movement and model confidence never authorize perception.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping


def location_state(actor):
    return actor.get('runtime_state') or actor.get('state') or {}


def anchor(actor, policy):
    state = location_state(actor)
    text = str(state.get('current_location') or state.get('location') or '').strip()
    # A stale explicit anchor must not silently fall back to an old mapping.
    spatial = state.get('spatial') or {}
    if spatial:
        if (spatial.get('status') == 'confirmed' and spatial.get('location_text') == text
                and spatial.get('evidence_id') in policy.get('evidence_ids', [])
                and spatial.get('scene_id') in policy.get('scenes', {})):
            return dict(spatial)
        return {'status': 'stale', 'location_text': text}
    entry = (policy.get('location_map') or {}).get(text)
    if (isinstance(entry, Mapping) and entry.get('evidence_id') in policy.get('evidence_ids', [])
            and entry.get('scene_id') in policy.get('scenes', {})):
        return dict(entry, status='confirmed', location_text=text)
    return {'status': 'unknown', 'location_text': text}


def names(npc):
    return [str(n) for n in [npc.get('name'), *(npc.get('aliases') or [])] if n]


def _normalized_place(text):
    return re.sub(r'\s+', '', str(text or '')).strip('（）。,，.；;')


def co_located(origin, dest):
    """Same scene if two confirmed anchors share a scene, or if the raw
    location text plainly names the same place.

    The author-maintained location_map is a migration bridge, not a complete
    world model: once the story walks into a place nobody mapped (a safe house,
    a cell, a shop), every actor's anchor degrades to ``unknown`` and a
    map-only rule would declare the whole scene non-existent. Text equality /
    containment keeps the obvious case (both sides hold the exact same place
    string) working without granting anything to merely similar names.
    """
    if origin.get('status') == 'confirmed' and dest.get('status') == 'confirmed':
        return origin.get('scene_id') == dest.get('scene_id')
    a, b = _normalized_place(origin.get('location_text')), _normalized_place(dest.get('location_text'))
    if not a or not b:
        return False
    return a == b or a in b or b in a


def speech_action(action, npcs):
    """Legacy offline parser; live prepare_direction bypasses this function.

    Only an explicit speech segment is exposed to listeners, not hidden plans
    from the rest of the player command.
    """
    mentioned, addressed, utterances = [], [], []
    for npc in npcs:
        nid = str(npc['id'])
        for name in names(npc):
            if name not in action:
                continue
            mentioned.append(nid)
            pattern = (r'(?:向|对|询问|追问|问|告诉|告知|请求)\s*' + re.escape(name)
                       + r'(?:\s*(?:说|说道|询问|追问|问|表示|喊道|喊|解释|请求))?\s*[：:，,]?([^。\n；;]+)')
            match = re.search(pattern, action)
            if match:
                addressed.append(nid)
                # Do not expose adjacent unspoken thoughts/plans to the listener.
                utterances.append(re.split(r'但我|同时|心里|心想|暗中|悄悄准备|打算随后', match.group(0))[0])
                break
    movement = bool(re.search(r'走进|走出|凑近|靠近|移到|传送|跑到|潜入|先.*再|然后', action))
    private = bool(re.search(r'低声|耳语|私语|悄悄|耳边', action))
    remote = bool(re.search(r'传讯|传信|写信|心灵|电话|通信|希望.*知道', action))
    mode = 'private_channel' if remote else 'whisper' if private else 'shout' if re.search(r'大喊|高声|喊话', action) else 'public'
    ambiguous_private = private and len(set(addressed)) > 1
    return {'mentioned_actor_ids': list(dict.fromkeys(mentioned)),
        'addressed_actor_ids': list(dict.fromkeys(addressed)),
        'speech_mode': mode, 'speech_content': '；'.join(dict.fromkeys(utterances))[:800],
        'pending_sequence': movement, 'requires_delivery': remote,
        'status': 'explicit_speech' if addressed and not movement and not remote and not ambiguous_private else 'unconfirmed'}


def scope_for_action(*, world, session, player, action, npcs, personal_text='', interaction=None):
    policy = ((world.get('rules') or {}).get('npc_direction') or {}).get('interaction', {})
    origin = anchor(player, policy)
    parsed = interaction if interaction is not None else speech_action(action, npcs)
    action_id = 'npc_action_' + hashlib.sha256((str(session.get('id')) + ':' + str(session.get('revision'))
        + ':' + str(player.get('id') or player.get('user_id')) + ':' + action).encode()).hexdigest()[:24]
    contexts, perceptions, decisions, dependencies = [], [], [], []
    for npc in npcs:
        if npc.get('lifecycle_status', 'active') != 'active' or npc.get('review_status') == 'rejected':
            continue
        nid = str(npc['id'])
        dest = anchor(npc, policy)
        same_scene = co_located(origin, dest)
        referenced = nid in parsed['mentioned_actor_ids']
        recent = any(n in personal_text for n in names(npc))
        if referenced or same_scene or recent:
            contexts.append(nid)
            dependencies.append({'id': nid, 'revision': int(npc.get('revision') or 0),
                                 'state_revision': int(npc.get('state_revision') or 0)})
        state = location_state(npc)
        capable = not (state.get('conscious') is False or state.get('hearing') is False
                       or state.get('status') in {'dead', 'unconscious'})
        qualifies = False
        if parsed['status'] == 'explicit_speech' and capable:
            scene = (policy.get('scenes') or {}).get(origin.get('scene_id'), {})
            if same_scene:
                if parsed['speech_mode'] in {'public', 'shout'}:
                    qualifies = scene.get('public_hearing') is True
                elif parsed['speech_mode'] == 'whisper':
                    qualifies = (nid in parsed['addressed_actor_ids'] and bool(origin.get('zone_id'))
                        and origin.get('zone_id') == dest.get('zone_id') and scene.get('whisper_same_zone') is True)
            if not same_scene and parsed['speech_mode'] == 'shout' and origin.get('status') == dest.get('status') == 'confirmed':
                qualifies = any(edge.get('from') == origin.get('scene_id') and edge.get('to') == dest.get('scene_id')
                    and edge.get('shout_content') is True and edge.get('evidence_id') in policy.get('evidence_ids', [])
                    for edge in policy.get('relations', []))
        if qualifies:
            perceptions.append({'observer_id': nid, 'source_action_id': action_id, 'modality': 'hearing',
                'level': 'content', 'knowledge_type': 'reported', 'content': parsed['speech_content'],
                'status': 'candidate', 'evidence_id': dest.get('evidence_id')})
            if nid in parsed['addressed_actor_ids'] and state.get('can_speak', True):
                decisions.append(nid)
    return {'action_id': action_id, 'action': parsed, 'origin': origin,
        'enforce_knowledge': parsed['status'] == 'explicit_speech',
        'context_actors': list(dict.fromkeys(contexts)), 'perception_candidates': perceptions,
        'decision_actors': decisions, 'dependencies': dependencies,
        'policy': '提及不是在场，同场不是知情，候选不是已完成；私语、未结算移动和传讯不得自动送达。未授权人物不得得知本次发言或回应；未知范围先处理可确定部分，不编造答复。'}


CREDENTIAL_STATE_KEYS = frozenset({'spatial', 'scene_id', 'location_evidence_id', 'knowledge_receipts'})


def sanitize_npc_ops(ops, scope):
    """Strip unverified knowledge and self-granted credentials from model ops.

    Returns ``(cleaned_ops, violations)``. This is deliberately non-fatal:
    2026-09-20 the whole turn was voided (「本轮裁定未完成，世界状态没有改变」)
    because the narrator added a ``known_facts`` entry for an on-stage NPC in a
    scene the author's location_map never covered, so the evidence gate could
    not certify anyone as a listener and the repair round failed too.

    Nothing authoritative is lost by dropping the entry: in a gated session
    ``known_facts`` is committed from perception receipts, never from model
    assertions (see ``repositories/worlds.py`` commit path), so the illegal
    entry would have been discarded anyway. Credential keys are stripped
    rather than accepted, so a model still cannot award itself space or
    knowledge receipts; only the bookkeeping is repaired instead of the turn.
    """
    cleaned: list[dict] = []
    violations: list[dict] = []
    if not scope:
        return [dict(op) for op in ops if isinstance(op, Mapping)], violations
    allowed = {p['observer_id'] for p in scope.get('perception_candidates', [])}
    enforce = bool(scope.get('enforce_knowledge'))
    for op in ops:
        if not isinstance(op, Mapping):
            continue
        item = dict(op)
        npc_id = str(item.get('npc_id') or '')
        if enforce and (item.get('known_facts') or item.get('misconceptions')):
            if npc_id not in allowed:
                violations.append({'npc_id': npc_id, 'kind': 'knowledge_without_perception',
                    'known_facts': len(list(item.get('known_facts') or [])),
                    'misconceptions': len(list(item.get('misconceptions') or []))})
                item['known_facts'] = []
                item['misconceptions'] = []
        state = item.get('runtime_state')
        if isinstance(state, Mapping):
            removed = sorted(k for k in state if k in CREDENTIAL_STATE_KEYS)
            if removed:
                violations.append({'npc_id': npc_id, 'kind': 'self_granted_credentials', 'removed': removed})
                item['runtime_state'] = {k: v for k, v in state.items() if k not in CREDENTIAL_STATE_KEYS}
        cleaned.append(item)
    return cleaned, violations


def validate_dependencies(connection, session_id, scope):
    from .database_support import DatabaseConflictError
    for dep in (scope or {}).get('dependencies', []):
        row = connection.execute('SELECT sc.revision, COALESCE(st.revision,0) FROM session_characters sc '
            'LEFT JOIN session_character_states st ON st.character_id=sc.id WHERE sc.id=? AND sc.session_id=?',
            (dep['id'], session_id)).fetchone()
        if not row or tuple(row) != (dep['revision'], dep['state_revision']):
            raise DatabaseConflictError('NPC状态在决策期间变化，请基于最新现场重试')


def record_perceptions(connection, session_id, scope, source_event_id, turn, now):
    """Only the turn transaction upgrades eligible speech into reported knowledge.

    Never turns a player's assertion into an objective fact. Uses a stable
    action id to avoid duplicate receipts on a retried commit.
    """
    import json
    for perception in (scope or {}).get('perception_candidates', []):
        row = connection.execute('SELECT sc.known_facts_json, st.state_json FROM session_characters sc '
            'LEFT JOIN session_character_states st ON st.character_id=sc.id WHERE sc.id=? AND sc.session_id=?',
            (perception['observer_id'], session_id)).fetchone()
        if not row:
            continue
        state = json.loads(row[1] or '{}')
        receipts = list(state.get('knowledge_receipts') or [])
        pid = scope['action_id'] + ':' + perception['observer_id']
        if any(r.get('id') == pid for r in receipts):
            continue
        fact = '听到玩家发言（只是其陈述/请求，不证明内容为真）：' + perception['content']
        receipts.append({'id': pid, 'source_event_id': source_event_id, 'acquired_turn': turn,
            'knowledge_type': 'reported', 'content': fact, 'validity': 'current'})
        state['knowledge_receipts'] = receipts[-40:]
        known = list(json.loads(row[0] or '[]'))
        if fact not in known:
            known.append(fact)
        connection.execute('UPDATE session_characters SET known_facts_json=?,revision=revision+1,updated_at=? WHERE id=?',
            (json.dumps(known[-60:], ensure_ascii=False), now, perception['observer_id']))
        connection.execute('INSERT INTO session_character_states(character_id,state_json,revision,updated_at) VALUES (?,?,1,?) '
            'ON CONFLICT(character_id) DO UPDATE SET state_json=excluded.state_json,revision=revision+1,updated_at=excluded.updated_at',
            (perception['observer_id'], json.dumps(state, ensure_ascii=False), now))
