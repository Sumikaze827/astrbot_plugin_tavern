"""世界包生成 agent 子系统。

目标：把「一次性单发提示词」（worlds/WORLD_GENERATOR_PROMPT.md）升级为
可审批、可继承前作、可反省的多阶段 agent 工作流。

本包与运行时引擎的关系：
- 只读地消费 sessions/events/story_ledger 等表，用于「继承前作玩家决策」；
- 绝不直接写运行库，生成产物只落文件到 worlds/，再由控制台导入通道过
  ``inspect_world_package`` 体检门；
- LLM 调用直连 ``context.llm_generate``，不复用 ``TavernEngine._llm_generate_metered``
  （后者的 ``token_usage.session_id`` 有指向 sessions 的外键，而生成任务不属于任何对局）。
"""

from __future__ import annotations

__all__ = ["lint"]
