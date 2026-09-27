# v0.9.3 世界包编写说明

世界包是可复用模板。新建副本时会复制世界版本、规则与时间规则快照；之后修改模板不会静默改变正在运行的团。

创作入口是 [世界包生成提示词](../worlds/WORLD_GENERATOR_PROMPT.md)，正反例与验收见 [作者实践指南](WORLD_AUTHORING_BEST_PRACTICES.md)。本说明负责将通用创作要求映射到字段，不代替当前 [协议 v5](WORLD_SCHEMA_V5.md) 和目标插件实现；下文历史版本示例不是所有新包必须沿用的版本或默认政策。

新包必须完成“创作准则 → 实际字段内容 → 交付验收”的闭环。运行时不会因为修改了这份 MD 就自动获得新能力：文字约束需要进入世界包，数值与权限需要已有结构化机制支持，自动化保障必须有实际实现和测试依据。

安装包自带可直接复制的通用模板：

- `templates/world-package.template.json`：当前世界协议、建卡向导与机器骰制的最小完整示例。
- `templates/world-package-preset-stack.template.json`：多个预设共同生成属性的可运行示例。
- `templates/npc-import.template.json`：可直接通过管理台导入的常驻 NPC 数据示例。
- `templates/template-manifest.json`：模板兼容的插件、世界协议、角色卡和 NPC 导入版本。
- `templates/README.md`：面向世界作者与 AI 修改工具的逐项说明和发布维护规则。

不要直接修改模板原件后覆盖发布包；应复制并重命名，再替换世界标识、稳定 ID、显示文案和实际设定。

## 顶层字段

| 字段 | 作用 |
|---|---|
| `slug` | 唯一标识；小写字母、数字、`_`、`-`，最多 64 字符 |
| `name` | 显示名称 |
| `description` | 题材、规模与玩法简介 |
| `system_prompt` | 稳定世界规律、能力边界与叙事语气 |
| `opening_scene` | 第一次开演时的开场 |
| `rules` | 人数、角色卡、检定、内容边界、NPC、事件与时间规则 |
| `initial_state` | 新副本的地点、时间、摘要、事实、物品与关系 |

## 叙事可读性与画面锚点

世界包中的 `description`、`system_prompt`、`opening_scene`、章节指令、事件、NPC、物品和地点说明，最终都会影响玩家实际读到的文字。题材可以陌生，句子不能故意陌生。修仙、西幻和中世纪背景尤其要遵守以下规则：

1. **先写常见名称，再补设定名称。** 第一次出现的对象先让玩家知道它是什么，再给专名、古称或风格名。例如写“殿前的高台和石阶，当地人称为丹墀”，不要只写“丹墀横在众人面前”。
2. **建筑部件必须能看出形状、位置或用途。** 不要单独使用建筑史、古建、宗教建筑和城防术语来代替画面。至少补足“它是什么、在哪里、能怎么通过或利用”中的两项。
3. **只解释行动所需的术语。** 必须理解才能判断眼前处境的名词，用日常含义或用途简短说明；不影响行动的历史、教义和原理留待玩家追问。不要把首次出现变成强制括注清单，更不要把逐条学完设定当作里程碑。
4. **降低名词负担。** 避免同段堆叠需要记忆的新专名；必须同时出现时先说明关系。不是每段或每回合都必须引入新概念，也不靠分多回合讲课规避开场篇幅限制。
5. **可行动信息优先于装饰。** 场景描写至少让玩家知道人物站在哪里、明显出口或障碍在哪里，以及眼下有什么可以观察、接近、躲避或使用。不要用一串材质、纹样和古称挤掉这些信息。

建筑与器物建议按“常见名称 + 外形/位置 + 作用”写：

| 避免只写 | 推荐写法 |
|---|---|
| 丹墀 | 殿门前的高台和石阶，当地人称为丹墀 |
| 甬道 | 只能容两人并行的狭长通道 |
| 雉堞 / 女儿墙 | 城墙顶齐胸高的矮墙，墙上留有射击缺口 |
| 券门 | 上端呈圆拱形的石门洞 |
| 飞扶壁 | 从外墙斜撑出去、用来托住高墙的石架 |
| 耳房 | 正屋两侧较小的房间 |
| 须弥座 | 托住神像的方形石台，四周刻着花纹 |

