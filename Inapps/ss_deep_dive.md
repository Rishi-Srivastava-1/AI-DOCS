# In-App Dispatch — Server-Side (SS) Deep Dive

Companion to [`understand.md`](./understand.md). This file zooms into the SS path only — every strategy, every collaborator, every response key. Assumes you've read the overview.

Reminder of the entry point (`core/lc/.../InAppProcessorAdapter.java:142`):
```
InAppProcessorAdapter.evalAndFillResponse
  → inAppProcessor.process(params)
      → DeliveryStrategyProvider.get(params)  // Mode.SS
          → DeliveryStrategyProvider.getSS()  // composes a list of strategies
```

`getSS` is a **composer** — it appends multiple strategies for one request, then `MergeDelivery.of(deliveries)` folds their `DeliveryResult`s into one response. That's why a single App-Launched request can populate `inapp_notifs_applaunched` **and** `inapp_notifs_applaunched_meta` **and** `inapp_notifs_meta` at the same time.

---

## 1. The composer

`core/inapps/service/.../delivery/DeliveryStrategyProvider.java:70-99`

Event-name / properties decides which strategies join the list:

| Condition | Strategies appended | Notes |
|---|---|---|
| `inactionDeliveryRequested(event)` == true | **only** `DeliveryStrategySSInactionContentFetch` | Short-circuits — nothing else runs |
| `metaDeliveryRequested(event)` == true | `DeliveryStrategyMetaSS` | Adds metadata for all SS campaigns w/ advanced rules |
| `event == "App Launched"` | `DeliveryStrategyAppLaunchedSS` + `DeliveryStrategyAppLaunchedSSMeta` | App-launched content + inaction metadata |
| any other event | `DeliveryStrategySS` + `DeliveryStrategySSInaction` | Per-event dispatch + inaction qualification |
| `!inAppSuppressIds.isEmpty()` (any event) | `DeliveryStrategySuppressionAppLaunchedSS` | Register incoming suppressions |

Two events matter to SS routing:
- **`metaDeliveryRequested`** — App Launched or explicit "fetch inapps" event (`EventsHelper.metaDeliveryRequested`)
- **`inactionDeliveryRequested`** — an explicit `wzrk_fetch` inaction content-fetch (`EventsHelper.inactionDeliveryRequested`)

---

## 2. Shared collaborators

Every SS strategy leans on the same set of building blocks. Understand these once and each strategy reads as a small orchestration on top.

### 2.1 `DeliveryTargetsProvider` — the target list source

Interface returning `List<TargetingInfo>`. Each strategy has its own `@Named` provider bound in `InAppsCommonModule` (`core/inapps/service/.../guice/InAppsCommonModule.java:127-186`), decorated with:
- `FilteredTargetsProvider` — runs every registered `TargetFilter`
- `MeteredDeliveryTargetsProvider` — tracks usage stats

| Provider (bound in `InAppsCommonModule`) | Filter applied | File |
|---|---|---|
| `DELIVERY_TARGETS_META_SS` | `hasAdvancedRules` AND `!journey` AND `!clientSide` AND not-on-app-launched-single-trigger | `DeliveryTargetsMetaSS.java:37-43` |
| `DELIVERY_TARGETS_APP_LAUNCH_SS` | targets on `APP_LAUNCHED` ∪ no-trigger targets; `!clientSide`; ordered by priority desc, id asc | `DeliveryTargetsAppLaunchedSS.java:30-44` |
| `DELIVERY_TARGETS_APP_LAUNCH_SS_META` | app-launched targets w/ advanced rules and inaction | `DeliveryTargetsAppLaunchedSSMeta.java` |
| `DELIVERY_TARGETS_SS_INACTION` | non-App-Launched inaction targets | `DeliveryTargetsSSInaction.java` |
| `DELIVERY_TARGETS_SS_INACTION_CONTENT_FETCH` | single-target content fetch | `DeliveryTargetsInactionContentFetch.java` |

All of them fan out from `InAppTargetingProvider.getInAppWebTargeting(accountId)` — the same in-memory `TargetingDto` cache used by the legacy flow (backed by `AccountWorker`).

### 2.2 `InAppTargetEval` — server-side trigger evaluation

