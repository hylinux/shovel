"""Ordered, additive runtime upgrades for existing local databases."""

from __future__ import annotations

from sqlalchemy import Connection, insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from shovel.capability.domain import CapabilityScope
from shovel.db.base import Base, now_ts
from shovel.db.model.automation import PROACTIVE_DDL
from shovel.db.model.capability import CAPABILITY_DDL
from shovel.db.model.control import WAKEUP_DDL
from shovel.db.model.effect import EFFECT_DDL
from shovel.db.model.enterprise import CONNECTION_DDL
from shovel.db.model.enterprise_execution import ENTERPRISE_EXECUTION_DDL
from shovel.db.model.execution import EXECUTION_DDL, KERNEL_INDEX_DDL, STEP_DDL
from shovel.db.model.governance import GOVERNANCE_DDL
from shovel.db.model.identity import AUTHENTICATION_DDL, IDENTITY_DDL
from shovel.db.model.interaction import INPUT_DDL, INTERACTION_DDL, NOTIFICATION_DDL
from shovel.db.model.organization import ORGANIZATION_DDL
from shovel.db.model.protection import PROTECTION_DDL
from shovel.db.model.retrieval import SchemaMigration
from shovel.db.model.security import APPROVAL_DDL
from shovel.execution.identity import execution_request_key
from shovel.runtime.errors import RuntimeFailure

_SEARCH_DDL = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS chunk_search "
    "USING fts5(chunk_id UNINDEXED, text, tokenize='trigram')",
    """CREATE TRIGGER IF NOT EXISTS chunk_search_insert AFTER INSERT ON chunk
       WHEN new.granularity = 'chunk' BEGIN
         INSERT INTO chunk_search(chunk_id, text) VALUES (new.id, new.text);
       END""",
    """CREATE TRIGGER IF NOT EXISTS chunk_search_delete AFTER DELETE ON chunk BEGIN
         DELETE FROM chunk_search WHERE chunk_id = old.id;
       END""",
    """CREATE TRIGGER IF NOT EXISTS chunk_search_update AFTER UPDATE OF text, granularity ON chunk BEGIN
         DELETE FROM chunk_search WHERE chunk_id = old.id;
         INSERT INTO chunk_search(chunk_id, text)
           SELECT new.id, new.text WHERE new.granularity = 'chunk';
       END""",
)


