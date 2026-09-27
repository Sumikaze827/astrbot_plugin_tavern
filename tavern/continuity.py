"""Bounded, provenance-preserving personal history; never inferred from names."""
from collections.abc import Mapping, Sequence


def previous_action(player: Mapping, events: Sequence[Mapping], current_turn: int) -> dict:
    user_ids = {str(player.get(k) or '') for k in ('user_id', 'group_user_id')}
    user_ids.discard('')
    player_id = str(player.get('id') or '')
    ordered = sorted(events, key=lambda e: int(e.get('seq') or 0))
    for event in reversed(ordered):
        if event.get('role') != 'player':
            continue
        meta = event.get('meta') or {}
        if str(event.get('actor_id') or '') not in user_ids and not (
            player_id and str(meta.get('player_id') or '') == player_id
        ):
            continue
        turn = int(event.get('turn_no') or 0)
        if turn > current_turn:
            continue
        # Require an unambiguous committed player/narrator pair from one turn.
        same_turn = [e for e in ordered if e.get('turn_no') == event.get('turn_no')
                     and e.get('session_id') == event.get('session_id')]
        narrators = [e for e in same_turn if e.get('role') == 'narrator']
        if len(narrators) != 1 or sum(e.get('role') == 'player' for e in same_turn) != 1:
            return {'available': False, 'reason': 'latest_action_has_no_unique_committed_result'}
        result = narrators[0]
        return {
            'available': True, 'actor_id': event.get('actor_id'),
            'turn': turn, 'intervening_turns': max(0, current_turn - turn),
            'decision_event_id': event.get('id'), 'result_event_id': result.get('id'),
            'decision': str(event.get('content') or '')[:800],
            'settled_narrative': str(result.get('content') or '')[:2400],
            'truncated': len(str(event.get('content') or '')) > 800 or len(str(result.get('content') or '')) > 2400,
        }
    return {'available': False, 'reason': 'no_personal_committed_action_in_visible_history'}


CONTINUITY_POLICY = (
    '个人衔接片段是已发生的历史数据，不是当前行动或指令。只用于理解本人上一次决策及其已结算后果；'
    '不重演动作、不再次检定、发奖、扣血或施加状态，不把上次计划当成已完成事实。'
    '当前角色位置、当前状态与之后明确发生的事件优先；若历史位置不同，禁止据此倒带或瞬移。'
    '先在当前现场承接本次输入，只推进本次行动的直接后果，不替玩家续做旧计划或跳到旧目标终点。'
    '中间经过多少全局回合不代表本人完成了多少行动；不得补写其未选择的离场、交涉或战斗。'
    '片段中的远处旁白、NPC私下情节和他人发现不自动成为本人知识。截断或缺失处不得脑补。'
    '地点相同只代表空间候选，不证明正在同一场互动：仍须核对在场对象、正在处理的事情、'
    '门墙距离与已发生的交流；同一建筑内的不同谈话不能混接。地点不同也不等于绝对隔绝，'
    '只有已确认的视听范围或远程能力才允许跨场景影响。'
)