这里不是禁止题材术语，而是禁止**只靠术语传达画面**。若一个词删掉后，普通玩家就不知道面前是门、窗、台子、走廊、矮墙还是支架，说明它需要改写或补一句解释。

推荐在 `system_prompt` 中直接写入类似约束：

```text
正文使用现代、直白的中文。行动所需的陌生名词先用日常词说明眼前用途，再给专名；不必讲解的背景留待追问，不逐条授课。建筑、门窗、台阶、平台、通道和城防设施优先使用常见名称；必要古称补充外形、位置或用途。新场景先写清空间和可行动对象，再补气氛。
```

开场示例：

```text
不推荐：众人越过瓮城，沿马道登上雉堞，丹墀尽头的券门已经洞开。
推荐：众人穿过城门内用于困敌的方形空地，沿贴着城墙的斜坡走上墙顶。齐胸高的矮墙上开着一排射击缺口；前方殿门外的高台尽头，一座上端圆拱的石门洞已经敞开。
```

## 通用可玩性契约与字段落点

以下是所有题材的创作底线。反转、悲剧、分头、多地点、道德两难和复杂设定均为可选手法，不要求每章具备。允许安全休整、关系交流、低风险好办法和明确的胜利；真实风险、损失与失败结局也应保留。

| 创作要求 | 现有字段落点 | 作者必须写清或核验的内容 |
|---|---|---|
| 玩家愿意介入 | `description`、`opening_scene`、`rules.story_spine` | 具体目标、牵挂或诱惑；“查明真相”怎样影响行动和人物，不以复述主题为通关目标 |
| 设定服务行动 | `system_prompt`、章节说明 | 后台真相、当下所需信息、可选背景分层；关键线索的新增用途，不把谜底塞进公开初始事实 |
| 人物有欲望 | `rules.character_card`、`key_npcs[].role`、NPC 定义 | 人物想要什么、知道什么、下一步做什么；不按排行假设职业，不强改玩家背景 |
| 结果型里程碑 | `rules.progress.chapters[].milestones`、`exits_when` | 真实完成结果、必要限定、替代方式与队伍累计范围；推荐场景和个人支线不进入必达退出集 |
| 合理节奏 | 章节 `min_turns`、`max_turns`、`pacing_directive` | 预算和事件因果；允许舒缓，不固定回合强制反转、失败、换地图或切章 |
| 分队与授权 | `rules.player_limits`、`opening_choices`、`system_prompt` | 最低人数可玩，同行/分队都可行；选项文本与 `collective`、已支持的 `vote_scope` 相符 |
| 收益、资源与治疗 | `rules.resolution`、`resources`、已支持的交互/效果模块 | 成功收益、失败后果、资源收支、状态来源与解除方法；纯文字不冒充机械执行 |
| 事实与感知区分 | `system_prompt`、章节与 NPC 说明 | 固定真实因果、伪装边界、可行验证办法；角色所见不等于全队已知 |
| 提前完成也能承接 | 章节目标、`pacing_directive`、`key_npcs` | 提前营救、取物、断线、治疗后的后续；切章不无条件重置 NPC 地点、生死或成果 |
| 一次完整收尾 | 主线说明、终章目标与退出条件 | 核心冲突结束条件、尚需决定的事、必交代的后果；个人后日谈可选，结尾不再开必做支线 |

此表不是新增 JSON 协议。“事实/感知/猜测”“替代路线”“自检”是作者需要表达和验证的内容，不意味着同名字段会被引擎自动解释。只使用当前已注册的字段与机制；关键能力不支持时简化设计或阻断发布，不能只在提示词里宣称“系统会自动修复”。

### 场景先于设定的写作顺序

1. 先用白话写主线目标、涉及的人与最终可结束的结果。
2. 写主要场景中玩家能争取什么、遇到什么阻力、行动会改变什么。NPC 提供完整线索或明确的获取条件，不连续用半句话扣留同一信息。
3. 为核心调查准备不同的可行方法，考虑拒绝某路线、缺职业、多人累计和连续失败。不保证每条路线成功，但不能只能无限重试。
4. 再补维持因果所需的后台设定和少量风味信息。每条关键线索说明信息增量、验证价值或新行动用途；重复日记、口供和遗物应合并。
5. 将真正必需的成果映射为里程碑，明确可选内容，最后填入 JSON 并执行验收。

### 里程碑与回合预算

