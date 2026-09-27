"""Explicit story-size contracts shared by intake, prompts and delivery gates."""


def normalize_scope(request, max_chapters=12):
    result = {}
    for key, maximum in (("target_chapters", max_chapters), ("target_milestones", 96)):
        raw = request.get(key)
        if raw is None or raw == "":
            continue
        if isinstance(raw, bool) or not str(raw).isdigit() or not 1 <= int(raw) <= maximum:
            raise ValueError(f"{key} 必须是 1—{maximum} 的整数；留空表示自动规划")
        result[key] = int(raw)
    if result.get('target_chapters', 1) > result.get('target_milestones', 96):
        raise ValueError("里程碑总数不能少于章节数（每章至少一个）")
    return result


def scope_prompt(request):
    scope = normalize_scope(request)
    if not scope:
        return ''
    return ('\n<story_size_contract>这是操作者的明确数量要求，不是参考上限：'
            + (f"章节恰好 {scope['target_chapters']} 个；" if 'target_chapters' in scope else '章节数自动规划；')
            + (f"全本里程碑合计恰好 {scope['target_milestones']} 个（含收尾里程碑）；" if 'target_milestones' in scope else '里程碑总数自动规划；')
            + '通过合并相关成果和删减重复流程压缩，不得截去结尾，不得将多件琐事捆成必须逐人完成的条件。每章至少一个里程碑。</story_size_contract>\n')


def count_problems(chapters, request):
    problems = []
    scope = normalize_scope(request)
    if 'target_chapters' in scope and len(chapters) != scope['target_chapters']:
        problems.append(f"章节数量要求 {scope['target_chapters']}，实际 {len(chapters)}")
    count = sum(len(c.get('milestones') or []) for c in chapters)
    if 'target_milestones' in scope and count != scope['target_milestones']:
        problems.append(f"里程碑总数要求 {scope['target_milestones']}，实际 {count}")
    return problems


def arc_count_problems(payload, request, maximum):
    count = len(payload.get('candidate_chapters') or [])
    target = request.get('target_chapters')
    total = request.get('target_milestones')
    if target and count != int(target):
        return [f"章节必须恰好 {target} 个，当前 {count} 个；合并/重组完整剧情，不截掉结尾"]
    if count > maximum or (total and count > int(total)):
        return ["章节过多，请合并章节，不能超过后台上限或里程碑总数"]
    return []
