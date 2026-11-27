"""Recommendation projection, versioned scoring, and transactional event intent."""

from contextlib import AbstractContextManager
from hashlib import sha256
from typing import Annotated, Literal, Protocol
from uuid import UUID, uuid5

from pydantic import AwareDatetime, Field

from careeros_matching.application.extraction import (
    EXTRACTOR_VERSION,
    ExtractionInput,
    ExtractionResult,
    ExtractRequirements,
    SourceText,
)
from careeros_matching.application.scoring import (
    Actor,
    Denied,
    Scorer,
    SnapshotNotFound,
    require_access,
)
from careeros_matching.domain.recommendation import (
    MATCHER_VERSION,
    RECENT,
    Criteria,
    Job,
    JobProjection,
    ProfileProjection,
    Recommendation,
    decision,
)
from careeros_matching.domain.scoring import (
    Contract,
    MatchInput,
    MatchResult,
    Owner,
    ProfileSnapshot,
)


class Update(Contract):
    event_id: UUID
    occurred_at: AwareDatetime
    revision: int = Field(strict=True, ge=1)


class ProfileUpdate(Update):
    kind: Literal["profile"]
    profile: ProfileSnapshot
    criteria: Criteria
    contact_only: bool = False


class JobUpdate(Update):
    kind: Literal["opportunity", "correction"]
    job: Job
    source: SourceText | None = None


ProjectionUpdate = Annotated[ProfileUpdate | JobUpdate, Field(discriminator="kind")]


class UpdateResult(Contract):
    outcome: Literal["applied", "duplicate", "stale", "irrelevant"] = "applied"
    evaluated: int = 0
    emitted: int = 0


class Receipt(Contract):
    digest: str


class CachedExtraction(Contract):
    digest: str
    result: ExtractionResult


class EventData(Owner):
    recommendation_id: UUID
    opportunity_version_id: UUID
    profile_snapshot_id: UUID
    criteria_version_id: UUID


class RecommendationEvent(Contract):
    specversion: Literal["1.0"] = "1.0"
    id: UUID
    source: Literal["urn:careeros:matching-service"] = "urn:careeros:matching-service"
    type: Literal["com.careeros.recommendation.created.v1"] = (
        "com.careeros.recommendation.created.v1"
    )
    time: AwareDatetime
    correlationid: UUID
    datacontenttype: Literal["application/json"] = "application/json"
    data: EventData


class RecommendationPage(Contract):
    items: tuple[Recommendation, ...]


class EventPage(Contract):
    items: tuple[RecommendationEvent, ...]


class ProjectionConflict(Exception):
    pass


class ProjectionInvalid(Exception):
    pass


class RecommendationTransaction(Protocol):
    def get[T: Contract](self, model: type[T], key: str) -> T | None: ...
    def save(self, key: str, value: Contract) -> None: ...
    def all[T: Contract](self, model: type[T]) -> tuple[T, ...]: ...
    def extraction(self, key: str) -> CachedExtraction | None: ...
    def save_extraction(self, key: str, extraction: CachedExtraction) -> None: ...


class RecommendationStore(Protocol):
    def transaction(self, owner: Owner) -> AbstractContextManager[RecommendationTransaction]: ...


class HistoryRow(Contract):
    recommendation: Recommendation


def _digest(value: Contract) -> str:
    return sha256(value.model_dump_json().encode()).hexdigest()


def _remember_snapshot(tx: RecommendationTransaction, key: str, snapshot: Contract) -> None:
    digest = _digest(snapshot)
    previous = tx.get(Receipt, key)
    if previous is None:
        tx.save(key, Receipt(digest=digest))
    elif previous.digest != digest:
        raise ProjectionConflict