def _upgrade(connection: Connection) -> None:
    from shovel.db import model  # noqa: F401

    Base.metadata.create_all(connection)
    versions = set(connection.scalars(select(SchemaMigration.version)))
    if 1 not in versions:
        for statement in _SEARCH_DDL:
            connection.exec_driver_sql(statement)
        connection.exec_driver_sql(
            "INSERT INTO chunk_search(chunk_id, text) SELECT id, text FROM chunk WHERE granularity = 'chunk'"
        )
        connection.execute(
            insert(SchemaMigration).values(
                version=1, name="hosted_operations_chat_and_keyword_index", applied_at=now_ts()
            )
        )
    if 2 not in versions:
        columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(operation)")}
        for name in ("request_key", "request_fingerprint"):
            if name not in columns:
                connection.exec_driver_sql(f"ALTER TABLE operation ADD COLUMN {name} TEXT")
        connection.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_operation_request_key ON operation(request_key)"
        )
        connection.execute(
            insert(SchemaMigration).values(
                version=2, name="idempotent_operation_requests", applied_at=now_ts()
            )
        )
    if 3 not in versions:
        connection.exec_driver_sql(
            """INSERT OR IGNORE INTO profile_document(profile_id, doc_uri, resource_id, last_seen_run)
            SELECT p.id, d.uri, d.resource_id, d.last_seen_run FROM document d
            JOIN scan_profile p ON p.resource_id = d.resource_id AND p.name = 'default'
            WHERE d.lifecycle_state = 'active'"""
        )
        connection.execute(
            insert(SchemaMigration).values(
                version=3, name="profile_scoped_document_membership", applied_at=now_ts()
            )
        )

    if 4 not in versions:
        columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(conversation)")}
        if "has_short_memory" not in columns:
            connection.exec_driver_sql(
                "ALTER TABLE conversation ADD COLUMN has_short_memory BOOLEAN NOT NULL DEFAULT 0"
            )
        connection.execute(
            insert(SchemaMigration).values(
                version=4, name="conversation_scoped_mem0_cleanup", applied_at=now_ts()
            )
        )

    if 5 not in versions:
        columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(chat_message)")}
        if "memory_operation_id" not in columns:
            connection.exec_driver_sql("ALTER TABLE chat_message ADD COLUMN memory_operation_id TEXT")
        connection.exec_driver_sql("INSERT OR IGNORE INTO memory_epoch(id, generation) VALUES (1, 0)")
        connection.execute(
            insert(SchemaMigration).values(version=5, name="durable_memory_extraction", applied_at=now_ts())
        )

    if 6 not in versions:
        epoch_columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(memory_epoch)")}
        for name in ("workspace_id", "user_id"):
            if name not in epoch_columns:
                connection.exec_driver_sql(f"ALTER TABLE memory_epoch ADD COLUMN {name} TEXT")
        connection.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_epoch_owner ON memory_epoch(workspace_id,user_id)"
        )
        # Development-era placeholder rows had no ownership; never expose them as native facts.
        for table in ("memory_belief", "memory_episode", "memory_procedure"):
            columns = {row[1] for row in connection.exec_driver_sql(f"PRAGMA table_info({table})")}
            additions = {
                "workspace_id": "TEXT NOT NULL DEFAULT ''",
                "user_id": "TEXT NOT NULL DEFAULT ''",
                "scope_kind": "TEXT NOT NULL DEFAULT 'agent'",
                "fingerprint": "TEXT NOT NULL DEFAULT ''",
                "provenance": "TEXT NOT NULL DEFAULT '[]'",
                "agent_id": "TEXT",
                "session_id": "TEXT",
                "model_version": "TEXT",
                "rev": "INTEGER NOT NULL DEFAULT 1",
                "vector_state": "TEXT NOT NULL DEFAULT 'pending'",
            }
            for name, definition in additions.items():
                if name not in columns:
                    connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
        connection.exec_driver_sql("DROP INDEX IF EXISTS uq_belief_active")
        connection.exec_driver_sql(
            "CREATE UNIQUE INDEX uq_belief_active ON memory_belief "
            "(workspace_id,user_id,scope_kind,agent_id,session_id,subject,predicate,object,object_domain) "
            "WHERE validity_state = 'active'"
        )
        connection.execute(
            insert(SchemaMigration).values(version=6, name="native_memory", applied_at=now_ts())
        )
    if 7 not in versions:
        for statements in CAPABILITY_DDL.values():
            for statement in statements:
                connection.exec_driver_sql(statement)
        connection.execute(
            insert(SchemaMigration).values(
                version=7, name="immutable_capability_versions", applied_at=now_ts()
            )
        )
    if 8 not in versions:
        columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(operation)")}
        additions = {
            "capability_version_id": "TEXT REFERENCES capability_version(id) ON DELETE RESTRICT",
            "root_execution_id": "TEXT REFERENCES operation(id) ON DELETE RESTRICT",
            "parent_execution_id": "TEXT REFERENCES operation(id) ON DELETE RESTRICT",
            "correlation_id": "TEXT",
            "child_key": "TEXT",
            "execution_data": "TEXT",
            "execution_usage": "TEXT NOT NULL DEFAULT '{}'",
            "row_version": "INTEGER NOT NULL DEFAULT 0",
            "claim_token": "TEXT",
            "execution_deadline": "REAL",
        }
        for name, definition in additions.items():
            if name not in columns:
                connection.exec_driver_sql(f"ALTER TABLE operation ADD COLUMN {name} {definition}")
        connection.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_execution_child ON operation(parent_execution_id,child_key)"
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_execution_root ON operation(root_execution_id)"
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_execution_correlation ON operation(correlation_id)"
        )
        for statement in EXECUTION_DDL:
            connection.exec_driver_sql(statement)
        connection.execute(
            insert(SchemaMigration).values(version=8, name="versioned_execution_kernel", applied_at=now_ts())
        )
    if 9 not in versions:
        for statement in APPROVAL_DDL:
            connection.exec_driver_sql(statement)
        connection.execute(
            insert(SchemaMigration).values(version=9, name="execution_approval_core", applied_at=now_ts())
        )
    if 10 not in versions:
        invalid = connection.exec_driver_sql(
            """SELECT id FROM operation WHERE execution_data IS NOT NULL AND CASE
            WHEN json_valid(execution_data) THEN
              COALESCE(json_type(execution_data, '$.context.scope.workspace_id'), '') != 'text'
              OR COALESCE(json_type(execution_data, '$.context.scope.user_id'), '') != 'text'
              OR length(json_extract(execution_data, '$.context.scope.workspace_id')) = 0
              OR length(json_extract(execution_data, '$.context.scope.user_id')) = 0
            ELSE 1 END LIMIT 1"""
        ).first()
        if invalid is not None:
            raise RuntimeFailure("Kernel migration requires valid execution ownership snapshots.")
        connection.exec_driver_sql("DROP TRIGGER IF EXISTS execution_snapshot_immutable")
        requests = connection.exec_driver_sql(
            """SELECT id, request_key,
            json_extract(execution_data, '$.context.scope.workspace_id'),
            json_extract(execution_data, '$.context.scope.user_id')
            FROM operation WHERE execution_data IS NOT NULL AND request_key IS NOT NULL"""
        ).all()
        for identifier, key, workspace_id, user_id in requests:
            if not key.startswith("capability/"):
                scoped_key = execution_request_key(
                    CapabilityScope(workspace_id=workspace_id, user_id=user_id),
                    key,
                )
                connection.exec_driver_sql(
                    "UPDATE operation SET request_key=? WHERE id=?",
                    (scoped_key, identifier),
                )
        for statement in STEP_DDL:
            name = statement.split("EXISTS ", 1)[1].split()[0]
            connection.exec_driver_sql(f"DROP TRIGGER IF EXISTS {name}")
        connection.exec_driver_sql(
            """INSERT INTO execution_step
            (execution_id,attempt_number,state,started_at,finished_at)
            SELECT execution_id,number,
              CASE WHEN finished_at IS NULL THEN 'running' ELSE outcome END,
              dispatched_at,finished_at
            FROM execution_attempt a WHERE dispatched_at IS NOT NULL AND NOT EXISTS
            (SELECT 1 FROM execution_step s
             WHERE s.execution_id=a.execution_id AND s.attempt_number=a.number)"""
        )
        for statement in (*EXECUTION_DDL, *STEP_DDL, *KERNEL_INDEX_DDL, *APPROVAL_DDL):
            connection.exec_driver_sql(statement)
        connection.exec_driver_sql(
            """CREATE UNIQUE INDEX IF NOT EXISTS uq_approval_consumed_attempt
            ON approval_request(execution_id,consumed_attempt) WHERE state='consumed'"""
        )
        connection.execute(
            insert(SchemaMigration).values(version=10, name="scoped_kernel_persistence", applied_at=now_ts())
        )
    if 11 not in versions:
        for statement in PROACTIVE_DDL:
            connection.exec_driver_sql(statement)
        connection.exec_driver_sql(
            """CREATE INDEX IF NOT EXISTS idx_execution_proactive ON operation(
            json_extract(execution_data, '$.context.scope.workspace_id'),
            json_extract(execution_data, '$.context.scope.user_id'),
            json_extract(execution_data, '$.context.proactive_snapshot.id'), queued_at)
            WHERE execution_data IS NOT NULL"""
        )
        connection.execute(
            insert(SchemaMigration).values(
                version=11, name="proactive_service_configuration", applied_at=now_ts()
            )
        )

    if 12 not in versions:
        columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(operation)")}
        if "workflow_data" not in columns:
            connection.exec_driver_sql("ALTER TABLE operation ADD COLUMN workflow_data TEXT")
        for statement in NOTIFICATION_DDL:
            connection.exec_driver_sql(statement)
        connection.execute(
            insert(SchemaMigration).values(
                version=12, name="workflow_checkpoints_and_interactions", applied_at=now_ts()
            )
        )

    if 13 not in versions:
        columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(operation)")}
        if "execution_control" not in columns:
            connection.exec_driver_sql(
                "ALTER TABLE operation ADD COLUMN execution_control TEXT NOT NULL DEFAULT '{}'"
            )
        attempts = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(execution_attempt)")}
        if "reason" not in attempts:
            connection.exec_driver_sql("ALTER TABLE execution_attempt ADD COLUMN reason TEXT")
        old_columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(interaction)")}
        if "response_schema" in old_columns:
            for statement in INPUT_DDL:
                name = statement.split("EXISTS ", 1)[1].split()[0]
                connection.exec_driver_sql(f"DROP TRIGGER IF EXISTS {name}")
            connection.exec_driver_sql(
                """INSERT INTO input_request
                (id,execution_id,workspace_id,user_id,node_id,state,prompt,response_schema,response,expires_at)
                SELECT id,execution_id,workspace_id,user_id,node_id,state,prompt,
                response_schema,response,expires_at FROM interaction"""
            )
            connection.exec_driver_sql("DROP TABLE interaction")
            Base.metadata.tables["interaction"].create(connection)
            connection.exec_driver_sql(
                """INSERT INTO interaction
                (id,execution_id,workspace_id,user_id,kind,source_kind,source_id,message,actions,state,created_at)
                SELECT id,execution_id,workspace_id,user_id,'input_request','input_request',id,prompt,
                CASE WHEN state='pending'
                  THEN '["VIEW","PROVIDE_INPUT","DISMISS","SNOOZE"]' ELSE '["VIEW"]' END,
                CASE WHEN state='pending' THEN 'open' ELSE 'resolved' END,0 FROM input_request"""
            )
        for statement in (*EXECUTION_DDL, *NOTIFICATION_DDL):
            name = statement.split("EXISTS ", 1)[1].split()[0]
            connection.exec_driver_sql(f"DROP TRIGGER IF EXISTS {name}")
            connection.exec_driver_sql(statement)
        for statement in (*INPUT_DDL, *INTERACTION_DDL, *WAKEUP_DDL):
            connection.exec_driver_sql(statement)
        for statement in (
            "CREATE INDEX IF NOT EXISTS idx_approval_expiration ON approval_request(state,expires_at)",
            "CREATE INDEX IF NOT EXISTS idx_input_expiration ON input_request(state,expires_at)",
            """CREATE INDEX IF NOT EXISTS idx_execution_deadline ON operation(state,execution_deadline)
            WHERE execution_data IS NOT NULL""",
        ):
            connection.exec_driver_sql(statement)
        connection.execute(
            insert(SchemaMigration).values(
                version=13,
                name="execution_timers_recovery_and_interaction_projections",
                applied_at=now_ts(),
            )
        )

    if 14 not in versions:
        columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(proactive_service)")}
        for name, definition in {
            "name": "TEXT NOT NULL DEFAULT ''",
            "description": "TEXT NOT NULL DEFAULT ''",
            "paused": "BOOLEAN NOT NULL DEFAULT 0",
        }.items():
            if name not in columns:
                connection.exec_driver_sql(f"ALTER TABLE proactive_service ADD COLUMN {name} {definition}")
        connection.exec_driver_sql("DROP TRIGGER IF EXISTS proactive_revision_guard")
        for statement in PROACTIVE_DDL:
            connection.exec_driver_sql(statement)
        connection.execute(
            insert(SchemaMigration).values(
                version=14,
                name="proactive_editor_metadata_and_pause",
                applied_at=now_ts(),
            )
        )

    if 15 not in versions:
        for statement in CONNECTION_DDL:
            connection.exec_driver_sql(statement)
        connection.execute(
            insert(SchemaMigration).values(
                version=15,
                name="disabled_enterprise_connection_profiles",
                applied_at=now_ts(),
            )
        )

    if 16 not in versions:
        for statement in (*IDENTITY_DDL, *AUTHENTICATION_DDL):
            connection.exec_driver_sql(statement)
        connection.execute(
            insert(SchemaMigration).values(
                version=16,
                name="unavailable_identity_and_durable_authentication_requests",
                applied_at=now_ts(),
            )
        )

    if 17 not in versions:
        for statement in PROTECTION_DDL:
            connection.exec_driver_sql(statement)
        connection.execute(
            insert(SchemaMigration).values(
                version=17, name="source_bound_enterprise_metadata", applied_at=now_ts()
            )
        )

    if 18 not in versions:
        for statement in ENTERPRISE_EXECUTION_DDL:
            connection.exec_driver_sql(statement)
        connection.execute(
            insert(SchemaMigration).values(
                version=18, name="enterprise_execution_authentication_and_audit", applied_at=now_ts()
            )
        )

    if 19 not in versions:
        for statement in ORGANIZATION_DDL:
            connection.exec_driver_sql(statement)
        connection.execute(
            insert(SchemaMigration).values(
                version=19, name="organization_policy_and_source_tombstones", applied_at=now_ts()
            )
        )

    if 20 not in versions:
        for statement in EFFECT_DDL:
            connection.exec_driver_sql(statement)
        connection.execute(
            insert(SchemaMigration).values(
                version=20, name="external_effect_intents_receipts_and_reconciliation", applied_at=now_ts()
            )
        )

    if 21 not in versions:
        connection.exec_driver_sql("DROP TRIGGER IF EXISTS enterprise_metadata_immutable")
        for statement in (*PROTECTION_DDL, *GOVERNANCE_DDL):
            connection.exec_driver_sql(statement)
        connection.execute(
            insert(SchemaMigration).values(
                version=21, name="enterprise_retention_and_retirement_guards", applied_at=now_ts()
            )
        )

    if 22 not in versions:
        for table in ("memory_belief", "memory_episode", "memory_procedure"):
            columns = {row[1] for row in connection.exec_driver_sql(f"PRAGMA table_info({table})")}
            if "review_data" not in columns:
                connection.exec_driver_sql(
                    f"ALTER TABLE {table} ADD COLUMN review_data TEXT NOT NULL DEFAULT '{{}}'"
                )
        columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(memory_working)")}
        for name, definition in {
            "workspace_id": "TEXT NOT NULL DEFAULT ''",
            "user_id": "TEXT NOT NULL DEFAULT ''",
            "task_id": "TEXT NOT NULL DEFAULT ''",
            "revision": "INTEGER NOT NULL DEFAULT 1",
            "updated_at": "INTEGER NOT NULL DEFAULT 0",
        }.items():
            if name not in columns:
                connection.exec_driver_sql(f"ALTER TABLE memory_working ADD COLUMN {name} {definition}")
        connection.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_wm_task ON memory_working "
            "(workspace_id,user_id,agent_id,session_id,task_id) WHERE task_id != ''"
        )
        connection.exec_driver_sql(
            "CREATE VIRTUAL TABLE IF NOT EXISTS memory_search "
            "USING fts5(memory_id UNINDEXED, kind UNINDEXED, text, tokenize='trigram')"
        )
        for table, kind, body in (
            ("memory_belief", "semantic", "COALESCE(note, subject || ' ' || predicate || ' ' || object)"),
            ("memory_episode", "episodic", "summary"),
            ("memory_procedure", "procedural", "instruction"),
        ):
            new_body = (
                "COALESCE(new.note, new.subject || ' ' || new.predicate || ' ' || new.object)"
                if kind == "semantic"
                else f"new.{body}"
            )
            for event in ("INSERT", "UPDATE"):
                changes = {
                    "semantic": "note,subject,predicate,object,validity_state",
                    "episodic": "summary,validity_state",
                    "procedural": "instruction,validity_state",
                }[kind]
                action = event if event == "INSERT" else f"UPDATE OF {changes}"
                connection.exec_driver_sql(
                    f"""CREATE TRIGGER IF NOT EXISTS {table}_search_{event.lower()}
                    AFTER {action} ON {table} BEGIN
                      DELETE FROM memory_search WHERE memory_id = new.id AND kind = '{kind}';
                      INSERT INTO memory_search(memory_id,kind,text)
                        SELECT new.id,'{kind}',{new_body} WHERE new.validity_state = 'active';
                    END"""  # noqa: S608 -- fixed migration identifiers, never user input
                )
            connection.exec_driver_sql(
                f"""CREATE TRIGGER IF NOT EXISTS {table}_search_delete AFTER DELETE ON {table} BEGIN
                  DELETE FROM memory_search WHERE memory_id = old.id AND kind = '{kind}';
                END"""  # noqa: S608 -- fixed migration identifiers, never user input
            )
            connection.exec_driver_sql(
                f"INSERT INTO memory_search(memory_id,kind,text) SELECT id,'{kind}',{body} "  # noqa: S608
                f"FROM {table} WHERE validity_state = 'active'"
            )
        connection.execute(
            insert(SchemaMigration).values(
                version=22, name="memory_review_keyword_and_working_context", applied_at=now_ts()
            )
        )

    if 23 not in versions:
        columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(chat_message)")}
        if "attachments" not in columns:
            connection.exec_driver_sql(
                "ALTER TABLE chat_message ADD COLUMN attachments TEXT NOT NULL DEFAULT '[]'"
            )
        connection.execute(
            insert(SchemaMigration).values(version=23, name="session_attachments", applied_at=now_ts())
        )

    if 24 not in versions:
        Base.metadata.create_all(connection, tables=[Base.metadata.tables["user_profile"]])
        connection.execute(
            insert(SchemaMigration).values(version=24, name="user_profile", applied_at=now_ts())
        )

    if 25 not in versions:
        Base.metadata.create_all(connection, tables=[Base.metadata.tables["memory_experience"]])
        connection.execute(
            insert(SchemaMigration).values(
                version=25, name="reviewed_experience_candidates", applied_at=now_ts()
            )
        )

    if 26 not in versions:
        columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(session_attachment)")}
        if "reading_scope" not in columns:
            connection.exec_driver_sql(
                "ALTER TABLE session_attachment ADD COLUMN reading_scope TEXT NOT NULL DEFAULT '{}'"
            )
        connection.execute(
            insert(SchemaMigration).values(version=26, name="attachment_reading_scope", applied_at=now_ts())
        )


async def upgrade_database(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.exec_driver_sql("BEGIN IMMEDIATE")
        await connection.run_sync(_upgrade)