推荐：“队伍取得验证幻象的方法（物证、实验或可靠证词之一即可，不要求每人重复）”。不推荐：“五人轮流进入幻境且至少一人失败”。前者验收成果，后者把人数、动作顺序和失败结果锁成通关条件。

- 关键限定写在 `milestones[].label`，达成信号写在 `evidence_required`；当前裁判使用它们核对实际叙事。不要把关键条件只放进未知字段，也不要靠泛关键词或 `auto_complete` 保证完成。
- 替代方法是 OR，不要拆成 `all_milestones` 里的多个必达项。队伍累计调查与“全队已获知”不是一回事，按主线真正需要的结果区分。
- 已完成但漏记，只能核对事件和原文补记；计划、提及、猜测、回合超期都不是完成证据。自动机制是否可用须按当前实现验证。
- 新包各章显式写正整数 `min_turns`，并满足 `1 <= min_turns <= max_turns`。省略或给非正值时，最低回退到 1 回合；显式正值仍受尊重。预算不作为已完成章节强制凑时长的依据，推进仍需真实里程碑证据及场景收束。
- `max_turns` 用于节奏提醒，不承诺到点完成。总预算核算个人行动、多人轮次、表决和结尾；不能靠反复观察、移动几步或逐人补演填满最短时长。
- 可变 NPC 状态不要写死在后章 `key_npcs[].state`：该字段会参与切章写入。已经救出的人不能因章序回到地下，死者也不能无因复活；没有可靠条件控制时，省略可变覆盖，改写章节承接。

### 分队、检定与可恢复状态

- 分头不是通关前提；提供同行与分组的合理收益差异，不用强制传送、共享知识或替别人决定来简化状态管理。固定人数玩法须在开本前限制人数或提供替代方式。
- 个人行动只影响本人的授权范围；现场小队表决只影响同地点参与者，全队决定不能缩小投票范围。群聊里看得到文字不代表角色知情，更不代表渠道支持秘密消息。
- 没有真实不确定性的移动和交接不强加检定。按已锁定结果兑现成功、代价成功与失败；禁止为营造恐怖追加无依据反噬，失败也不自动变成功。
- 每个持续状态应说明来源、机械效果、持续方式与解除规则。除明确的剧情锁定外，应有牧师或世界等价治疗能力可执行的解除规则；可解除的剧情锁定写清原因和可发现的解除路径。确属剧情规定不可解除的状态，应事先声明永久性、触发条件与风险告知，不临场把普通状态永久化。已治愈状态只能因新的实际事件再次产生。
- 资源需明确个人/小队/全队归属、消耗与恢复数值、触发条件及本局可达的恢复机会。必需动作与资源预算一起演练，不能让恢复条件落在本局永远不会到达的时间点。

### 事实与终局

悬疑允许欺骗感知，不允许后台真相任意改口。写清可伪造范围及关键结论的验证办法；已确认成果不能被新章、反转或气氛描写暗中撤销。非悬疑题材不必为满足模板强加幻觉。

终局按“核心冲突达到结束条件 → 尚未作出的最终决定 → 实际结果结算 → 一次完整结尾”组织。具体退出仍服从引擎场景收束核验，不按回合数提前报完结。玩家不必解答全部背景问题，个人后日谈不作为退出条件；尾声可以平静、可以有遗憾，但不能再添加必做危机或为反大团圆临时扣除成果。

## 规则骨架

```json
{
  "resolution": {
    "mode": "attribute",
    "dice_system": "d20",
    "difficulty_policy": {
      "safe": null,
      "controlled": 9,
      "dangerous": 13,
      "desperate": 17,
      "lethal": 21
    },
    "outcome_policy": {
      "natural_20_critical": true,
      "natural_1_critical": true,
      "critical_success_margin": 10,
      "cost_success_min_margin": -4,
      "failure_min_margin": -9
    }
  },
  "strict_choices": true,
  "check_density": "standard",
  "player_limits": {},
  "character_card": {},
  "dice_rules": {},
  "inspiration": {},
  "content_boundaries": {},
  "progress": {},
  "npc_policy": {},
  "context_budget": {},
  "time_rules": {},
  "opening_choices": [],
  "event_pool": [],
  "safe_exit_templates": [],
  "return_rules": {}
}
```

世界 JSON 是声明式数据，不能包含脚本或可执行表达式。

## 步骤驱动预设

预设属于角色卡结构化数据，不要把可选名单写进 `system_prompt` 或字段标题。插件只在建卡进入当前字段时解析和展示该字段的预设。

