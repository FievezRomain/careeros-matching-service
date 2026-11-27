"""Validated scoring evidence and the deterministic V1 policy."""

import unicodedata
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StringConstraints, model_validator
from pydantic.alias_generators import to_camel

Key = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Level = Annotated[int, Field(strict=True, ge=0, le=5)]
SourceAccess = Literal["full_text", "external_to_verify"]
Status = Literal["satisfied", "partial", "missing", "unknown"]


def normalize_key(key: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", key).casefold().split())


class Contract(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, alias_generator=to_camel, populate_by_name=True
    )


class Owner(Contract):
    account_id: UUID
    tenant_id: UUID


class Fact(Contract):
    fact_id: UUID
    key: Key
    level: Level


class ProfileSnapshot(Owner):
    snapshot_id: UUID
    confirmed_facts: tuple[Fact, ...] = Field(max_length=100)

    @model_validator(mode="after")
    def unique_facts(self) -> Self:
        count = len(self.confirmed_facts)
        if (
            len({normalize_key(fact.key) for fact in self.confirmed_facts}) != count
            or len({fact.fact_id for fact in self.confirmed_facts}) != count
        ):
            raise ValueError("confirmed fact keys and IDs must be unique")
        return self


class Requirement(Contract):
    requirement_id: UUID
    key: Key
    minimum_level: Level = Field(gt=0)
    mandatory: StrictBool
    hard_eligibility: StrictBool = False
    statement: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=800)
    ]

    @model_validator(mode="after")
    def hard_requirement_is_mandatory(self) -> Self:
        if self.hard_eligibility and not self.mandatory:
            raise ValueError("a hard eligibility requirement must be mandatory")
        return self


class OpportunityVersion(Owner):
    version_id: UUID
    source_access: SourceAccess
    requirements: tuple[Requirement, ...] = Field(max_length=100)

    @model_validator(mode="after")
    def validate_evidence(self) -> Self:
        if self.source_access == "external_to_verify" and self.requirements:
            raise ValueError("link-only opportunities cannot supply verified requirements")
        if len({item.requirement_id for item in self.requirements}) != len(self.requirements):
            raise ValueError("requirement IDs must be unique")
        return self


class MatchInput(Contract):
    profile: ProfileSnapshot
    opportunity: OpportunityVersion


class Finding(Contract):
    requirement_id: UUID
    statement: str
    status: Status
    fact_id: UUID | None


class MatchResult(Contract):
    scorer_version: Literal["baseline-v1"] = "baseline-v1"
    profile_snapshot_id: UUID
    opportunity_version_id: UUID
    source_access: SourceAccess
    score: int | None = Field(ge=0, le=100)
    findings: tuple[Finding, ...]
    hard_conflicts: tuple[UUID, ...]
    limitation: str = (
        "This score supports prioritization and does not guarantee an interview or employment."
    )


def status_for(requirement: Requirement, fact: Fact | None) -> Status:
    if fact is None:
        return "unknown"
    if fact.level >= requirement.minimum_level:
        return "satisfied"
    return "partial" if fact.level > 0 else "missing"


class BaselineScorer:
    def score(self, pair: MatchInput) -> MatchResult:
        facts = {normalize_key(fact.key): fact for fact in pair.profile.confirmed_facts}
        findings: list[Finding] = []
        conflicts: list[UUID] = []
        earned = known_weight = 0
        for item in sorted(pair.opportunity.requirements, key=lambda item: item.requirement_id):
            fact = facts.get(normalize_key(item.key))
            status = status_for(item, fact)
            findings.append(
                Finding(
                    requirement_id=item.requirement_id,
                    statement=item.statement,
                    status=status,
                    fact_id=fact.fact_id if fact else None,
                )
            )
            if status == "unknown":
                continue
            weight = 2 if item.mandatory else 1
            known_weight += weight
            earned += weight * {"satisfied": 2, "partial": 1, "missing": 0}[status]
            if item.hard_eligibility and status != "satisfied":
                conflicts.append(item.requirement_id)
        score = (100 * earned + known_weight) // (2 * known_weight) if known_weight else None
        return MatchResult(
            profile_snapshot_id=pair.profile.snapshot_id,
            opportunity_version_id=pair.opportunity.version_id,
            source_access=pair.opportunity.source_access,
            score=score,
            findings=tuple(findings),
            hard_conflicts=tuple(conflicts),
        )
