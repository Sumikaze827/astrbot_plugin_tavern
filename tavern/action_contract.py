"""Small, structured current-turn context shared by review and repair."""
import json


ACTION_POLICY = (
    '先兑现本轮行动，再衔接章节。章节进度是主线阶段，不是人物已抵达的地点或时间。'
    '会面、询问、谈判必须交代实际回应、结果或具体阻碍；不能用风景和等待替代。'
    '计划、答应和准备不等于已执行；不能用“按之前商定”虚构尚未谈定的细节。'
    '允许略写无事件的路程，但不能省略玩家选择的核心互动，也不能替其他分队转场。'
    '章节已推进而人物尚在上一场景时，先结算当前行动，再提供有因果的过渡；'
    '不强迫回退章节或重复已经完成的任务。等待也应说明有无变化，不能强制凭空造事。'
    '原作片段只作参考，不能代替本局行动、时间、位置和知识证据。'
)


def turn_contract(prompt):
    # Read known protocol blocks, not natural-language keywords/regular expressions.
    result = {}
    for tag in ('player_input', 'player_freeform_action', 'dm_instruction', 'acting_scene',
                'acting_player', 'next_actor', 'active_party', 'previous_personal_action',
                'authoritative_check', 'authoritative_check_result', 'runtime_state'):
        start = prompt.find('<' + tag)
        if start < 0:
            continue
        opening = prompt.find('>', start)
        end = prompt.find('</' + tag + '>', opening)
        if opening < 0 or end < 0:
            continue
        try:
            result[tag] = json.loads(prompt[opening + 1:end])
        except (ValueError, TypeError):
            continue
    runtime = result.pop('runtime_state', {})
    if isinstance(runtime, dict) and isinstance(runtime.get('travel_groups'), list):
        result['travel_groups'] = runtime['travel_groups']
    # Exclude inventories and unrelated history; preserve the actual action intact.
    for key in ('acting_player', 'next_actor'):
        actor = result.get(key)
        if isinstance(actor, dict):
            result[key] = {k: v for k, v in actor.items() if k in
                {'participant_id', 'name', 'character_name', 'visible_location', 'same_as_acting_player'}}
    past = result.get('previous_personal_action')
    if isinstance(past, dict):
        result['previous_personal_action'] = {k: v for k, v in past.items()
            if k in {'available', 'turn', 'decision', 'settled_narrative', 'intervening_turns', 'truncated'}}
    return result
