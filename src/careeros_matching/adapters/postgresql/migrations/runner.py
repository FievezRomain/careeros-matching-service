"""Single adapter-owned recommendation migration and head verification."""

import argparse

from pydantic import SecretStr
from pydantic_settings import BaseSettings
from sqlalchemy import (
    Column,
    Engine,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    inspect,
    select,
    text,
)

HEAD = "0001_recommendations"
ledger = Table("matching_schema", MetaData(), Column("revision", String(80), primary_key=True))
schema = MetaData()
tenant_locks = Table(
    "matching_tenant_lock", schema, Column("tenant_id", String(36), primary_key=True)
)
records = Table(
    "matching_records",
    schema,
    Column("ordinal", Integer, primary_key=True, autoincrement=True),
    Column("tenant_id", String(36), nullable=False),
    Column("account_id", String(36), nullable=False),
    Column("kind", String(48), nullable=False),
    Column("record_key", String(200), nullable=False),
    Column("payload", Text, nullable=False),
    UniqueConstraint(
        "tenant_id", "account_id", "kind", "record_key", name="uq_matching_owned_record"
    ),
)
extractions = Table(
    "matching_extractions",
    schema,
    Column("tenant_id", String(36), primary_key=True),
    Column("record_key", String(120), primary_key=True),
    Column("payload", Text, nullable=False),
)


class SqlMigrations:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def apply_pending(self) -> None:
        with self._engine.begin() as connection:
            if connection.dialect.name == "postgresql":
                connection.execute(text("SELECT pg_advisory_xact_lock(851580001)"))
            ledger.create(connection, checkfirst=True)
            applied: tuple[str, ...] = tuple(connection.scalars(select(ledger.c.revision)))
            if not applied:
                schema.create_all(connection, checkfirst=False)
                connection.execute(ledger.insert().values(revision=HEAD))

    def verify(self) -> bool:
        with self._engine.connect() as connection:
            inspector = inspect(connection)
            if not inspector.has_table(ledger.name):
                return False
            heads: tuple[str, ...] = tuple(connection.scalars(select(ledger.c.revision)))
            return heads == (HEAD,) and all(
                inspector.has_table(table.name)
                and {column["name"] for column in inspector.get_columns(table.name)}
                == set(table.c.keys())
                for table in schema.tables.values()
            )

    def downgrade(self) -> None:
        with self._engine.begin() as connection:
            if connection.dialect.name == "postgresql":
                connection.execute(text("SELECT pg_advisory_xact_lock(851580001)"))
            if not inspect(connection).has_table(ledger.name):
                return
            if tuple(connection.scalars(select(ledger.c.revision))) != (HEAD,):
                raise RuntimeError("refusing downgrade from unexpected recommendation schema")
            schema.drop_all(connection)
            ledger.drop(connection)


class MigrationSettings(BaseSettings):
    matching_database_url: SecretStr


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate the owned matching database")
    parser.add_argument("operation", choices=("upgrade", "downgrade"))
    args = parser.parse_args()
    url = MigrationSettings().matching_database_url.get_secret_value()
    if not url.startswith("postgresql+psycopg://"):
        raise ValueError("MATCHING_DATABASE_URL must use PostgreSQL psycopg")
    engine = create_engine(url)
    try:
        migrations = SqlMigrations(engine)
        if args.operation == "upgrade":
            migrations.apply_pending()
            return int(not migrations.verify())
        migrations.downgrade()
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