`core/inapps/service/.../delivery/common/InAppTargetEval.java` (interface)
Impl: `core/lc/.../targeting/inapps/InAppTargetEvalImpl.java`

Three entry points:
1. `processTarget(params, ti)` — evaluates the target's **entry** query against the request (event name + properties + profile + geo + IP).
2. `processTarget(triggerEvent, params, ti)` — same but with an override trigger event (used when the actual dispatch trigger differs from the request event, e.g., inaction content fetch).
3. `processTargetExitQuery(params, ti)` — evaluates the **exit / "mini-who"** query (`InAppTargetEvalImpl.java:56-66`), used for inaction content-fetch to confirm the user still qualifies right before serving delayed content.

Under the hood it builds `EvalParams` and delegates to `TargetEvalManager.processTarget` (`core/lc/.../targeting/TargetEvalManager.java:79-96`), which itself calls `TargetingUtils.processOnlineTarget(evalParams, logInfo)` — the shared predicate engine used by push/email/etc.

Inputs picked up from `ProcessParams`:
- `eventName`, `eventPropertiesForEval`
- `profile`, `epHandlerProps` (device/app fields already merged in)
- `journeySteps` (for journey qualification)
- `ip`, `isIPEnabled` (for geo evaluation via `BasicEPHandler.GEO`)
- `firstSession`, `device`

### 2.3 `InAppQuotaCheck` — frequency cap enforcement

Interface: `core/inapps/service/.../delivery/common/InAppQuotaCheck.java`

Two implementations, glued together by `InAppCombinedQuotaCheck` (`InAppCombinedQuotaCheck.java:24-33`):

```java
if (ADVANCED_FCAPS enabled  &&  !ti.isJourneyTarget()) {
    return newQuotaCheck.targetQuotaReached(params, ti);   // InAppAdvancedFCapsGlobalQuotaCheck
}
return legacyQuotaCheck.targetQuotaReached(params, ti);    // legacy per-target caps
```

- **New (`InAppAdvancedFCapsGlobalQuotaCheck`)** — checks only the **global daily** cap (`Account.getInappMaxPerUserPerDay`) against `params.getShownToday()`. Honours `AdvancedLimits.excludeGlobalFCaps` — if the campaign opts out, it always returns `false`. Per-target daily/lifetime caps are enforced by the SDK in this mode.
- **Legacy** — checks per-target `tlc` / `tdc` / `efc` counters against `inappTargetUserCounts` supplied by the caller.

Journey targets always use the legacy quota check — journeys have their own per-node semantics.

Note: injected everywhere as `@Named(NamedConstants.COMBINED_QUOTA_CHECK)` — never bind the individual impls directly.

### 2.4 `ControlGroupSuppressor` — target-group vs control-group

Every SS strategy that returns actual content must call it (`shouldSuppress(ti, i, j, notificationClickedId, contentKey)`). Three outcomes:

| Result | Meaning | Payload |
|---|---|---|
| `DELIVER` | User is in target group | append payload; `payload.setMarkAsSent(true)` |
| `SUPPRESS` | User is in control group | drop payload; emit a `SuppressEvent` (goes into `DeliveryResult.suppressedEvents`) |
| `SKIP` | Neither — skip silently | no payload, no event |

`SuppressEvent` carries `cgId`, `cgType`, `key`, `batchId`, `notificationClickedId` — used by `InAppProcessorAdapter.convertSuppressEvents` (`InAppProcessorAdapter.java:238-275`) to create a `NotificationSuppressed` supplementary event that LP writes to ES so control-group A/B math stays honest.

### 2.5 `PayloadProviderSS` vs `PayloadProviderMetaSS`

Two payload builders. Both live in `core/inapps/service/.../payload/`.

**`PayloadProviderMetaSS`** (`PayloadProviderMetaSS.java:20-31`) — metadata-only payload:
- calls `contentUtil.getCommonFields(params, ti)` (target id, `wzrk_id`, trigger metadata, freq-cap ceilings)
- appends custom-template name (if any) and inaction-duration
- **no content field** — SDK will fetch content separately when it decides to display

