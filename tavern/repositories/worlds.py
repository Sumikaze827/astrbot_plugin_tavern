"""Domain repository methods extracted from the SQLite store."""

import logging

from ..database_support import *
from ..entity_registry import EntityRegistry
from ..resolution_receipts import content_hash
from ..rule_runtime import enabled_feature_versions

logger = logging.getLogger(__name__)


class WorldRepositoryMixin:
    async def list_worlds(
        self,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        return await self._run(self._list_worlds, include_archived)

    def _list_worlds(self, include_archived: bool) -> list[dict[str, Any]]:
        with self._connect() as connection:
            condition = "" if include_archived else "WHERE archived = 0"
            rows = connection.execute(
                f"""
                SELECT * FROM worlds
                {condition}
                ORDER BY archived ASC, sort_order ASC, display_no ASC
                """
            ).fetchall()
            result: list[dict[str, Any]] = []
            for row in rows:
                item = self._world(row)
                count = connection.execute(
                    "SELECT COUNT(*) FROM characters WHERE world_id = ?",
                    (row["id"],),
                ).fetchone()[0]
                item["character_count"] = count
                result.append(item)
            return result

    async def get_world(self, world_ref: str) -> dict[str, Any]:
        return await self._run(self._get_world, world_ref)

    def _get_world(self, world_ref: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM worlds WHERE id = ? OR slug = ?",
                (world_ref, world_ref),
            ).fetchone()
            if not row:
                raise DatabaseNotFoundError("世界包不存在")
            world = self._world(row)
            character_rows = connection.execute(
                """
                SELECT * FROM characters
                WHERE world_id = ? AND enabled = 1
                ORDER BY sort_order ASC, name COLLATE NOCASE
                """,
                (row["id"],),
            ).fetchall()
            world["characters"] = [
                self._character(character) for character in character_rows
            ]
            return world

    async def save_world(
        self,
        payload: Mapping[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(self._save_world, dict(payload), actor_id)

    async def set_world_sort_order(
        self,
        world_id: str,
        sort_order: int,
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._set_world_sort_order, world_id, int(sort_order), actor_id
        )

    def _set_world_sort_order(
        self, world_id: str, sort_order: int, actor_id: str
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                current = connection.execute(
                    "SELECT * FROM worlds WHERE id=?", (world_id,)
                ).fetchone()
                if not current:
                    raise DatabaseNotFoundError("世界包不存在")
                connection.execute(
                    "UPDATE worlds SET sort_order=? WHERE id=?",
                    (max(1, sort_order), world_id),
                )
                self._insert_audit(
                    connection, "", actor_id, "world.reorder", world_id,
                    {"display_no": current["display_no"], "sort_order": max(1, sort_order)},
                )
                row = connection.execute(
                    "SELECT * FROM worlds WHERE id=?", (world_id,)
                ).fetchone()
                connection.execute("COMMIT")
                return self._world(row)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    @staticmethod
    def _allocate_world_display_no(connection: sqlite3.Connection) -> int:
        maximum = int(connection.execute(
            "SELECT COALESCE(MAX(display_no), 0) + 1 AS value FROM worlds"
        ).fetchone()["value"])
        row = connection.execute(
            "SELECT value FROM tavern_meta WHERE key='next_world_display_no'"
        ).fetchone()
        try:
            counter = int(row["value"]) if row else 1
        except (TypeError, ValueError):
            counter = 1
        value = max(maximum, counter)
        connection.execute(
            """
            INSERT INTO tavern_meta(key, value) VALUES ('next_world_display_no', ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (str(value + 1),),
        )
        return value

    def _persist_world_revision(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        world: Mapping[str, Any],
        now: str,
    ) -> None:
        revision = int(row["revision"])
        snapshot_hash = content_hash(world)
        connection.execute(
            """
            INSERT OR IGNORE INTO world_rule_revisions(
                id, world_id, world_revision, content_hash, rules_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("world_rule"), row["id"], revision, snapshot_hash,
                json_dump(world.get("rules") or {}), now,
            ),
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO world_snapshots(
                id, world_id, world_revision, content_hash, snapshot_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("world_snapshot"), row["id"], revision, snapshot_hash,
                json_dump(dict(world)), now,
            ),
        )
        features = enabled_feature_versions(world)
        required = {
            str(item).split("@", 1)[0]
            for item in world.get("required_features", [])
            if isinstance(item, str)
        }
        for feature, version in features.items():
            connection.execute(
                """
                INSERT OR REPLACE INTO world_feature_versions(
                    world_id, world_revision, feature_name, feature_version,
                    required, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (row["id"], revision, feature, version, int(feature in required), now),
            )
        try:
            registry = EntityRegistry(world)
        except Exception:
            registry = None
        if registry is not None:
            for item in registry.export():
                connection.execute(
                    """
                    INSERT OR REPLACE INTO world_entity_registry(
                        world_id, world_revision, entity_ref, entity_type,
                        label, definition_json, content_hash, visibility, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["id"], revision, item["ref"], item["entity_type"],
                        item["label"], json_dump(item["definition"]),
                        content_hash(item["definition"]),
                        str(item["definition"].get("visibility") or "world"), now,
                    ),
                )

    def _save_world(
        self,
        payload: dict[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        world_id = str(payload.get("id") or "").strip()
        slug = validate_slug(payload.get("slug"))
        name = clean_text(payload.get("name"), max_chars=400)
        if not name:
            raise ValueError("世界名称不能为空")
        description = clean_text(payload.get("description"), max_chars=20000)
        system_prompt = clean_text(
            payload.get("system_prompt"),
            max_chars=200000,
        )
        if not system_prompt:
            raise ValueError("世界设定不能为空")
        opening_scene = clean_text(
            payload.get("opening_scene"),
            max_chars=50000,
        )
        rules = payload.get("rules")
        initial_state = payload.get("initial_state")
        if not isinstance(rules, Mapping):
            raise ValueError("规则必须是 JSON 对象")
        if not isinstance(initial_state, Mapping):
            raise ValueError("初始状态必须是 JSON 对象")
        rules = dict(rules)
        if "world_schema_version" in payload:
            rules["world_schema_version"] = payload["world_schema_version"]
        if "capabilities" in payload:
            rules["capabilities"] = payload["capabilities"]
        validate_world_contract({**payload, "rules": rules})
        known_fields = {
            "id", "slug", "name", "description", "system_prompt", "rules",
            "display_no", "sort_order",
            "opening_scene", "initial_state", "archived", "revision",
            "created_at", "updated_at", "world_schema_version", "capabilities",
            "player_limits", "card_template", "time_rules", "choice_mode",
            "check_density",
        }
        provided_extensions = {
            str(key): value
            for key, value in payload.items()
            if key not in known_fields
        }
        if "character_card" in rules:
            raw_card = rules["character_card"]
            if isinstance(raw_card, Mapping) and "fields" not in raw_card:
                rules["character_card"] = card_template({"rules": rules})
            validate_card_template_config(rules["character_card"])
        now = utc_now()

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if world_id:
                    current = connection.execute(
                        "SELECT * FROM worlds WHERE id = ?",
                        (world_id,),
                    ).fetchone()
                    if not current:
                        raise DatabaseNotFoundError("世界包不存在")
                    current_extensions = json_load(
                        current["extensions_json"],
                        {},
                    )
                    extensions = (
                        {**dict(current_extensions), **provided_extensions}
                        if isinstance(current_extensions, Mapping)
                        else dict(provided_extensions)
                    )
                    expected_revision = payload.get("revision")
                    if (
                        expected_revision is not None
                        and int(expected_revision) != current["revision"]
                    ):
                        raise DatabaseConflictError(
                            "世界包已被其他操作更新，请刷新后重试"
                        )
                    connection.execute(
                        """
                        UPDATE worlds SET
                            slug = ?, name = ?, description = ?,
                            system_prompt = ?, rules_json = ?, extensions_json = ?,
                            opening_scene = ?, initial_state_json = ?,
                            archived = ?, revision = revision + 1,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            slug,
                            name,
                            description,
                            system_prompt,
                            json_dump(dict(rules)),
                            json_dump(extensions),
                            opening_scene,
                            json_dump(dict(initial_state)),
                            (
                                int(bool(payload["archived"]))
                                if "archived" in payload
                                else current["archived"]
                            ),
                            now,
                            world_id,
                        ),
                    )
                    action = "world.update"
                else:
                    world_id = new_id("world")
                    display_no = self._allocate_world_display_no(connection)
                    connection.execute(
                        """
                        INSERT INTO worlds(
                            id, slug, display_no, sort_order, name, description, system_prompt,
                            rules_json, extensions_json, opening_scene, initial_state_json,
                            archived, revision, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1, ?, ?)
                        """,
                        (
                            world_id,
                            slug,
                            display_no,
                            display_no,
                            name,
                            description,
                            system_prompt,
                            json_dump(dict(rules)),
                            json_dump(provided_extensions),
                            opening_scene,
                            json_dump(dict(initial_state)),
                            now,
                            now,
                        ),
                    )
                    action = "world.create"
                self._insert_audit(
                    connection,
                    "",
                    actor_id,
                    action,
                    world_id,
                    {"slug": slug, "name": name},
                )
                row = connection.execute(
                    "SELECT * FROM worlds WHERE id = ?",
                    (world_id,),
                ).fetchone()
                self._persist_world_revision(
                    connection, row, self._world(row), now
                )
                connection.execute("COMMIT")
                return self._world(row)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def _commit_vnext_workflow(
        self,
        connection: sqlite3.Connection,
        *,
        session: sqlite3.Row,
        new_turn: int,
        acting_round: int,
        next_turn_state: Mapping[str, Any],
        player_user_id: str,
        player_event_id: str,
        narrator_event_id: str,
        world_state: Mapping[str, Any],
        check_payload: Mapping[str, Any],
        workflow: Mapping[str, Any],
        now: str,
    ) -> dict[str, Any]:
        """Persist choices, votes, rolls, events and timers in the turn TX."""

        result: dict[str, Any] = {}
        if not workflow:
            return result

        participant = connection.execute(
            """
            SELECT * FROM participants
            WHERE session_id = ? AND group_user_id = ?
            """,
            (session["id"], player_user_id),
        ).fetchone()
        if not participant:
            raise InvalidTransitionError("当前玩家没有有效的副本参与记录")

        choice_set_id = str(workflow.get("choice_set_id") or "")
        selected_key = str(workflow.get("selected_key") or "").upper()
        is_freeform = bool(workflow.get("freeform"))
        if not is_freeform and (
            not choice_set_id or selected_key not in CHOICE_KEYS
        ):
            raise ValueError("缺少有效的选项提交信息")
        if is_freeform:
            # 自由演绎：作废尚未选择的活跃选项集，释放「每会话仅一个
            # active 选项集」的占位，让下方 next_choices 能正常插入。
            connection.execute(
                """
                UPDATE choice_sets SET status = 'superseded', updated_at = ?
                WHERE session_id = ? AND status = 'active'
                """,
                (now, session["id"]),
            )
            connection.execute(
                """
                UPDATE timer_instances
                SET status = 'completed', updated_at = ?
                WHERE session_id = ? AND participant_id = ?
                  AND timer_type = 'turn' AND status = 'active'
                """,
                (now, session["id"], participant["id"]),
            )
            connection.execute(
                """
                UPDATE participants
                SET consecutive_timeouts = 0, updated_at = ?
                WHERE id = ?
                """,
                (now, participant["id"]),
            )
            result["choice"] = {
                "choice_set_id": "",
                "key": "",
                "text": "自由演绎",
                "freeform": True,
            }
        else:
            flavor_text = clean_text(
                workflow.get("flavor_text"),
                max_chars=160,
            )
            choice_row = connection.execute(
                """
                SELECT * FROM choice_sets
                WHERE id = ? AND session_id = ? AND status = 'active'
                """,
                (choice_set_id, session["id"]),
            ).fetchone()
            if not choice_row:
                raise DatabaseConflictError("当前选项已经失效，请重新查看回合")
            if choice_row["participant_id"] != participant["id"]:
                raise PermissionError("该选项不属于当前玩家")
            if int(choice_row["session_revision"]) != int(session["revision"]):
                # 0.13.x：选择集创建后被无关写入推进了会话 revision（死亡
                # 落库/并发回合等）时，该 active 选择集仍属于当前回合的
                # 当前玩家，只是快照 revision 过期——自愈到当前 revision
                # 再继续，避免「场景已变化」反复拒绝（曾导致回合永久卡死）。
                # status='active' + participant 匹配已保证它是当前回合
                # 唯一有效的选择集；revision 相等性对无关推进过于严格。
                connection.execute(
                    """
                    UPDATE choice_sets
                    SET session_revision = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (int(session["revision"]), now, choice_set_id),
                )
            choices = normalize_choices(json_load(choice_row["choices_json"], []))
            selected = next(
                item for item in choices if item["key"] == selected_key
            )
            connection.execute(
                """
                UPDATE choice_sets SET
                    status = 'selected', selected_key = ?,
                    flavor_text = ?, updated_at = ?
                WHERE id = ?
                """,
                (selected_key, flavor_text, now, choice_set_id),
            )
            connection.execute(
                """
                UPDATE timer_instances
                SET status = 'completed', updated_at = ?
                WHERE session_id = ? AND participant_id = ?
                  AND timer_type = 'turn' AND status = 'active'
                """,
                (now, session["id"], participant["id"]),
            )
            connection.execute(
                """
                UPDATE participants
                SET consecutive_timeouts = 0, updated_at = ?
                WHERE id = ?
                """,
                (now, participant["id"]),
            )
            result["choice"] = {
                "choice_set_id": choice_set_id,
                "key": selected_key,
                "text": selected["text"],
            }

        if check_payload:
            roll_id = new_id("roll")
            # 自由演绎回合可能没有对应的 choice_set_id，此时写 NULL，
            # 避免往 rolls.choice_set_id 写入空串触发外键约束失败。
            roll_choice_set_id = (
                None if not choice_set_id else choice_set_id
            )
            connection.execute(
                """
                INSERT INTO rolls(
                    id, session_id, choice_set_id, participant_id,
                    roll_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    roll_id,
                    session["id"],
                    roll_choice_set_id,
                    participant["id"],
                    json_dump(dict(check_payload)),
                    now,
                ),
            )
            result["roll_id"] = roll_id

        ending_completion = workflow.get("ending_completion")
        if (
            isinstance(ending_completion, Mapping)
            and ending_completion.get("complete") is True
        ):
            # A validated ending must not create a fallback A-D choice set or a
            # last-moment world event. The v0.5 operation pass below records the
            # ending progress and exact milestone IDs in this same transaction.
            result["ending_completion"] = {
                "chapter_id": clean_text(
                    ending_completion.get("chapter_id"), max_chars=128
                ),
                "milestone_ids": [
                    clean_text(item, max_chars=128)
                    for item in (
                        ending_completion.get("milestone_ids") or []
                    )[:16]
                    if clean_text(item, max_chars=128)
                ],
            }
            result["next_choice_set_id"] = ""
            return result

        config = connection.execute(
            """
            SELECT * FROM instance_configs WHERE session_id = ?
            """,
            (session["id"],),
        ).fetchone()
        world = json_load(
            config["world_snapshot_json"] if config else "",
            {},
        )
        time_rules = normalize_time_rules(
            json_load(config["time_rules_json"] if config else "", {})
        )
        round_completed = int(next_turn_state["round_no"]) > acting_round
        if round_completed:
            selected_event = self._select_world_event(
                connection,
                session_id=session["id"],
                round_no=acting_round,
                world=world,
                turn_no=new_turn,
                now=now,
            )
            if selected_event:
                result["world_event"] = selected_event

        return_progress = workflow.get("return_progress")
        if isinstance(return_progress, Mapping):
            request_id = str(return_progress.get("request_id") or "")
            evidence = clean_text(
                return_progress.get("evidence"),
                max_chars=500,
            )
            if request_id and evidence:
                progress_result = self._record_return_progress(
                    connection,
                    session_id=session["id"],
                    request_id=request_id,
                    evidence=evidence,
                    completed=bool(return_progress.get("completed", False)),
                    round_no=int(next_turn_state["round_no"]),
                    turn_no=new_turn,
                    now=now,
                )
                if progress_result:
                    result["return_progress"] = progress_result

        next_user_id = str(next_turn_state["current_user_id"] or "")
        next_participant = connection.execute(
            """
            SELECT * FROM participants
            WHERE session_id = ? AND group_user_id = ?
              AND participation_status = 'active'
              AND card_status = 'approved'
            """,
            (session["id"], next_user_id),
        ).fetchone()
        if not next_participant:
            result["next_choice_set_id"] = ""
            return result

        group_decision = workflow.get("group_decision")
        if isinstance(group_decision, Mapping):
            question = clean_text(
                group_decision.get("question"),
                max_chars=500,
            )
            options = self._normalize_vote_options(
                group_decision.get("options")
            )
            if question and len(options) >= 2:
                eligible = [
                    str(row["group_user_id"])
                    for row in connection.execute(
                        """
                        SELECT group_user_id FROM participants
                        WHERE session_id = ?
                          AND participation_status = 'active'
                          AND card_status = 'approved'
                        GROUP BY group_user_id
                        ORDER BY MIN(created_at)
                        """,
                        (session["id"],),
                    ).fetchall()
                ]
                if group_decision.get("vote_scope") == "local":
                    from ..party_scope import local_voters
                    eligible = local_voters(connection, session["id"], participant["group_user_id"], eligible)
                    question = "【现场小队】" + question
                vote_id = new_id("vote")
                connection.execute(
                    """
                    INSERT INTO group_votes(
                        id, session_id, source_event_id, question,
                        options_json, eligible_user_ids_json, stage,
                        status, suspended_user_id, deadline_at,
                        result_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 1, 'open', ?, ?, '{}', ?, ?)
                    """,
                    (
                        vote_id,
                        session["id"],
                        narrator_event_id,
                        question,
                        json_dump(options),
                        json_dump(eligible),
                        next_user_id,
                        deadline_after(
                            time_rules["vote_round_one_seconds"]
                        ),
                        now,
                        now,
                    ),
                )
                self._create_timer(
                    connection,
                    session_id=session["id"],
                    participant_id="",
                    timer_type="vote",
                    timeout_seconds=time_rules["vote_round_one_seconds"],
                    reminder_seconds=time_rules["vote_reminder_seconds"],
                    action={"vote_id": vote_id, "stage": 1},
                )
                result["vote_id"] = vote_id
                return result

        next_choices_raw = workflow.get("next_choices")
        try:
            next_choices = normalize_choices(next_choices_raw)
        except ValueError:
            next_choices = fallback_choices(world_state)
            result["choice_fallback"] = True
        choice_id = new_id("choices")
        connection.execute(
            """
            INSERT INTO choice_sets(
                id, session_id, participant_id, round_no,
                session_revision, choices_json, status, reroll_count,
                idempotency_key, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'active', 0, ?, ?, ?)
            """,
            (
                choice_id,
                session["id"],
                next_participant["id"],
                next_turn_state["round_no"],
                int(session["revision"]) + 1,
                json_dump(next_choices),
                f"turn:{session['id']}:{new_turn + 1}",
                now,
                now,
            ),
        )
        self._create_timer(
            connection,
            session_id=session["id"],
            participant_id=next_participant["id"],
            timer_type="turn",
            timeout_seconds=time_rules["turn_timeout_seconds"],
            reminder_seconds=time_rules["turn_reminder_seconds"],
            action={
                "choice_set_id": choice_id,
                "user_id": next_user_id,
            },
        )
        result["next_choice_set_id"] = choice_id
        result["next_participant_id"] = next_participant["id"]
        return result

    def _apply_v05_turn_ops(
        self,
        connection: sqlite3.Connection,
        *,
        session: sqlite3.Row,
        participant: sqlite3.Row,
        new_turn: int,
        acting_round: int,
        source_event_id: str,
        workflow: Mapping[str, Any],
        check_payload: Mapping[str, Any],
        now: str,
    ) -> dict[str, Any]:
        """Apply validated v0.5 state operations inside the turn transaction."""

        result: dict[str, Any] = {
            "npc": [],
            "clocks": [],
            "ledger": [],
            "locations": [],
            "statuses": [],
            "assists": [],
        }
        session_id = str(session["id"])
        from ..npc_scope import sanitize_npc_ops, record_perceptions
        scope = workflow.get("npc_scope")
        # 提交时再清一遍：越界条目就地剥离，绝不让记账问题作废整轮。
        sanitized_ops, scope_violations = sanitize_npc_ops(
            workflow.get("npc_ops") or [], scope
        )
        if scope_violations:
            logger.warning(
                "NPC ops sanitized during commit in %s: %s",
                session_id,
                [item.get("kind") for item in scope_violations],
            )

        inspiration_mode = str(
            workflow.get("inspiration_mode") or ""
        ).lower()
        if inspiration_mode in {"advantage", "reroll"} and check_payload:
            runtime = connection.execute(
                """
                SELECT * FROM character_runtime_states
                WHERE session_id = ? AND participant_id = ?
                """,
                (session_id, participant["id"]),
            ).fetchone()
            if not runtime:
                raise InvalidTransitionError("角色缺少副本运行状态")
            state = json_load(runtime["state_json"], {})
            state = dict(state) if isinstance(state, Mapping) else {}
            balance = bounded_int(state.get("inspiration"), 1, 0, 3)
            if balance < 1:
                raise InvalidTransitionError("灵感点不足，本轮没有提交")
            operation_id = (
                f"inspiration:{workflow.get('choice_set_id')}:{inspiration_mode}"
            )
            existing = connection.execute(
                """
                SELECT balance_after FROM inspiration_transactions
                WHERE operation_id = ?
                """,
                (operation_id,),
            ).fetchone()
            if not existing:
                balance -= 1
                state["inspiration"] = balance
                state["inspiration_max"] = bounded_int(
                    state.get("inspiration_max"),
                    3,
                    1,
                    10,
                )
                connection.execute(
                    """
                    UPDATE character_runtime_states SET
                        state_json = ?, revision = revision + 1,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (json_dump(state), now, runtime["id"]),
                )
                connection.execute(
                    """
                    INSERT INTO inspiration_transactions(
                        id, session_id, participant_id, delta,
                        balance_after, reason, operation_id, created_at
                    ) VALUES (?, ?, ?, -1, ?, ?, ?, ?)
                    """,
                    (
                        new_id("inspire"),
                        session_id,
                        participant["id"],
                        balance,
                        (
                            "投骰前取得优势"
                            if inspiration_mode == "advantage"
                            else "预授权重投完整骰池"
                        ),
                        operation_id,
                        now,
                    ),
                )
            else:
                balance = int(existing["balance_after"])
            result["inspiration"] = {
                "mode": inspiration_mode,
                "balance": balance,
            }

        assist_token_id = str(
            workflow.get("assist_token_id") or ""
        ).strip()
        if assist_token_id and check_payload:
            consumed = connection.execute(
                """
                UPDATE assist_tokens SET status = 'consumed',
                    consumed_at = ?
                WHERE id = ? AND session_id = ? AND status = 'active'
                """,
                (now, assist_token_id, session_id),
            )
            if consumed.rowcount:
                result["consumed_assist_id"] = assist_token_id

        location_ops = workflow.get("location_ops")
        if isinstance(location_ops, Sequence) and not isinstance(
            location_ops,
            (str, bytes),
        ):
            for operation in location_ops[:16]:
                if not isinstance(operation, Mapping):
                    continue
                target_ref = clean_text(
                    operation.get("target_id"),
                    max_chars=128,
                )
                location = clean_text(
                    operation.get("location"),
                    max_chars=160,
                )
                if not target_ref or not location:
                    continue
                target = connection.execute(
                    """
                    SELECT * FROM participants
                    WHERE session_id = ? AND (
                        id = ? OR group_user_id = ? OR
                        lower(character_name) = lower(?) OR
                        lower(character_code) = lower(?)
                    )
                    ORDER BY CASE WHEN id = ? THEN 0 ELSE 1 END
                    LIMIT 1
                    """,
                    (
                        session_id,
                        target_ref,
                        target_ref,
                        target_ref,
                        target_ref,
                        target_ref,
                    ),
                ).fetchone()
                if not target:
                    continue
                runtime = connection.execute(
                    """
                    SELECT * FROM character_runtime_states
                    WHERE session_id = ? AND participant_id = ?
                    """,
                    (session_id, target["id"]),
                ).fetchone()
                if runtime is None:
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO character_runtime_states(
                            id, session_id, participant_id, character_card_id,
                            state_json, revision, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, '{}', 1, ?, ?)
                        """,
                        (
                            new_id("runtime"),
                            session_id,
                            target["id"],
                            target["character_card_id"],
                            now,
                            now,
                        ),
                    )
                    runtime = connection.execute(
                        """
                        SELECT * FROM character_runtime_states
                        WHERE session_id = ? AND participant_id = ?
                        """,
                        (session_id, target["id"]),
                    ).fetchone()
                if runtime is None:
                    continue
                state = json_load(runtime["state_json"], {})
                state = dict(state) if isinstance(state, Mapping) else {}
                state["current_location"] = location
                connection.execute(
                    """
                    UPDATE character_runtime_states SET
                        state_json = ?, revision = revision + 1,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (json_dump(state), now, runtime["id"]),
                )
                result["locations"].append(
                    {
                        "target_id": target["id"],
                        "location": location,
                    }
                )

        status_ops = workflow.get("status_ops")
        if isinstance(status_ops, Sequence) and not isinstance(
            status_ops,
            (str, bytes),
        ):
            for operation in status_ops[:16]:
                if not isinstance(operation, Mapping):
                    continue
                target_ref = clean_text(
                    operation.get("target_id"),
                    max_chars=128,
                )
                name = clean_text(operation.get("name"), max_chars=100)
                if not target_ref or not name:
                    continue
                target = connection.execute(
                    """
                    SELECT * FROM participants
                    WHERE session_id = ? AND (
                        id = ? OR group_user_id = ? OR
                        lower(character_name) = lower(?) OR
                        lower(character_code) = lower(?)
                    )
                    ORDER BY CASE WHEN id = ? THEN 0 ELSE 1 END
                    LIMIT 1
                    """,
                    (
                        session_id,
                        target_ref,
                        target_ref,
                        target_ref,
                        target_ref,
                        target_ref,
                    ),
                ).fetchone()
                if not target:
                    continue
                runtime = connection.execute(
                    """
                    SELECT * FROM character_runtime_states
                    WHERE session_id = ? AND participant_id = ?
                    """,
                    (session_id, target["id"]),
                ).fetchone()
                if runtime is None:
                    # 兜底：缺运行状态行时自动补一条，避免「状态更新不上」被
                    # 静默丢弃。正常路径该行在角色卡落卡时创建，个别参与者
                    # 可能没有（如未走落卡流程），这里直接补上保证状态可写。
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO character_runtime_states(
                            id, session_id, participant_id, character_card_id,
                            state_json, revision, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, '{}', 1, ?, ?)
                        """,
                        (
                            new_id("runtime"),
                            session_id,
                            target["id"],
                            target["character_card_id"],
                            now,
                            now,
                        ),
                    )
                    runtime = connection.execute(
                        """
                        SELECT * FROM character_runtime_states
                        WHERE session_id = ? AND participant_id = ?
                        """,
                        (session_id, target["id"]),
                    ).fetchone()
                if runtime is None:
                    continue
                state = json_load(runtime["state_json"], {})
                state = dict(state) if isinstance(state, Mapping) else {}
                statuses = [
                    dict(item)
                    for item in state.get("statuses", [])
                    if isinstance(item, Mapping)
                ]
                op = str(operation.get("op") or "add").lower()

                def _status_name_match(stored_name: str) -> bool:
                    stored = str(stored_name or "").casefold()
                    wanted = name.casefold()
                    if not stored:
                        return False
                    if op == "remove":
                        # remove 用包含匹配：模型措辞略异（如「位置暴露」vs
                        # 存量「位置暴露风险」）也应能删掉，避免「取消不了」。
                        return stored == wanted or stored in wanted or wanted in stored
                    # add/update 用精确匹配，避免误合并不同的状态。
                    return stored == wanted

                matched_indexes = [
                    index
                    for index, item in enumerate(statuses)
                    if _status_name_match(str(item.get("name") or ""))
                ]
                if op == "remove":
                    exact = [index for index in matched_indexes
                             if str(statuses[index].get("name") or "").casefold() == name.casefold()]
                    # A canonical removal must not also erase a longer, distinct
                    # condition (including a story lock). Alias fallback is safe
                    # only when it identifies exactly one stored condition.
                    matched_indexes = exact or (matched_indexes if len(matched_indexes) == 1 else [])
                if op == "remove":
                    for index in reversed(matched_indexes):
                        statuses.pop(index)
                else:
                    existing_index = (
                        matched_indexes[0] if matched_indexes else -1
                    )
                    existing_status = (
                        statuses[existing_index] if existing_index >= 0 else {}
                    )
                    entry = {
                        "name": name,
                        "severity": str(
                            operation.get("severity") or "minor"
                        ),
                        "affects": [
                            clean_text(item, max_chars=80)
                            for item in (
                                operation.get("affects")
                                if isinstance(
                                    operation.get("affects"),
                                    list,
                                )
                                else []
                            )[:12]
                            if clean_text(item, max_chars=80)
                        ],
                        "effect": clean_text(
                            operation.get("effect"),
                            max_chars=300,
                        ),
                        "removal": clean_text(
                            operation.get("removal"),
                            max_chars=300,
                        ),
                        "healing_policy": clean_text(
                            existing_status.get("healing_policy") or "ordinary",
                            max_chars=30,
                        ),
                        "removable_by_healing": bool(
                            existing_status.get("removable_by_healing", True)
                        ),
                        "permanent": bool(existing_status.get("permanent", False)),
                        "policy_source": clean_text(
                            existing_status.get("policy_source"), max_chars=30
                        ),
                        "source_event_id": source_event_id,
                        "created_turn": new_turn,
                    }
                    if existing_index >= 0:
                        statuses[existing_index] = entry
                    else:
                        statuses.append(entry)
                state["statuses"] = statuses[:40]
                connection.execute(
                    """
                    UPDATE character_runtime_states SET
                        state_json = ?, revision = revision + 1,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (json_dump(state), now, runtime["id"]),
                )
                result["statuses"].append(
                    {
                        "target_id": target["id"],
                        "name": name,
                        "op": op,
                    }
                )

        config = connection.execute(
            """
            SELECT npc_policy_json FROM session_rule_states
            WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
        npc_policy = json_load(
            config["npc_policy_json"] if config else "",
            {},
        )
        max_new_npcs = bounded_int(
            npc_policy.get("max_new_per_turn"),
            3,
            0,
            3,
        )
        if not bool(npc_policy.get("enabled", True)):
            max_new_npcs = 0
        created_count = 0
        npc_ops = sanitized_ops
        if isinstance(npc_ops, Sequence) and not isinstance(
            npc_ops,
            (str, bytes),
        ):
            for operation in npc_ops[:12]:
                if not isinstance(operation, Mapping):
                    continue
                op = str(operation.get("op") or "").lower()
                name = clean_text(operation.get("name"), max_chars=80)
                npc_id = clean_text(operation.get("npc_id"), max_chars=128)
                aliases = [
                    clean_text(item, max_chars=80)
                    for item in (
                        operation.get("aliases")
                        if isinstance(operation.get("aliases"), list)
                        else []
                    )[:12]
                    if clean_text(item, max_chars=80)
                ]
                npc = None
                matched_by_name = False
                if npc_id:
                    npc = connection.execute(
                        """
                        SELECT * FROM session_characters
                        WHERE id = ? AND session_id = ?
                        """,
                        (npc_id, session_id),
                    ).fetchone()
                if not npc and name:
                    normalized_names = {
                        self._stable_key(name),
                        *(self._stable_key(item) for item in aliases),
                    }
                    for candidate in connection.execute(
                        """
                        SELECT * FROM session_characters
                        WHERE session_id = ?
                        """,
                        (session_id,),
                    ).fetchall():
                        candidate_names = {
                            self._stable_key(candidate["name"]),
                            *(
                                self._stable_key(item)
                                for item in json_load(
                                    candidate["aliases_json"],
                                    [],
                                )
                            ),
                        }
                        if normalized_names & candidate_names:
                            npc = candidate
                            matched_by_name = True
                            break
                if op == "create" and npc and matched_by_name:
                    state_row = connection.execute(
                        """
                        SELECT state_json FROM session_character_states
                        WHERE character_id = ?
                        """,
                        (npc["id"],),
                    ).fetchone()
                    raw_duplicate_state = json_load(
                        state_row["state_json"] if state_row else "",
                        {},
                    )
                    duplicate_state = (
                        dict(raw_duplicate_state)
                        if isinstance(raw_duplicate_state, Mapping)
                        else {}
                    )
                    proposals = list(
                        duplicate_state.get("duplicate_proposals") or []
                    )
                    proposals.append(
                        {
                            "name": name,
                            "aliases": aliases,
                            "public_profile": dict(
                                operation.get("public_profile") or {}
                            ),
                            "source_event_id": source_event_id,
                            "turn_no": new_turn,
                        }
                    )
                    duplicate_state["duplicate_proposals"] = proposals[-5:]
                    connection.execute(
                        """
                        UPDATE session_characters
                        SET review_status = 'duplicate',
                            revision = revision + 1, updated_at = ?
                        WHERE id = ?
                        """,
                        (now, npc["id"]),
                    )
                    connection.execute(
                        """
                        INSERT INTO session_character_states(
                            character_id, state_json, revision, updated_at
                        ) VALUES (?, ?, 1, ?)
                        ON CONFLICT(character_id) DO UPDATE SET
                            state_json = excluded.state_json,
                            revision = revision + 1,
                            updated_at = excluded.updated_at
                        """,
                        (npc["id"], json_dump(duplicate_state), now),
                    )
                    result["npc"].append(
                        {
                            "id": npc["id"],
                            "op": "duplicate_suspected",
                            "name": name,
                        }
                    )
                    continue
                if op == "create" and not npc:
                    registration_reasons = {
                        str(item)
                        for item in (
                            operation.get("registration_reasons") or []
                        )
                        if str(item)
                        in {
                            "direct_interaction",
                            "important_clue",
                            "long_term_memory",
                        }
                    }
                    if (
                        created_count >= max_new_npcs
                        or not name
                        or not bool(operation.get("persistent", True))
                        or not registration_reasons
                    ):
                        continue
                    created_count += 1
                    npc_id = new_id("snpc")
                    review_status = (
                        "pending"
                        if bool(
                            npc_policy.get(
                                "generated_requires_review",
                                True,
                            )
                        )
                        else "approved"
                    )
                    connection.execute(
                        """
                        INSERT INTO session_characters(
                            id, session_id, stable_key, name, aliases_json,
                            role_type, public_profile_json, known_facts_json,
                            misconceptions_json, source, review_status,
                            lifecycle_status, persistent, first_event_id,
                            last_event_id, first_turn, last_turn, revision,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?,
                                  'model_generated', ?, 'active', 1,
                                  ?, ?, ?, ?, 1, ?, ?)
                        """,
                        (
                            npc_id,
                            session_id,
                            f"generated:{self._stable_key(name)}",
                            name,
                            json_dump(aliases),
                            clean_text(
                                operation.get("role_type") or "npc",
                                max_chars=40,
                            ),
                            json_dump(
                                dict(operation.get("public_profile") or {})
                            ),
                            json_dump(
                                list(operation.get("known_facts") or [])[:30]
                            ),
                            json_dump(
                                list(operation.get("misconceptions") or [])[:20]
                            ),
                            review_status,
                            source_event_id,
                            source_event_id,
                            new_turn,
                            new_turn,
                            now,
                            now,
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO session_character_states(
                            character_id, state_json, revision, updated_at
                        ) VALUES (?, ?, 1, ?)
                        """,
                        (
                            npc_id,
                            json_dump(
                                dict(operation.get("runtime_state") or {})
                            ),
                            now,
                        ),
                    )
                    result["npc"].append(
                        {"id": npc_id, "op": "create", "name": name}
                    )
                    continue
                if not npc:
                    continue
                npc_id = str(npc["id"])
                lifecycle_status = str(npc["lifecycle_status"])
                if lifecycle_status == "dead":
                    # Narrative edits, departures and re-registration cannot
                    # undo death. Explicit administrator correction is separate.
                    pass
                elif op == "archive":
                    lifecycle_status = "archived"
                elif op == "depart":
                    lifecycle_status = "departed"
                elif op == "kill":
                    lifecycle_status = "dead"
                elif op in {"update", "create"}:
                    lifecycle_status = "active"
                profile = dict(
                    json_load(npc["public_profile_json"], {})
                )
                if isinstance(operation.get("public_profile"), Mapping):
                    from ..npc_direction import CORE_FIELDS
                    # Existing author/registered identity is not model-editable.
                    # Explicit administrator profile edits use rules.py instead.
                    profile.update({k: v for k, v in operation["public_profile"].items()
                                    if k not in CORE_FIELDS})
                known = list(json_load(npc["known_facts_json"], []))
                for fact in list(operation.get("known_facts") or [])[:30]:
                    if scope and scope.get('enforce_knowledge'):
                        # Gated sessions derive new received speech from receipts,
                        # not model-authored assertions about who knows what.
                        continue
                    text = clean_text(fact, max_chars=400)
                    if text and text not in known:
                        known.append(text)
                misconceptions = list(
                    json_load(npc["misconceptions_json"], [])
                )
                for fact in list(
                    operation.get("misconceptions") or []
                )[:20]:
                    if scope and scope.get('enforce_knowledge'):
                        continue
                    text = clean_text(fact, max_chars=400)
                    if text and text not in misconceptions:
                        misconceptions.append(text)
                connection.execute(
                    """
                    UPDATE session_characters SET
                        public_profile_json = ?, known_facts_json = ?,
                        misconceptions_json = ?, lifecycle_status = ?,
                        last_event_id = ?, last_turn = ?,
                        revision = revision + 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        json_dump(profile),
                        json_dump(known[-60:]),
                        json_dump(misconceptions[-40:]),
                        lifecycle_status,
                        source_event_id,
                        new_turn,
                        now,
                        npc_id,
                    ),
                )
                if isinstance(operation.get("runtime_state"), Mapping) or lifecycle_status == "dead":
                    state_row = connection.execute(
                        """
                        SELECT state_json FROM session_character_states
                        WHERE character_id = ?
                        """,
                        (npc_id,),
                    ).fetchone()
                    state = dict(
                        json_load(
                            state_row["state_json"] if state_row else "",
                            {},
                        )
                    )
                    if isinstance(operation.get("runtime_state"), Mapping):
                        state.update(dict(operation["runtime_state"]))
                    state["status"] = lifecycle_status
                    connection.execute(
                        """
                        INSERT INTO session_character_states(
                            character_id, state_json, revision, updated_at
                        ) VALUES (?, ?, 1, ?)
                        ON CONFLICT(character_id) DO UPDATE SET
                            state_json = excluded.state_json,
                            revision = revision + 1,
                            updated_at = excluded.updated_at
                        """,
                        (npc_id, json_dump(state), now),
                    )
                result["npc"].append(
                    {"id": npc_id, "op": op, "name": npc["name"]}
                )

        if scope:
            record_perceptions(connection, session_id, scope, source_event_id, new_turn, now)
        ledger_ops = workflow.get("ledger_ops")
        if isinstance(ledger_ops, Sequence) and not isinstance(
            ledger_ops,
            (str, bytes),
        ):
            for operation in ledger_ops[:16]:
                if not isinstance(operation, Mapping):
                    continue
                op = str(operation.get("op") or "update").lower()
                entry_id = clean_text(
                    operation.get("entry_id"),
                    max_chars=128,
                )
                supplied_stable_key = clean_text(
                    operation.get("stable_key"),
                    max_chars=128,
                )
                title = clean_text(operation.get("title"), max_chars=160)
                # clue 已废弃（原关键词匹配的里程碑判定输入，那条路径 2026-08-23
                # 删除后生成侧一直没跟着删）。这里同样显式丢弃，理由与
                # resolution.py 中相同：只从白名单里拿掉会落进 objective 兜底，
                # 换个名字继续写。历史行保留，续作继承仍会带它们。
                if str(operation.get("kind") or "").strip().lower() == "clue":
                    continue
                kind = str(operation.get("kind") or "objective").lower()
                if kind not in {
                    "main",
                    "side",
                    "objective",
                    "milestone",
                    "failed",
                }:
                    kind = "objective"
                row = None
                if entry_id:
                    row = connection.execute(
                        """
                        SELECT * FROM story_ledger
                        WHERE id = ? AND session_id = ?
                        """,
                        (entry_id, session_id),
                    ).fetchone()
                if not row and kind == "milestone" and supplied_stable_key:
                    row = connection.execute(
                        """
                        SELECT * FROM story_ledger
                        WHERE session_id = ? AND kind = 'milestone'
                          AND stable_key = ?
                        """,
                        (session_id, supplied_stable_key),
                    ).fetchone()
                if not row and kind != "milestone" and title:
                    row = connection.execute(
                        """
                        SELECT * FROM story_ledger
                        WHERE session_id = ? AND stable_key = ?
                        """,
                        (session_id, self._stable_key(title)),
                    ).fetchone()
                status = {
                    "complete": "completed",
                    "fail": "failed",
                    "archive": "archived",
                }.get(op, "active")
                if kind == "milestone" and op in {"create", "complete"}:
                    status = "completed"
                if (
                    not row
                    and op in {"create", "complete"}
                    and title
                    and (kind != "milestone" or supplied_stable_key)
                ):
                    entry_id = new_id("ledger")
                    connection.execute(
                        """
                        INSERT INTO story_ledger(
                            id, session_id, stable_key, kind, title,
                            description, status, visibility,
                            source_event_id, completed_event_id,
                            revision, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                  1, ?, ?)
                        """,
                        (
                            entry_id,
                            session_id,
                            (
                                supplied_stable_key
                                if kind == "milestone"
                                else self._stable_key(title)
                            ),
                            kind,
                            title,
                            clean_text(
                                operation.get("description"),
                                max_chars=800,
                            ),
                            status,
                            (
                                "host"
                                if str(
                                    operation.get("visibility") or ""
                                ).lower()
                                == "host"
                                else "public"
                            ),
                            source_event_id,
                            (
                                source_event_id
                                if status in {"completed", "failed"}
                                else ""
                            ),
                            now,
                            now,
                        ),
                    )
                elif row:
                    entry_id = str(row["id"])
                    connection.execute(
                        """
                        UPDATE story_ledger SET
                            kind = ?, title = ?, description = ?,
                            status = ?, visibility = ?,
                            completed_event_id = ?,
                            revision = revision + 1, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            kind,
                            title or row["title"],
                            clean_text(
                                operation.get("description")
                                or row["description"],
                                max_chars=800,
                            ),
                            status,
                            (
                                "host"
                                if str(
                                    operation.get("visibility")
                                    or row["visibility"]
                                ).lower()
                                == "host"
                                else "public"
                            ),
                            (
                                source_event_id
                                if status in {"completed", "failed"}
                                else row["completed_event_id"]
                            ),
                            now,
                            entry_id,
                        ),
                    )
                else:
                    continue
                result["ledger"].append(
                    {"id": entry_id, "op": op, "status": status}
                )

        clock_ops = workflow.get("clock_ops")
        if isinstance(clock_ops, Sequence) and not isinstance(
            clock_ops,
            (str, bytes),
        ):
            for operation in clock_ops[:12]:
                if not isinstance(operation, Mapping):
                    continue
                op = str(operation.get("op") or "advance").lower()
                clock_id = clean_text(
                    operation.get("clock_id"),
                    max_chars=128,
                )
                title = clean_text(operation.get("title"), max_chars=100)
                row = None
                if clock_id:
                    row = connection.execute(
                        """
                        SELECT * FROM scene_clocks
                        WHERE id = ? AND session_id = ?
                        """,
                        (clock_id, session_id),
                    ).fetchone()
                if not row and title:
                    row = connection.execute(
                        """
                        SELECT * FROM scene_clocks
                        WHERE session_id = ? AND stable_key = ?
                        """,
                        (session_id, self._stable_key(title)),
                    ).fetchone()
                if not row and op == "create" and title:
                    segments = bounded_int(
                        operation.get("segments"),
                        4,
                        4,
                        8,
                    )
                    if segments not in {4, 6, 8}:
                        segments = 4
                    clock_id = new_id("clock")
                    connection.execute(
                        """
                        INSERT INTO scene_clocks(
                            id, session_id, stable_key, title, segments,
                            current_value, visibility, trigger_text, status,
                            triggered_event_id, revision, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, 0, ?, ?, 'active', '',
                                  1, ?, ?)
                        """,
                        (
                            clock_id,
                            session_id,
                            self._stable_key(title),
                            title,
                            segments,
                            str(operation.get("visibility") or "public"),
                            clean_text(
                                operation.get("trigger"),
                                max_chars=500,
                            ),
                            now,
                            now,
                        ),
                    )
                    current_value = 0
                    status = "active"
                elif row:
                    clock_id = str(row["id"])
                    segments = int(row["segments"])
                    current_value = int(row["current_value"])
                    if op == "advance":
                        current_value += bounded_int(
                            operation.get("delta"),
                            1,
                            -8,
                            8,
                        )
                    elif op == "set":
                        current_value = bounded_int(
                            operation.get("value"),
                            current_value,
                            0,
                            segments,
                        )
                    elif op == "complete":
                        current_value = segments
                    current_value = max(0, min(segments, current_value))
                    status = (
                        "archived"
                        if op == "archive"
                        else "completed"
                        if current_value >= segments
                        else "active"
                    )
                    triggered_event_id = str(row["triggered_event_id"] or "")
                    trigger_text = clean_text(
                        operation.get("trigger") or row["trigger_text"],
                        max_chars=500,
                    )
                    if (
                        status == "completed"
                        and not triggered_event_id
                    ):
                        triggered_event_id = new_id("event")
                        connection.execute(
                            """
                            INSERT INTO events(
                                id, session_id, turn_no, role, actor_id,
                                actor_name, content, meta_json, created_at
                            ) VALUES (?, ?, ?, 'system', 'clock',
                                      '场景时钟', ?, ?, ?)
                            """,
                            (
                                triggered_event_id,
                                session_id,
                                new_turn,
                                trigger_text
                                or f"场景时钟「{row['title']}」已填满。",
                                json_dump(
                                    {
                                        "kind": "scene_clock_trigger",
                                        "clock_id": clock_id,
                                    }
                                ),
                                now,
                            ),
                        )
                    connection.execute(
                        """
                        UPDATE scene_clocks SET
                            current_value = ?, status = ?,
                            triggered_event_id = ?, trigger_text = ?,
                            revision = revision + 1, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            current_value,
                            status,
                            triggered_event_id,
                            trigger_text,
                            now,
                            clock_id,
                        ),
                    )
                else:
                    continue
                result["clocks"].append(
                    {
                        "id": clock_id,
                        "op": op,
                        "current_value": current_value,
                        "segments": segments,
                        "status": status,
                    }
                )

        assist_ops = workflow.get("assist_ops")
        selected_text = str(
            (workflow.get("selected_choice") or {}).get("text")
            if isinstance(workflow.get("selected_choice"), Mapping)
            else ""
        )
        if (
            isinstance(assist_ops, Sequence)
            and not isinstance(assist_ops, (str, bytes))
            and any(word in selected_text for word in ("协助", "帮助", "支援"))
        ):
            for operation in assist_ops[:1]:
                if not isinstance(operation, Mapping):
                    continue
                target_ref = clean_text(
                    operation.get("target_id"),
                    max_chars=128,
                )
                method = clean_text(
                    operation.get("method"),
                    max_chars=300,
                )
                target = connection.execute(
                    """
                    SELECT * FROM participants
                    WHERE session_id = ? AND (
                        id = ? OR group_user_id = ? OR
                        lower(character_name) = lower(?) OR
                        lower(character_code) = lower(?)
                    ) LIMIT 1
                    """,
                    (
                        session_id,
                        target_ref,
                        target_ref,
                        target_ref,
                        target_ref,
                    ),
                ).fetchone()
                if not target or not method or target["id"] == participant["id"]:
                    continue
                connection.execute(
                    """
                    UPDATE assist_tokens SET status = 'expired'
                    WHERE session_id = ? AND target_participant_id = ?
                      AND status = 'active'
                    """,
                    (session_id, target["id"]),
                )
                token_id = new_id("assist")
                expires_round = bounded_int(
                    operation.get("expires_round"),
                    acting_round + 1,
                    acting_round,
                    acting_round + 1,
                )
                connection.execute(
                    """
                    INSERT INTO assist_tokens(
                        id, session_id, source_participant_id,
                        target_participant_id, stat, method, status,
                        expires_round, source_event_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
                    """,
                    (
                        token_id,
                        session_id,
                        participant["id"],
                        target["id"],
                        clean_text(operation.get("stat"), max_chars=40),
                        method,
                        expires_round,
                        source_event_id,
                        now,
                    ),
                )
                result["assists"].append(
                    {"id": token_id, "target_id": target["id"]}
                )

        connection.execute(
            """
            UPDATE assist_tokens SET status = 'expired'
            WHERE session_id = ? AND status = 'active'
              AND expires_round > 0 AND expires_round < ?
            """,
            (session_id, acting_round),
        )

        milestone = connection.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END)
                    AS completed
            FROM story_ledger
            WHERE session_id = ? AND kind = 'milestone'
              AND status <> 'archived'
            """,
            (session_id,),
        ).fetchone()
        objective = connection.execute(
            """
            SELECT title FROM story_ledger
            WHERE session_id = ? AND status = 'active'
              AND kind IN ('main', 'objective')
            ORDER BY CASE kind WHEN 'main' THEN 0 ELSE 1 END,
                     updated_at DESC
            LIMIT 1
            """,
            (session_id,),
        ).fetchone()
        rule_row = connection.execute(
            """
            SELECT progress_json FROM session_rule_states
            WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
        if rule_row:
            progress = normalize_progress(
                json_load(rule_row["progress_json"], {})
            )
            if int(milestone["total"] or 0) > 0:
                # total_milestones 以世界配置为准（规则里声明的总数），
                # 不能用 ledger 当前条数覆盖——ledger 只含已完成/已创建的
                # 里程碑，条数永远小于世界声明的总数，会把进度显示成
                # 「已完成 n / 总数 n」的错误形态。世界未声明总数时才用
                # ledger 条数兜底。
                if not progress.get("total_milestones"):
                    progress["total_milestones"] = int(milestone["total"])
                progress["completed_milestones"] = int(
                    milestone["completed"] or 0
                )
            if objective:
                progress["current_objective"] = str(objective["title"])
            ending_completion = workflow.get("ending_completion")
            if (
                isinstance(ending_completion, Mapping)
                and ending_completion.get("complete") is True
            ):
                progress["ending_pending"] = False
                progress["ending_narrated"] = True
                progress["ending_narrated_at_turn"] = new_turn
                progress["ending_event_id"] = source_event_id
                result["ending_completion"] = {
                    "chapter_id": clean_text(
                        ending_completion.get("chapter_id"), max_chars=128
                    ),
                    "event_id": source_event_id,
                }
            connection.execute(
                """
                UPDATE session_rule_states SET progress_json = ?,
                    revision = revision + 1, updated_at = ?
                WHERE session_id = ?
                """,
                (json_dump(progress), now, session_id),
            )
            result["progress"] = progress
        return result

    async def sync_chapter_npc_states(
        self,
        session_id: str,
        world_id: str,
        entries: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        """切章时把章节声明的 key_npcs[].state 合并进副本 NPC 的运行时状态。

        这是「NPC 地点/状态跟随剧情」的引擎层强制同步：章节声明了某关键
        NPC 在本章的立场/位置/状态，切到该章时必须写进
        session_character_states.state_json，否则模型不主动输出 npc_ops 时
        NPC 信息会一直停留在开场播种值。合并而非整段替换，避免覆盖模型
        在运行时补写的其他字段（伤势、别名提案等）。幂等：无 state 的
        key_npcs 条目、找不到的角色直接跳过。
        """
        if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
            return []
        try:
            return await self._run(
                self._sync_chapter_npc_states,
                session_id,
                world_id,
                list(entries),
            )
        except Exception as exc:
            logger.warning("章节 NPC 状态同步失败：%s", exc)
            return []

    def _sync_chapter_npc_states(
        self,
        session_id: str,
        world_id: str,
        entries: list[Any],
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        with self._connect() as connection:
            slug_to_id = {
                str(r["slug"]): str(r["id"])
                for r in connection.execute(
                    "SELECT id, slug FROM characters WHERE world_id = ?",
                    (world_id,),
                ).fetchall()
            }
            if not slug_to_id:
                return result
            session_rows = connection.execute(
                """
                SELECT id, stable_key, name, lifecycle_status FROM session_characters
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchall()
            char_by_key = {
                str(r["stable_key"]): r for r in session_rows
            }
            now = utc_now()
            for entry in entries:
                if not isinstance(entry, Mapping):
                    continue
                ref = clean_text(entry.get("ref"), max_chars=128)
                state = entry.get("state")
                if not ref or not isinstance(state, Mapping) or not state:
                    continue
                char_id = slug_to_id.get(ref)
                if not char_id:
                    continue
                sess_char = char_by_key.get(f"world:{char_id}")
                if not sess_char:
                    continue
                if sess_char["lifecycle_status"] == "dead":
                    # Chapter defaults describe a living actor's expected scene;
                    # do not move a corpse or overwrite its recorded death.
                    continue
                state_row = connection.execute(
                    """
                    SELECT state_json FROM session_character_states
                    WHERE character_id = ?
                    """,
                    (sess_char["id"],),
                ).fetchone()
                merged = dict(
                    json_load(state_row["state_json"] if state_row else "", {})
                )
                merged.update(
                    {
                        key: value
                        for key, value in state.items()
                        if value not in (None, "")
                    }
                )
                connection.execute(
                    """
                    INSERT INTO session_character_states(
                        character_id, state_json, revision, updated_at
                    ) VALUES (?, ?, 1, ?)
                    ON CONFLICT(character_id) DO UPDATE SET
                        state_json = excluded.state_json,
                        revision = revision + 1,
                        updated_at = excluded.updated_at
                    """,
                    (sess_char["id"], json_dump(merged), now),
                )
                result.append(
                    {
                        "character_id": str(sess_char["id"]),
                        "name": str(sess_char["name"]),
                        "op": "chapter_state_sync",
                    }
                )
        return result

    @staticmethod
    def _normalize_vote_options(value: Any) -> list[dict[str, str]]:
        if not isinstance(value, Sequence) or isinstance(
            value,
            (str, bytes),
        ):
            return []
        result: list[dict[str, str]] = []
        seen: set[str] = set()
        for index, item in enumerate(value[:4]):
            if not isinstance(item, Mapping):
                continue
            key = str(item.get("key") or CHOICE_KEYS[index]).upper()
            text = clean_text(item.get("text"), max_chars=240)
            if key not in CHOICE_KEYS or key in seen or not text:
                continue
            seen.add(key)
            entry: dict[str, Any] = {"key": key, "text": text}
            # 0.11.4：透传「同意执行」选项上声明的检定定义，
            # 供表决通过后按该检定执行（如全队行动的 魔力 DC17）。
            if isinstance(item.get("check"), Mapping):
                entry["check"] = dict(item["check"])
            result.append(entry)
        return result

    def _select_world_event(
        self,
        connection: sqlite3.Connection,
        *,
        session_id: str,
        round_no: int,
        world: Mapping[str, Any],
        turn_no: int,
        now: str,
    ) -> dict[str, Any] | None:
        if connection.execute(
            """
            SELECT 1 FROM selected_world_events
            WHERE session_id = ? AND round_no = ?
            """,
            (session_id, round_no),
        ).fetchone():
            return None
        rules = world.get("rules")
        rules = rules if isinstance(rules, Mapping) else {}
        pool = rules.get("event_pool")
        if not isinstance(pool, Sequence) or isinstance(pool, (str, bytes)):
            return None
        session_row = connection.execute(
            """
            SELECT world_state_json FROM sessions WHERE id = ?
            """,
            (session_id,),
        ).fetchone()
        state = json_load(
            session_row["world_state_json"] if session_row else "",
            {},
        )
        location = str(state.get("location") or "").casefold()
        facts = {
            str(item).casefold()
            for item in (
                state.get("facts")
                if isinstance(state.get("facts"), list)
                else []
            )
        }
        active_count = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM participants
                WHERE session_id = ? AND participation_status = 'active'
                  AND card_status = 'approved'
                """,
                (session_id,),
            ).fetchone()[0]
        )
        candidates: list[tuple[dict[str, Any], int]] = []
        for raw in pool[:200]:
            if not isinstance(raw, Mapping):
                continue
            item_id = clean_text(raw.get("id"), max_chars=80)
            description = clean_text(
                raw.get("description"),
                max_chars=1000,
            )
            if not item_id or not description:
                continue
            minimum_round = bounded_int(
                raw.get("minimum_round"),
                1,
                1,
                1_000_000,
            )
            if round_no < minimum_round:
                continue
            conditions = raw.get("conditions")
            conditions = (
                conditions if isinstance(conditions, Mapping) else {}
            )
            allowed_locations = conditions.get("locations")
            if isinstance(allowed_locations, Sequence) and not isinstance(
                allowed_locations,
                (str, bytes),
            ):
                normalized_locations = {
                    str(item).casefold()
                    for item in allowed_locations
                    if str(item).strip()
                }
                if normalized_locations and location not in normalized_locations:
                    continue
            required_facts = conditions.get("required_facts")
            if isinstance(required_facts, Sequence) and not isinstance(
                required_facts,
                (str, bytes),
            ):
                required = {
                    str(item).casefold()
                    for item in required_facts
                    if str(item).strip()
                }
                if not required.issubset(facts):
                    continue
            excluded_facts = conditions.get("excluded_facts")
            if isinstance(excluded_facts, Sequence) and not isinstance(
                excluded_facts,
                (str, bytes),
            ):
                excluded = {
                    str(item).casefold()
                    for item in excluded_facts
                    if str(item).strip()
                }
                if excluded.intersection(facts):
                    continue
            minimum_players = bounded_int(
                conditions.get("minimum_players"),
                0,
                0,
                32,
            )
            if active_count < minimum_players:
                continue
            maximum_players = conditions.get("maximum_players")
            if (
                maximum_players not in {None, ""}
                and active_count
                > bounded_int(maximum_players, 32, 0, 32)
            ):
                continue
            previous = connection.execute(
                """
                SELECT round_no FROM selected_world_events
                WHERE session_id = ? AND pool_item_id = ?
                ORDER BY round_no DESC LIMIT 1
                """,
                (session_id, item_id),
            ).fetchone()
            if previous and bool(raw.get("once", False)):
                continue
            cooldown = bounded_int(
                raw.get("cooldown_rounds"),
                0,
                0,
                1_000_000,
            )
            if previous and round_no - int(previous["round_no"]) <= cooldown:
                continue
            weight = bounded_int(raw.get("weight"), 1, 1, 1000)
            candidates.append((dict(raw), weight))
        if not candidates:
            return None
        total = sum(weight for _, weight in candidates)
        pick = secrets.randbelow(total)
        selected = candidates[-1][0]
        for item, weight in candidates:
            if pick < weight:
                selected = item
                break
            pick -= weight
        event_id = new_id("worldevent")
        item_id = clean_text(selected.get("id"), max_chars=80)
        description = clean_text(
            selected.get("description"),
            max_chars=1000,
        )
        payload = {
            "id": item_id,
            "title": clean_text(selected.get("title"), max_chars=120),
            "description": description,
            "severity": clean_text(
                selected.get("severity") or "standard",
                max_chars=30,
            ),
        }
        connection.execute(
            """
            INSERT INTO selected_world_events(
                id, session_id, round_no, pool_item_id, payload_json,
                status, narrative, created_at, resolved_at
            ) VALUES (?, ?, ?, ?, ?, 'narrated', ?, ?, ?)
            """,
            (
                event_id,
                session_id,
                round_no,
                item_id,
                json_dump(payload),
                description,
                now,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO events(
                id, session_id, turn_no, role, actor_id, actor_name,
                content, meta_json, created_at
            ) VALUES (?, ?, ?, 'system', 'world', '世界脉冲', ?, ?, ?)
            """,
            (
                new_id("event"),
                session_id,
                turn_no,
                description,
                json_dump(
                    {
                        "kind": "world_pulse",
                        "selected_world_event_id": event_id,
                        "round_no": round_no,
                    }
                ),
                now,
            ),
        )
        return {"id": event_id, **payload}

    # ── 删除世界包（连带它展开的所有副本）──────────────────────────────
    #
    # 为什么不能直接 DELETE FROM worlds：
    #   sessions.world_id       → ON DELETE NO ACTION   （有本就删不掉）
    #   world_snapshots.world_id→ ON DELETE RESTRICT    （有快照就删不掉）
    #   character_cards.world_id→ ON DELETE NO ACTION   （有角色卡就删不掉）
    # 外键是开着的（``PRAGMA foreign_keys = ON``），所以天真的删除只会报错。
    # 必须**先删本**——本再级联掉它自己那三十来张子表。
    #
    # 而且删本要沿用既有的那条规矩：只删**已关闭 / 已完结**的本。
    # 跑着的本连着真人的一局游戏，删世界包不该把它一起带走。

    #: 允许被连带删除的副本状态。与 ``_delete_session`` 保持同一套判据。
    _DELETABLE_SESSION_STATES = frozenset({SESSION_CLOSED, SESSION_FINISHED})

    async def world_deletion_impact(self, world_id: str) -> dict[str, Any]:
        """预览删掉这个世界的后果。**只读**，面板拿它做确认弹窗。"""
        return await self._run(self._world_deletion_impact, world_id)

    def _world_deletion_impact(self, world_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM worlds WHERE id = ?", (world_id,)
            ).fetchone()
            if not row:
                raise DatabaseNotFoundError("世界包不存在")

            sessions = [
                {
                    "id": str(item["id"]),
                    "instance_name": str(item["instance_name"] or ""),
                    "state": str(item["state"] or ""),
                    "deletable": str(item["state"] or "")
                    in self._DELETABLE_SESSION_STATES,
                }
                for item in connection.execute(
                    """
                    SELECT id, instance_name, state FROM sessions
                    WHERE world_id = ? ORDER BY updated_at DESC
                    """,
                    (world_id,),
                )
            ]
            blocked = [s for s in sessions if not s["deletable"]]

            counts = {
                table: connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE world_id = ?", (world_id,)
                ).fetchone()[0]
                for table in ("world_snapshots", "character_cards", "migration_receipts")
            }
            # 这些是 CASCADE，删世界时自动跟着走，列出来只是为了让操作者
            # 知道会连带掉什么（面板要如实展示代价）。
            cascaded = {
                table: connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE world_id = ?", (world_id,)
                ).fetchone()[0]
                for table in (
                    "characters",
                    "world_entity_registry",
                    "world_feature_versions",
                    "world_rule_revisions",
                )
            }

            return {
                "world": {
                    "id": str(row["id"]),
                    "slug": str(row["slug"] or ""),
                    "name": str(row["name"] or ""),
                    "archived": bool(row["archived"]),
                },
                "sessions": sessions,
                "session_count": len(sessions),
                "blocked_sessions": blocked,
                "can_delete": not blocked,
                "counts": counts,
                "cascaded": cascaded,
                "confirm_name": str(row["name"] or ""),
            }

    async def delete_world(
        self,
        world_id: str,
        actor_id: str,
        confirm_name: str,
    ) -> dict[str, Any]:
        """删除世界包，**连带它展开的所有副本**。

        调用前请先走 :meth:`world_deletion_impact` 让操作者看清代价。
        有跑着的本时直接拒绝——``InvalidTransitionError``。
        """
        impact = await self.world_deletion_impact(world_id)
        if impact["blocked_sessions"]:
            names = "、".join(
                s["instance_name"] or s["id"] for s in impact["blocked_sessions"][:5]
            )
            raise InvalidTransitionError(
                f"仍有 {len(impact['blocked_sessions'])} 个未结束的副本"
                f"（{names}）在使用这个世界包，请先关闭或完结它们"
            )
        if str(confirm_name or "").strip() != impact["confirm_name"]:
            raise ValueError("确认名称与世界包名称不一致")

        # ① 先删本。每个本各自一个事务，走既有的 _delete_session——
        #    它负责级联子表、把故事文件挪进回收站、并把"当前选中"交给同群的其它本。
        deleted_sessions: list[dict[str, Any]] = []
        for session in impact["sessions"]:
            detail = await self._run(
                self._delete_session, session["id"], actor_id, session["instance_name"]
            )
            try:
                trashed = await asyncio.to_thread(
                    self.storage.trash_relative_path,
                    str(detail.get("relative_path") or ""),
                    label=str(detail.get("instance_slug") or "story"),
                )
                detail["trash_path"] = str(trashed or "")
            except Exception as exc:  # 文件挪不动不影响数据删除，如实记下
                detail["trash_error"] = str(exc)[:500]
            deleted_sessions.append(detail)

        # ② 再删世界自己的东西。顺序由外键决定：
        #    RESTRICT 的 world_snapshots 与 NO ACTION 的 character_cards
        #    必须在世界行之前清掉。
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM worlds WHERE id = ?", (world_id,)
                ).fetchone()
                if not row:
                    raise DatabaseNotFoundError("世界包不存在")
                removed = {
                    table: connection.execute(
                        f"DELETE FROM {table} WHERE world_id = ?", (world_id,)
                    ).rowcount
                    for table in (
                        "world_snapshots",
                        "character_cards",
                        "migration_receipts",
                    )
                }
                connection.execute("DELETE FROM worlds WHERE id = ?", (world_id,))
                self._insert_audit(
                    connection,
                    "",
                    actor_id,
                    "world.delete",
                    world_id,
                    {
                        "slug": str(row["slug"] or ""),
                        "name": str(row["name"] or ""),
                        "sessions_deleted": [s["session_id"] for s in deleted_sessions],
                        "removed": removed,
                    },
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

        return {
            "deleted": True,
            "world_id": world_id,
            "slug": impact["world"]["slug"],
            "name": impact["world"]["name"],
            "sessions": deleted_sessions,
            "session_count": len(deleted_sessions),
            "removed": removed,
        }

    async def archive_world(
        self,
        world_id: str,
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(self._archive_world, world_id, actor_id)

    def _archive_world(
        self,
        world_id: str,
        actor_id: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM worlds WHERE id = ?",
                    (world_id,),
                ).fetchone()
                if not row:
                    raise DatabaseNotFoundError("世界包不存在")
                active_sessions = connection.execute(
                    """
                    SELECT COUNT(*) FROM sessions
                    WHERE world_id = ? AND state != 'closed'
                    """,
                    (world_id,),
                ).fetchone()[0]
                if active_sessions:
                    raise ValueError("仍有运行中的会话使用该世界，不能归档")
                connection.execute(
                    """
                    UPDATE worlds
                    SET archived = 1, revision = revision + 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (utc_now(), world_id),
                )
                self._insert_audit(
                    connection,
                    "",
                    actor_id,
                    "world.archive",
                    world_id,
                    {"slug": row["slug"]},
                )
                updated = connection.execute(
                    "SELECT * FROM worlds WHERE id = ?",
                    (world_id,),
                ).fetchone()
                connection.execute("COMMIT")
                return self._world(updated)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def restore_world(
        self,
        world_id: str,
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(self._restore_world, world_id, actor_id)

    def _restore_world(
        self,
        world_id: str,
        actor_id: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM worlds WHERE id = ?",
                    (world_id,),
                ).fetchone()
                if not row:
                    raise DatabaseNotFoundError("世界包不存在")
                connection.execute(
                    """
                    UPDATE worlds
                    SET archived = 0, revision = revision + 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (utc_now(), world_id),
                )
                self._insert_audit(
                    connection,
                    "",
                    actor_id,
                    "world.restore",
                    world_id,
                    {"slug": row["slug"]},
                )
                updated = connection.execute(
                    "SELECT * FROM worlds WHERE id = ?",
                    (world_id,),
                ).fetchone()
                connection.execute("COMMIT")
                return self._world(updated)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def list_characters(
        self,
        world_id: str = "",
    ) -> list[dict[str, Any]]:
        return await self._run(self._list_characters, world_id)

    def _list_characters(self, world_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            if world_id:
                rows = connection.execute(
                    """
                    SELECT * FROM characters WHERE world_id = ?
                    ORDER BY sort_order ASC, name COLLATE NOCASE
                    """,
                    (world_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM characters
                    ORDER BY world_id, sort_order ASC, name COLLATE NOCASE
                    """
                ).fetchall()
            return [self._character(row) for row in rows]

    async def save_character(
        self,
        payload: Mapping[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._save_character,
            dict(payload),
            actor_id,
        )

    def _save_character(
        self,
        payload: dict[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        character_id = str(payload.get("id") or "").strip()
        world_id = validate_platform_id(
            payload.get("world_id"),
            label="世界 ID",
        )
        slug = validate_slug(payload.get("slug"))
        name = clean_text(payload.get("name"), max_chars=100)
        if not name:
            raise ValueError("角色名称不能为空")
        role = clean_text(payload.get("role") or "npc", max_chars=40)
        prompt = clean_text(payload.get("prompt"), max_chars=20000)
        profile = payload.get("profile")
        if not isinstance(profile, Mapping):
            raise ValueError("角色资料必须是 JSON 对象")
        try:
            sort_order = max(-10000, min(10000, int(payload.get("sort_order", 0))))
        except (TypeError, ValueError):
            sort_order = 0
        enabled = int(bool(payload.get("enabled", True)))
        now = utc_now()

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if not connection.execute(
                    "SELECT 1 FROM worlds WHERE id = ?",
                    (world_id,),
                ).fetchone():
                    raise DatabaseNotFoundError("世界包不存在")
                if character_id:
                    current = connection.execute(
                        "SELECT * FROM characters WHERE id = ?",
                        (character_id,),
                    ).fetchone()
                    if not current:
                        raise DatabaseNotFoundError("角色不存在")
                    expected_revision = payload.get("revision")
                    if (
                        expected_revision is not None
                        and int(expected_revision) != current["revision"]
                    ):
                        raise DatabaseConflictError(
                            "角色已被其他操作更新，请刷新后重试"
                        )
                    connection.execute(
                        """
                        UPDATE characters SET
                            world_id = ?, slug = ?, name = ?, role = ?,
                            profile_json = ?, prompt = ?, enabled = ?,
                            sort_order = ?, revision = revision + 1,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            world_id,
                            slug,
                            name,
                            role,
                            json_dump(dict(profile)),
                            prompt,
                            enabled,
                            sort_order,
                            now,
                            character_id,
                        ),
                    )
                    action = "character.update"
                else:
                    character_id = new_id("char")
                    connection.execute(
                        """
                        INSERT INTO characters(
                            id, world_id, slug, name, role, profile_json,
                            prompt, enabled, sort_order, revision,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                        """,
                        (
                            character_id,
                            world_id,
                            slug,
                            name,
                            role,
                            json_dump(dict(profile)),
                            prompt,
                            enabled,
                            sort_order,
                            now,
                            now,
                        ),
                    )
                    action = "character.create"
                self._insert_audit(
                    connection,
                    "",
                    actor_id,
                    action,
                    character_id,
                    {"world_id": world_id, "slug": slug, "name": name},
                )
                row = connection.execute(
                    "SELECT * FROM characters WHERE id = ?",
                    (character_id,),
                ).fetchone()
                connection.execute("COMMIT")
                return self._character(row)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def delete_character(
        self,
        character_id: str,
        actor_id: str,
    ) -> None:
        await self._run(self._delete_character, character_id, actor_id)

    def _delete_character(
        self,
        character_id: str,
        actor_id: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM characters WHERE id = ?",
                    (character_id,),
                ).fetchone()
                if not row:
                    raise DatabaseNotFoundError("角色不存在")
                connection.execute(
                    "DELETE FROM characters WHERE id = ?",
                    (character_id,),
                )
                self._insert_audit(
                    connection,
                    "",
                    actor_id,
                    "character.delete",
                    character_id,
                    {"name": row["name"], "world_id": row["world_id"]},
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
