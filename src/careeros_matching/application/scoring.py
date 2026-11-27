from typing import Protocol

from pydantic import Field

from careeros_matching.domain.scoring import MatchInput, MatchResult, Owner


class Actor(Owner):
    scopes: tuple[str, ...] = Field(default=(), max_length=100)


class Denied(Exception):
    pass


class SnapshotNotFound(Exception):
    pass


class Scorer(Protocol):
    def score(self, pair: MatchInput) -> MatchResult: ...


class ScoreMatch:
    def __init__(self, scorer: Scorer) -> None:
        self._scorer = scorer

    def execute(self, actor: Actor, pair: MatchInput) -> MatchResult:
        if "matching:score" not in actor.scopes:
            raise Denied
        owner = (actor.account_id, actor.tenant_id)
        if any(
            (item.account_id, item.tenant_id) != owner for item in (pair.profile, pair.opportunity)
        ):
            raise SnapshotNotFound
        return self._scorer.score(pair)
