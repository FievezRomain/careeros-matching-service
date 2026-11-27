"""HTTP adapter for the AI gateway's provider-neutral requirements capability."""

from typing import Literal
from uuid import UUID

import httpx2
from pydantic import ConfigDict, Field, ValidationError

from careeros_matching.application.extraction import (
    CAPABILITY,
    Candidate,
    ExtractionResponseInvalid,
    ExtractionUnavailable,
    GatewayExtraction,
    Provenance,
    SourceText,
)
from careeros_matching.domain.scoring import Contract


class GatewayOutput(Contract):
    model_config = ConfigDict(extra="ignore")
    requirements: tuple[Candidate, ...] = Field(max_length=100)


class GatewayResponse(Contract):
    model_config = ConfigDict(extra="ignore")
    capability_id: Literal["extract-job-requirements"]
    capability_version: Literal["1.0"]
    output_schema_version: Literal["1.0"]
    output: GatewayOutput
    provenance: Provenance


class HttpRequirementGateway:
    def __init__(self, client: httpx2.Client, gateway_url: str, token: str) -> None:
        self._client = client
        self._url = f"{gateway_url.rstrip('/')}/v1/capabilities/{CAPABILITY}/invocations"
        self._token = token

    def extract(self, source: SourceText, key: str, correlation_id: UUID) -> GatewayExtraction:
        body = bytearray()
        try:
            with self._client.stream(
                "POST",
                self._url,
                follow_redirects=False,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Idempotency-Key": key,
                    "X-Correlation-ID": str(correlation_id),
                },
                json={
                    "capabilityVersion": "1.0",
                    "inputSchemaVersion": "1.0",
                    "input": source.model_dump(mode="json", by_alias=True),
                },
            ) as response:
                if response.status_code in {400, 422, 502}:
                    raise ExtractionResponseInvalid
                response.raise_for_status()
                for chunk in response.iter_bytes(chunk_size=8192):
                    body.extend(chunk)
                    if len(body) > 1_048_576:
                        raise ExtractionResponseInvalid
            result = GatewayResponse.model_validate_json(body)
        except httpx2.HTTPError:
            raise ExtractionUnavailable from None
        except ValidationError:
            raise ExtractionResponseInvalid from None
        return GatewayExtraction(
            requirements=result.output.requirements, provenance=result.provenance
        )
