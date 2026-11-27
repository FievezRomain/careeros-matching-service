from collections.abc import Callable
from typing import Annotated
from uuid import UUID

from fastapi import FastAPI, HTTPException, Query, Request, Security

from careeros_matching.application.extraction import (
    ExtractionResponseInvalid,
    ExtractionResult,
    ExtractionUnavailable,
)
from careeros_matching.application.recommendation import (
    EventPage,
    ProjectionConflict,
    ProjectionInvalid,
    ProjectionUpdate,
    RecommendationPage,
    Recommendations,
    UpdateResult,
)
from careeros_matching.application.scoring import Actor, Denied, SnapshotNotFound
from careeros_matching.domain.recommendation import Recommendation

Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0)]


def mount_recommendations(
    app: FastAPI, authenticate: Callable[..., Actor], service: Recommendations | None
) -> None:
    def invoke[T](action: Callable[[Recommendations], T]) -> T:
        if service is None:
            raise HTTPException(503, "RECOMMENDATIONS_UNAVAILABLE")
        try:
            return action(service)
        except Denied:
            raise HTTPException(403) from None
        except SnapshotNotFound:
            raise HTTPException(404) from None
        except ProjectionConflict:
            raise HTTPException(409) from None
        except ProjectionInvalid:
            raise HTTPException(422) from None
        except ExtractionResponseInvalid:
            raise HTTPException(502) from None
        except ExtractionUnavailable:
            raise HTTPException(503) from None

    @app.post("/v1/internal/recommendation-updates", response_model=UpdateResult)
    def update(
        event: ProjectionUpdate,
        request: Request,
        actor: Annotated[Actor, Security(authenticate, scopes=["matching:recommendations:write"])],
    ) -> UpdateResult:
        if event.kind == "correction":
            raise HTTPException(422)
        return invoke(lambda handler: handler.apply(actor, event, request.state.correlation_id))

    @app.post("/v1/internal/recommendation-corrections", response_model=UpdateResult)
    def correct(
        event: ProjectionUpdate,
        request: Request,
        actor: Annotated[
            Actor, Security(authenticate, scopes=["matching:recommendations:correct"])
        ],
    ) -> UpdateResult:
        if event.kind != "correction":
            raise HTTPException(422)
        return invoke(lambda handler: handler.apply(actor, event, request.state.correlation_id))

    @app.get("/v1/recommendations", response_model=RecommendationPage)
    def listing(
        actor: Annotated[Actor, Security(authenticate, scopes=["matching:recommendations:read"])],
        limit: Limit = 50,
        offset: Offset = 0,
    ) -> RecommendationPage:
        return invoke(lambda handler: handler.listing(actor, limit, offset))

    @app.get("/v1/recommendations/{group_id}", response_model=Recommendation)
    def detail(
        group_id: UUID,
        actor: Annotated[Actor, Security(authenticate, scopes=["matching:recommendations:read"])],
    ) -> Recommendation:
        return invoke(lambda handler: handler.detail(actor, group_id))

    @app.get("/v1/recommendations/{group_id}/extraction", response_model=ExtractionResult)
    def extraction(
        group_id: UUID,
        actor: Annotated[Actor, Security(authenticate, scopes=["matching:recommendations:read"])],
    ) -> ExtractionResult:
        return invoke(lambda handler: handler.extraction_evidence(actor, group_id))

    @app.get("/v1/recommendations/{group_id}/history", response_model=RecommendationPage)
    def history(
        group_id: UUID,
        actor: Annotated[Actor, Security(authenticate, scopes=["matching:recommendations:read"])],
        limit: Limit = 50,
        offset: Offset = 0,
    ) -> RecommendationPage:
        return invoke(lambda handler: handler.history(actor, group_id, limit, offset))

    @app.get("/v1/internal/recommendation-events", response_model=EventPage)
    def events(
        actor: Annotated[
            Actor, Security(authenticate, scopes=["matching:recommendations:deliver"])
        ],
        limit: Limit = 50,
        offset: Offset = 0,
    ) -> EventPage:
        return invoke(lambda handler: handler.events(actor, limit, offset))
