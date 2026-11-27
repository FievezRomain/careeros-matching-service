from datetime import timedelta
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field

from careeros_matching.domain.scoring import (
    Contract,
    Key,
    MatchResult,
    OpportunityVersion,
    ProfileSnapshot,
    normalize_key,
)

RECENT = timedelta(days=30)
MATCHER_VERSION = "baseline-v1"
Decision = Literal["active", "unassessed", "inactive"]
Reason = Literal[
    "qualified",
    "paused",
    "ignored",
    "expired",
    "applied",
    "not_recent",
    "excluded",
    "criteria_mismatch",
    "criteria_unknown",
    "hard_conflict",
    "insufficient_evidence",
    "external_to_verify",
    "below_threshold",
]


class Criteria(Contract):
    criteria_version_id: UUID
    threshold: int = Field(strict=True, ge=0, le=100)
    active: bool = True
    target_roles: tuple[Key, ...] = Field(default=(), max_length=20)
    locations: tuple[Key, ...] = Field(default=(), max_length=20)
    remote_policy: Literal["any", "remote", "hybrid", "onsite"] = "any"
    contract_types: tuple[Key, ...] = Field(default=(), max_length=20)
    exclusions: tuple[Key, ...] = Field(default=(), max_length=30)


class Job(Contract):
    group_id: UUID
    opportunity: OpportunityVersion
    title: str = Field(min_length=1, max_length=200)
    status: Literal["active", "saved", "ignored", "expired", "applied"]
    employer: Key | None = None
    location: Key | None = None
    work_mode: Literal["remote", "hybrid", "onsite"] | None = None
    contract_type: Key | None = None


class ProfileProjection(Contract):
    revision: int
    observed_at: AwareDatetime
    profile: ProfileSnapshot
    criteria: Criteria


class JobProjection(Contract):
    revision: int
    observed_at: AwareDatetime
    job: Job
    source_digest: str | None = None
    correction_id: UUID | None = None
    correction_revision: int = 0


class Recommendation(Contract):
    recommendation_id: UUID
    evaluation_id: UUID
    group_id: UUID
    criteria_version_id: UUID
    correction_id: UUID | None
    title: str
    employer: str | None
    state: Decision
    reason: Reason
    updated_at: AwareDatetime
    match: MatchResult


def decision(
    profile: ProfileProjection, job: JobProjection, result: MatchResult
) -> tuple[Decision, Reason]:
    criteria, offer = profile.criteria, job.job
    if offer.status in ("ignored", "expired", "applied"):
        return "inactive", offer.status
    if not criteria.active:
        return "inactive", "paused"
    if profile.observed_at - job.observed_at > RECENT:
        return "inactive", "not_recent"
    title, employer = normalize_key(offer.title), normalize_key(offer.employer or "")
    if any(normalize_key(term) in (title, employer) for term in criteria.exclusions):
        return "inactive", "excluded"
    filters = (
        (criteria.target_roles, offer.title),
        (criteria.locations, offer.location),
        (criteria.contract_types, offer.contract_type),
        (() if criteria.remote_policy == "any" else (criteria.remote_policy,), offer.work_mode),
    )
    if any(
        allowed
        and value is not None
        and normalize_key(value) not in {normalize_key(term) for term in allowed}
        for allowed, value in filters
    ):
        return "inactive", "criteria_mismatch"
    if offer.opportunity.source_access == "external_to_verify":
        return "unassessed", "external_to_verify"
    if any(allowed and value is None for allowed, value in filters):
        return "unassessed", "criteria_unknown"
    if result.hard_conflicts:
        return "inactive", "hard_conflict"
    if result.score is None:
        return "unassessed", "insufficient_evidence"
    if result.score < criteria.threshold:
        return "inactive", "below_threshold"
    return "active", "qualified"
