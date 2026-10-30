from __future__ import annotations

from pathlib import Path

from sqlalchemy import delete, inspect, select, text

from shovel.db.base import Base
from shovel.db.database import create_async_database_engine, create_async_session_factory, init_database
from shovel.db.migrations import upgrade_database
from shovel.db.model.capability import CAPABILITY_DDL
from shovel.db.model.execution import EXECUTION_DDL
from shovel.db.model.retrieval import SchemaMigration
from shovel.db.model.runtime import Operation
from shovel.db.model.security import APPROVAL_DDL


async def test_version_six_database_upgrade_preserves_receipts_and_is_repeatable(tmp_path: Path) -> None:
    url = f"sqlite+aiosqlite:///{(tmp_path / 'legacy.db').as_posix()}"
    context = create_async_database_engine(url)
    engine = await anext(context)
    try:
        async with engine.begin() as connection:
            capability_tables = set(CAPABILITY_DDL) | {
                Operation.__table__,
                Base.metadata.tables["execution_attempt"],
            }
            await connection.run_sync(
                lambda sync: Base.metadata.create_all(
                    sync,
                    tables=[table for table in Base.metadata.sorted_tables if table not in capability_tables],
                ),
            )
            await connection.exec_driver_sql(
                """CREATE TABLE operation(
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, state TEXT NOT NULL,
                resource_id TEXT, job_id TEXT, payload TEXT NOT NULL, result TEXT NOT NULL,
                error TEXT, queued_at INTEGER NOT NULL, started_at INTEGER,
                finished_at INTEGER, attempts INTEGER NOT NULL,
                request_key TEXT, request_fingerprint TEXT)"""
            )
            await connection.exec_driver_sql(
                """INSERT INTO operation(id,kind,state,payload,result,queued_at,attempts)
                VALUES ('legacy','cleanup','succeeded','{}','{"preserved":true}',1,1)"""
            )
        sessions = create_async_session_factory(engine)
        async with sessions() as session:
            session.add_all(
                SchemaMigration(version=number, name=f"existing-{number}", applied_at=number)
                for number in range(1, 7)
            )
            await session.commit()
        await upgrade_database(engine)
        await upgrade_database(engine)
        async with sessions() as session:
            receipt = await session.get(Operation, "legacy")
            assert receipt is not None and receipt.result == {"preserved": True}
            assert receipt.execution_data is None and receipt.capability_version_id is None
            assert receipt.execution_usage == {} and receipt.row_version == 0
            versions = list(
                await session.scalars(select(SchemaMigration.version).order_by(SchemaMigration.version))
            )
            assert versions == list(range(1, 15))
        async with engine.connect() as connection:
            assert not (await connection.execute(text("PRAGMA foreign_key_check"))).all()
            tables = await connection.run_sync(lambda sync: inspect(sync).get_table_names())
            assert "capability_version" in tables
            assert "execution_attempt" in tables
            assert not {
                "execution",
                "proactive_task",
                "workflow_task",
                "skill_task",
                "agent_task",
                "function_task",
            } & set(tables)
    finally:
        await context.aclose()


async def test_init_database_and_migration_both_install_immutability_guards(tmp_path: Path) -> None:
    url = f"sqlite+aiosqlite:///{(tmp_path / 'initialized.db').as_posix()}"
    init_database(url)
    context = create_async_database_engine(url)
    engine = await anext(context)
    try:
        expected = {
            statement.split("EXISTS ", 1)[1].split()[0]
            for group in CAPABILITY_DDL.values()
            for statement in group
        }
        expected.update(statement.split("EXISTS ", 1)[1].split()[0] for statement in EXECUTION_DDL)
        expected.update(statement.split("EXISTS ", 1)[1].split()[0] for statement in APPROVAL_DDL)
        async with engine.begin() as connection:
            installed = set(
                await connection.scalars(text("SELECT name FROM sqlite_master WHERE type = 'trigger'"))
            )
            assert expected <= installed
            await connection.exec_driver_sql("DROP TRIGGER capability_published_update")
            await connection.exec_driver_sql("DROP TRIGGER execution_snapshot_immutable")
            await connection.exec_driver_sql("DROP TRIGGER approval_identity_immutable")
            await connection.execute(delete(SchemaMigration).where(SchemaMigration.version == 7))
            await connection.execute(delete(SchemaMigration).where(SchemaMigration.version == 8))
            await connection.execute(delete(SchemaMigration).where(SchemaMigration.version == 9))
        await upgrade_database(engine)
        await upgrade_database(engine)
        async with engine.connect() as connection:
            installed = set(
                await connection.scalars(text("SELECT name FROM sqlite_master WHERE type = 'trigger'"))
            )
            assert expected <= installed
    finally:
        await context.aclose()
