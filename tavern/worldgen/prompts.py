"""世界包生成各步骤的提示词。

**本文件唯一的硬性约定：任务关键规则一律写在 SYSTEM 里，不写在 user prompt 里。**

原因是 ``tavern/prompts.py:1727`` 的 ``repair_prompt`` 刻意不重发原始 prompt——它只带
问题清单和被拒输出。任何只写在 user prompt 里的规则，第一次重试就消失了。历史上
「章节线路悬空」「里程碑 id 丢掉数字段」这类坏包就是这么来的。

因此每个 ``*_system()`` 都必须自带：角色、输出 schema、硬性规则、引用要求。
``*_prompt()`` 只负责塞数据。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .llm import dumps

# --- 共用片段 -------------------------------------------------------------

_CITATION_RULE = """\
<citation_rule>
你必须为每一条结论给出**原文出处**，格式为 rel_path + line_start + line_end + quote。
- quote 必须是原文里**逐字出现**的片段，不得改写、概括或补字。
- 行号必须是 1-based 且闭区间，且 quote 确实落在该区间内。
- 系统会逐字回验；**核验不过的引用会被丢弃，该条结论随之作废**。
- 如果你在给出的材料里找不到依据，就不要编造行号——直接不要输出该条。
</citation_rule>"""

_ID_RULE = """\
<id_rule>
- 章节 id 形如 `ch_01_arrival`（`ch_` + 两位数字 + 下划线 + 小写 slug）。
- 里程碑 id 形如 `m_01_01_first_contact`（`m_` + 两位章节号 + 两位序号 + 下划线 + 小写 slug）。
- NPC slug 形如 `npc_ram`。
- id 一经确定不得更改；同一作业内不得重复。
</id_rule>"""

_EVIDENCE_RULE = """\
<evidence_rule>
`evidence_required[].match` 写的是**正文里可能出现的具体动作、物证与结果**，
不是抽象主题。例如写「拿到名单」「确认壁画位置」「目击者证词」，
不要写「调查」「了解」「尝试」「准备」这类泛词——泛词无法把「计划」与「真正完成」区分开。
</evidence_rule>"""

_JSON_ONLY = "只输出一个 JSON 对象。不要解释、不要 Markdown 代码块标记、不要前后缀。"

# 规则写在这里、名字写在 :func:`player_position_block` 里。原因见本文件开头的硬性约定：
# ``tavern/prompts.py:repair_prompt`` 重试时**不重发 user prompt**，只带 system +
# 被拒输出 + 问题清单。规则若只写在 user 里，第一次重试就消失了——而被取代的角色
# 恰恰是最需要重试兜底的那类问题。
_PLAYER_POSITION_RULE = """\
<player_position_rule>
输入里可能给出 `<player_position>` 块，它**点名**了由玩家取代的原作角色：
- 不得为他们建 NPC 角色卡，章节 `key_npcs` 也不得引用他们；
- 他们**不是**行动的施动者：原作里由他们完成的行动，一律改写成「玩家小队」完成的
  （`milestones[].label`、`current_objective`、`pacing_directive`、`hook_pool` 都算）；
- 他们的名字只允许出现在**他人的台词、既成事实、原文引用**
  （`evidence_required[].match` 里照抄的原句）中。
