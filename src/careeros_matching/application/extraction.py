"""Versioned, evidence-checked extraction through a core-owned gateway port."""

import hashlib
import json
import re
from typing import Annotated, Final, Literal, Protocol, Self
from uuid import UUID, uuid5

from pydantic import ConfigDict, Field, StrictBool, StringConstraints, model_validator

from careeros_matching.application.scoring import Actor, require_access
from careeros_matching.domain.scoring import Contract, Owner, SourceAccess, normalize_key

CAPABILITY = "extract-job-requirements"
EXTRACTOR_VERSION: Final = "requirements-v1"
Excerpt = Annotated[str, StringConstraints(max_length=800)]
ExtractionStatus = Literal["extracted", "partial", "inconclusive", "external_to_verify"]


class SourceText(Contract):
    job_text: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=30_000)
    ]
    source_language: str = Field(pattern=r"^[a-z]{2}(?:-[A-Z]{2})?$")


class ExtractionInput(Owner):
    opportunity_version_id: UUID
    source_access: SourceAccess
    job_text: str | None = Field(default=None, max_length=30_000)
    source_language: str | None = Field(default=None, pattern=r"^[a-z]{2}(?:-[A-Z]{2})?$")

    @model_validator(mode="after")
    def require_full_text(self) -> Self:
        if self.source_access == "full_text" and not (
            self.job_text and self.job_text.strip() and self.source_language
        ):
            raise ValueError("full-text extraction requires jobText and sourceLanguage")
        if self.source_access == "external_to_verify" and self.job_text is not None:
            raise ValueError("link-only opportunities cannot supply verified job text")
        return self


class Candidate(Contract):
    model_config = ConfigDict(extra="ignore")
    category: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]
    description: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)
    ]
    required: StrictBool
    evidence: tuple[Excerpt, ...] = Field(max_length=5)


class Provenance(Contract):
    model_config = ConfigDict(extra="ignore")
    model_version: str = Field(min_length=1, max_length=200)
    prompt_version: str = Field(min_length=1, max_length=100)
    provider_attempts: int | None = Field(default=None, ge=1, le=3)
    latency_ms: int | None = Field(default=None, ge=0)
    input_units: int | None = Field(default=None, ge=0)
    output_units: int | None = Field(default=None, ge=0)


class GatewayExtraction(Contract):
    requirements: tuple[Candidate, ...] = Field(max_length=100)
    provenance: Provenance


class Suggestion(Contract):
    requirement_id: UUID
    category: str
    description: str
    suggested_mandatory: bool
    evidence: tuple[str, ...]


class ExtractionResult(Contract):
    opportunity_version_id: UUID
    extractor_version: Literal["requirements-v1"] = EXTRACTOR_VERSION
    status: ExtractionStatus
    suggestions: tuple[Suggestion, ...] = ()
    rejected_count: int = 0
    provenance: Provenance | None = None


class ExtractionUnavailable(Exception):
    pass


class ExtractionResponseInvalid(Exception):
    pass


class RequirementGateway(Protocol):
    def extract(self, source: SourceText, key: str, correlation_id: UUID) -> GatewayExtraction: ...


class ExtractRequirements:
    def __init__(self, gateway: RequirementGateway | None) -> None:
        self._gateway = gateway

    def execute(
        self, actor: Actor, command: ExtractionInput, correlation_id: UUID
    ) -> ExtractionResult:
        require_access(actor, "matching:extract", command)
        version = command.opportunity_version_id
        if command.job_text is None or command.source_language is None:
            return ExtractionResult(opportunity_version_id=version, status="external_to_verify")
        if self._gateway is None:
            raise ExtractionUnavailable
        source = SourceText(job_text=command.job_text, source_language=command.source_language)
        identity = json.dumps(
            [
                str(actor.tenant_id),
                str(actor.account_id),
                str(version),
                EXTRACTOR_VERSION,
                source.model_dump_json(),
            ]
        )
        upstream = self._gateway.extract(
            source, hashlib.sha256(identity.encode()).hexdigest(), correlation_id
        )
        suggestions: dict[UUID, Suggestion] = {}
        rejected = 0
        for item in upstream.requirements:
            quotes = tuple(dict.fromkeys(quote.strip() for quote in item.evidence))
            description = normalize_key(item.description)
            pattern = rf"(?<!\w){re.escape(description)}(?!\w)"
            if (
                not quotes
                or any(not quote or quote not in source.job_text for quote in quotes)
                or not description
                or not any(re.search(pattern, normalize_key(quote)) for quote in quotes)
            ):
                rejected += 1
                continue
            name = json.dumps([EXTRACTOR_VERSION, normalize_key(item.category), description])
            identifier = uuid5(version, name)
            suggestions.setdefault(
                identifier,
                Suggestion(
                    requirement_id=identifier,
                    category=item.category,
                    description=item.description,
                    suggested_mandatory=item.required,
                    evidence=quotes,
                ),
            )
        status: ExtractionStatus = (
            "inconclusive" if not suggestions else "partial" if rejected else "extracted"
        )
        return ExtractionResult(
            opportunity_version_id=version,
            status=status,
            suggestions=tuple(suggestions.values()),
            rejected_count=rejected,
            provenance=upstream.provenance,
        )