```json
{
  "character_card": {
    "version": 4,
    "preset_sets": {
      "origin_regions": [
        {
          "id": "northern_kingdom",
          "value": "北境王国",
          "label": "北境王国",
          "summary": "王国腹地、农庄与边境堡垒。"
        }
      ]
    },
    "fields": [
      {
        "key": "origin_region",
        "label": "选择出身地区",
        "type": "preset_select",
        "preset_source": "origin_regions",
        "page_size": 5,
        "required": true,
        "max_chars": 20
      }
    ]
  }
}
```

玩家可回复当前页序号、`id`、`value`、`label` 或别名。角色卡保存展示值，并在 `_preset_refs` 中保存稳定 ID 与当时效果快照。`page_size` 范围为 `1..10`。

条件字段可使用：

```json
{
  "key": "contract_source",
  "label": "选择契约来源",
  "type": "preset_select",
  "preset_source": "warlock_contracts",
  "visible_when": {"profession": ["术士"]},
  "required": true
}
```

字段还可声明：

- `clear_on_change`：当前字段修改后需要清理的后续字段 key。
- `must_differ_from`：当前选择不得与指定字段相同。
- `options_source`：兼容既有世界包的预设源名称；新包优先使用 `preset_source`。

导入体检会拒绝不存在的预设源、必填字段无有效选项、非法字段引用与条件循环依赖。

## 机器骰制与 DC 权限

世界包只声明骰制名称和确定性策略。随机数、加值、DC 映射及成功档位由插件执行，文字 AI 没有重投或修改权限。

- `dice_system` 必须对应运行时已注册骰制；找不到时直接拒绝裁定。
- `difficulty_policy` 把风险等级映射为 DC，选项或模型给出的任意 DC 不会覆盖它。
- `outcome_policy` 决定大成功、成功、代价成功、失败和大失败的边界。
- 必检选项必须在展示给玩家之前带有属性、风险、类型和已知后果。
- 骰点先锁定并公开，再调用文字模型续写；模型重试复用同一回执。

## 人数

```json
{
  "player_limits": {
    "recommended_min": 2,
    "recommended_max": 4,
    "minimum_start": 2,
    "maximum": 4
  }
}
```

`recommended_min/max` 只用于展示；`minimum_start` 是开演前置检查；`maximum` 是数据库强制席位上限，范围 `1..32`。

## 玩家角色卡

### 新包建卡要求

1. **基础属性必须由系统生成，不让玩家逐项手填，也不由运行时叙事临场分配。** 两条路选一条：`preset` 由写包阶段预设职业属性，`authored` 则由玩家写一段自拟设定、由模型按设定分配基础值（见下节）。两条路都保留玩家选择不同主、副属性的固定加点。
2. **职业建议至少 6 个，通常 6–8 个。** 题材中可称职业、专修或岗位，须有行动方式和属性侧重的实际区别；少于 6 个说明理由，不强行凑同质职业。（`authored` 模式没有职业字段，本项不适用。）
3. **保留基础值与主副属性加点。** 默认采用 `stats.mode = preset`、`profession_presets`、`primary_attribute`、`secondary_attribute`，职业基础合计 50 点，玩家选择不同主属性 +7、副属性 +3，最终合计 60 点；复用原有基础值、加成、最终值和修正面板。关闭任意手填数值，换职业或主副属性时重算而不叠加。只有用户明确要求取消加点时才改用纯 `preset_stack`；无属性玩法说明不适用。
4. **少量选择即可开玩。** 姓名、代号等必要短标识保留；其他字段优先预设选择或默认值，只保留对玩法确有用途的项目。背景、目标、信念、性格、关系、能力来源不拆成多道自由作文题，也不强迫填完一长串选择题。
5. **仅一个可选自由补充项。** 默认使用“补充说明”（例如 `key: supplement`、`type: text`、`required: false`、`max_chars: 300`），可留空。它用于风格和背景补充，不自动授予数值、技能或特殊权限；不要另开自由背景、秘密、经历等字段。**若采用 `authored` 模式，这个自由补充项就是自拟设定本身**：改成必填、放宽 `max_chars`，不再允许留空。

逐职业预览最终属性与检定修正，确认预算、上下界、能力说明一致；演练更换职业后重算、不叠加旧加成，以及跳过补充可正常开演。下列旧版示例仅解释兼容字段，不是新包的建卡设计模板；复制通用 JSON 模板时同样要按本节缩减自由填写项并扩充职业选择。

