"""Validated scoring evidence and the deterministic V1 policy."""

import re
import unicodedata
from typing import Annotated, Final, Literal, Self, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StringConstraints, model_validator
from pydantic.alias_generators import to_camel

Key = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Level = Annotated[int, Field(strict=True, ge=0, le=5)]
Months = Annotated[int, Field(strict=True, ge=0, le=1200)]
Cefr = Literal["A1", "A2", "B1", "B2", "C1", "C2"]
CEFR_RANK = {value: index for index, value in enumerate(("A1", "A2", "B1", "B2", "C1", "C2"))}
QualificationUnit = Literal["level", "experience_months", "cefr"]
QUALIFICATION_POLICY: Final = "explicit-requirements-v1"
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
    experience_months: Months | None = Field(default=None, exclude_if=lambda value: value is None)
    cefr: Cefr | None = Field(default=None, exclude_if=lambda value: value is None)


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


class Qualification(Contract):
    unit: QualificationUnit
    required: int | Cefr
    confirmed: int | Cefr | None
    status: Literal["met", "not_met", "unknown"]


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
    qualification: Qualification | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


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
    qualification_policy: Literal["explicit-requirements-v1"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    limitation: str = (
        "This score supports prioritization and does not guarantee an interview or employment."
    )


def status_for(requirement: Requirement, fact: Fact | None) -> Status:
    if fact is None:
        return "unknown"
    if fact.level >= requirement.minimum_level:
        return "satisfied"
    return "partial" if fact.level > 0 else "missing"


AMBIGUOUS = re.compile(
    r";|\b(?:or|ou|and|et|either|between|entre|maximum|max|under|overall|including|dont)\b|"
    r"\b(?:up to|at most|less than|au plus|moins de|no minimum|not required|non requis)\b|"
    r"\b\d+\s*(?:to|à|[-–—])\s*\d+\b"
)
DURATION = re.compile(r"(?<![0-9.,-])([1-9][0-9]?)\s*\+?\s*(years?|ans?|années?|months?|mois)\b")
EXPERIENCE_WORD = re.compile(r"\b(?:experience|expérience|expériences)\b")
SCALED_LEVEL = re.compile(r"\b(?:level|niveau)\s*([1-5])\s*/\s*5\b")
CEFR_LEVEL = re.compile(r"\b(?:a1|a2|b1|b2|c1|c2)\b")
LANGUAGE = re.compile(
    r"\b(?:cefr|cecrl|english|anglais|french|français|german|allemand|"
    r"spanish|espagnol|italian|italien|portuguese|portugais|dutch|néerlandais)\b"
)
BEFORE_MINIMUM = re.compile(
    r"[\s:(–—-]*(?:(?:at least|minimum(?: level)?|au moins|niveau(?: minimum)?)\s+)?"
)
AFTER_MINIMUM = re.compile(
    r"[\s:–—-]*(?:(?:of experience (?:in|with|using)|"
    r"d['’]expérience (?:en|avec|sur)|in|with|using|en|avec|sur)\s+)?"
)


def _linked(statement: str, key: re.Match[str], threshold: re.Match[str]) -> bool:
    if key.end() <= threshold.start():
        return BEFORE_MINIMUM.fullmatch(statement[key.end() : threshold.start()]) is not None
    if threshold.end() <= key.start():
        return AFTER_MINIMUM.fullmatch(statement[threshold.end() : key.start()]) is not None
    return False


def explicit_threshold(requirement: Requirement) -> tuple[QualificationUnit, int | Cefr] | None:
    statement = normalize_key(requirement.statement)
    key = re.escape(normalize_key(requirement.key))
    keys = list(re.finditer(rf"(?<![\w+#]){key}(?![\w+#])", statement))
    if "\n" in requirement.statement or AMBIGUOUS.search(statement) or len(keys) != 1:
        return None
    matches: list[tuple[QualificationUnit, re.Match[str]]] = [
        ("experience_months", match)
        for match in DURATION.finditer(statement)
        if EXPERIENCE_WORD.search(statement)
    ]
    matches.extend(("level", match) for match in SCALED_LEVEL.finditer(statement))
    if LANGUAGE.search(normalize_key(requirement.key)) or re.search(
        r"\b(?:cefr|cecrl)\b", statement
    ):
        matches.extend(("cefr", match) for match in CEFR_LEVEL.finditer(statement))
    if len(matches) != 1:
        return None
    unit, match = matches[0]
    if not _linked(statement, keys[0], match):
        return None
    if unit == "cefr":
        return unit, cast(Cefr, match.group().upper())
    value = int(match.group(1))
    if unit == "experience_months" and match.group(2).startswith(("year", "an")):
        value *= 12
    return unit, value


def assess_qualification(
    threshold: tuple[QualificationUnit, int | Cefr], fact: Fact | None
) -> Qualification:
    unit, required = threshold
    confirmed: int | Cefr | None = None
    if fact is not None:
        if unit == "experience_months":
            confirmed = fact.experience_months
        elif unit == "cefr":
            confirmed = fact.cefr
        else:
            confirmed = fact.level
    status: Literal["met", "not_met", "unknown"] = "unknown"
    if isinstance(confirmed, int) and isinstance(required, int):
        status = "met" if confirmed >= required else "not_met"
    elif isinstance(confirmed, str) and isinstance(required, str):
        status = "met" if CEFR_RANK[confirmed] >= CEFR_RANK[required] else "not_met"
    return Qualification(unit=unit, required=required, confirmed=confirmed, status=status)


QUALIFICATION_STATUS: dict[Literal["met", "not_met", "unknown"], Status] = {
    "met": "satisfied",
    "not_met": "partial",
    "unknown": "unknown",
}


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
            threshold = explicit_threshold(item)
            qualification = assess_qualification(threshold, fact) if threshold is not None else None
            status = (
                QUALIFICATION_STATUS[qualification.status]
                if qualification
                else status_for(item, fact)
            )
            hard = item.hard_eligibility or (qualification is not None and item.mandatory)
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
                    hard_eligibility=hard,
                    weight=weight,
                    earned_weight=earned / 2 if earned is not None else None,
                    qualification=qualification,
                )
            )
            if earned is None:
                continue
            assessed_weight += weight
            assessed_count += 1
            earned_half_units += earned
            if hard and status != "satisfied":
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
            qualification_policy=(
                QUALIFICATION_POLICY
                if any(finding.qualification is not None for finding in findings)
                else None
            ),
        )