class Recommendations:
    def __init__(
        self, store: RecommendationStore, scorer: Scorer, extractor: ExtractRequirements
    ) -> None:
        self._store, self._scorer, self._extractor = store, scorer, extractor

    def apply(self, actor: Actor, event: ProjectionUpdate, correlation_id: UUID) -> UpdateResult:
        owner = event.profile if isinstance(event, ProfileUpdate) else event.job.opportunity
        scope = "correct" if event.kind == "correction" else "write"
        require_access(actor, f"matching:recommendations:{scope}", owner)
        key, digest = str(event.event_id), _digest(event)
        with self._store.transaction(actor) as transaction:
            receipt = transaction.get(Receipt, key)
            if receipt is not None:
                if receipt.digest != digest:
                    raise ProjectionConflict
                return UpdateResult(outcome="duplicate")
            result = self._project(transaction, event, correlation_id)
            transaction.save(key, Receipt(digest=digest))
            return result

    def _project(
        self, tx: RecommendationTransaction, event: ProjectionUpdate, correlation: UUID
    ) -> UpdateResult:
        profile = tx.get(ProfileProjection, "current")
        if isinstance(event, ProfileUpdate):
            if event.contact_only:
                return UpdateResult(outcome="irrelevant")
            if profile is not None and event.revision <= profile.revision:
                return UpdateResult(outcome="stale")
            _remember_snapshot(tx, f"profile:{event.profile.snapshot_id}", event.profile)
            _remember_snapshot(
                tx,
                f"criteria:{event.criteria.criteria_version_id}",
                event.criteria.model_copy(update={"active": True}),
            )
            profile = ProfileProjection(
                revision=event.revision,
                observed_at=event.occurred_at,
                profile=event.profile,
                criteria=event.criteria,
            )
            tx.save("current", profile)
            jobs = tx.all(JobProjection)
        else:
            job = self._job(tx, event, correlation)
            if isinstance(job, UpdateResult):
                return job
            jobs = (job,)
        if profile is None:
            return UpdateResult()
        evaluated = emitted = 0
        for job in jobs:
            if isinstance(event, ProfileUpdate) and job.job.status in (
                "ignored",
                "expired",
                "applied",
            ):
                continue
            computed, qualified = self._evaluate(tx, profile, job, event, correlation)
            evaluated += computed
            emitted += qualified
        return UpdateResult(evaluated=evaluated, emitted=emitted)

    def _job(
        self, tx: RecommendationTransaction, event: JobUpdate, correlation: UUID
    ) -> JobProjection | UpdateResult:
        key = str(event.job.group_id)
        current = tx.get(JobProjection, key)
        offer = event.job.opportunity
        digest = _digest(event.source) if event.source is not None else None
        same_version = (
            current is not None and current.job.opportunity.version_id == offer.version_id
        )
        if event.kind == "correction":
            if (
                not same_version
                or current is None
                or event.source is None
                or digest != current.source_digest
            ):
                raise ProjectionConflict
            if event.revision <= current.correction_revision:
                return UpdateResult(outcome="stale")
            self._validate_source(event)
            corrected = current.job.model_copy(update={"opportunity": offer})
            projection = current.model_copy(
                update={
                    "job": corrected,
                    "correction_id": event.event_id,
                    "correction_revision": event.revision,
                }
            )
        elif current is not None and event.revision <= current.revision:
            return UpdateResult(outcome="stale")
        elif same_version and current is not None:
            if digest is not None and digest != current.source_digest:
                raise ProjectionConflict
            _remember_snapshot(
                tx,
                f"opportunity:{offer.version_id}",
                event.job.model_copy(update={"status": "active"}),
            )
            job = current.job.model_copy(update={"status": event.job.status})
            projection = current.model_copy(update={"revision": event.revision, "job": job})
            if job.status == current.job.status:
                tx.save(key, projection)
                return UpdateResult()
        else:
            self._validate_source(event)
            _remember_snapshot(
                tx,
                f"opportunity:{offer.version_id}",
                event.job.model_copy(update={"status": "active"}),
            )
            if event.source is not None and digest is not None:
                self._cache_extraction(tx, event, event.source, digest, correlation)
            projection = JobProjection(
                revision=event.revision,
                observed_at=event.occurred_at,
                job=event.job,
                source_digest=digest,
            )
        tx.save(key, projection)
        return projection

    @staticmethod
    def _validate_source(event: JobUpdate) -> None:
        offer, source = event.job.opportunity, event.source
        if (offer.source_access == "full_text") != (source is not None):
            raise ProjectionInvalid
        if source is not None and any(
            item.statement not in source.job_text for item in offer.requirements
        ):
            raise ProjectionInvalid

    def _cache_extraction(
        self,
        tx: RecommendationTransaction,
        event: JobUpdate,
        source: SourceText,
        digest: str,
        correlation: UUID,
    ) -> None:
        offer = event.job.opportunity
        key = f"{offer.version_id}:{EXTRACTOR_VERSION}"
        cached = tx.extraction(key)
        if cached is None:
            command = ExtractionInput(
                account_id=offer.account_id,
                tenant_id=offer.tenant_id,
                opportunity_version_id=offer.version_id,
                source_access=offer.source_access,
                job_text=source.job_text,
                source_language=source.source_language,
            )
            shared_key = sha256(f"{offer.tenant_id}:{key}:{digest}".encode()).hexdigest()
            tx.save_extraction(
                key,
                CachedExtraction(
                    digest=digest,
                    result=self._extractor.extract(command, correlation, shared_key=shared_key),
                ),
            )
        elif cached.digest != digest:
            raise ProjectionConflict

    def _evaluate(
        self,
        tx: RecommendationTransaction,
        profile: ProfileProjection,
        job: JobProjection,
        event: ProjectionUpdate,
        correlation: UUID,
    ) -> tuple[int, int]:
        offer, owner = job.job.opportunity, profile.profile
        group_id = job.job.group_id
        prior = tx.get(Recommendation, str(group_id))
        basis = (
            f"{offer.version_id}:{owner.snapshot_id}:"
            f"{profile.criteria.criteria_version_id}:{MATCHER_VERSION}"
        )
        if job.correction_id is not None:
            basis += f":{job.correction_id}"
        evaluation_id = uuid5(owner.account_id, f"{owner.tenant_id}:{basis}")
        stale_projection = (
            isinstance(event, ProfileUpdate) and profile.observed_at - job.observed_at > RECENT
        )
        if stale_projection and (prior is None or prior.state == "inactive"):
            return 0, 0
        match: MatchResult | None
        if stale_projection and prior is not None:
            evaluation_id, match = prior.evaluation_id, prior.match
        else:
            match = tx.get(MatchResult, str(evaluation_id))
        computed = int(match is None)
        if match is None:
            match = self._scorer.score(MatchInput(profile=owner, opportunity=offer))
            tx.save(str(evaluation_id), match)
        state, reason = decision(profile, job, match)
        if prior is not None and (prior.evaluation_id, prior.state, prior.reason) == (
            evaluation_id,
            state,
            reason,
        ):
            return computed, 0
        recommendation_id = uuid5(owner.account_id, f"{owner.tenant_id}:{group_id}")
        recorded_at = (
            max(event.occurred_at, prior.updated_at) if prior is not None else event.occurred_at
        )
        record = Recommendation(
            recommendation_id=recommendation_id,
            evaluation_id=evaluation_id,
            group_id=group_id,
            criteria_version_id=profile.criteria.criteria_version_id,
            correction_id=job.correction_id,
            title=job.job.title,
            employer=job.job.employer,
            state=state,
            reason=reason,
            updated_at=recorded_at,
            match=match,
        )
        tx.save(str(group_id), record)
        evidence_id = uuid5(recommendation_id, str(event.event_id))
        tx.save(str(evidence_id), HistoryRow(recommendation=record))
        if state != "active" or (prior is not None and prior.state == "active"):
            return computed, 0
        tx.save(
            str(evidence_id),
            RecommendationEvent(
                id=evidence_id,
                time=recorded_at,
                correlationid=correlation,
                data=EventData(
                    account_id=owner.account_id,
                    tenant_id=owner.tenant_id,
                    recommendation_id=recommendation_id,
                    opportunity_version_id=offer.version_id,
                    profile_snapshot_id=owner.snapshot_id,
                    criteria_version_id=profile.criteria.criteria_version_id,
                ),
            ),
        )
        return computed, 1

    def listing(self, actor: Actor, limit: int, offset: int) -> RecommendationPage:
        if "matching:recommendations:read" not in actor.scopes:
            raise Denied
        with self._store.transaction(actor) as tx:
            items = [item for item in tx.all(Recommendation) if item.state != "inactive"]
        items.sort(
            key=lambda item: (item.state != "active", -(item.match.score or 0), item.group_id)
        )
        return RecommendationPage(items=tuple(items[offset : offset + limit]))

    def detail(self, actor: Actor, group_id: UUID) -> Recommendation:
        if "matching:recommendations:read" not in actor.scopes:
            raise Denied
        with self._store.transaction(actor) as tx:
            item = tx.get(Recommendation, str(group_id))
        if item is None:
            raise SnapshotNotFound
        return item

    def history(self, actor: Actor, group_id: UUID, limit: int, offset: int) -> RecommendationPage:
        self.detail(actor, group_id)
        with self._store.transaction(actor) as tx:
            items = tuple(
                row.recommendation
                for row in tx.all(HistoryRow)
                if row.recommendation.group_id == group_id
            )
        return RecommendationPage(items=items[offset : offset + limit])

    def events(self, actor: Actor, limit: int, offset: int) -> EventPage:
        if "matching:recommendations:deliver" not in actor.scopes:
            raise Denied
        with self._store.transaction(actor) as tx:
            return EventPage(items=tx.all(RecommendationEvent)[offset : offset + limit])

    def extraction_evidence(self, actor: Actor, group_id: UUID) -> ExtractionResult:
        if "matching:recommendations:read" not in actor.scopes:
            raise Denied
        with self._store.transaction(actor) as tx:
            projection = tx.get(JobProjection, str(group_id))
            if projection is None:
                raise SnapshotNotFound
            offer = projection.job.opportunity
            if offer.source_access == "external_to_verify":
                return ExtractionResult(
                    opportunity_version_id=offer.version_id, status="external_to_verify"
                )
            result = tx.extraction(f"{offer.version_id}:{EXTRACTOR_VERSION}")
            if result is None or result.digest != projection.source_digest:
                raise SnapshotNotFound
            return result.result
