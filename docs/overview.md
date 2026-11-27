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

## Contract rules

- APIs and events are versioned.
- Consumers must be idempotent where retries are possible.
- Correlation identifiers are propagated without logging sensitive payloads.
- Authorization and ownership are checked at the service boundary.

## Non-goals

This service does not absorb responsibilities owned by another CareerOS service. Cross-service workflows are coordinated through explicit contracts rather than shared databases.

## Security and privacy

Persist the minimum data required, record provenance, define retention, redact logs and use synthetic examples in this public repository.
