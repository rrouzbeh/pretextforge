# Input and output contracts

## Scenario

`scenarios/acme.yaml` is the full example. Unknown fields, duplicate YAML keys,
duplicate identities/action IDs, invalid manager references, overlapping scripted
lifecycles for one user and out-of-window actions fail validation.

Top-level fields: `version: "1"`, `name`, `seed`, UTC `start`, `workdays` (1–5 within
one Monday–Friday span), `users`, `helpdesk_agent`, unique `helpdesk_number`, `policy`,
`baseline`, `observation` (`complete`/`degraded`) and `actions`. Timestamps must be timezone-aware; action times
are normalized to UTC. No includes or executable YAML extensions are supported.

Users have nonblank `id`/`name`, unique `phone`, optional `manager` and `department`.
Phones use `+` and 7–15 digits with optional spaces/dashes; uniqueness compares digits.
The `routine-` action-ID prefix is reserved for generated support cases. Action times
are checked before sorting and normalized to UTC. Approved recovery followed by denial
is outside the modeled flow and is rejected; VPN does not accept recovery denial options.
Policy specifies `recovery_requires: manager_approval`, `bulk_download_threshold`
and `departure_watch_days`. Baseline specifies mean daily logins/files per user and
mean daily support conversations. Scoped seeded draws vary these by user and day.
Set all three counts to zero for a minimal fixture.

Actions share `id`, `at`, `user`, `type` and `malicious` (default false).

| Type | Additional fields |
| --- | --- |
| `helpdesk` | `true_actor` (private), `channel` (`call`/`chat`), `request` (`mfa_reset`/`vpn`), `verification` (`approved`/`refused`/`not_attempted`), `execute`, `urgent`, optional `caller_number`, `new_number`, `pretext` (`lost_phone`/`replacement_device`) |
| `files` | `departure`, `approved_migration`, `count`, `recipient` |

The `malicious` flag controls labels only. The same procedural behavior may be
malicious or legitimate. Approval and intent are independent: an approved action
can be labeled malicious without exposing that label in public records. A verified reset requires a registered
manager. A file migration is authorized by the configured helpdesk agent.

## Public evidence

Every JSONL row has `record_id`, `source`, `correlation_id`, `event_time`, `available_at`
and typed `data`. UTC timestamps have fixed microsecond precision. Record/correlation
IDs are opaque hashes with no attack prefixes. A correlation ID joins one lifecycle.
Ordering is `(available_at, event_time, record_id)`. The stable tie-break preserves
collection order independently of the chronology of simulated actions.

| Source | `data` fields |
| --- | --- |
| IdP | event type, actor, user, outcome, factor/destination, authenticated session/device/IP |
| ITSM | ticket ID, user, action, category, optional approver |
| Telephony | call ID, user, start/end type, endpoints, duration only at end |
| HR | user, notice status, last day |
| SaaS files | user, download/share action, object ID, bytes, optional recipient/approval ticket, session/device/IP |
| Transcripts | conversation ID, user, channel, caller/agent speaker, turn index, text, speech duration, explicit wait/reason |

`models.py` contains the exact machine-validated field definitions. User IDs in
public records represent the account or claimed identity; internal true actors are
private. Every source file exists even when it is empty.

`reference.json` contains the start-of-window directory, initial factors, device/IP
contexts, service number, agent and policy. Public logs contain no `malicious`,
`true_actor`, `class`, `case_id`, planned acts or technique labels.

## Private labels and observation

`GROUND_TRUTH.json` holds complete pre-observation records and `cases`, allowing
independent checks of missing-source causality.
`labels/cases.jsonl` contains the same cases for convenience. Each case has its source
record IDs, correlation ID, user, internal actor, malicious intent, class, procedural
status, action point and first planned procedural-evidence record ID.

The first-evidence label marks a script-defined procedural event. For recovery it is the first bypass request or commitment to an unverified
reset. For departure activity it is the first case record that reaches the rolling
hour's download threshold (including routine baseline downloads) or an external share
after a known notice, without migration approval. Labels use typed canonical events,
not interpretations of generated transcript text. Procedure-satisfied cases have
no such label.

The action point is the IdP reset time or external-share time. Denied resets have
no execution action point. These private timeline annotations let downstream
evaluations relate detection results to the scenario's events.

Observation decisions apply per `(source, correlation_id)` and never depend on intent.
Complete has zero loss/delay. Degraded drops 25% of telephony/transcript groups using
scoped seeds; ITSM delay is 300 seconds and HR delay is 900 seconds. Other sources
stay visible. Delayed records remain in the output even when they arrive after the
simulated event window. The observation manifest accounts for every canonical record.

## Data-quality reports

`pforge eval BUNDLE` independently checks schemas, correlations, action chronology,
factor/session consistency, observation accounting, labels and file integrity. It
reports dataset counts and invariant failures, with a nonzero exit status on failure.

`MANIFEST.json` hashes every static evidence/label/reference input. Quality reports
are derived, excluded from those hashes, and recomputed by `pforge eval`. The manifest
provides file-integrity checks; it is not a cryptographic signature of the dataset.

## Cached text

`generate --text-mode cached_llm --render-cache PATH` preflights validated private
conversation entries before emitting data. It adds private `TURN_PLANS.json` and
rendering provenance to `MANIFEST.json`. Record IDs, planned acts and outcomes preserve
scenario meaning. Speech duration
and downstream event times are derived from accepted wording; private timing labels
and hashes therefore change with wording. Transcript event_time is the end of speech,
so a complete utterance becomes observable only after it has been spoken. Template
mode remains the default.

`plan` exports private conversation plans. `render` prepares validated text through
an explicit model endpoint. `audit` produces private transcript review packets and
`cache-export` copies the accepted entries needed by a scenario for offline reuse.
See [the authoring workflow](llm-workflow.md) for provenance and validation details.

## Coherence checks

Recovery consent precedes the spoken request, ITSM request and real grant. The
approval confirmation starts after the grant; recovery confirmation starts after
the enrolled-factor login. Call duration includes speech, pauses and recorded waits.
Every authentication uses the active factor at its timestamp. Files join an earlier
session with matching user/device/IP; object sizes remain stable across reuse and
external shares refer to a downloaded object.

Published recovery labels include `recovery_confirmation_record_id`; its caller speech
starts after the corresponding successful login. Cache reads revalidate accepted
text against the current conversation constraints before any bundle is published.
