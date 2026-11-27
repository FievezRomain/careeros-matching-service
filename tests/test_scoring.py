import json
import logging
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from careeros_matching.adapters.http import Settings, create_app
from careeros_matching.application.scoring import Actor
from careeros_matching.domain.scoring import BaselineScorer, MatchInput, ScoreBounds

PATH = "/v1/internal/matches/score"
STATEMENT = "Synthetic source evidence"


def uid(number: int) -> str:
    return str(UUID(int=number))


def pair() -> dict[str, dict[str, object]]:
    owner = {"accountId": uid(1), "tenantId": uid(2)}
    return {
        "profile": {**owner, "snapshotId": uid(3), "confirmedFacts": []},
        "opportunity": {
            **owner,
            "versionId": uid(4),
            "sourceAccess": "full_text",
            "requirements": [
                {
                    "requirementId": uid(5),
                    "key": "python",
                    "minimumLevel": 3,
                    "mandatory": True,
                    "hardEligibility": True,
                    "statement": STATEMENT,
                },
            ],
        },
    }


class Verifier:
    def verify(self, token: str) -> Actor:
        if token not in {"fixture-allowed", "fixture-denied"}:
            raise ValueError
        scopes = ("matching:score",) if token == "fixture-allowed" else ()
        return Actor(account_id=UUID(int=1), tenant_id=UUID(int=2), scopes=scopes)


@pytest.fixture
def client() -> TestClient:
    settings = Settings(
        oidc_issuer="https://id.example.invalid",
        oidc_audience="matching",
        oidc_jwks_url="https://id.example.invalid/jwks",
    )
    return TestClient(create_app(settings, Verifier()))


def test_unknown_does_not_create_a_score_or_conflict() -> None:
    result = BaselineScorer().score(MatchInput.model_validate(pair()))
    assert result.score is None
    assert result.findings[0].status == "unknown"
    assert result.hard_conflicts == ()
    assert result.findings[0].earned_weight is None
    assert result.explanation.assessed_weight == 0
    assert result.explanation.score_bounds == ScoreBounds(minimum=0, maximum=100)


def test_complete_evidence_collapses_bounds_to_the_baseline_score() -> None:
    data = pair()
    data["profile"]["confirmedFacts"] = [{"factId": uid(6), "key": "Python", "level": 1}]
    result = BaselineScorer().score(MatchInput.model_validate(data))
    assert result.score == 50
    assert result.findings[0].earned_weight == 1
    assert result.hard_conflicts == (UUID(int=5),)
    assert result.explanation.assessed_weight == result.explanation.total_weight == 2
    assert result.explanation.uncertainty is None
    assert result.explanation.score_bounds == ScoreBounds(minimum=50, maximum=50)


def test_insecure_oidc_configuration_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(
            oidc_issuer="http://identity.example.invalid",
            oidc_audience="matching",
            oidc_jwks_url="https://identity.example.invalid/jwks",
        )


def test_http_contract_and_safe_logs(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    assert client.get("/health/live").json()["status"] == "ok"
    assert client.get("/health/ready").json()["status"] == "ready"
    schema = client.get("/openapi.json").json()
    operation = schema["paths"][PATH]["post"]
    assert operation["security"]
    assert "explanation" in schema["components"]["schemas"]["MatchResult"]["properties"]
    assert "application/problem+json" in operation["responses"]["404"]["content"]
    headers = {"Authorization": "Bearer fixture-allowed", "X-Correlation-ID": uid(99)}
    with caplog.at_level(logging.INFO, logger="careeros.matching.http"):
        response = client.post(PATH, json=pair(), headers=headers)
    assert response.status_code == 200
    assert response.headers["X-Correlation-ID"] == uid(99)
    assert json.loads(caplog.records[-1].getMessage())["correlation_id"] == uid(99)
    assert all(value not in caplog.text for value in (STATEMENT, "fixture-allowed", uid(1), uid(3)))


@pytest.mark.parametrize(("token", "status"), [("invalid", 401), ("fixture-denied", 403)])
def test_authentication_and_scope(client: TestClient, token: str, status: int) -> None:
    response = client.post(PATH, json=pair(), headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == status
    assert response.headers["Content-Type"] == "application/problem+json"


def test_foreign_snapshots_and_invalid_evidence(client: TestClient) -> None:
    data = pair()
    headers = {"Authorization": "Bearer fixture-allowed"}
    data["opportunity"]["tenantId"] = uid(10)
    assert client.post(PATH, json=data, headers=headers).status_code == 404
    data["opportunity"]["sourceAccess"] = "external_to_verify"
    invalid = client.post(PATH, json=data, headers=headers)
    assert invalid.status_code == 422 and STATEMENT not in invalid.text