角色卡模板在管理台中拥有独立入口：

```text
酒馆控制台 → 世界与角色 → 对应世界卡片 → 角色卡模板
```

该入口支持导入 JSON、导出 JSON、结构校验、玩家建卡表单预览、恢复默认和确认保存。导入与编辑不会即时生效，只有通过校验并点击保存后才会更新世界模板。它不应与常驻 NPC 管理混为一体。

```json
{
  "character_card": {
    "version": 4,
    "auto_approve": false,
    "edit_requires_review": true,
    "fields": [
      {
        "key": "name",
        "label": "角色姓名",
        "required": true,
        "private": false,
        "max_chars": 12,
        "type": "text"
      },
      {
        "key": "code",
        "label": "副本代号",
        "required": true,
        "private": false,
        "max_chars": 12,
        "type": "text"
      },
      {
          "key": "supplement",
          "label": "补充说明（可留空）",
          "required": false,
          "private": false,
          "max_chars": 300,
        "type": "text"
      }
    ],
    "stats": {
      "mode": "manual",
      "budget": 10,
      "attributes": [
        {
          "key": "body",
          "label": "体魄",
          "minimum": 0,
          "maximum": 5,
          "default": 2
        },
        {
          "key": "agility",
          "label": "敏捷",
          "minimum": 0,
          "maximum": 5,
          "default": 2
        }
      ],
      "modifier_table": {
        "0": -3,
        "1": -2,
        "2": -1,
        "3": 0,
        "4": 1,
        "5": 2
      }
    }
  }
}
```

校验规则：

- `version` 必须是正整数。
- `fields` 的 `key` 不可为空或重复，并必须包含 `name` 与 `code`。
- 属性 `key` 不可重复。
- 属性默认值必须在最小值与最大值之间。
- 总预算必须介于全部属性最小值之和与最大值之和之间。
- 上述历史 `manual` 模式会创建属性填写步骤，不用于新包；新包应采用下述职业预设自动生成方式。

以下是历史 v0.9.3 / 世界协议 v3 的 `preset_stack` 字段示例，角色卡模板版本为 4。新包应按当前协议和模板声明版本，不因使用该功能把世界协议降回 v3。在 `character_card` 下增加：

```json
{
  "stat_generation": {
    "mode": "preset_stack",
    "base_stats": {"body": 2, "agility": 2},
    "bonus_sources": ["profession"],
    "bonus_source_rules": {
      "profession": {"expected_bonus_total": 2}
    },
    "expected_total": 6,
    "min_per_stat": 2,
    "max_per_stat": 4,
    "allow_manual_edit": false
  }
}
```

同时将 `stats.mode` 设为 `preset_stack`，并为每个来源选项写入非空 `stat_bonus`。所有来源选完后插件自动显示最终属性和来源、保存快照并继续后续字段，不创建 `stat_<key>` 手动填写步骤。返回修改来源时从基础值重算。

上述数值配置是字段片段，使用时还须在 `fields` 定义职业选择，并关联包含各职业 `stat_bonus` 的 `preset_sets`；职业加成总和应与示例的 2 一致。示例属性仅作结构演示，实际属性维度、职业数量和数值由写包模型按题材预设。

背景、目标、信念、羁绊、专长和知识边界等内容可由职业默认、少量预设选择和一个补充说明承载，不要求玩家分别自由填写；安全与内容边界仍按本团约定处理，不等于额外的角色作文题。

#### 自拟设定生成基础值（`authored`）

`authored` 用一段玩家自写的设定换掉职业预设：写包方不再预设职业与基础属性，基础值由模型按玩家那段文字分配，**主属性 +7 / 副属性 +3 仍然由玩家自己选**（复用 `preset` 那条加点路径）。`worlds/re0-ch-chapter030.json`（re0王选篇）是现成例子。

