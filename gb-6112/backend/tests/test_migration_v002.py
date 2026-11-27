"""The v002 migration must be additive and idempotent on both fresh and old schemas."""
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.pool import StaticPool

from migrations.v001_workbench import upgrade as upgrade_v001
from migrations.v002_run_cancellation import upgrade as upgrade_v002
from src.models.base import metadata


def _fresh_engine():
    return create_engine("sqlite://", poolclass=StaticPool)


def test_v002_runs_cleanly_on_fresh_declarative_schema():
    engine = _fresh_engine()
    with engine.begin() as connection:
        upgrade_v001(connection)
        upgrade_v002(connection)
        columns = {column["name"] for column in inspect(connection).get_columns("runs")}
    assert {"cancel_reason", "cancelled_by"} <= columns
    engine.dispose()


def test_v002_adds_columns_to_legacy_database_and_is_idempotent():
    engine = _fresh_engine()
    with engine.begin() as connection:
        # Simulate a pre-cancellation deployment: create the original runs table without the new columns.
        connection.execute(text(
            "CREATE TABLE runs ("
            "id INTEGER PRIMARY KEY, case_id INTEGER NOT NULL, status VARCHAR(16) NOT NULL, "
            "parameter INTEGER NOT NULL, actor VARCHAR(48) NOT NULL, created_at BIGINT NOT NULL, "
            "finished_at BIGINT, result JSON, failure VARCHAR(320), digest VARCHAR(64))"
        ))
        upgrade_v002(connection)
        columns = {column["name"] for column in inspect(connection).get_columns("runs")}
        assert {"cancel_reason", "cancelled_by"} <= columns
        # Running again must not raise on duplicate columns.
        upgrade_v002(connection)
    engine.dispose()


def test_declarative_model_matches_migration_target():
    assert {"cancel_reason", "cancelled_by"} <= {column.name for column in metadata.tables["runs"].columns}