**`PayloadProviderSS`** (`PayloadProviderSS.java:26-75`) — full content payload:
- picks the message variant via `params.getContentKey(ti, contentUtil)` (memoised per target in `ProcessParams.contentKeys`)
- deep-copies the message DBObject from `ti.message(device, contentKey, null)`
- runs `InAppDimensionsFix.apply` on the "w" dimensional block
- merges common fields (non-journey) OR journey fields (journey target), including `wzrk_ttl` (with delay-on-trigger applied via `contentUtil.getInAppTTLWithDelay`)
- runs `contentBuilder.prepareContent(...)` — personalisation replacements, recommendations, image resolution. If it fails, returns empty (target dropped)
- constructs a `notificationClickedId` (`contentUtil.getNotificationClickId(ti.getID())`) — the wzrk_id-like identifier used to correlate the future NOTIF_CLICKED event
- returns a `PayloadMeta` carrying `batchId`, `contentKey`, `notificationClickedId`, `sdkPayload`, `ti`

`PayloadMeta.markAsSent` is set to `true` only by strategies that finalise a delivery. `InAppProcessorAdapter.markAsSent` (`InAppProcessorAdapter.java:277-291`) then bumps `TargetingCountManager.markAsSent(1, accountId, targetId, batchId, device, InApp, contentKey)`.

### 2.6 `MergeDelivery` — folder

`DeliveryStrategyProvider.get(params)` returns `Optional<InAppDelivery>` even though multiple strategies contribute. `MergeDelivery` implements `InAppDelivery` and just sums up each sub-strategy's `DeliveryResult`. So the `evalAndFillResponse` fill loop in the adapter iterates the merged `payloads` map, dedups by target-id, and writes to the response.

---

## 3. Strategy-by-strategy walkthrough

### 3.1 `DeliveryStrategyMetaSS` — bulk metadata bootstrap

`core/inapps/service/.../delivery/ss/DeliveryStrategyMetaSS.java` (38 lines)

**Fired when:** `metaDeliveryRequested(event)` is true — App Launched or explicit inapps-fetch event.

**What it does** (`.java:23-37`):
```
targets = DELIVERY_TARGETS_META_SS.fetchTargets(params)
  // hasAdvancedRules && !journey && !clientSide && not-on-app-launched-single-trigger
for ti in targets:
    payload = PayloadProviderMetaSS.getPayload(ti, params)   // metadata only
result → payloads[INAPP_NOTIFS_SS]
```

**No segmentation, no suppression, no caps** — this ships the trigger/limit contract for every SS campaign the SDK might qualify for during the session, so the SDK can send content-fetch requests later.

**Response key:** `inapp_notifs_ss` (`PayloadKey.java:10`). Same key as `DeliveryStrategySS` — the fill loop dedupes by target id.

### 3.2 `DeliveryStrategyAppLaunchedSS` — first-render pick

`core/inapps/service/.../delivery/ss/DeliveryStrategyAppLaunchedSS.java` (159 lines)

**Fired when:** `event == "App Launched"`.

Purpose: pick what to render on session-start in one round-trip. Serves either **one journey in-app** OR **all qualifying campaign in-apps** — never both.

Target list: from `DELIVERY_TARGETS_APP_LAUNCH_SS` (see 2.1 above) — union of app-launched-triggered and no-trigger campaigns, plus journey inapps, ordered by priority desc, id asc.

**Algorithm** (`.java:52-136`):
```
isCampaignPayload = false        // once true, we ignore all journeys thereafter
continueTargetSearch = true
payloadKey = INAPP_NOTIFS_APPLAUNCHED   // may be overridden to INAPP_NOTIFS for journey

for ti in ordered targets:
    skip if:
        - (isCampaignPayload && ti.isJourneyTarget())
        - target already qualified this request
        - inAppTargetEval.processTarget(params, ti) == false
        - combinedQuotaCheck.targetQuotaReached(params, ti)

    payload = PayloadProviderSS.getPayload(ti, params)
    targetsQualifiedSet.add(ti.getID())

    switch (controlGroupSuppressor.shouldSuppress(ti, ...)):
        SKIP     → nothing
        SUPPRESS →
            if journey:
                add SuppressEvent  // journey suppression is a first-class outcome
            else:
                isCampaignPayload = true
                payload = PayloadProviderSuppressedCS.getPayload(...)  // empty-payload placeholder
                if present, add to deliverPayloads
        DELIVER  →
            payload.markAsSent = true
            deliverPayloads.add(payload)
            if journey:
                continueTargetSearch = false
                payloadKey = INAPP_NOTIFS          // return just this one journey
            else:
                isCampaignPayload = true
                payloadKey = INAPP_NOTIFS_APPLAUNCHED

if deliverPayloads not empty:
    payloads[payloadKey] = deliverPayloads
```