```json
{
  "fields": [
    {"key": "name", "type": "text", "required": true, "label": "角色姓名"},
    {"key": "code", "type": "text", "required": true, "label": "副本代号"},
    {
      "key": "supplement",
      "type": "text",
      "required": true,
      "max_chars": 600,
      "label": "自拟设定（写清你的背景与能力，系统据此生成基础属性）"
    },
    {"key": "primary_attribute", "type": "preset_select", "required": true,
     "options": ["力量", "敏捷", "体质", "智力", "意志", "感知", "魅力"],
     "label": "选择主属性（固定+7）"},
    {"key": "secondary_attribute", "type": "preset_select", "required": true,
     "must_differ_from": "primary_attribute",
     "options": ["力量", "敏捷", "体质", "智力", "意志", "感知", "魅力"],
     "label": "选择副属性（固定+3）"}
  ],
  "stats": {
    "mode": "authored",
    "base_budget": 50,
    "budget": 60,
    "total_validation": {"base_total": 50, "final_total": 60},
    "stat_generation": {
      "mode": "authored",
      "source_field": "supplement",
      "expected_total": 50,
      "min_per_stat": 2,
      "max_per_stat": 13,
      "guide": "按设定的具体程度给分：明确写出的专长给对应属性高分，没写到的属性不要高于中位。"
    }
  }
}
```

要点：

- `source_field` 必须指向一个自由文本字段（`text`/`long_text`/`textarea`/`paragraph`）且 `required: true`，否则发布体检不通过。
- `expected_total` 是**基础值**总和（对应 `total_validation.base_total`），不是最终值。它必须落在「逐项下限之和 … 逐项上限之和」之间，否则体检不通过。
- `min_per_stat` / `max_per_stat` 省略时用属性表自己的 `minimum` / `maximum`。**若保留了主属性 +7，`max_per_stat` 必须 ≤ 属性上限 − 7**，否则玩家选完主属性会算出越界值。
- 自拟设定那一项填完就会触发一次模型调用；模型输出不合规（缺项、越界、总和对不上）时自动退到「先给下限、余量均摊」的确定性兜底，**建卡不会因为模型问题卡住**。生成结果里只有基础值，玩家接着选主、副属性。
- 字段顺序两种都支持：自拟设定在姓名/代号之后、主副属性之前（re0王选篇采用），或留在最后一段。排在主副属性之后时，插件会等设定写完再一次性算加点，不会提前报“尚未生成基础属性”。
- `profession_presets` 与 `preset_selector` 可以留着不动：切到 `authored` 后它们只是数据，不再被选到；万一有旧卡缺 `profession_base_stats`，还能按老的职业路径解析出来。
- 切换已有世界包时不要再用 `preset` 的 `input_mode` / `allocation_mode` 取值（`automatic_profession_base_plus_two_fixed_bonus_choices` / `profession_base_plus_primary7_secondary3`），否则 `uses_profession_preset_stats` 仍会把它当职业预设卡。
- 已经建好的角色卡不受影响：它们的基础值早就落在 `profession_base_stats` 里，切模式后最终属性不变。可用 `tools/install_authored_card_stats.py`（默认 dry-run）完成切换并逐张验证。

## 检定规则

```json
{
  "dice_rules": {
    "system": "d20",
    "advantage": "2d20_keep_high",
    "disadvantage": "2d20_keep_low",
    "stacking": false,
    "opposites_cancel": true,
    "outcome_bands": true,
    "visibility": "public"
  },
  "inspiration": {
    "initial": 1,
    "maximum": 3,
    "uses": ["advantage_before_roll", "reroll_full_pool"]
  }
}
```

`visibility` 可为：

- `public`：显示骰池、取值、加值、DC、来源和结果。
- `immersive`：隐藏具体 DC。
- `hidden`：群内只显示叙事，完整数据留在后台。

建议内部测试固定使用 `public`。

## 风险与难度

DC 只表示成功难度：

| DC | 难度 |
|---:|---|
| 5 | 极易 |
| 8 | 容易 |
| 10 | 普通 |
| 12 | 标准 |
| 15 | 困难 |
| 18 | 非常困难 |
| 20 | 极限 |
| 25 | 传奇 |

风险只表示失败后果：

- `safe`
- `controlled`
- `dangerous`
- `desperate`
- `lethal`

`lethal` 必须提前填写玩家可见后果。不要让同一个原因同时提高 DC 和造成劣势。

## 正式进度

```json
{
  "progress": {
    "chapter": "第一章：暴雪来客",
    "current_objective": "调查酒馆地下的异响",
    "completed_milestones": 2,
    "total_milestones": 8
  }
}
```

只有 `total_milestones > 0` 时管理台才显示百分比。没有正式里程碑时只展示章节、目标和场景摘要。

## 内容边界

