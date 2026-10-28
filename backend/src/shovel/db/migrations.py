"""Ordered, additive runtime upgrades for existing local databases."""

from __future__ import annotations

from sqlalchemy import Connection, insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from shovel.db.base import Base, now_ts
from shovel.db.model.retrieval import SchemaMigration

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


async def upgrade_database(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(_upgrade)