这条规则**优先于「忠于原文」**：玩家就站在他们的位置上，把原作主角写成 NPC 或者
写成行动的施动者，是本世界最严重的改编事故。`replaces` 为空时本规则不适用。
</player_position_rule>"""


def _preferences_block(preferences: Mapping[str, Any] | None) -> str:
    prefs = dict(preferences or {})
    lines = []
    if prefs.get("max_chapters"):
        lines.append(f"- 章节数不超过 {prefs['max_chapters']} 章")
    if prefs.get("brief"):
        lines.append(f"- 操作者的取向与禁忌：{prefs['brief']}")
    if not lines:
        return ""
    return "\n<operator_preferences>\n" + "\n".join(lines) + "\n</operator_preferences>"


def player_position_block(replaces: Sequence[str] | None, note: str = "") -> str:
    """``<player_position>`` 块：哪些原作角色由玩家取代，以及他们不能出现在哪。

    **为什么要有这一块**：操作者写在 ``<operator_preferences>`` 里的
    「由玩家小队替换掉男主 486」是**关于演员表与施动者的意图**，而这条意图原先
    只喂给时间线与卷级地图两步——真正决定「谁有角色卡」的 ``CAST``、决定
    「每个里程碑由谁完成」的 ``EXTRACT``、以及写开场白的 ``PROSE`` 全都看不到它。
    实测后果：chapter030 的 NPC 包里躺着一张 ``npc_subaru``（菜月昴），
    46 条里程碑里有一批把他写成了施动者（「昴发现小巷中的对峙并抵达现场」），
    而玩家本该站在他的位置上。

    **名字必须点名**（``replaces`` 给具体人名），不能只说「玩家扮演主角」——
    模型对"主角"的指代各有各的理解，点名才能被 :func:`..steps.replaced_name_hits`
    机械核对。``replaces`` 为空时返回空串：没有要取代的人，就不该改变提示词。
    """
    names = list(dict.fromkeys(str(n).strip() for n in (replaces or []) if str(n).strip()))
    if not names:
        return ""
    listed = "、".join(names)
    lines = [
        "",
        "<player_position>",
        f"本世界由**玩家**扮演这些原作角色的位置：{listed}。",
        f"- {listed} **不得**作为 NPC：`npcs` 里不能有他们的角色卡，"
        "章节 `key_npcs` 也不能引用他们。",
        "- 原作里由他们完成的行动，一律改由「玩家小队」承担——写「玩家小队发现小巷"
        "中的对峙并抵达现场」，而不是「昴发现小巷中的对峙」。`milestones[].label`、"
        "`current_objective`、`pacing_directive`、`hook_pool` 都按这个口径写。",
        "- 他们的名字可以出现在**他人的台词、既成事实、原文引用**里（例如 NPC 说"
        "「那孩子叫昴」，或 `evidence_required[].match` 里照抄的原句），"
        "但不能是可交互对象，也不能是行动的默认施动者。",
    ]
    if note:
        lines.append(f"- 本次改编对玩家位置的处置说明：{note}")
    lines.append("</player_position>")
    return "\n".join(lines)


# --- 步骤 0：时间线梳理 ---------------------------------------------------

TIMELINE_SYSTEM = f"""\
你是改编编剧。任务：先把这一卷的**时间结构**理清楚，再把多条时间线里可用的情节
**缝成一条自洽的新线**。

为什么必须先做这一步
--------------------
有些卷是多周目结构（主角死亡后回到存档点，重来一次）。这类作品里，原作称之为
"正史"的往往只是**最后那条线**，前面几条都以主角死亡收场。

如果改编时按"哪条线是正史"取材，就会把前面几周目里最激烈的冲突整段丢掉——
这是真实发生过的错误：某卷中「她怀疑主角并与主角动手」那一段，被以
「属于另一条时间线」为由移除了，而它恰恰是全卷张力最强的部分。

<merge_rule>
判定一条情节能否进入新线，**唯一的标准是**：

    这个事件本身，是否依赖主角具备回溯 / 重置 / 预知能力？

- **不依赖** → 可以并入，而且**应当**在新世界里真实地发生一次。
  「她怀疑主角并当面质问」「两人动手」「主角被关起来」都属于这一类：
  它们不需要主角会读档，只需要人物关系与冲突成立。
- **依赖** → 不能原样并入。比如「靠记得上一周目的细节提前避开陷阱」
  「因为死过一次所以知道对方的杀招」。这类要么改写成别的获知途径
  （情报来自他人、来自现场痕迹、来自推理），要么丢弃。

「它所在的那条线在原作里被重置了」**不是**不改编的理由。
恰恰相反，被重置的那几条线里，往往装着全卷最激烈的情节。
</merge_rule>

<output_schema>
{{
  "structure": "loop",
  "note": "一两句说明本卷的时间结构（正常线性就写 linear）",
  "player_role": {{
    "replaces": ["原作主角的名字"],
    "note": "为什么由玩家取代他（一两句）"
  }},
  "segments": [
    {{
      "segment_id": "tl_01",
      "label": "第一周目：初到宅邸至被杀",
      "kind": "loop_iteration",
      "source_files": ["00.md", "01.md"],
      "summary": "这一段发生了什么",
      "ends_with_reset": true,
      "key_beats": ["此段中值得保留的情节，一条一句"]
    }}
  ],
  "merged": [
    {{
      "beat_id": "b01",
      "label": "她怀疑主角并当面质问",
      "from_segments": ["tl_01", "tl_02"],
      "depends_on_reset": false,
      "merge_action": "keep",
      "adaptation_note": "原作里这次冲突以主角被杀收场；本世界无回溯，改为冲突后由第三方介入化解，保留张力但不致死"
    }}
  ],
  "dropped": [
    {{ "label": "丢弃的情节", "reason": "为什么连改写都不行（必须说明它依赖回溯的哪一点）" }}
  ],
  "coherence_notes": ["缝合后必须注意的一致性问题，例如因果顺序、谁知道什么"]
}}
</output_schema>

