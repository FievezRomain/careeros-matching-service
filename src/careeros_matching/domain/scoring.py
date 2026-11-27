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
Uncertainty = Literal["unconfirmed_profile_facts", "no_verified_requirements", "external_to_verify"]


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
    minimum_level: Level
    confirmed_level: Level | None
    hard_eligibility: bool
    weight: Literal[1, 2]
    earned_weight: float | None = Field(ge=0, le=2)


class ScoreBounds(Contract):
    minimum: int = Field(ge=0, le=100)
    maximum: int = Field(ge=0, le=100)


class ScoreExplanation(Contract):
    version: Literal["evidence-v1"] = "evidence-v1"
    assessed_requirements: int = Field(ge=0, le=100)
    total_requirements: int = Field(ge=0, le=100)
    assessed_weight: int = Field(ge=0, le=200)
    total_weight: int = Field(ge=0, le=200)
    earned_weight: float = Field(ge=0, le=200)
    uncertainty: Uncertainty | None
    score_bounds: ScoreBounds | None


class MatchResult(Contract):
    scorer_version: Literal["baseline-v1"] = "baseline-v1"
    profile_snapshot_id: UUID
    opportunity_version_id: UUID
    source_access: SourceAccess
    score: int | None = Field(ge=0, le=100)
    findings: tuple[Finding, ...]
    hard_conflicts: tuple[UUID, ...]
    explanation: ScoreExplanation
    limitation: str = (
        "This score supports prioritization and does not guarantee an interview or employment."
    )


def status_for(requirement: Requirement, fact: Fact | None) -> Status:
    if fact is None:
        return "unknown"
    if fact.level >= requirement.minimum_level:
        return "satisfied"
    return "partial" if fact.level > 0 else "missing"


def _rounded_score(earned_half_units: int, weight: int) -> int:
    return (100 * earned_half_units + weight) // (2 * weight)


class BaselineScorer:
    def score(self, pair: MatchInput) -> MatchResult:
        facts = {normalize_key(fact.key): fact for fact in pair.profile.confirmed_facts}
        findings: list[Finding] = []
        conflicts: list[UUID] = []
        earned_half_units = assessed_weight = assessed_count = total_weight = 0
        source_access = pair.opportunity.source_access
        for item in sorted(pair.opportunity.requirements, key=lambda item: item.requirement_id):
            fact = facts.get(normalize_key(item.key))
            status = status_for(item, fact)
            weight: Literal[1, 2] = 2 if item.mandatory else 1
            total_weight += weight
            credit = {"satisfied": 2, "partial": 1, "missing": 0}.get(status)
            earned = credit * weight if credit is not None else None
            findings.append(
                Finding(
                    requirement_id=item.requirement_id,
                    statement=item.statement,
                    status=status,
                    fact_id=fact.fact_id if fact else None,
                    minimum_level=item.minimum_level,
                    confirmed_level=fact.level if fact else None,
                    hard_eligibility=item.hard_eligibility,
                    weight=weight,
                    earned_weight=earned / 2 if earned is not None else None,
                )
            )
            if earned is None:
                continue
            assessed_weight += weight
            assessed_count += 1
            earned_half_units += earned
            if item.hard_eligibility and status != "satisfied":
                conflicts.append(item.requirement_id)
        unknown_weight = total_weight - assessed_weight
        uncertainty: Uncertainty | None = None
        if source_access == "external_to_verify":
            uncertainty = "external_to_verify"
        elif not total_weight:
            uncertainty = "no_verified_requirements"
        elif unknown_weight:
            uncertainty = "unconfirmed_profile_facts"
        bounds = (
            ScoreBounds(
                minimum=_rounded_score(earned_half_units, total_weight),
                maximum=_rounded_score(earned_half_units + 2 * unknown_weight, total_weight),
            )
            if total_weight
            else None
        )
        return MatchResult(
            profile_snapshot_id=pair.profile.snapshot_id,
            opportunity_version_id=pair.opportunity.version_id,
            source_access=source_access,
            score=_rounded_score(earned_half_units, assessed_weight) if assessed_weight else None,
            findings=tuple(findings),
            hard_conflicts=tuple(conflicts),
            explanation=ScoreExplanation(
                assessed_requirements=assessed_count,
                total_requirements=len(findings),
                assessed_weight=assessed_weight,
                total_weight=total_weight,
                earned_weight=earned_half_units / 2,
                uncertainty=uncertainty,
                score_bounds=bounds,
            ),
        )
