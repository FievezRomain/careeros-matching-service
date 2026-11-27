import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, make_url

from careeros_matching.adapters.http import Settings, create_app
from careeros_matching.adapters.postgresql.migrations.runner import SqlMigrations
from careeros_matching.adapters.postgresql.store import SqlRecommendationStore
from careeros_matching.application.scoring import Actor


@pytest.fixture(params=["sqlite", "postgresql"])
def engine(request: pytest.FixtureRequest, tmp_path: object) -> Iterator[Engine]:
    if request.param == "postgresql":
        url = os.environ.get("TEST_MATCHING_DATABASE_URL")
        if url is None:
            pytest.skip("dedicated PostgreSQL test database unavailable")
        if make_url(url).database != "careeros_matching_test":
            pytest.fail("the migration test requires a dedicated careeros_matching_test database")
    else:
        url = f"sqlite:///{tmp_path}/recommendations.db"
    instance = create_engine(url)
    try:
        yield instance
    finally:
        instance.dispose()


def test_migration_head_upgrade_replay_and_downgrade(engine: Engine) -> None:
    migrations = SqlMigrations(engine)
    migrations.apply_pending()
    migrations.apply_pending()
    assert migrations.verify()
    migrations.downgrade()
    assert not migrations.verify()


class IncompleteMigrations:
    applied = False

    def apply_pending(self) -> None:
        self.applied = True

    def verify(self) -> bool:
        return False


def settings() -> Settings:
    return Settings(
        oidc_issuer="https://id.example.invalid",
        oidc_audience="matching",
        oidc_jwks_url="https://id.example.invalid/jwks",
    )


def test_startup_refuses_an_incomplete_head(engine: Engine) -> None:
    runner = IncompleteMigrations()
    app = create_app(
        settings(), recommendation_store=SqlRecommendationStore(engine), migration_runner=runner
    )
    with pytest.raises(RuntimeError, match="expected head"), TestClient(app):
        pytest.fail("startup accepted an incomplete migration")
    assert runner.applied


class Identity:
    def verify(self, token: str) -> Actor:
        scopes = ("matching:recommendations:write",) if token == "synthetic-projector" else ()
        return Actor(account_id=UUID(int=1), tenant_id=UUID(int=2), scopes=scopes)


def test_concurrent_redelivery_claims_one_projection(engine: Engine) -> None:
    migrations = SqlMigrations(engine)
    app = create_app(
        settings(),
        Identity(),
        recommendation_store=SqlRecommendationStore(engine),
        migration_runner=migrations,
    )
    payload: dict[str, object] = {
        "kind": "profile",
        "eventId": str(UUID(int=3)),
        "revision": 1,
        "occurredAt": "2026-11-26T10:00:00Z",
        "profile": {
            "accountId": str(UUID(int=1)),
            "tenantId": str(UUID(int=2)),
            "snapshotId": str(UUID(int=4)),
            "confirmedFacts": [],
        },
        "criteria": {"criteriaVersionId": str(UUID(int=5)), "threshold": 70},
    }
    path = "/v1/internal/recommendation-updates"
    try:
        with TestClient(app) as api, ThreadPoolExecutor(max_workers=2) as pool:

            def send(_: int) -> str:
                result = api.post(
                    path, headers={"Authorization": "Bearer synthetic-projector"}, json=payload
                )
                assert result.status_code == 200
                return str(result.json()["outcome"])

            assert sorted(pool.map(send, range(2))) == ["applied", "duplicate"]
            assert (
                api.post(path, headers={"Authorization": "Bearer other"}, json=payload).status_code
                == 403
            )
            assert (
                api.post(
                    path,
                    headers={"Authorization": "Bearer synthetic-projector"},
                    json={**payload, "revision": 2},
                ).status_code
                == 409
            )
    finally:
        migrations.downgrade()