Key nuances:
- A **journey wins** if it's the first eligible target (highest priority) — sent alone under `inapp_notifs`, no other campaigns.
- A **campaign wins** by beating all higher-priority journeys — then **all** eligible campaigns (not just one) go out under `inapp_notifs_applaunched`.
- A **suppressed campaign** still switches the mode to campaign-search — subsequent journeys are then blocked, matching the "campaign locked in" rule even under CG suppression. The suppressed payload is emitted via `PayloadProviderSuppressedCS` as a stub so the SDK sees the target and increments its own counter.

Response keys used: `inapp_notifs_applaunched` (campaigns) or `inapp_notifs` (journey).

### 3.3 `DeliveryStrategyAppLaunchedSSMeta` — inaction bootstrap on App Launched

`core/inapps/service/.../delivery/ss/DeliveryStrategyAppLaunchedSSMeta.java` (113 lines)

**Fired when:** `event == "App Launched"`, alongside 3.2.

Ships **metadata** for inaction campaigns triggered on App Launched. Inaction campaigns fire after a delay if the user hasn't done something — so the SDK needs to know they *exist* and start its own timer.

**Algorithm** (`.java:40-102`):

1. Fetch targets (only non-journey app-launched inaction targets that pass `inAppTargetEval.processTarget`).
2. Compute `targetIds` list.
3. **FStore check:**
   - If `isFreshLaunch`: `fsClientUtils.deleteOnlineTargetQualificationsForUser(accountId, iid)` and treat all targets as valid (a fresh session wipes prior qualifications).
   - Else: `validTargets = fsClientUtils.onlineTargetQualificationNotPresent(accountId, iid, targetIds)` — only targets **not already qualified** are eligible for this session.
4. For each valid target, build metadata payload and stage an FStore put entry (`updatePutBatchEntriesForOnlineTargets`).
5. `fsClientUtils.targetQualificationBatchUpdate(accountId, iid, Context.ONLINE_INACTION, putEntries)` — **atomic batch persist**.
6. On success: emit payload under `inapp_notifs_applaunched_meta` (`PayloadKey.java:13`); on failure: drop the batch and report a P1 profiler counter — no partial state.

The FStore write happens **before** the response goes out. Metadata is only shipped if the qualification was successfully durable — otherwise the SDK might arm a timer for a campaign we forgot we qualified them for, causing over-delivery. That's the whole reason for the pre-write.

### 3.4 `DeliveryStrategySS` — per-event dispatch (the workhorse)

`core/inapps/service/.../delivery/ss/DeliveryStrategySS.java` (115 lines)

**Fired when:** non-App-Launched event, and `metaDeliveryRequested` is false (i.e., a regular user action).

Delivers **at most one campaign or journey in-app** per event — the first eligible target in priority order.

Target list: from `DELIVERY_TARGETS_SS` (`DeliveryTargetsSS.java:20-37`) — targets bound to `params.getEventName()`, non-clientSide, non-`InstantEventClosure`, sorted by priority desc / id asc.

**`shouldSkipTarget`** (`.java:106-114`):
```java
boolean skipCampaign = !ti.isJourneyTarget()
        && !params.getInAppEvalIds().contains(ti.getID())
        && ti.getAdvancedLimits().map(AdvancedLimits::hasAdvancedRules).orElse(false);
return skipCampaign
        || params.getTargetsQualifiedSet().contains(ti.getID())
        || !inAppTargetEval.processTarget(params, ti)
        || combinedQuotaCheck.targetQuotaReached(params, ti);
```