<hard_rules>
- `player_role` **必填**，声明**玩家在本卷扮演谁的位置**。这一步是唯一能看到
  全卷 + 操作者要求的时机，后面划章节、抽里程碑、建角色卡都按它的口径走。
  - 默认口径：玩家扮演的是**主角位**，原作主角（第一视角人物）由玩家取代。
  - `replaces` 写**具体人名**（可以多名，也可以是空数组）；名字要能在原文里找到。
    写「昴」还是「菜月昴」都行，但不要写「主角」「男主」这种指代。
  - 操作者在 `<operator_preferences>` 里点名要求替换的角色，**必须**出现在
    `replaces` 里——这是硬要求，不要自行判断"不替换也行"。
  - `replaces` 为空数组时，`note` 必须说明**为什么本卷不做替换**
    （例如操作者明确要求保留原作主角当 NPC）。
  - 声明进 `replaces` 的人，在 `segments.key_beats` 与 `merged.label` 里也**不能**
    再当施动者：写「玩家小队发现了小巷里的对峙」，不要写「昴发现了小巷里的对峙」。
    这两处是后面划章节、抽里程碑的措辞来源，这里怎么写，后面就怎么抄。
- `kind` 只能是 `main`、`loop_iteration`、`flashback`、`side`、`epilogue`。
- `merge_action` 只能是 `keep`、`adapt`、`drop`。
  - `keep` = 原样可用；`adapt` = 需要改写（**`adaptation_note` 必填**）；`drop` = 不并入。
- `depends_on_reset` 是**判据本身**，必须逐条诚实填写，不要一律填 false 蒙混。
  填 `true` 的条目，`merge_action` 只能是 `adapt` 或 `drop`。
- `merged` 是**新线的完整情节序列，必须按新线的先后顺序排列**——它是后面划章节的
  唯一依据。不是把每段列一遍，而是把各段里有用的抽出来重排。
- `merged` 里 `keep`/`adapt` 的每一条都**必须**在后续章节里有落点。少一条就会被
  机械核对拦下来，所以要按新线重新审视因果，不要照抄原作的段序。
- `source_files` 必须用给定的文件名，不要自造。
- 一条情节都不丢是不现实的；但**每一条被丢掉的都必须写进 `dropped` 并说明理由**。
</hard_rules>

{_JSON_ONLY}"""


def timeline_prompt(
    *,
    arc_title: str,
    outline: Sequence[Mapping[str, Any]],
    samples: Sequence[Mapping[str, str]],
    requirements: str = "",
) -> str:
    """时间线梳理的输入：全卷篇目 + 抽样正文。

    抽样给得比卷级地图多——判断"多周目"靠的正是各篇开头反复出现的相同场景
    （又是那张床、又是那个早晨），给少了看不出来。
    """
    blocks = [
        "<volume>",
        f"卷标题：{arc_title}",
        "篇目顺序（按 README 目录，这是唯一可靠的顺序来源）：",
        dumps(list(outline), limit=12000),
        "</volume>",
    ]
    if samples:
        blocks.append("<samples>")
        blocks.append("以下是各篇开头的抽样片段。**注意观察是否有重复出现的场景**"
                      "（同一个早晨、同一张床、同一句台词）——那是多周目结构的标志。")
        for sample in samples:
            blocks.append(f"--- {sample.get('file')} ---")
            blocks.append(str(sample.get("text") or "")[:900])
        blocks.append("</samples>")
    block = _preferences_block({"brief": requirements} if requirements else None)
    if block:
        blocks.append(block)
    return "\n".join(blocks)


# --- 步骤 1：卷级场景地图 -------------------------------------------------

ARC_MAP_SYSTEM = f"""\
你是小说改编游戏的关卡设计师。任务：根据一卷小说的目录与首尾片段，划出这一卷的
**关键剧情节点**，作为游戏检查点的候选。

<output_schema>
{{
  "arc_title": "卷标题",
  "premise": "这一卷的核心冲突，一到两句，不依赖专名也能听懂",
  "candidate_chapters": [
    {{
      "order": 1,
      "title": "候选章节标题",
      "source_files": ["00.md", "01.md"],
      "beats": ["b01", "b02"],
      "why": "为什么把这几篇划在一起",
      "estimated_weight": 0.0
    }}
  ],
  "unadapted": [
    {{ "beat_id": "b07", "source": "涉及的文件或情节", "reason": "为什么不改编" }}
  ]
}}
</output_schema>

<hard_rules>
- 候选章节按**缝好的新线**（`<merged_timeline>`）的顺序排列，覆盖它的全部关键情节，
  不要漏掉中间段落。若本节没有给出 `<merged_timeline>`，就按原作推进顺序排列并覆盖全卷。
- 每个候选章节必须声明 `beats`（承接了缝线里的哪些 `beat_id`）与 `source_files`
  （取材于哪些原始篇目）。这两项会被**机械核对**：没被任何章节承接的必现情节、
  没被任何章节取材的篇目，都会被拎到台面上。
