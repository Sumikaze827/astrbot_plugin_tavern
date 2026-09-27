from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .api import ExtensionRegistry, HookRegistry, TavernPublicAPI
from .database import TavernDatabase
from .engine import TavernEngine
from .events import EventBroker
from .web_console import TavernWebConsole
from .worldgen.service import WorldgenService
from .worldgen.store import JobStore

#: 插件自带的 ``worlds/``——生成的包默认落这里，世界市场才能自动发现。
PLUGIN_WORLDS_DIR = Path(__file__).resolve().parent.parent / "worlds"


@dataclass(slots=True)
class TavernRuntime:
    database: TavernDatabase
    broker: EventBroker
    engine: TavernEngine
    web_console: TavernWebConsole
    hooks: HookRegistry
    extensions: ExtensionRegistry
    public_api: TavernPublicAPI
    worldgen: WorldgenService


def build_runtime(
    *,
    context: Any,
    plugin_config: Any,
    data_dir: Path,
    config_provider: Any,
    logger: Any,
    allow_group: Any,
    config_lock: Any,
) -> TavernRuntime:
    hooks = HookRegistry()
    extensions = ExtensionRegistry()
    broker = EventBroker(hooks=hooks)
    database = TavernDatabase(data_dir)
    engine = TavernEngine(
        context=context,
        database=database,
        config_provider=config_provider,
        broker=broker,
        extensions=extensions,
    )
    config = config_provider() if callable(config_provider) else config_provider
    # 语料根 / 输出目录由配置决定；配置里没写就退到插件自带的目录，
    # 保证"什么都不配也能起来"——只是没有原文可改编（原创路径照常可用）。
    corpus_root = str(getattr(config, "worldgen_corpus_root", "") or "").strip()
    output_dir = str(getattr(config, "worldgen_output_dir", "") or "").strip()

    worldgen = WorldgenService(
        context=context,
        database=database,
        broker=broker,
        store=JobStore(root=data_dir / "worldgen_jobs"),
        plugin_config=config_provider,
        corpus_root=corpus_root or (data_dir / "corpus"),
        index_dir=data_dir / "worldgen_index",
        output_dir=output_dir or PLUGIN_WORLDS_DIR,
        logger=logger,
    )
    web_console = TavernWebConsole(
        context=context,
        plugin_config=plugin_config,
        database=database,
        broker=broker,
        data_dir=data_dir,
        logger=logger,
        allow_group=allow_group,
        config_lock=config_lock,
        extensions=extensions,
        hooks=hooks,
        engine=engine,
        worldgen=worldgen,
    )

    return TavernRuntime(
        database=database,
        broker=broker,
        engine=engine,
        web_console=web_console,
        hooks=hooks,
        extensions=extensions,
        public_api=TavernPublicAPI(database, hooks, extensions, engine),
        worldgen=worldgen,
    )