`skipCampaign` is the important line: **advanced-rules campaigns are only delivered when the SDK requested them** (their ID appears in `inAppEvalIds` from the request). Otherwise the SDK is expected to gate them locally using metadata it already got from Meta/AppLaunched flows. This is what prevents advanced-fcap campaigns from double-firing.

**Algorithm** (`.java:37-88`) — iterate targets, first eligible one wins:
```
for ti in ordered targets:
    if shouldSkipTarget: continue
    payload = PayloadProviderSS.getPayload(ti, params)  // full content
    if payload empty: continue
    targetsQualifiedSet.add(ti.getID())
    switch controlGroupSuppressor.shouldSuppress(...):
        SKIP     → keep looking
        SUPPRESS → add SuppressEvent, keep looking (unlike App Launched — no lock-in)
        DELIVER  → payload.markAsSent = true; payloads[INAPP_NOTIFS] = [payload]; RETURN
```

Response key: `inapp_notifs` (`PayloadKey.java:9`) — same key used by the legacy flow. Only one payload per event.

### 3.5 `DeliveryStrategySSInaction` — inaction qualification per event

`core/inapps/service/.../delivery/ss/DeliveryStrategySSInaction.java` (138 lines)

**Fired when:** non-App-Launched event, alongside 3.4.

Purpose: when a normal event occurs, arm any inaction campaigns whose entry trigger matches — so their timer starts. **Persists qualification to FStore then returns metadata to the SDK.**

**`shouldSkipTarget`** (`.java:131-137`):
- Skip if `hasAdvancedRules && ti.getID()` not in `inAppEvalIds` (same "advanced ⇒ SDK-driven" rule as 3.4)
- Skip if already qualified this request
- Skip if `inAppTargetEval.processTarget(params, ti)` fails

**Algorithm** (`.java:42-87`):
1. Compute qualified target IDs.
2. `validTargets = fsClientUtils.onlineTargetQualificationNotPresent(accountId, iid, targetIds)` — de-dup against prior FStore qualifications.
3. Iterate qualified targets in order; for the **first** valid one:
   - `fsClientUtils.addOnlineTargetQualification(accountId, uid, ti, eventName, eventProperties)` — persist qualification
   - Drop the payload if `hasAdvancedRules` (SDK already has metadata from earlier)
   - Otherwise, `PayloadProviderMetaSS.getPayload(ti, params)` and add to `inapp_notifs_meta`
   - **Return immediately** — priority-ordered, one per event

Response key: `inapp_notifs_meta` (`PayloadKey.java:14`). Only one payload per event.

Notice the asymmetry with 3.3: this strategy uses a **per-target single-write** (`addOnlineTargetQualification`) while 3.3 uses a **batch-write** (`targetQualificationBatchUpdate`). App Launched can carry many at once; per-event fires only one.

### 3.6 `DeliveryStrategySSInactionContentFetch` — the delayed content delivery

`core/inapps/service/.../delivery/ss/DeliveryStrategySSInactionContentFetch.java` (152 lines)

**Fired when:** the SDK sends the explicit inaction fetch event (`inactionDeliveryRequested == true`). This is the **short-circuit** case — `getSS` returns *only* this strategy and skips everything else.

Purpose: the SDK's inaction timer fired; deliver the actual content now. Server re-validates because the world may have changed since the qualification was written.

**Algorithm** (`.java:47-127`):
1. Fetch targets from `DELIVERY_TARGETS_SS_INACTION_CONTENT_FETCH` — expected to be exactly one target (the ID the SDK is fetching).
2. `shouldSkipTarget` (`.java:148-151`):
   - `!inAppTargetEval.processTargetExitQuery(params, ti)` — **exit query** eval, not the entry query. If the user no longer matches, drop.
   - OR `combinedQuotaCheck.targetQuotaReached(params, ti)`.
   - If skipped: `addToDeleteBatchTask(accountId, iid, targetId)` — schedule FStore cleanup. Return empty.