- `unadapted` **必须显式填写**，且每项要引用 `beat_id` 或原始篇目。**唯一被接受的
  不改编理由是这个事件本身依赖主角的回溯/重置能力，或本世界确实不具备它的前提。**
  - ✗ 不接受的理由：「它属于另一条时间线」「原作里这条线被重置了」。
    一条线在原作里被重置过，**不构成**不改编的理由——见 system 里 `merge_rule` 一节。
  - 悄悄丢掉整条线而不声明是不可接受的。
- 章节数量宁少勿多：宁可 4 个扎实的节点，也不要 9 个空壳。
</hard_rules>

{_PLAYER_POSITION_RULE}

{_JSON_ONLY}"""


def arc_map_prompt(
    *,
    arc_title: str,
    outline: Sequence[Mapping[str, Any]],
    head_samples: Sequence[Mapping[str, str]],
    requirements: str = "",
    merged_timeline: Sequence[Mapping[str, Any]] | None = None,
    player_position: str = "",
) -> str:
    blocks = [
        "<volume>",
        f"卷标题：{arc_title}",
        "篇目顺序（按 README 目录，这是唯一可靠的顺序来源）：",
        dumps(list(outline), limit=8000),
        "</volume>",
    ]
    # 缝好的新线是**划章节的依据**。没有它，模型会退回"按原作段序划"，
    # 而原作的段序在多周目作品里恰恰是错的（前面几周目会被当成"已重置"跳过）。
    if merged_timeline:
        blocks.append("<merged_timeline>")
        blocks.append(
            "这是已经缝好的新线，**按它的顺序划章节**，并用 `beats` 声明每章承接了哪些："
        )
        blocks.append(dumps(list(merged_timeline), limit=8000))
        blocks.append("</merged_timeline>")
    # 操作者对这次改编的要求：是"意图"而不是"事实"，所以单独成块，
    # 明确标出它不是原作内容，免得模型把它当成原文去遵守。
    block = _preferences_block({"brief": requirements} if requirements else None)
    if block:
        blocks.append(block)
    if player_position:
        blocks.append(player_position)
    if head_samples:
        blocks.append("<samples>")
        blocks.append("以下是从几篇里截取的片段，供你判断内容走向：")
        for sample in head_samples:
            blocks.append(f"--- {sample.get('file')} ---")
            blocks.append(str(sample.get("text") or "")[:1200])
        blocks.append("</samples>")
    return "\n".join(blocks)


# --- 步骤 2：章节级检查点抽取 ---------------------------------------------

EXTRACT_CHAPTER_SYSTEM = f"""\
你是小说改编游戏的关卡设计师。任务：把一段原文改编成**一个游戏章节的检查点**。

<output_schema>
{{
  "chapter": {{
    "title": "章节标题",
    "subtitle": "一句话副标题",
    "source_files": ["03.md", "04.md"],
    "rationale": "为什么这样切",
    "min_turns": 1,
    "max_turns": 12,
    "current_objective": "这一章玩家要达成的可行动目标",
    "pacing_directive": "主线的可变节奏：写清条件与后果，不要写死玩家的选择",
    "hook_pool": ["能跨章回收的钩子"],
    "milestones": [
      {{
        "label": "完成的结果，写清可替代途径与人数要求",
        "evidence_required": [{{ "type": "clue_keyword_any", "match": ["具体信号"] }}],
        "source": {{ "rel_path": "04.md", "line_start": 10, "line_end": 20, "quote": "原文原句" }}
      }}
    ],
    "key_npcs": [
      {{ "ref": "npc_ram", "name": "拉姆", "role": "在本章的立场与作用" }}
    ]
  }}
}}
</output_schema>

<hard_rules>
- 里程碑写**完成的结果**，不写演员排练。不要写成「指定三人必须各看一遍」。
- 里程碑不得以「至少一人失败/受伤」为必达条件——玩家可能全员成功。
- 不要预写 NPC 的固定地点、生死或立场：`key_npcs` 只写 `ref` 与 `role`，
  不要给 `state` 钉死位置或生死，否则会覆盖玩家已经取得的成果。
- `pacing_directive` 写条件与因果（「若警报未解除且援军已到，则……」），
  不要写「无论玩家做什么，第六回合必断退路」。
- `min_turns` 必须是不小于 1 的整数，且不大于 `max_turns`。
</hard_rules>

{_PLAYER_POSITION_RULE}

{_CITATION_RULE}

{_ID_RULE}

{_EVIDENCE_RULE}

