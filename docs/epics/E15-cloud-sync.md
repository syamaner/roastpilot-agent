# E15 — Cloud sync

**Status:** open. A0 establishes this epic through documentation and state only.
The client feature is off. No runtime, activation, live cloud or hardware
completion is claimed. E15 is not the active epic; the existing E11 pointer
and its status remain unchanged.

## Goal

Record the approved Agent work for LA-1 (sync, sharing and reviews) and LA-2
(references) under D220. The pinned Plan's
[R1–R9 product requirements](https://github.com/syamaner/roastpilot-plan/blob/3e33bb140fe600740ea00b85046f2e4aef4ccc9c/cloud-sync-plan.md#2-settled-product-requirements-r1-r9)
govern the later implementation units; this epic adds no product decision.
E13 and E14 keep their D91 meanings for product agents.

## Plan links

- Governing [cloud-sync product contract](https://github.com/syamaner/roastpilot-plan/blob/3e33bb140fe600740ea00b85046f2e4aef4ccc9c/cloud-sync-plan.md),
  particularly [layering](https://github.com/syamaner/roastpilot-plan/blob/3e33bb140fe600740ea00b85046f2e4aef4ccc9c/cloud-sync-plan.md#4-layering-b2),
  [eligibility and the start marker](https://github.com/syamaner/roastpilot-plan/blob/3e33bb140fe600740ea00b85046f2e4aef4ccc9c/cloud-sync-plan.md#7-eligibility-and-the-test-run-marker-b9),
  [ordered delivery](https://github.com/syamaner/roastpilot-plan/blob/3e33bb140fe600740ea00b85046f2e4aef4ccc9c/cloud-sync-plan.md#11-ordered-per-repository-pr-plan-b7),
  [activation gates](https://github.com/syamaner/roastpilot-plan/blob/3e33bb140fe600740ea00b85046f2e4aef4ccc9c/cloud-sync-plan.md#12-activation-gates-b8)
  and [supersession index](https://github.com/syamaner/roastpilot-plan/blob/3e33bb140fe600740ea00b85046f2e4aef4ccc9c/cloud-sync-plan.md#13-supersession-index).
- [D220 definition in Cloud plan §1](https://github.com/syamaner/roastpilot-plan/blob/3e33bb140fe600740ea00b85046f2e4aef4ccc9c/roastpilot-cloud/plan.md#1-decisions-this-drill-down).
- [Agent component plan](https://github.com/syamaner/roastpilot-plan/blob/3e33bb140fe600740ea00b85046f2e4aef4ccc9c/roastpilot-agent/plan.md),
  §9 E15 row.

All Plan references use pin `3e33bb140fe600740ea00b85046f2e4aef4ccc9c`.
P0 merged as Plan PR #82 at that pin. These are planning sources, not
runtime or activation evidence.

## Unit identifiers

E1m and E1–E8 below are Plan §11 unit IDs inside E15, not Agent epics E01–E08.
The local unit and status rows are prefixed `E15-`. Dependency cells retain
Plan §11's unit IDs exactly; they do not redirect work to the original epics.
A0 is the documentation/state unit `E15-A0`.

## Intended architecture

This section records Plan §4's intended responsibilities and dependency direction
as documentation only. It introduces no implementation or new abstraction name.

- The domain covers eligibility, the versioned curated export, consent and
  per-field intent/acknowledgement merge. It has no SDK, database or UI dependency.
- The application layer owns outbox use, dispatch and refresh scheduling.
- Infrastructure adapters own SQLite persistence, D97 connector writes, HTTP
  review reads and process lifecycle.
- The Agent API and SPA consume application capabilities. The SPA renders server
  events and snapshots.
- Dependencies point inward; concrete wiring happens at the composition root.
  There is no generic synchronisation framework.

The later contracts own their boundary and behavioural proofs. The presence of
this architecture text does not establish a runtime boundary or implementation.

## Ordered Agent units

Each row is its own serial PR. Scope, dependencies and predicted reviewers below
are the pinned Plan §11 entries; the tracking issues are families rather than
one-PR implementation contracts.

| Order | Unit | Scope (Plan §11) | Depends on | Predicted reviewers | Tracking issue(s) | Status |
|---|---|---|---|---|---|---|
| 1 | E15-E1m | Start/API/SPA marker capture; immutable local persistence; no export field or control change | A0 | Safety, schema-migration, ui-reviewer, qa | #957 | not started |
| 2 | E15-E1 | Closed curated export, fail-closed eligibility including marker exclusion, consent and intent merge | A0, E1m, C1 contract | Security, qa, privacy | #957 | not started |
| 3 | E15-E2 | Durable latest-intent and acknowledgement outbox persistence | E1 | Qa, schema-migration | #958 | not started |
| 4 | E15-E3 | D97 Snowflake connector write adapter; credentials by env-var name only | E1, C2 contract | Security | #956, #958 | not started |
| 5 | E15-E4 | Idle admission and bounded, pre-emptible, reaped dispatcher lifetime | E2, E3, E1m | Safety, security | #956, #958 | not started |
| 6 | E15-E5 | Owner-action and status API | E4 | Security | #958 | not started |
| 7 | E15-E6 | SPA sync status, unsynced values, cloud deletion and Refresh now | E5 | Ui-reviewer | #958 | not started |
| 8 | E15-E7 | HTTP review-read adapter and atomic cache | E3, C3 | Security | #959 | not started |
| 9 | E15-E8 | Idle reference refresh and cached-only prepare | E3 | Safety, security | #959 | not started |

For E15-E1m, any real-roaster claim needs E12 supervised hardware acceptance.
A software test of a real-driver path does not establish that acceptance.

## Cross-repository prerequisites

This table records dependency contracts, not delivery evidence for Cloud units.
The lead has reconciled issue titles and bodies; this PR changes only local
epic/state documentation and performs no issue mutation.

| Unit | Scope (Plan §11) | Depends on | Current status |
|---|---|---|---|
| P0 | This requirements amendment | None | merged (Plan PR #82; pin 3e33bb140fe600740ea00b85046f2e4aef4ccc9c) |
| E15-A0 (Plan A0) | E15 epic and registry; reconcile #955 title and #956-#959 | P0 | this PR; docs/state only |
| C0 | C9 epic or state docs | P0 | Cloud repository; not tracked or verified here |
| C1 | Curated projections and schema: private operator-note field, consent, tombstones, version | C0 | Cloud repository; not tracked or verified here |
| C2 | Write ordering, acknowledgement and idempotency; cover or disable every accepted path | C1 | Cloud repository; not tracked or verified here |
| C3 | Reviews HTTP GET/ETag/pagination within the two-view boundary | C1 | Cloud repository; not tracked or verified here |
| C4 | Offline verifier and qualification procedure, delivery only | C2 | Cloud repository; not tracked or verified here |

E15-E1 waits on the C1 contract; E15-E3 waits on the C2 contract;
E15-E7 waits on C3. These supplement the local dependencies in the ordered table.
Cloud contracts precede any activated client. Every client unit ships feature-off.

## Tracking issue families

#955 is the open umbrella. The following family mapping is not one PR per issue;
each later Plan unit requires its own fresh contract and PR.

| Tracking issue | Agent units |
|---|---|
| #956 | E15-E3 and E15-E4's idle process-lifetime requirements |
| #957 | E15-E1m, E15-E1 |
| #958 | E15-E2, E15-E3, E15-E4, E15-E5, E15-E6 |
| #959 | E15-E7, E15-E8 |

## Activation gates

The feature stays off until all G1–G6 are met. Documents, mocks and software
delivery cannot satisfy these operator-only gates. No gate is performed here.
Unqualified server consistency fails closed, with no client-only fallback.

| Gate | Meaning (Plan §12) | Current status |
|---|---|---|
| G1 | Authorised synthetic DEV concurrency, role and procedure qualification. | operator-only; not performed |
| G2 | Deployed-caller and data compatibility checks. | operator-only; not performed |
| G3 | Target packaging and resource evidence. | operator-only; not performed |
| G4 | Credential provisioning. | operator-only; not performed |
| G5 | Deployment. | operator-only; not performed |
| G6 | Feature activation. | operator-only; not performed |

## Delivery rules

Each unit needs a fresh main and Plan SHA, an issue snapshot, a maintainer-ratified
story-planner contract, the applicable D218 validation route and an independent
review. An earlier contract or gate result does not authorise the next unit.

The actual diff may add reviewers but never remove a predicted reviewer.
Product audit happens at true story or epic completion, or when drift is suspected.
Naming a future unit's safety, security or other lens does not trigger that review
for this documentation-only A0 slice.

## Non-claims

No runtime, activation, live cloud, upload, credential, deployment or hardware
completion is claimed. E11, #954 and E11-S3 remain unchanged. #955 stays open
until all approved software scope is delivered and audited independently.
The existing active epic pointer and epic order remain unchanged.

## Status

| Story | Title | Status |
|---|---|---|
| E15-A0 | Epic and registry documentation/state | this PR; docs/state only |
| E15-E1m | Roast-start marker | not started |
| E15-E1 | Curated export, eligibility, consent and intent merge | not started |
| E15-E2 | Durable outbox persistence | not started |
| E15-E3 | D97 connector write adapter | not started |
| E15-E4 | Idle dispatcher and process lifetime | not started |
| E15-E5 | Owner API and status | not started |
| E15-E6 | SPA sync state and actions | not started |
| E15-E7 | HTTP review reads and cache | not started |
| E15-E8 | Idle reference refresh and cached-only prepare | not started |

Epic status: **open**. A0 is documentation/state only; no implementation unit
has started; the feature is off.