3. Look up the stored qualification proto: `fsClientUtils.getRecordsForOnlineTargets(accountId, iid, [targetId])` → `InactionQualificationProto{evtName, evtProps}`.
4. `cloneParamsWithActionEventDetails(params, actionEventName, actionEventProps)` — **rebuild** `ProcessParams` with the *original* trigger event that qualified the user, so personalisation (event-property replacements) resolves against the right event, not the fetch event.
5. `PayloadProviderSS.getPayload(target, newParams)` — full content payload.
6. Empty payload? Schedule delete and return empty.
7. `controlGroupSuppressor.shouldSuppress(...)`:
   - DELIVER → mark sent, emit under `inapp_notifs`
   - SUPPRESS → emit `SuppressEvent`
   - SKIP → nothing
8. **Always** `addToDeleteBatchTask(accountId, iid, targetId)` at the end — the qualification has served its purpose; batch-delete it from FStore.

Response key: `inapp_notifs` (delivery only).

### 3.7 `DeliveryStrategySuppressionAppLaunchedSS` — CG plumbing

Piggy-backed when the SDK includes `inAppSuppressIds` in the request (the SDK is reporting a CG suppression that fired locally). This strategy converts them into first-class `SuppressEvent`s so ES stats stay consistent. Uses `DeliveryTargetsAppLaunchedSS` as its target list.

---

## 4. Response-key matrix

| Strategy | Payload key | Contains |
|---|---|---|
| `DeliveryStrategyMetaSS` | `inapp_notifs_ss` | Bulk metadata (triggers + caps) for advanced-rules SS campaigns |
| `DeliveryStrategyAppLaunchedSS` — campaign win | `inapp_notifs_applaunched` | Full content for all eligible non-journey campaigns |
| `DeliveryStrategyAppLaunchedSS` — journey win | `inapp_notifs` | Full content for one journey in-app |
| `DeliveryStrategyAppLaunchedSSMeta` | `inapp_notifs_applaunched_meta` | Metadata for App-Launched inaction campaigns (with FStore write) |
| `DeliveryStrategySS` | `inapp_notifs` | Full content for the winning non-App-Launched target |
| `DeliveryStrategySSInaction` | `inapp_notifs_meta` | Metadata for one newly-qualified inaction campaign |
| `DeliveryStrategySSInactionContentFetch` | `inapp_notifs` | Delayed content when SDK's timer fires |

Plus every SS response gets `inapp_delivery_mode: "SS"`, `imc`, and (if set) `imp` from `InAppProcessorAdapter.fillMetadata` (`InAppProcessorAdapter.java:80-84`) + `InAppManager.fillMetadata` (`InAppManager.java:120-130`).

---

## 5. FStore usage (SS-only concern)

Only SS touches FStore for inaction — CS doesn't (the SDK owns state). All calls go through `com.wizrocket.inaction.InactionFSClientUtils`, all under `Context.ONLINE_INACTION`.

| Strategy | FStore operation | Timing |
|---|---|---|
| `AppLaunchedSSMeta` | `deleteOnlineTargetQualificationsForUser` (only if `isFreshLaunch`) | Before checking valid targets |
| `AppLaunchedSSMeta` | `onlineTargetQualificationNotPresent` | To filter already-qualified targets |
| `AppLaunchedSSMeta` | `targetQualificationBatchUpdate` | Batch put before shipping metadata |
| `SSInaction` | `onlineTargetQualificationNotPresent` | Filter |
| `SSInaction` | `addOnlineTargetQualification` | Per-target put before shipping metadata |
| `SSInactionContentFetch` | `getRecordsForOnlineTargets` | Fetch the qualification proto to restore original event context |
| `SSInactionContentFetch` | `InactionBatchDeleteTask.addToBatchTask` | Schedule cleanup after delivery / skip |

Failures at write time (`AppLaunchedSSMeta` batch, `SSInaction` per-target put) drop the payload — Ultron would rather ship nothing than ship inconsistent inaction state.

---

## 6. Per-request state carried in `ProcessParams`

`core/inapps/service/.../ProcessParams.java:27-60`. Fields the SS strategies rely on:

| Field | Set by | Used by |
|---|---|---|
| `eventName`, `eventProperties`, `eventPropertiesForEval` | request parsing | trigger eval, meta-delivery gate, inaction fetch |
| `profile`, `epHandlerProps`, `journeySteps`, `ipEnabled`, `firstSession` | request parsing | trigger eval, personalisation |
| `inAppEvalIds` (Set<Integer>) | SDK request body | `shouldSkipTarget` for advanced-rules campaigns |
| `inAppSuppressIds` | SDK request body | 3.7 suppression register |
| `shownToday` | precomputed for user | `InAppAdvancedFCapsGlobalQuotaCheck` |
| `inappTargetUserCounts` | precomputed per user | legacy per-target quota check |
| `targetsQualifiedSet` | mutated across strategies **in one request** | dedup across `MergeDelivery` composition |
| `journeySteps` | precomputed | journey progression |
| `isFreshLaunch` | LC container | 3.3 fresh-launch FStore wipe |
| `contentKeys` (private map) | populated by `getContentKey` | dedup content-variant resolution |

`targetsQualifiedSet` is the critical shared state — every strategy adds to it and every strategy reads it, so a target that already fired (e.g. via `AppLaunchedSS`) never fires again through `AppLaunchedSSMeta` in the same request.

---

## 7. End-to-end sequence — a worked example

Scenario: `Charged` event, account has `ADVANCED_FCAPS` enabled, `CLIENT_SIDE` disabled.

```
InAppProcessorAdapter.evalAndFillResponse
  ↓
  getDeliveryMode(account) → SS
  ↓
  inAppProcessor.process(params)
    ↓
    DeliveryStrategyProvider.getSS(params)
      ↓
      event != wzrk_fetch                     → not inaction content fetch
      metaDeliveryRequested(Charged) == false → skip MetaSS
      event != App Launched                   → skip AppLaunchedSS + AppLaunchedSSMeta
      inAppSuppressIds empty                  → skip suppression
      → composed list: [DeliveryStrategySS, DeliveryStrategySSInaction]
    ↓
    MergeDelivery{[SS, SSInaction]}.generatePayload
      ↓
      SS:
        fetch targets on Charged, sorted by priority
        for each target:
          skip if hasAdvancedRules && !inAppEvalIds.contains(id)
          if inAppTargetEval.processTarget(params, ti):
            if !combinedQuotaCheck.targetQuotaReached (advanced fcap => shownToday >= imp?):
              payload = PayloadProviderSS.getPayload(ti, params)
              suppressor.shouldSuppress(...):
                DELIVER: markAsSent, payloads[INAPP_NOTIFS] = [payload], return
        (else all skipped → empty)
      ↓
      SSInaction:
        fetch inaction targets on Charged
        FStore: onlineTargetQualificationNotPresent → validTargets
        for each valid target:
          if hasAdvancedRules && !inAppEvalIds contains id → skip
          FStore: addOnlineTargetQualification (persist)
          if hasAdvancedRules → drop payload (SDK already has meta)
          else → PayloadProviderMetaSS.getPayload → payloads[INAPP_NOTIFS_META]
          break                                          # only one per event
    ↓
  fillResponse: dedup payloads by target id, write to response
  markAsSent: bump TargetingCountManager
  convertSuppressEvents: no-op here (empty)
  ↓
  fillMetadata: response["inapp_delivery_mode"] = "SS", response["imc"] = N, response["imp"] = M
```

Response body sample (fields simplified):
```json
{
  "inapp_notifs": [
    { "ti": 1234, "wzrk_id": "1234_20260713", "w": {...}, "d": {...}, "wzrk_ttl": 3600 }
  ],
  "inapp_notifs_meta": [
    { "ti": 5678, "wzrk_id": "5678_20260713", "inaction_dur": 300 }
  ],
  "inapp_delivery_mode": "SS",
  "imc": 5,
  "imp": 10
}
```

The SDK renders the target from `inapp_notifs` immediately and arms a 300-second inaction timer for target 5678.

---

## 8. Distinguishing skips vs deliveries — one glance

| Return | Where it can happen | What the SDK sees |
|---|---|---|
| `DeliveryResult.EMPTY` from a strategy | no eligible target, or FStore write failed | that strategy contributes nothing |
| `PayloadMeta.markAsSent = false` | metadata-only payloads (MetaSS, SSInaction, AppLaunchedSSMeta) | SDK gets metadata; stats not incremented server-side |
| `PayloadMeta.markAsSent = true` | full deliveries (SS, AppLaunchedSS, SSInactionContentFetch) | SDK gets content; `TargetingCountManager.markAsSent` fires |
| `SuppressEvent` emitted | CG suppression during a full-delivery strategy | supplementary `NotificationSuppressed` event flows through LC→LP→ES |