```json
{
  "content_boundaries": {
    "character_death": "ask",
    "player_conflict": "consent",
    "romance": "fade_to_black",
    "horror": "moderate",
    "sexual_content": "blocked",
    "safety_pause": true
  }
}
```

世界规则可在副本详情中覆盖。安全暂停会冻结全部计时，且玩家不必公开原因。

## 自动 NPC

```json
{
  "npc_policy": {
    "enabled": true,
    "max_new_per_turn": 3,
    "generated_requires_review": true,
    "archive_after_inactive_rounds": 12
  },
  "context_budget": {
    "recent_turns": 6,
    "memories": 6,
    "active_npcs": 6,
    "ledger_items": 8
  }
}
```

模型生成 NPC 必须有名字，并至少满足直接互动、掌握重要线索或写入长期记忆之一。自动 NPC 只能写公开资料、已知事实、误解和运行状态，不能创建系统级私密提示词。

`v0.9.3` 会按用途编译上下文：建卡预设、职业数值、开场选项和事件池不会进入每轮叙事；选项生成只接收当前场景、精简角色、最近事件、允许属性和风险—DC。`context_budget` 应用于新副本的运行快照，数值越高并不等于叙事质量越高；长期事实应进入记忆或故事账本，而不是无限增加最近回合。

## 开场四选一

```json
{
  "opening_choices": [
    {
      "key": "A",
      "text": "谨慎观察现场",
      "risk": "safe",
      "requires_check": false,
      "collective": false
    },
    {
      "key": "B",
      "text": "询问公开信息",
      "risk": "safe",
      "requires_check": false,
      "collective": false
    },
    {
      "key": "C",
      "text": "借助绳索翻越断桥",
      "risk": "desperate",
      "requires_check": true,
      "collective": false,
      "check_type": "standard",
      "check_stat": "敏捷",
      "difficulty": 15,
      "known_consequences": "失败可能坠落并与队伍分离",
      "advantage_sources": ["装备：绳索"],
      "disadvantage_sources": []
    },
    {
      "key": "D",
      "text": "带领全队离开当前区域",
      "risk": "controlled",
      "requires_check": false,
      "collective": true
    }
  ]
}
```

必须恰好包含 A、B、C、D。当前版本不强制每组至少一个 `safe`：保留真实可用的安全办法，也不为凑格式虚构安全退路。选项只能声明行动意图，不能提前保证结果或揭露谜底。影响全队的行为必须设为 `collective: true`，所有集体选项总数不超过 2；不能依赖引擎降级超量标记来取得授权。

当前实现支持 `collective: true` 配合 `vote_scope: local` 表达纯现场小队决定；省略时为全队 `party`。小队选项不能决定远处成员行动或全队主线利益。核对目标版本后再使用；正文写“让其他人守门”却标记个人行动仍是不合格选项。

## 世界脉冲事件

```json
{
  "event_pool": [
    {
      "id": "late-courier",
      "title": "迟到的信使",
      "description": "负伤信使带来一条新线索和可响应威胁。",
      "weight": 2,
      "minimum_round": 2,
      "cooldown_rounds": 4,
      "once": true,
      "severity": "standard",
      "conditions": {
        "locations": ["边境无名酒馆·大厅"],
        "required_facts": ["酒馆保持中立"],
        "excluded_facts": ["信使事件已经解决"],
        "minimum_players": 2,
        "maximum_players": 4
      }
    }
  ]
}
```

每个完整多人轮次最多抽取一次。抽取结果先落库，模型失败重试不会换事件。

## 时间规则

JSON 内部仍以秒保存；`null` 或 `-1` 表示不限时，`0` 非法。管理台可直接选择秒、分钟、小时或天。

```json
{
  "time_rules": {
    "card_code_ttl_seconds": 1800,
    "card_draft_ttl_seconds": 604800,
    "card_completion_timeout_seconds": 86400,
    "preparation_timeout_seconds": 86400,
    "ready_timeout_seconds": 1800,
    "turn_timeout_seconds": 600,
    "turn_reminder_seconds": 180,
    "max_consecutive_timeouts": 2,
    "standby_timeout_seconds": 604800,
    "delegation_ttl_seconds": 86400,
    "check_timeout_seconds": 300,
    "vote_round_one_seconds": 600,
    "vote_round_two_seconds": 300,
    "vote_reminder_seconds": 120,
    "all_idle_pause_seconds": 600,
    "pause_stops_clock": true,
    "announce_timeouts": true,
    "turn_timeout_action": "skip",
    "card_timeout_action": "standby",
    "ready_timeout_action": "standby"
  }
}
```