{_JSON_ONLY}"""


def extract_chapter_prompt(
    *,
    arc_title: str,
    candidate: Mapping[str, Any],
    passages: Sequence[Mapping[str, Any]],
    already_approved: Sequence[str] | None = None,
    player_position: str = "",
) -> str:
    blocks = [
        f"<volume arc_title={arc_title!r}>",
        f"本次要改编的候选章节：{dumps(dict(candidate), limit=4000)}",
        "</volume>",
    ]
    if player_position:
        blocks.append(player_position)
    if already_approved:
        blocks.append("<frozen>")
        blocks.append("以下是**已批准、不得改动**的章节 id（不要重复生成它们）：")
        blocks.append(dumps(list(already_approved), limit=2000))
        blocks.append("</frozen>")
    blocks.append("<source_passages>")
    blocks.append("以下是相关原文，带行号。只能依据这些内容生成，并据此给出引用：")
    for passage in passages:
        blocks.append(
            f"--- {passage.get('rel_path')} 行 {passage.get('line_start')}-{passage.get('line_end')} ---"
        )
        blocks.append(str(passage.get("text") or ""))
    blocks.append("</source_passages>")
    return "\n".join(blocks)


# --- 步骤 3：NPC 名录 -----------------------------------------------------

CAST_SYSTEM = f"""\
你是小说改编游戏的角色设计师。任务：从给定的原文材料里，挑出这一卷需要作为
**可交互 NPC** 的角色，并写清他们的行为边界。

<output_schema>
{{
  "npcs": [
    {{
      "slug": "npc_ram",
      "name": "拉姆",
      "identity": "一句话身份",
      "appearance": "外形要点",
      "personality": "性格要点",
      "public_background": "玩家一开始能知道多少",
      "location": "主要活动地点",
      "capabilities": ["能做到的事"],
      "limitations": ["做不到的事与代价"],
      "prompt": "给扮演模型的指示：她想要什么、知道什么、隐瞒什么、接下来会做什么",
      "source": {{ "rel_path": "02.md", "line_start": 3, "line_end": 9, "quote": "原文原句" }}
    }}
  ]
}}
</output_schema>

<hard_rules>
- 只收录**在本卷实际出场**且玩家可能与之交互的角色。背景板人物不要收。
- `limitations` 必须写实：能力边界、代价、以及**不能做的事**。
  这一栏是防止扮演模型凭空开挂的关键。
- `prompt` 要写清角色的**欲望、认知与隐瞒**，不要只写职业和语气。
- 不要把任何角色写成本世界玩家无法达成/无法对抗的存在。
</hard_rules>

{_PLAYER_POSITION_RULE}

{_CITATION_RULE}

{_JSON_ONLY}"""


def cast_prompt(
    *,
    arc_title: str,
    referenced: Sequence[str],
    passages: Sequence[Mapping[str, Any]],
    player_position: str = "",
) -> str:
    blocks = [f"<volume arc_title={arc_title!r}>"]
    if referenced:
        blocks.append("已批准的章节检查点里引用了这些 NPC slug（请优先覆盖它们）：")
        blocks.append(dumps(list(referenced), limit=2000))
    blocks.append("</volume>")
    if player_position:
        blocks.append(player_position)
    blocks.append("<source_passages>")
    for passage in passages:
        blocks.append(
            f"--- {passage.get('rel_path')} 行 {passage.get('line_start')}-{passage.get('line_end')} ---"
        )
        blocks.append(str(passage.get("text") or ""))
    blocks.append("</source_passages>")
    return "\n".join(blocks)


# --- 步骤 4：局部重写（被驳回的条目）--------------------------------------

REGEN_MILESTONE_SYSTEM = f"""\
你是小说改编游戏的关卡设计师。操作者**驳回**了下面这一个里程碑检查点，
请只重写这一条。

<output_schema>
{{
  "milestone": {{
    "label": "重写后的完成结果，写清可替代途径",
    "evidence_required": [{{ "type": "clue_keyword_any", "match": ["具体信号"] }}],
    "source": {{ "rel_path": "07.md", "line_start": 1, "line_end": 9, "quote": "原文原句" }}
  }}
}}
</output_schema>

<hard_rules>
- **只重写被驳回的那一条**，不要新增或删除其它里程碑。
- 严格遵守操作者给出的驳回意见；意见里提到的出处优先去查。
- 如果驳回意见指的是「这件事发生在本卷别处」，请给出**正确位置**的引用。
</hard_rules>

{_PLAYER_POSITION_RULE}

{_CITATION_RULE}

{_EVIDENCE_RULE}

{_JSON_ONLY}"""


def regen_milestone_prompt(
    *,
    chapter_context: Mapping[str, Any],
    rejected: Mapping[str, Any],
    note: str,
    passages: Sequence[Mapping[str, Any]],
    player_position: str = "",
) -> str:
    blocks = [
        "<chapter_context>",
        "所属章节（已批准部分，不得改动）：",
        dumps(dict(chapter_context), limit=4000),
        "</chapter_context>",
        "<rejected_item>",
        dumps(dict(rejected), limit=3000),
        "</rejected_item>",
        "<operator_note>",
        note or "（操作者未填写具体意见，请依据原文重新判断这一条是否恰当）",
        "</operator_note>",
    ]
    if player_position:
        blocks.append(player_position)
    blocks.append("<source_passages>")
    for passage in passages:
        blocks.append(
            f"--- {passage.get('rel_path')} 行 {passage.get('line_start')}-{passage.get('line_end')} ---"
        )
        blocks.append(str(passage.get("text") or ""))
    blocks.append("</source_passages>")
    return "\n".join(blocks)


# --- 步骤 5：前作影响确认 -------------------------------------------------

CONFIRM_CARRYOVER_SYSTEM = f"""\
你是剧本统筹。系统从前作的对局日志里**模式匹配**出了一批可能的影响项，
需要你逐条判定它是否真的成立。