---

## 9. Key files quick reference (SS-focused)

| Concern | File | Line |
|---|---|---|
| SS composer | `core/inapps/service/.../delivery/DeliveryStrategyProvider.java` | 70–99 |
| Mode gate (`InAppsFeature.CLIENT_SIDE`) | `core/lc/.../targeting/inapps/InAppProcessorAdapter.java` | 306–308 |
| MetaSS | `core/inapps/service/.../delivery/ss/DeliveryStrategyMetaSS.java` | 23–37 |
| AppLaunchedSS | `core/inapps/service/.../delivery/ss/DeliveryStrategyAppLaunchedSS.java` | 52–136 |
| AppLaunchedSSMeta | `core/inapps/service/.../delivery/ss/DeliveryStrategyAppLaunchedSSMeta.java` | 40–102 |
| SS (per-event) | `core/inapps/service/.../delivery/ss/DeliveryStrategySS.java` | 37–114 |
| SSInaction | `core/inapps/service/.../delivery/ss/DeliveryStrategySSInaction.java` | 42–137 |
| SSInactionContentFetch | `core/inapps/service/.../delivery/ss/DeliveryStrategySSInactionContentFetch.java` | 47–151 |
| Trigger eval impl | `core/lc/.../targeting/inapps/InAppTargetEvalImpl.java` | 28–66 |
| Shared eval engine | `core/lc/.../targeting/TargetEvalManager.java` | 79–96 |
| Combined quota check | `core/inapps/service/.../delivery/common/InAppCombinedQuotaCheck.java` | 24–33 |
| Advanced FCaps quota check | `core/inapps/service/.../delivery/common/InAppAdvancedFCapsGlobalQuotaCheck.java` | 16–33 |
| Meta payload builder | `core/inapps/service/.../payload/PayloadProviderMetaSS.java` | 20–31 |
| Content payload builder | `core/inapps/service/.../payload/PayloadProviderSS.java` | 26–75 |
| Response keys | `core/inapps/service/.../delivery/common/PayloadKey.java` | 9–14 |
| Target providers module | `core/inapps/service/.../guice/InAppsCommonModule.java` | 127–186 |
| ProcessParams | `core/inapps/service/.../ProcessParams.java` | 27–60 |
| Adapter fill + markAsSent | `core/lc/.../targeting/inapps/InAppProcessorAdapter.java` | 218–291 |

---

## 10. Take-aways (SS-specific)

- **SS is not one strategy but a set** — `DeliveryStrategyProvider.getSS` composes several, and `MergeDelivery` folds their results. Understanding which combination fires for which event is the mental model.
- **`inAppTargetEval.processTarget` is the shared entry-query check**; `processTargetExitQuery` is used only by the delayed content-fetch path to re-validate before serving.
- **Advanced-rules campaigns are gated on `inAppEvalIds`** — the SS server refuses to fire them unless the SDK explicitly asks (having previously received metadata via MetaSS / AppLaunchedSSMeta). This is the guard against double-firing when advanced local rules are enabled.
- **Frequency-cap enforcement is bifurcated by feature flag** — `ADVANCED_FCAPS` on and not-a-journey ⇒ global-daily only (SDK owns the rest); otherwise ⇒ legacy per-target `tlc`/`tdc`/`efc`.
- **Inaction correctness relies on FStore**, and every write is fail-safe: batch write for App-Launched, single write per event otherwise. A write failure drops the payload, never risks over-delivery.
- **Content-fetch replays the original event context** — `cloneParamsWithActionEventDetails` rebuilds `ProcessParams` with the qualification's stored event so personalisation resolves against the right event.
- **Control-group suppression is first-class** — CG-suppressed targets still flow through as `SuppressEvent`s so the A/B math downstream is correct.
- **Content vs metadata split saves bandwidth and CPU** — regular events return metadata; content is fetched only when the SDK is about to render.
