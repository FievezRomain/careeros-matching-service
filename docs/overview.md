# CareerOS Matching Service â€” service overview

## Responsibility

Explainable matching between verified profiles and normalized job opportunities.

## Data ownership

Match runs, feature explanations and versioned scores.

## Interfaces

- Inputs: Profile snapshots, job snapshots and declared preferences.
- Outputs: Ranked matches with evidence and uncertainty.
- Dependencies: profile, job and AI gateway services.

### V1-023 scoring

Install with `uv sync --locked --all-groups`, configure the OIDC values in
`.env.example` and run `uv run uvicorn careeros_matching.adapters.http:create_app --factory`.
`GET /health/live`, `GET /health/ready` and `/openapi.json` expose operational and contract data.

`POST /v1/internal/matches/score` requires an account-bound RS256 bearer token with
`matching:score`. A trusted caller projects both immutable snapshots into `profile`
and `opportunity`. Each supplies `accountId` and `tenantId`, which must match the token.
Foreign snapshots return `404`; invalid input returns `422` with Problem Details.

The profile carries `snapshotId` and `confirmedFacts` with `factId`, `key` and integer
`level` (0–5). The opportunity carries `versionId`, `sourceAccess` and `requirements`
with `requirementId`, `key`, `minimumLevel` (1–5), `mandatory`, `statement` and optional
`hardEligibility`. The caller maps only confirmed profile facts and source-stated job
requirements. The service uses exact normalized keys and performs no extraction.

An absent fact is unknown. Explicit level zero is missing. A known level above zero
but below the minimum is partial. Requirements earn 100%, 50%, or 0% when satisfied,
partial, or missing. Mandatory findings weigh twice as much as optional ones. Unknown
items are excluded from scoring and the result is null when no item can be assessed.
The weighted average rounds to the nearest integer with ties up. Known unsatisfied hard
eligibility requirements appear separately. `external_to_verify` returns a null score
and an empty findings list because it supplies no verified requirements.

Results carry the input versions, `baseline-v1`, requirement statements, confirmed fact
IDs where available, and a prioritization limitation. Replaying the same input is safe.
Calculations are stateless and logs contain correlation and status only. V1-024 handles
extraction, V1-025 expands explanations and V1-058 owns recommendation orchestration.

### V1-024 requirement extraction

`POST /v1/internal/requirements/extract` requires `matching:extract` and the
same account and tenant as the supplied `opportunityVersionId`. For `full_text`,
provide `jobText` (at most 30,000 characters) and `sourceLanguage` (e.g. `fr-BE`).
For `external_to_verify`, omit `jobText`. The result leaves the link unassessed.

Set `AI_GATEWAY_URL` and inject `AI_GATEWAY_TOKEN` with scope `ai:invoke`. The
gateway receives the text and language using `extract-job-requirements` v1.0.
It owns provider redaction. Matching propagates correlation and a stable key
derived from the owner, version, extractor and input. The HTTP timeout defaults
to 45 seconds. Matching keeps no response cache and performs no automatic retries.

`requirements-v1` returns suggestions only when every quoted excerpt occurs in
the source and the requirement text occurs in an excerpt. `partial` reports
discarded suggestions. `inconclusive` means none could be retained. The result
records the opportunity, extractor, model and prompt versions and available usage
metadata. A bad gateway response returns `502`; unavailability returns `503`.
Suggestions remain separate from the confirmed requirement input to scoring.

### Explicit qualification minima

Verified standalone requirement statements can carry a whole number of months
or years of experience, a level such as `4/5`, or a CEFR language level. The
threshold must be adjacent to the named criterion, as in `Python: at least 5
years of experience`, `SQL level 4/5` or `English B2 required`. Ambiguous ranges,
alternatives, and upper bounds keep their existing non-quantitative assessment.

The projector may supply confirmed `experienceMonths` and `cefr` on the fact
for the same criterion. A skill level, a mention or an unrelated dated role
does not supply a duration or CEFR value. Matching exposes the required and
confirmed values and the qualification status in the finding. A known
shortfall on a mandatory threshold produces a hard conflict. An unconfirmed
value stays unknown and is excluded from scoring. New evaluations with these
thresholds use `qualificationPolicy: explicit-requirements-v1`.

### V1-025 score explanation

The scoring response adds `explanation` version `evidence-v1`. Each finding
includes the minimum level, confirmed level or null, eligibility flag, weight
and earned weight. A confirmed missing requirement earns zero. An unknown one
has null earned weight and is excluded from the assessed weight and score.

The explanation shows assessed and total requirement counts and weights, and
the earned weight. `scoreBounds` is a hypothetical interval for the same listed
requirements. The lower endpoint assigns zero credit to every unknown weight,
and the upper endpoint assigns full credit. Both use the usual integer rounding.
This interval describes missing evidence; it is not a probability or forecast.
With all requirements assessed, both endpoints equal the score and `uncertainty`
is null. With some evidence absent, `uncertainty` is `unconfirmed_profile_facts`.
With no verified requirements, the bounds are null and `uncertainty` is either
`no_verified_requirements` or `external_to_verify`.

### V1-058 recommendations

Set `MATCHING_DATABASE_URL` to the service-owned PostgreSQL database. Startup
applies and verifies the adapter-owned schema before accepting requests.
Account-bound projectors use `POST /v1/internal/recommendation-updates` with
`matching:recommendations:write`. A `profile` update carries the confirmed
snapshot and active criteria. An `opportunity` update carries its stable group,
immutable version, state and verified scoring requirements. A new full-text
version includes source text and language. An unchanged version can update its
lifecycle state and reuses the prior verified requirements. Contact changes are
acknowledged without rematching. Each stream supplies a monotonic revision and an
event ID. Replays have no side effects, and conflicting reuse returns `409`.

The matching database keeps validated evidence and a source fingerprint. A
successful extraction is cached by tenant, opportunity and extractor version.
A retry after an interrupted upstream call reuses the same gateway key.
Requirement suggestions remain available for inspection through
`GET /v1/recommendations/{groupId}/extraction`. Matching scores only requirements
grounded in the owned full-text source. Corrections carry revised requirements
and that same source through `POST /v1/internal/recommendation-corrections` with
`matching:recommendations:correct`. The correction records a separate evidence
revision and preserves both the canonical job version and the older result.

Profile changes rematch actionable opportunities observed in the preceding 30
days. Exact normalized role, location, contract and remote values apply when
configured, alongside exclusions and the score threshold. Unknown criteria and
link-only jobs remain unassessed. `GET /v1/recommendations` returns active results
by score followed by unassessed results. Owned detail and history at
`/v1/recommendations/{groupId}` and `/history` expose inactive decisions and earlier
evidence. All three operations require `matching:recommendations:read`.

Each inactive-to-active transition commits one CloudEvent with minimal identifiers.
`GET /v1/internal/recommendation-events` provides the append-only, owned outbox
with `matching:recommendations:deliver`. List and history endpoints accept `limit`
and `offset`. A relay advances its offset after downstream delivery is confirmed
and consumers deduplicate using the event ID. The event expresses a matching
decision and the delivery workflow owns channel preferences and approvals.

## Contract rules

- APIs and events are versioned.
- Consumers must be idempotent where retries are possible.
- Correlation identifiers are propagated without logging sensitive payloads.
- Authorization and ownership are checked at the service boundary.

## Non-goals

This service does not absorb responsibilities owned by another CareerOS service. Cross-service workflows are coordinated through explicit contracts rather than shared databases.

## Security and privacy

Persist the minimum data required, record provenance, define retention, redact logs and use synthetic examples in this public repository.