<output_schema>
{{
  "verdicts": [
    {{
      "impact_id": "imp_01",
      "verdict": "confirmed",
      "reason": "为什么成立或不成立",
      "downstream_effect": "这条影响对新卷意味着什么（一句话）",
      "source": {{ "rel_path": "", "line_start": 0, "line_end": 0, "quote": "" }}
    }}
  ]
}}
</output_schema>

<hard_rules>
- `verdict` 只能是 `confirmed`、`demote`、`drop` 三者之一。
  - `confirmed`：给出的材料**明确**支持这条影响。此时必须给出原文引用。
  - `demote`：材料不足以判断，需要人来看。不要求引用。
  - `drop`：材料明确**否定**这条影响（例如只是计划、假设、玩笑或反事实）。
- **没有明确依据时必须选 `demote`，不要选 `confirmed`。**
  宁可让人多看一条，也不要把没发生的事写进新卷。
- 不要把计划当成结果：出现「准备救」「打算去」不等于已经救了。
</hard_rules>

{_CITATION_RULE}

{_JSON_ONLY}"""


def confirm_carryover_prompt(
    *,
    impacts: Sequence[Mapping[str, Any]],
    passage_texts: Mapping[str, str],
) -> str:
    blocks = ["<candidate_impacts>", dumps(list(impacts), limit=8000), "</candidate_impacts>"]
    if passage_texts:
        blocks.append("<execution_log>")
        blocks.append("以下是相关回合的实际发生内容（按关键帧给出）：")
        for key, text in passage_texts.items():
            blocks.append(f"--- {key} ---")
            blocks.append(str(text)[:2000])
        blocks.append("</execution_log>")
    return "\n".join(blocks)


# --- 步骤 6：生成章节散文 -------------------------------------------------

PROSE_SYSTEM = f"""\
你是游戏叙事设计师。任务：为已经**批准**的检查点补上玩家可见的文案。

<output_schema>
{{
  "opening_scene": "开场白，一屏以内（约 500 字），先让玩家看清身处何处、发生了什么、为何值得介入、现在能做什么",
  "opening_choices": [
    {{ "key": "A", "text": "具体行动", "risk": "safe", "requires_check": false, "collective": false }}
  ],
  "system_prompt": "本世界的稳定规律、能力边界与叙事语气",
  "chapters": [
    {{ "chapter_id": "ch_01_arrival", "pacing_directive": "本章节奏目标与拖延后果" }}
  ]
}}
</output_schema>

<hard_rules>
- `opening_choices` 恰好 4 个，key 为 A/B/C/D；其中 `collective: true` 的**不超过 2 个**。
- 选项写**具体意图**，不要提前宣布成功、不要隐藏真相、不要替玩家完成推理。
  写「盖住画像观察铃声变化」，而不是「切断师祖归来的诱饵」。
- `opening_scene` **不要用术语目录开场**，也不要塞满设定说明。
- `system_prompt` 写**稳定的世界规律与能力边界**，不写具体剧情。
  它是常驻上下文，每一回合都要花 token，务必克制。
- 不要在本步骤发明新的章节 id 或里程碑——那些已经批准并冻结。
</hard_rules>

{_PLAYER_POSITION_RULE}

{_JSON_ONLY}"""


def prose_prompt(
    *,
    name: str,
    description: str,
    chapters: Sequence[Mapping[str, Any]],
    npcs: Sequence[Mapping[str, Any]],
    continuity_facts: Sequence[str] | None = None,
    player_position: str = "",
) -> str:
    blocks = [
        "<world_brief>",
        f"名称：{name}",
        f"简介：{description}",
        "</world_brief>",
        "<approved_chapters>",
        "以下章节与里程碑**已经批准并冻结**，你的任务是补文案，不是改结构：",
        dumps(list(chapters), limit=14000),
        "</approved_chapters>",
        "<npc_roster>",
        dumps(list(npcs), limit=6000),
        "</npc_roster>",
    ]
    if player_position:
        blocks.append(player_position)
    if continuity_facts:
        blocks.append("<continuity>")
        blocks.append("以下是上一卷**已经发生**的结果，属于既成事实，不要推翻也不要重演：")
        for fact in continuity_facts:
            blocks.append(f"- {fact}")
        blocks.append("</continuity>")
    return "\n".join(blocks)


# --- 步骤 6.5：职业预设 ---------------------------------------------------

CARD_SYSTEM = """\
你是游戏数值设计师。任务：为这个世界设计**职业预设**。

