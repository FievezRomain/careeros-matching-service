# CareerOS Matching Service â€” service overview

## Responsibility

Explainable matching between verified profiles and normalized job opportunities.

## Data ownership

Match runs, feature explanations and versioned scores.

## Interfaces

- Inputs: Profile snapshots, job snapshots and declared preferences.
- Outputs: Ranked matches with evidence and uncertainty.
- Dependencies: profile, job and AI gateway services.

## Contract rules

- APIs and events are versioned.
- Consumers must be idempotent where retries are possible.
- Correlation identifiers are propagated without logging sensitive payloads.
- Authorization and ownership are checked at the service boundary.

## Non-goals

This service does not absorb responsibilities owned by another CareerOS service. Cross-service workflows are coordinated through explicit contracts rather than shared databases.

## Security and privacy

Persist the minimum data required, record provenance, define retention, redact logs and use synthetic examples in this public repository.