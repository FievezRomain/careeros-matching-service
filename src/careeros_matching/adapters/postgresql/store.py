"""Owned SQL transactions for versioned recommendation records."""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Connection, Engine, select, text
from sqlalchemy.engine import ScalarResult
from sqlalchemy.sql import ColumnElement

from careeros_matching.adapters.postgresql.migrations.runner import extractions, records
from careeros_matching.application.recommendation import (
    CachedExtraction,
    HistoryRow,
    Receipt,
    RecommendationEvent,
    RecommendationTransaction,
)
from careeros_matching.domain.recommendation import JobProjection, ProfileProjection, Recommendation
from careeros_matching.domain.scoring import Contract, MatchResult, Owner

_RECORD_TYPES: dict[type[Contract], str] = {
    ProfileProjection: "profile",
    JobProjection: "job",
    Recommendation: "listing",
    HistoryRow: "history",
    Receipt: "receipt",
    MatchResult: "evaluation",
    RecommendationEvent: "outbox",
}


class SqlRecommendationStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    @contextmanager
    def transaction(self, owner: Owner) -> Iterator[RecommendationTransaction]:
        with self._engine.begin() as connection:
            tenant = str(owner.tenant_id)
            # Lock the matching tenant until commit so extraction and transitions are serialized.
            connection.execute(
                text(
                    "INSERT INTO matching_tenant_lock (tenant_id) "
                    "VALUES (:tenant) ON CONFLICT DO NOTHING"
                ),
                {"tenant": tenant},
            )
            connection.execute(
                text(
                    "UPDATE matching_tenant_lock SET tenant_id = :tenant WHERE tenant_id = :tenant"
                ),
                {"tenant": tenant},
            )
            yield _Transaction(connection, owner)


class _Transaction:
    def __init__(self, connection: Connection, owner: Owner) -> None:
        self._connection = connection
        self._tenant = str(owner.tenant_id)
        self._account = str(owner.account_id)

    def _scope(self, kind: str) -> tuple[ColumnElement[bool], ...]:
        return (
            records.c.tenant_id == self._tenant,
            records.c.account_id == self._account,
            records.c.kind == kind,
        )

    def get[T: Contract](self, model: type[T], key: str) -> T | None:
        payload = self._connection.scalar(
            select(records.c.payload).where(
                *self._scope(_RECORD_TYPES[model]),
                records.c.record_key == key,
            )
        )
        return model.model_validate_json(payload) if payload is not None else None

    def all[T: Contract](self, model: type[T]) -> tuple[T, ...]:
        payloads: ScalarResult[str] = self._connection.scalars(
            select(records.c.payload)
            .where(
                *self._scope(_RECORD_TYPES[model]),
            )
            .order_by(records.c.ordinal)
        )
        return tuple(model.model_validate_json(payload) for payload in payloads)

    def save(self, key: str, value: Contract) -> None:
        kind, payload = _RECORD_TYPES[type(value)], value.model_dump_json()
        updated = self._connection.execute(
            records.update()
            .where(
                *self._scope(kind),
                records.c.record_key == key,
            )
            .values(payload=payload)
        )
        if updated.rowcount == 0:
            self._connection.execute(
                records.insert().values(
                    tenant_id=self._tenant,
                    account_id=self._account,
                    kind=kind,
                    record_key=key,
                    payload=payload,
                )
            )

    def extraction(self, key: str) -> CachedExtraction | None:
        payload = self._connection.scalar(
            select(extractions.c.payload).where(
                extractions.c.tenant_id == self._tenant,
                extractions.c.record_key == key,
            )
        )
        return CachedExtraction.model_validate_json(payload) if payload is not None else None

    def save_extraction(self, key: str, extraction: CachedExtraction) -> None:
        self._connection.execute(
            extractions.insert().values(
                tenant_id=self._tenant,
                record_key=key,
                payload=extraction.model_dump_json(),
            )
        )