<output_schema>
{
  "professions": [
    {
      "id": "servant",
      "name": "佣人",
      "description": "擅长清扫、跑腿与察言观色。正面冲突中明显吃亏。",
      "base_attributes": {"strength": 6, "agility": 7, "vitality": 7, "intellect": 8, "willpower": 7, "perception": 8, "charisma": 7}
    }
  ],
  "attribute_labels": {"strength": "力量"}
}
</output_schema>

<hard_rules>
- 职业 **6 到 8 个**。按题材给出**有实际玩法差异**的职业，不能只换名字。
- 每个职业的 `base_attributes` **必须恰好包含全部属性 key，且总和恰好等于 50**。
  系统会逐个核算；不平的直接作废。
- 每个职业必须在 `description` 里写清**擅长什么、短板是什么**。
- 不要把任一职业写成通关刚需，也不要有职业明显无用于本世界。
- `id` 用小写英文，`name` 用中文。
</hard_rules>

只输出一个 JSON 对象。不要解释、不要 Markdown 代码块标记、不要前后缀。"""


def card_prompt(
    *,
    name: str,
    description: str,
    attribute_keys: Sequence[str],
    attribute_labels: Mapping[str, str],
    profession_count: int = 7,
) -> str:
    return "\n".join(
        [
            "<world_brief>",
            f"名称：{name}",
            f"简介：{description}",
            "</world_brief>",
            "<attributes>",
            "本世界的属性（base_attributes 必须恰好使用这些 key）：",
            dumps(dict(attribute_labels), limit=2000),
            f"key 清单：{', '.join(attribute_keys)}",
            "</attributes>",
            f"<requirement>设计 {profession_count} 个职业，每个 base_attributes 合计 50。</requirement>",
        ]
    )


# --- 步骤 7：反省 ---------------------------------------------------------

REFLECT_SYSTEM = f"""\
你是事实核查员。系统会给你若干条**待核查的断言**，以及为它们检索到的原文段落。
你的任务：判断每条断言是否被原文支持。

<output_schema>
{{
  "findings": [
    {{
      "claim_id": "c01",
      "verdict": "supported",
      "severity": "info",
      "reason": "判断理由",
      "source": {{ "rel_path": "07.md", "line_start": 120, "line_end": 134, "quote": "原文原句" }}
    }}
  ]
}}
</output_schema>

<hard_rules>
- `verdict` 只能是 `supported`、`contradicted`、`unsupported`、`partial`。
- **判 `contradicted` 必须给出原文引用**。给不出引用就只能判 `unsupported`。
- **只能使用系统提供的段落。** 不要动用你自己的先验知识，哪怕你熟悉这部作品。
- 如果提供的段落**没有覆盖**这条断言，判 `unsupported`——**不要猜**。
  这是本任务最重要的一条规则：不确定就说不知道，编造依据比漏判严重得多。
- 不要把断言换个说法复述回去，你只做判断。
- `severity`：`blocking` 只用于与原文**明确冲突**且会误导玩家的内容；
  依据不足用 `warning`；仅供参考用 `info`。
</hard_rules>

<player_position_rule>
输入里可能给出 `<player_position>` 块，它点名了**由玩家取代的原作角色**。
这是本次改编的**既定前提**，不是一条待核查的事实：

- 「这件事在原文里是某某做的」**不构成** `contradicted`。玩家就站在他的位置上，
  草稿把施动者写成「玩家小队」是对的，不要改回他。
- 草稿里他不再出场、或他的戏份由玩家承担，都不是矛盾，也不是"遗漏"。
- 只有草稿**把事实本身写反了**（因果颠倒、把他人的行为安到另一人头上），
  才按上面的常规标准判。

反过来也要留意：若草稿**又把他写成了行动的施动者**（「昴发现了……」），
而原文如此、玩家却已取代他，那是本次改编的偏差，按 `partial` 报告即可，
不要通过 `suggested_fix` 把他改回去。
</player_position_rule>

{_CITATION_RULE}

{_JSON_ONLY}"""


COVERAGE_SYSTEM = f"""\
你是剧情校对。系统给你一份**必现情节清单**（从原作缝出来的新线），以及生成出来的
世界包草稿（章节 / 目标 / 里程碑）。逐条判断：这条情节在草稿里**有没有落点**。

这是**遗漏检查**，和"内容是否与原文矛盾"是两回事。一条情节即使写得很准，
只要草稿里根本没有承接它的章节或里程碑，就是 `omitted`。

