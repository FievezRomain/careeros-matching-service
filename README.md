# CareerOS Matching Service

Explainable matching between verified profiles and normalized job opportunities.

## Scope

- **Owns:** Match runs, feature explanations and versioned scores.
- **Inputs:** Profile snapshots, job snapshots and declared preferences.
- **Outputs:** Ranked matches with evidence and uncertainty.
- **Dependencies:** profile, job and AI gateway services.

## Safety baseline

This public repository must never contain secrets, real resumes, real applications or personal data. External communication and application submission always require explicit user approval.

## Documentation

See [docs/overview.md](docs/overview.md) and the central [CareerOS workspace](https://github.com/FievezRomain/careeros-workspace).

## Status

Architecture and security baseline. Functional implementation will be added incrementally with tests and versioned contracts.

## License

Apache-2.0.