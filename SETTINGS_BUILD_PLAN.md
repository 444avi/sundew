# User Notification Settings — Build Plan

## Objective

Build a user-facing settings UI and delivery layer for Sundew and Sentinel. Users will be able to reduce or reorganize the notifications they receive without causing additional classification, triage, source-fetching, polling, or per-user market-data work.

Every preference must be applied after the monitors have performed their existing ingestion and classification. The monitors create one canonical event, and the delivery layer filters that event for each user.

The supported user settings are:

- Enable or disable each monitor.
- Categories.
- Delivery mode.
- Quiet hours.
- Direction.
- Minimum significance.
- Kalshi section for Sundew.
- Update emails for Sentinel.
- Digest time.

Operational settings such as polling intervals, model selection, clustering thresholds, source URLs, SMTP credentials, and source-health alerts remain operator-only.

## Current scope: single user

The system today serves exactly one recipient identity (the operator). Build for that now:

- No multi-tenant authentication, login, or session layer. Settings access is gated by operator control of the host (the same trust boundary as `.env` today), not by per-request user auth.
- Email verification is a one-time local fact, not an emailed round-trip flow: the single seeded user is created already verified and active.
- Subscriber-privacy isolation (never exposing one subscriber's address to another) is moot with one user, but the delivery worker still addresses only that user in `To` so the property holds for free when a second user is added.
- The unsubscribe path is a local disable, not a public tokenized endpoint.

The data model below stays multi-user-capable on purpose (a `users` table, per-user preferences and deliveries) so adding a second recipient later is a data change, not a reshaping. Wherever this plan says "each user" or "per-user", read it as "the one user" for this phase; the machinery is simply evaluated with N = 1. The auth, verification, and public unsubscribe work in the API section is explicitly deferred until a second user is real.

## Core resource constraint

User preferences must not cause:

- Additional LLM classification or triage calls.
- Additional source polling.
- Additional Finnhub or other market-data lookups.
- A higher polling frequency.
- Per-user Kalshi lookups.
- Reprocessing of historical events.

The required delivery flow is:

```text
Fetch and classify once
        ↓
Store one canonical event
        ↓
Evaluate existing event fields against each user's preferences
        ↓
Create a per-user delivery decision
        ↓
Send immediately or place in that user's digest
```

Kalshi data may be fetched once per canonical Sundew event when at least one matched recipient has the Kalshi section enabled. The result must be cached and reused for every recipient of that event.

## User settings

### Availability by monitor

| Setting | Sundew | Sentinel |
|---|---:|---:|
| Enable/disable | Yes | Yes |
| Categories | Yes | Yes |
| Delivery mode | Immediate or daily digest | Immediate, smart, or daily digest |
| Quiet hours | Yes | Yes |
| Direction | Yes | Yes |
| Minimum significance | Yes | Yes |
| Kalshi section | Yes | No |
| Update emails | No | Yes |
| Digest time | Yes | Yes |

### Enable or disable

Sundew and Sentinel each have an independent enabled toggle.

- Disabling a monitor stops all delivery from that monitor for the user.
- It does not stop ingestion, classification, clustering, or delivery to other users.
- Re-enabling a monitor applies only to future events.
- Re-enabling must not replay a historical backlog.

### Categories

Users may select any combination of:

- Company.
- Commodity.
- Country.
- Macro.

At least one category must remain selected while a monitor is enabled. Category matching uses OR logic. If an event matches more than one selected category, it still creates at most one email for that user.

#### Sundew category mapping

Categories must be derived locally from Sundew's existing classification and entity fields.

| Existing classification data | User-facing category |
|---|---|
| Category is `company`, or an entity has type `company` | Company |
| Category is `commodity`, or an entity has type `commodity` | Commodity |
| Category is `geopolitical`, or an entity has type `country` | Country |
| Category is `monetary` or `tariff` | Macro |
| An entity has type `index`, `currency`, or `sector` | Macro |

An event can receive multiple labels. For example, a tariff announcement involving China can be both `macro` and `country`.

#### Sentinel category mapping

Sentinel categories must be derived from existing event types and entities without a new classification call.

- Ordinary SEC filings and company trading halts are `company`.
- Market-wide circuit breakers are `macro`.
- Existing Sentinel sources do not reliably produce `country` or `commodity` events.
- Do not add LLM calls simply to populate Sentinel categories.
- Future sources may add country or commodity events using the same preference model.

### Delivery mode

#### Sundew

Sundew supports:

- `Immediate`: each matching event is sent as it happens.
- `Daily digest`: all matching events are collected into one daily email.

Sundew does not need a Smart option because its current relevant events belong to one immediate tier.

#### Sentinel

Sentinel supports:

- `Immediate`: send only existing immediate-tier events as they happen; suppress existing digest-tier events.
- `Smart`: send immediate-tier events as they happen and include lower-tier eligible events in the daily digest.
- `Daily digest`: collect both immediate-tier and digest-tier eligible events into one daily email.

Smart is Sentinel's default because it reproduces its current behavior. A delivery mode may reorganize or reduce the canonical eligible set, but it must not make log-only events eligible.

### Quiet hours

Users may configure:

- Whether quiet hours are enabled.
- A local start time.
- A local end time.
- Their timezone.

Quiet hours apply only to events that would otherwise be delivered immediately.

When an event arrives during quiet hours:

1. Create the user's delivery record.
2. Queue it rather than sending it immediately.
3. Include it in the user's next scheduled digest.
4. Combine multiple queued events into one digest rather than sending a burst when quiet hours end.

Quiet hours must support ranges crossing midnight, such as 10:00 PM to 7:00 AM. Time calculations must use an IANA timezone and correctly handle daylight-saving transitions.

Changing quiet hours affects future delivery decisions only. It must not recall sent messages or replay previously suppressed messages.

### Direction

Users may select any combination of:

- Bullish.
- Bearish.
- Unclear.

At least one direction must remain selected while a monitor is enabled. Direction matching uses OR logic.

Use direction already produced by the current pipeline. Do not call the LLM again to satisfy a user's direction setting. A Sentinel event without an existing direction is treated as `unclear`.

### Minimum significance

Users choose:

- Low.
- Medium.
- High.

Low means the broadest set already eligible under the production monitor. It must not expose events that the current global system considers log-only or irrelevant.

#### Sundew mapping

| User setting | Allowed existing magnitude |
|---|---|
| Low | Low, medium, and high |
| Medium | Medium and high |
| High | High only |

#### Sentinel mapping

Sentinel scores events on a continuous `impact` (0–1), not a low/medium/high label. To let the shared evaluator compare significance uniformly across both monitors, bucket that impact into the same three-value enum when the canonical event is created:

| Derived significance | Impact band |
|---|---|
| High | impact ≥ `0.85` |
| Medium | `0.70` ≤ impact < `0.85` |
| Low | eligible for immediate or digest delivery, but impact < `0.70` |

The user setting then filters against the derived enum exactly as Sundew does:

| User setting | Allowed derived significance |
|---|---|
| Low | Low, medium, and high |
| Medium | Medium and high |
| High | High only |

Only events already eligible for immediate or digest delivery are bucketed at all — a `log`-tier or sub-cap event never becomes a canonical event, so it can never appear at any significance. The global Sentinel relevance, novelty, impact, market-cap, and triage gates run first. A user preference can narrow their results but cannot override those gates. The evaluator reads only the derived `significance` enum; it never needs the raw impact float.

### Kalshi section

This setting appears only for Sundew.

- Off sends the normal Sundew content.
- On appends the existing relevant Kalshi-market section.
- All other preference filters run before Kalshi formatting.
- Kalshi results are fetched at most once per canonical event and reused for all matched users.
- If no matched user has Kalshi enabled, skip the lookup.
- A Kalshi error must not block the normal Sundew email.
- Users do not control the number of markets, series mappings, cache time, or ranking logic.

This is a presentation option within Sundew, not a separate monitor subscription.

### Sentinel update emails

This setting appears only for Sentinel.

An update email is additional material information attached to an event Sentinel previously reported. The current monitor recognizes an update when a cluster's impact increases materially and formats the subject with `UPDATE:`.

- On allows the user to receive material updates.
- Off suppresses all Sentinel update deliveries for that user.
- An update may be delivered only if the user received the original event.
- The update must pass the user's current enabled, category, direction, and minimum-significance filters.
- The user's delivery mode and quiet hours apply to the update.
- Each event version creates at most one delivery per user.
- Turning updates on must not replay old updates.
- The default is on, preserving existing behavior.

### Digest time

Users select a local digest time. The time is interpreted using the timezone stored on their account.

- The setting is active for Daily Digest mode.
- It is also active for Sentinel Smart mode and for items delayed by quiet hours.
- The value remains saved if the user temporarily switches to Immediate mode.
- A user receives at most one digest per monitor per scheduled digest period.
- Empty digests are never sent.

## Operator-only behavior

The following settings must not appear in the user UI:

- Source polling frequency.
- Source endpoints.
- LLM provider, model, or prompts.
- Classifier confidence gates.
- Clustering thresholds.
- Impact and novelty formulas.
- SMTP credentials.
- Market-cap provider configuration.
- Heartbeat thresholds.
- Cold-start behavior.
- Kalshi series mappings, ranking configuration, and cache timing.

Sentinel source-health warnings are operational alerts. They must be routed to a separate administrator address such as `ADMIN_EMAIL_TO`, never to the subscriber list.

## Data model

User preferences and deliveries should live in an application database rather than `.env`. Existing `.env` recipient lists are retained only during migration.

This is a third SQLite file (e.g. `app.db`) alongside `sundew.db` and `sentinel.db`, on the same EBS volume, opened in WAL mode like the existing stores. It introduces a new coupling: both monitor processes and the delivery worker now open and write to `app.db` — the monitors to append canonical events, the worker to read events and write deliveries. Because there are now multiple writers to one file, keep every writer in WAL mode with a busy timeout, keep write transactions short, and never hold the connection open across a network call (SMTP, Kalshi, Finnhub). The monitors' own `sundew.db` / `sentinel.db` writes are unchanged.

### Users

```text
users
- id
- email
- email_verified_at
- timezone
- status: active | unsubscribed | disabled
- created_at
- updated_at
```

Email addresses must be unique after normalization. Only verified, active users may receive notifications.

### Monitor preferences

```text
notification_preferences
- id
- user_id
- monitor: sundew | sentinel
- enabled
- delivery_mode: immediate | smart | digest
- minimum_significance: low | medium | high
- quiet_hours_enabled
- quiet_start_local
- quiet_end_local
- digest_time_local
- kalshi_enabled
- update_emails_enabled
- created_at
- updated_at
```

Constraints:

- One preference record per user and monitor.
- `smart` is valid only for Sentinel.
- `kalshi_enabled` is valid only for Sundew.
- `update_emails_enabled` is valid only for Sentinel.
- Local times must be valid wall-clock values.
- Times are interpreted using `users.timezone`.

### Categories

```text
preference_categories
- preference_id
- category: company | commodity | country | macro
```

Use a unique constraint on `(preference_id, category)`.

### Directions

```text
preference_directions
- preference_id
- direction: bullish | bearish | unclear
```

Use a unique constraint on `(preference_id, direction)`.

### Canonical notification events

The monitors keep their existing detailed databases. Add a common delivery-facing event representation:

```text
notification_events
- id
- monitor: sundew | sentinel
- source_event_id
- event_version
- event_kind: original | update
- categories
- direction
- significance
- canonical_tier: immediate | digest
- occurred_at
- subject_data
- body_data
- kalshi_data
- created_at
```

Add a unique constraint on `(monitor, source_event_id, event_version)`.

`significance` is the derived low/medium/high enum for both monitors — Sundew's magnitude verbatim, Sentinel's impact bucketed by the bands above — so the evaluator compares it uniformly and never needs a raw impact float. `direction` is the value the pipeline already produced; for Sentinel that comes from triage and exists only for events that reached the triage step, so a canonical event without one stores `unclear`.

Structured subject and body data should be stored so messages can be rendered for immediate delivery or a later digest without reclassifying the event.

### Per-user deliveries

```text
deliveries
- id
- user_id
- event_id
- status: pending | queued_digest | sent | suppressed | failed
- suppression_reason
- scheduled_for
- attempt_count
- last_error
- sent_at
- created_at
- updated_at
```

Add a unique constraint on `(user_id, event_id)` to prevent duplicates.

For updates, the delivery layer must be able to verify that a sent delivery exists for the original event and the same user.

Useful suppression reasons include:

- `user_inactive`
- `monitor_disabled`
- `category_filtered`
- `direction_filtered`
- `below_significance`
- `updates_disabled`
- `original_not_delivered`
- `immediate_only`
- `duplicate`

## Preference evaluation

Use one deterministic evaluator for both monitors.

Evaluation order:

1. Confirm the user is active and email-verified.
2. Confirm the monitor is enabled.
3. Confirm the user does not already have a delivery for this event version.
4. For an update, confirm update emails are enabled and the original was sent to this user.
5. Confirm at least one event category matches.
6. Confirm the direction matches.
7. Confirm the event meets the minimum significance.
8. Apply delivery mode to the canonical tier.
9. If delivery is immediate, determine whether quiet hours are active.
10. Send immediately or queue for the user's digest.

The evaluator must not invoke external APIs or an LLM. It operates entirely on the stored canonical event and preference record.

## Monitor integration

### Sundew changes

After Sundew's existing relevance gate:

1. Derive the four user-facing category labels from the existing classification.
2. Store one canonical Sundew notification event.
3. Evaluate the event against active Sundew preferences.
4. Create immediate, digest, or suppressed delivery records.
5. Check whether any matched delivery requires Kalshi.
6. Fetch and cache Kalshi once if needed.
7. Render plain or augmented content without changing the canonical classification.
8. Record each delivery result separately.

Sundew must no longer treat its global `notified` field as proof that every intended user received the message. It can remain as monitor-level historical metadata during migration.

### Sentinel changes

Keep the existing ingestion, clustering, novelty calculation, impact calculation, market-cap gate, and triage pipeline global and single-run.

After canonical eligibility is determined:

1. Store an original or update notification event.
2. Derive user-facing categories from existing event data.
3. Evaluate the event against active Sentinel preferences.
4. Create per-user immediate, digest, or suppressed delivery records.
5. Use per-user delivery history to determine update eligibility.

The existing global `alerted_at` state may remain for compatibility, but it must not replace per-user delivery history.

The current global digest queue and single digest-sent date must be replaced by per-user scheduling. A single global queue cannot support different timezones, delivery modes, or digest times.

## Delivery worker

Add one delivery worker shared by both monitors.

Responsibilities:

- Select pending immediate deliveries.
- Select users whose digest time has arrived.
- Group digest events by user and monitor.
- Render one digest for each due user and monitor.
- Send each message with only that user's address in `To`.
- Retry transient SMTP failures with capped exponential backoff.
- Record sent and failed states.
- Never mark a delivery sent until SMTP succeeds.
- Use stable message identifiers for idempotency.
- Include manage-preferences and unsubscribe links.

A failed delivery to one user must not affect delivery to another user. Subscriber email addresses must never be exposed to other subscribers.

## Settings UI

Use a single settings page with separate Sundew and Sentinel cards.

### Shared page elements

- Verified email address.
- Account timezone.
- Save confirmation.
- Validation errors next to the affected control.
- Manage subscription and unsubscribe actions.
- A preview generated from static sample data.

The preview must not invoke an LLM, Kalshi, Finnhub, or a monitor source.

### Sundew card

- Enable Sundew toggle.
- Category checkboxes.
- Delivery mode: Immediate or Daily Digest.
- Quiet-hours toggle and start/end inputs.
- Direction checkboxes.
- Minimum-significance selector.
- Include Kalshi section toggle.
- Digest-time input when applicable.
- Static email preview.

### Sentinel card

- Enable Sentinel toggle.
- Category checkboxes.
- Delivery mode: Immediate, Smart, or Daily Digest.
- Quiet-hours toggle and start/end inputs.
- Direction checkboxes.
- Minimum-significance selector.
- Update emails toggle.
- Digest-time input when applicable.
- Static email preview.

### UI explanations

Explain that:

- `Unclear` includes events whose market direction cannot be confidently determined. For Sentinel this also covers most digest-tier events: a direction is only produced when an event reaches LLM triage (an immediate/update candidate), so lower-tier events carry `unclear` by default. Deselecting `Unclear` for Sentinel therefore filters out most digest content — the UI should say so next to the control.
- `Low` is the broadest normal coverage, not every event collected by the service.
- Events received during quiet hours will appear in the next digest.
- The Kalshi option changes Sundew email content rather than enabling a third monitor.
- Sentinel country and commodity coverage is limited by its current sources.
- Turning off updates suppresses follow-up emails but not new Sentinel events.

## API design

A minimal API surface:

```text
GET  /api/settings
PUT  /api/settings/sundew
PUT  /api/settings/sentinel
POST /api/settings/test-email
POST /api/unsubscribe
```

In the single-user phase these endpoints resolve the current user implicitly (the one seeded user); there is no login, token, or per-request authentication, and access is gated by host control rather than an auth layer. `POST /api/unsubscribe` is a local disable in this phase, not a public tokenized action. The per-user authentication and tokenized-unsubscribe requirements below apply when a second user becomes real and are deferred until then; the validation, atomicity, and normalization requirements apply now.

Every settings update must:

- Resolve the user. (Single-user phase: implicit. Multi-user: authenticate the request.)
- Validate enum values and time formats.
- Validate the timezone against the IANA timezone database.
- Require at least one category and one direction when the monitor is enabled.
- Reject fields that do not apply to the selected monitor.
- Update the preference and category/direction selections atomically.
- Return the normalized saved preference.
- Affect only future delivery decisions.
- Never initiate historical delivery.

The unsubscribe endpoint should use a scoped, expiring or revocable token. It must not expose account data.

## Defaults

Defaults must preserve existing behavior as closely as possible.

### Sundew defaults

- Enabled.
- All categories selected.
- Immediate delivery.
- Quiet hours disabled.
- All directions selected.
- Minimum significance Low.
- Kalshi off for plain-Sundew recipients and on for existing `sundew.ks` recipients.
- A sensible saved digest time even when digest mode is inactive.

### Sentinel defaults

- Enabled.
- All categories selected.
- Smart delivery.
- Quiet hours disabled.
- All directions selected.
- Minimum significance Low.
- Update emails enabled.
- Existing global digest hour converted to the user's local digest time.

## Migration

Seed the application database from existing recipient lists.

- Each `EMAIL_TO` address receives a Sentinel preference.
- Each `EMAIL_TO_SUNDEW` address receives a Sundew preference with Kalshi disabled.
- Each `EMAIL_TO_KS` address receives a Sundew preference with Kalshi enabled.
- If an address appears in multiple lists, create one user with the appropriate monitor preferences.
- Normalize and deduplicate email addresses before insertion.
- Seed every migrated user as `status = active` with `email_verified_at` set (they are pre-verified legacy recipients). The evaluator refuses unverified or inactive users, so a seed that leaves either unset would silently stop all delivery at cutover.
- Preserve the current recipient experience through seeded defaults.

Rollout sequence:

1. Add the application database and schema migrations.
2. Add canonical-event creation without changing current email routing.
3. Seed users and preferences from the current environment lists.
4. Run preference matching in shadow mode.
5. Compare shadow recipients and formatting with current deliveries.
6. Add the per-user delivery worker and digest scheduling.
7. Route a small internal test group through the new delivery path.
8. Enable the new path for all seeded users.
9. Keep environment-list delivery as a temporary rollback option.
10. After a stable observation period, remove static recipient routing.
11. Route Sentinel source-health warnings to `ADMIN_EMAIL_TO`.

## Implementation phases

### Phase 1: Domain and persistence

- Add users, preferences, category, direction, canonical-event, and delivery tables.
- Add uniqueness and validation constraints.
- Implement timezone-aware quiet-hours helpers.
- Implement deterministic category and significance mapping.
- Implement the shared preference evaluator.

### Phase 2: Canonical event adapters

- Convert relevant Sundew classifications into canonical events.
- Convert Sentinel originals and material updates into canonical events.
- Preserve existing monitor databases and deduplication.
- Add idempotent event-version keys.

### Phase 3: Delivery infrastructure

- Add immediate-delivery processing.
- Add per-user digest queues.
- Add quiet-hours routing.
- Add SMTP retry and failure tracking.
- Add reusable per-event Kalshi enrichment.
- Add unsubscribe and manage-preferences links.

### Phase 4: API and authentication

- Add settings read/update endpoints (single-user: implicit current user).
- Add static test-email previews.
- Add a local disable path for unsubscribe.
- Deferred to multi-user: email-verification round-trips, authenticated settings access, and tokenized public unsubscribe.

### Phase 5: UI

- Build Sundew and Sentinel settings cards.
- Add conditional controls and validation.
- Add timezone, quiet-hours, and digest-time inputs.
- Add accessible descriptions and save feedback.
- Test mobile and desktop layouts.

### Phase 6: Migration and rollout

- Seed existing recipients.
- Run shadow comparisons.
- Deploy to the internal group.
- Monitor delivery failures and duplicate prevention.
- Move all recipients to per-user delivery.
- Remove user delivery from environment variables.

## Testing plan

### Unit tests

Cover:

- Enabled and disabled monitors.
- Every individual category.
- Multi-category event matching.
- Every direction and multiple selected directions.
- All significance levels.
- Original events and updates.
- Update emails enabled and disabled.
- Update where the original was not delivered.
- Immediate, Smart, and Daily Digest modes.
- Quiet hours before, during, and after the interval.
- Quiet hours crossing midnight.
- Multiple timezones.
- Daylight-saving transitions.
- Kalshi enabled and disabled.
- One delivery for a multi-category event.
- No duplicate delivery when an event is reprocessed.
- No historical replay after a setting change.

### Integration tests

Verify:

- One canonical classification serves many users.
- Preference evaluation performs no external calls.
- Kalshi is fetched at most once per event.
- Digest creation is isolated per user and monitor.
- Digests use each user's timezone and configured time.
- Quiet-hour events appear in the next digest.
- SMTP failure leaves a delivery retryable.
- One user's failure does not block another user's delivery.
- Updates reach only users who received the original.
- Unsubscribed and unverified users receive nothing.
- Subscriber email addresses are not exposed.
- Operational warnings go only to administrators.

### Regression tests

Verify that the refactor does not change:

- Sundew polling, deduplication, prefiltering, or relevance classification.
- Sentinel ingestion, clustering, scoring, triage, or market-cap gating.
- Cold-start suppression behavior.
- Kalshi fail-open behavior.
- Existing monitor database history.

## Observability

Add metrics or structured logs for:

- Canonical events created by monitor.
- Users matched per event.
- Deliveries sent, queued, suppressed, retried, and failed.
- Suppression counts by reason.
- Digests generated and number of items per digest.
- Updates suppressed because the original was not delivered.
- Kalshi cache hits, fetches, and failures.
- Delivery latency from event creation to send.

Do not log SMTP passwords, authentication tokens, full preference-management tokens, or unnecessary personal data.

## Acceptance criteria

The feature is complete when:

- Users can configure every requested setting independently for the applicable monitor.
- Defaults reproduce the current experience for existing recipients.
- Preferences only preserve, reduce, delay, or batch the canonical eligible event set.
- No preference causes an additional LLM classification, triage, source fetch, market-cap lookup, or polling operation.
- Kalshi data is generated at most once per canonical event, not once per user.
- Multi-category events create no duplicate emails.
- Update emails can be disabled independently.
- An update is never sent to someone who did not receive the original event.
- Quiet hours and digest scheduling respect the user's timezone.
- Every delivery decision is idempotent and auditable.
- SMTP failures can be retried without duplicate successful deliveries.
- Subscriber email addresses remain private.
- Operational warnings are delivered only to administrators.
- Existing Sundew and Sentinel monitoring behavior remains intact.
