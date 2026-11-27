from sqlalchemy import inspect, text


CANCEL_COLUMNS = {
    "cancel_reason": "VARCHAR(320) NULL",
    "cancelled_by": "VARCHAR(48) NULL",
}


def upgrade(connection):
    # Idempotent additive change: existing databases receive the cancellation columns,
    # fresh databases already have them from the declarative v001 schema.
    existing = {column["name"] for column in inspect(connection).get_columns("runs")}
    for name, ddl in CANCEL_COLUMNS.items():
        if name not in existing:
            connection.execute(text(f"ALTER TABLE runs ADD COLUMN {name} {ddl}"))