`max_consecutive_timeouts: -1` 表示永不自动转候补。模型请求和数据库锁属于技术超时，不能设为不限时。

## 安全退场与返场

```json
{
  "safe_exit_templates": [
    "{character}去追查一条只能由其本人确认的线索，并留下未来重新联络的记号。"
  ],
  "return_rules": {
    "allow_return": true,
    "allow_resurrection": false,
    "requires_vote": true,
    "requires_story_condition": true
  }
}
```

模板必须中性，不羞辱、不擅自杀死角色，也不消耗团队公共物品。

## 初始状态

```json
{
  "location": "边境无名酒馆·大厅",
  "time": "雨夜",
  "scene_summary": "酒馆刚开门，尚无事件发生。",
  "facts": ["酒馆保持中立"],
  "inventory": {},
  "relationships": {},
  "check_modifiers": {
    "advantages": [],
    "disadvantages": []
  }
}
```

`check_modifiers` 可登记当前场景中明确、可验证的优劣势来源。模型不能直接改写权限、角色卡、会话阶段、骰点、投票或计时规则。

## 发布前检查

格式通过不等于剧情可玩。结合 [生成提示词质量自检](../worlds/WORLD_GENERATOR_PROMPT.md#210-交付前自检质量部分) 和 [实践指南验收矩阵](WORLD_AUTHORING_BEST_PRACTICES.md#六交付验收矩阵)，为每项记录具体字段/场景、证据、结果与未解决问题，状态统一使用“通过 / 待改 / 未验证 / 不适用”；仅写“已保证”不算验收。静态体检、机械模拟、叙事桌面演练分别记录，未运行的检查不得报通过。

- 人数最小值与上限是否合理？
- 角色卡是否保留必要短标识，以少量预设选择为主，并且只有一个可留空的补充说明，没有多道自由发挥题？
- 职业是否建议至少 6 个且有实际区别（不足时说明理由），属性是否按职业预设而非玩家分点？
- 是否逐职业验证最终属性与修正，关闭任意手动改值、保留主副属性选择与基础/加成/最终值/修正面板，并演练换职业重算与跳过补充？
- 属性预算、区间和加值表是否一致？
- A—D 是否只表达行动意图？
- 是否保留合理的安全办法，且风险符合实际处境，没有机械地每轮塞危机或安全退路？
- DC、风险和优劣势是否分别承担不同作用？
- 致命风险是否提前明示后果？
- 集体行为是否标记 `collective`？
- 自动 NPC 和上下文预算是否适合长期副本？
- 内容边界与安全暂停是否符合本团约定？
- 行动所需的陌生名词是否已经说明“它是什么”或“有什么用”，无关背景是否留作可选而非授课？
- 建筑与器物描写是否让普通玩家一眼分清门、窗、平台、台阶、通道、墙和支架？
- 新场景是否写清空间关系、出口或障碍，以及至少一个可互动对象？
- 是否演练优势、劣势、灵感、集体检定、投票、超时、存档、克隆和模型失败？
- 开场能否用白话回答“我为什么想参与、现在能决定什么”？关键 NPC 是否有动机和行动，而不只是线索容器？
- 是否区分必需成果、推荐场景与可选支线；最低人数、缺席、缺职业、拒绝分头与全员成功时是否仍有可行承接？
- 关键线索是否带来信息增量、可靠验证或行动机会，而非反复观察同一异常？
- 里程碑是否按证据记录成果，不要求某人失败、不因超期跳章、不把计划当完成，也不要求重复已完成事件？
- `min_turns`、`max_turns` 与总预算是否符合当前计数和门槛，是否给表决、结算与尾声留出空间？
- 连续失败后是否有真实后果及其他办法、撤退或失败终局，而不是无限原地重试？
- 普通状态是否有可执行的治疗路径，剧情锁定是否明确，资源恢复是否本局可达？
- 提前营救、取物、断线、治疗后，后章是否保留成果；客观事实、角色感知和假说是否区分？
- 核心冲突是否有明确终点和完整结尾，个人后日谈、知识点与可选谜团是否不会阻塞完结？
- 文档、示例、提示词是否自相矛盾？关键机械能力是否有实现和演练依据，而不是新增几句文字就假定已实现？
