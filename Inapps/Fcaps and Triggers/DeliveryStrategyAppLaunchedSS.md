# `DeliveryStrategyAppLaunchedSS` — Consolidated

## Purpose

On the **first launch after a campaign is created**, the SDK doesn't know the campaign exists, so it can't request it via `inapps_eval`. This strategy pushes **full content** for every eligible App-Launched campaign up front and lets the SDK pick.

Runs on **App Launched only**, beside `MetaSS` and `AppLaunchedSSMeta`.

---

## Members

| Member | Declared | Concrete |
|---|---|---|
| `targetingProvider` | `@Named(DELIVERY_TARGETS_APP_LAUNCH_SS)` | `Metered(Filtered(DeliveryTargetsAppLaunchedSS))` |
| `payloadProvider` | concrete | `PayloadProviderSS` — full content |
| `payloadProviderSuppressedCS` | concrete | `PayloadProviderSuppressedCS` — the stub |
| `controlGroupSuppressor` | concrete | `ControlGroupSuppressor` |
| `combinedQuotaCheck` | `@Named(COMBINED_QUOTA_CHECK)` | `InAppCombinedQuotaCheck` |
| `inAppTargetEval` | interface | `InAppTargetEvalImpl` |

---

## Targets

`APP_LAUNCHED ∪ null-trigger`, `!isClientSide`, `targetType != InstantEventClosure`, sorted **priority ↓, id ↑**.

Campaigns and journeys are **interleaved in one list**.

### `shouldSkipTarget` — note what's missing

```java
needCampaignPayload && ti.isJourneyTarget()
 || targetsQualifiedSet.contains(id)
 || !inAppTargetEval.processTarget(params, ti)      // segmentation
 || combinedQuotaCheck.targetQuotaReached(params, ti)
```

> **No `inapps_eval` gate** — unlike `DeliveryStrategySS`. That's the whole point: the SDK hasn't had a chance to ask.

---

## The state machine

```
isCampaignPayload    = false;   // sticky
continueTargetSearch = true;
payloadKey = INAPP_NOTIFS_APPLAUNCHED;

for each target (priority order, while continueTargetSearch):
    skip? → continue
    payload = PayloadProviderSS.getPayload(ti)     // full content
    empty? → continue
    targetsQualifiedSet.add(id)

    SKIP     → nothing
    SUPPRESS → journey : record SuppressEvent NOW
               campaign: isCampaignPayload = true; add suppressed STUB
    DELIVER  → markAsSent = true; add payload
               journey : continueTargetSearch = false; key = INAPP_NOTIFS              ← STOP
               campaign: isCampaignPayload = true;     key = INAPP_NOTIFS_APPLAUNCHED
```

---

## The three asymmetries

### 1. Journey stops, campaign doesn't

A journey `DELIVER` returns that **one** in-app under `inapp_notifs` — *server decided*.
A campaign `DELIVER` keeps collecting every other eligible campaign into `inapp_notifs_applaunched` — *SDK decides*.

### 2. `isCampaignPayload` kills journeys retroactively

Set on campaign `DELIVER` **or** `SUPPRESS`. Since the list is priority-ordered: **the highest-priority eligible target decides the mode.**

### 3. Suppression handled oppositely

| | on `SUPPRESS` | why |
|---|---|---|
| **journey** | `SuppressEvent` now → LP; journey step advanced with `cgId` | server knows it would have shown |
| **campaign** | stub into the payload; SDK reports back later | server can't know — SDK's local fcaps decide |

A **suppressed journey** does *not* set `isCampaignPayload` and does *not* stop the loop — a later target can still win.

---

## `SKIP` vs `SUPPRESS`

| | meaning | tracked |
|---|---|---|
| `SKIP` | couldn't evaluate — anonymous user (`i/j ≤ 0`) on a campaign that has control groups | no |
| `SUPPRESS` | evaluated → withhold — user in a holdout | yes, required for target-vs-control reporting |

No control groups configured → always `DELIVER`.

---

## The invariant

> `deliverPayloads` holds **either exactly one journey, or only campaigns** — never a mix.

A campaign is added only on `DELIVER`/`SUPPRESS`, both of which set `isCampaignPayload = true`, after which journeys are skipped. A journey `DELIVER` sets `continueTargetSearch = false`. One `payloadKey`, one list.

Ties at the same priority break on **lowest id**, so the outcome is deterministic.

---

## Payloads

### Real campaign — `PayloadProviderSS`, non-journey branch

message copy + `getCommonFields` (`ti`, `wzrk_id`, `wzrk_pivot`, `priority`, `whenTriggers`, `frequencyLimits`/`excludeGlobalFCaps`, `occurrenceLimits`) + `wzrk_ttl` (absolute) + `prepareContent`. **Content and metadata in one.**

### Journey — `PayloadProviderSS`, journey branch → `getJourneyFields`

`ti`, `wzrk_id`, `wzrk_pivot`, `wzrk_ttl`, `delay`, legacy limits only. **No `whenTriggers`, no `priority`, no advanced limits.**

### Suppressed campaign stub — `PayloadProviderSuppressedCS`

(doesn't implement `PayloadProvider`, extra `cgId` arg)

`getCommonFields` + `suppressed: true` + `wzrk_cgId`. **No content.** Carries the rules (so the SDK can decide) and the round-trip identifiers `wzrk_id` / `wzrk_pivot` / `wzrk_cgId` — exactly what comes back in `meta.inapps_suppressed` → `SuppressionMeta` → `SuppressEvent`.

Stubs have `markAsSent = false`, so they occupy a slot but never increment `TargetingCountManager`.

---

## Relationship to `MetaSS`

Because this strategy already ships triggers + limits inside the full payload, `DeliveryTargetsMetaSS` explicitly **subtracts** single-trigger App-Launched campaigns from the catalogue:

```java
.filter(ti -> !targetsOnAppLaunch.contains(ti.getID()))
```

Multi-trigger campaigns stay in **both** — their other triggers still need a catalogue entry.

---

## Open edge

Null-trigger campaigns are unioned in here but **not** subtracted in `DeliveryTargetsMetaSS` (which builds its exclusion set from the `APP_LAUNCHED` bucket only). A null-trigger campaign with advanced rules therefore appears in **both** `inapp_notifs_ss` and `inapp_notifs_applaunched` in the same response.