<output_schema>
{{
  "findings": [
    {{
      "claim_id": "cv001",
      "verdict": "omitted",
      "reason": "为什么这么判（引用草稿里的哪一章/哪个里程碑，或说明通篇找不到）",
      "suggestion": "要补的话，补在哪一章、补成什么里程碑"
    }}
  ]
}}
</output_schema>

<hard_rules>
- `verdict` 只能是这三个：
  - `covered` —— 草稿里有明确的落点（某章的目标或某个里程碑就是在承接它）。
  - `distorted` —— 有落点，但被压扁或改变了性质（例如把「当面质问并动手」写成了
    「听说她有些不满」）。这种情况要点名是哪里被削弱了。
  - `omitted` —— 通篇找不到落点。
- **判 `covered` 与 `distorted` 都必须点名具体的章节或里程碑**。点不出来就是 `omitted`。
- 不要因为草稿"主题相近"就算 covered。必须是**这条情节本身**有落点。
- 宁可判 `omitted` 让操作者看一眼，也不要放过一条真被漏掉的。
  漏判的代价是玩家玩到的世界里少了一整段关键剧情，而且**事后无法发现**。
- 只做判断，不要改写草稿。
</hard_rules>

{_JSON_ONLY}"""


def coverage_prompt(*, claims: Sequence[Mapping[str, Any]]) -> str:
    blocks = [
        "<required_beats>",
        "以下情节是缝好的新线里**必须出现**的，逐条核对草稿是否有落点：",
        dumps(list(claims), limit=12000),
        "</required_beats>",
    ]
    return "\n".join(blocks)


def reflect_prompt(
    *,
    claims: Sequence[Mapping[str, Any]],
    passages_by_claim: Mapping[str, Sequence[Mapping[str, Any]]],
    replaced_names: Sequence[str] | None = None,
) -> str:
    blocks = ["<claims>", "待核查断言：", dumps(list(claims), limit=8000), "</claims>"]
    # 只给**名单**，不给那套写作指令：这一步是事实核查员，它需要知道"谁的位置
    # 已经属于玩家"，好把「原文里是他做的」判成既定前提而不是矛盾（见 system 里
    # 的 player_position_rule）。判据本身写在 system 里，重试时不会丢。
    names = [str(n).strip() for n in (replaced_names or []) if str(n).strip()]
    if names:
        blocks.append(
            "<player_position>\n"
            f"本卷由玩家取代的原作角色：{'、'.join(names)}\n"
            "</player_position>"
        )
    blocks.append("<retrieved_passages>")
    blocks.append("以下是为每条断言检索到的原文段落（只能依据这些内容判断）：")
    for claim in claims:
        claim_id = str(claim.get("claim_id") or "")
        blocks.append(f"=== 断言 {claim_id} 的检索结果 ===")
        for passage in passages_by_claim.get(claim_id, []):
            blocks.append(
                f"--- {passage.get('rel_path')} 行 {passage.get('line_start')}-{passage.get('line_end')} ---"
            )
            blocks.append(str(passage.get("text") or "")[:1500])
    blocks.append("</retrieved_passages>")
    return "\n".join(blocks)


# --- 步骤 8：原创故事的大纲（B 路径暂不使用，接口先留好）-----------------

ORIGIN_PLAN_SYSTEM = f"""\
你是游戏叙事设计师。操作者给了一个故事构想，请把它拆成可玩的检查点。

<output_schema>
{{
  "premise": "核心冲突，不依赖专名也能听懂",
  "chapters": [
    {{
      "title": "章节标题",
      "subtitle": "一句话副标题",
      "min_turns": 1,
      "max_turns": 12,
      "current_objective": "玩家要达成的可行动目标",
      "pacing_directive": "条件与因果式的主线推进",
      "hook_pool": ["能跨章回收的钩子"],
      "milestones": [
        {{ "label": "完成的结果", "evidence_required": [{{ "type": "clue_keyword_any", "match": ["具体信号"] }}] }}
      ],
      "key_npcs": [{{ "ref": "npc_x", "name": "名字", "role": "在本章的立场与作用" }}]
    }}
  ]
}}
</output_schema>

<hard_rules>
- 先写**玩家的追求**，再写世界知识。用一句不依赖专名的话说明目标、牵挂或困境。
- 区分必需成果与可选支线；只有必需成果进退出条件。
- 不要强制每章反转、每次成功受罚、每回合升级危机。
- 里程碑不得以「至少一人失败」为必达条件。
</hard_rules>

{_ID_RULE}

{_EVIDENCE_RULE}

{_JSON_ONLY}"""


def origin_plan_prompt(*, concept: str, preferences: Mapping[str, Any] | None = None) -> str:
    return "\n".join(
        [
            "<concept>",
            concept.strip(),
            "</concept>",
            _preferences_block(preferences),
        ]
    ).strip()
