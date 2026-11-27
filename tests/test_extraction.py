from uuid import UUID

import httpx2
import pytest
from fastapi.testclient import TestClient

from careeros_matching.adapters.gateway import HttpRequirementGateway
from careeros_matching.adapters.http import Settings, create_app
from careeros_matching.application.scoring import Actor

PATH = "/v1/internal/requirements/extract"
HEADERS = {"Authorization": "Bearer synthetic-caller"}


class Verifier:
    def verify(self, token: str) -> Actor:
        scopes = ("matching:extract",) if token == "synthetic-caller" else ("matching:score",)
        return Actor(account_id=UUID(int=1), tenant_id=UUID(int=2), scopes=scopes)


def app(backend: httpx2.Client) -> TestClient:
    settings = Settings(
        oidc_issuer="https://id.example.invalid",
        oidc_audience="matching",
        oidc_jwks_url="https://id.example.invalid/jwks",
    )
    gateway = HttpRequirementGateway(
        backend, "https://gateway.example.invalid", "synthetic-service"
    )
    return TestClient(create_app(settings, Verifier(), gateway))


def command() -> dict[str, str]:
    return {
        "accountId": str(UUID(int=1)),
        "tenantId": str(UUID(int=2)),
        "opportunityVersionId": str(UUID(int=3)),
        "sourceAccess": "full_text",
        "jobText": "Python required",
        "sourceLanguage": "en",
    }


@pytest.mark.parametrize(("upstream_status", "status"), [(200, 502), (429, 503), (502, 502)])
def test_upstream_failures_are_safe(upstream_status: int, status: int) -> None:
    def upstream(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            upstream_status,
            json={
                "capabilityId": "extract-job-requirements",
                "capabilityVersion": "2.0",
                "outputSchemaVersion": "1.0",
                "output": {"requirements": []},
                "provenance": {"modelVersion": "synthetic", "promptVersion": "fixture"},
                "raw": "private upstream payload",
            },
        )

    with httpx2.Client(transport=httpx2.MockTransport(upstream)) as backend, app(backend) as api:
        response = api.post(PATH, json=command(), headers=HEADERS)
    assert response.status_code == status
    assert response.headers["Content-Type"] == "application/problem+json"
    assert "private upstream payload" not in response.text


def test_invalid_input_and_denied_scope_do_not_invoke_gateway() -> None:
    def upstream(_: httpx2.Request) -> httpx2.Response:
        pytest.fail("invalid or unauthorized request reached the gateway")

    payload = command()
    with httpx2.Client(transport=httpx2.MockTransport(upstream)) as backend, app(backend) as api:
        operation = api.get("/openapi.json").json()["paths"][PATH]["post"]
        assert {"OpenIdConnect": ["matching:extract"]} in operation["security"]
        assert (
            api.post(PATH, json=payload, headers={"Authorization": "Bearer scoring"}).status_code
            == 403
        )
        assert api.post(PATH, json={**payload, "jobText": " "}, headers=HEADERS).status_code == 422
        assert (
            api.post(
                PATH, json={**payload, "sourceAccess": "external_to_verify"}, headers=HEADERS
            ).status_code
            == 422
        )
