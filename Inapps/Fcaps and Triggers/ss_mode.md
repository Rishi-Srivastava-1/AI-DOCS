# In-app SS (Server-Side) Mode — Fcaps + Triggers Deep Dive

Scope: how in-apps flow **when the account is in SS (server-side) mode under the new-generation dispatcher** — the flow that keeps trigger evaluation and segmentation on the server but ships trigger metadata to the SDK so it can (a) know what to ask for and (b) enforce advanced fcaps/display rules on the device before rendering.

Do not confuse this with the pre-6.0 dispatcher (see [`legacy_flow.md`](./legacy_flow.md)) — that path is a different code branch entirely.

Everything below is verified against current `master` and cross-checked with the BE TAN (`4008706201`), SDK TAN (`4085613262`), and Key Decisions doc (`4052779089`).

---

## 1. When SS mode is chosen

Two-step gate, same as CS.

### 1.1 Manager selector — pick the new adapter

`core/lc/src/java/com/wizrocket/lc/targeting/inapps/InAppManagerProvider.java:21-35`
```java
boolean useAdvancedEvalManager =
        supportChecker.advancedInAppsSupported(sdkVersion, device) &&
                localTriggeringRequired(accountId);
return useAdvancedEvalManager ? newEvalManager : legacyEvalManager;

private boolean localTriggeringRequired(int accountId) {
    return featureFlag.isEnabled(InAppsFeature.ADVANCED_FCAPS, accountId) ||
            featureFlag.isEnabled(InAppsFeature.ADVANCED_DISPLAY_TRIGGERS, accountId) ||
            featureFlag.isEnabled(InAppsFeature.CLIENT_SIDE, accountId) ||
            inactionDynamicConfig.getChannelsInactionWithEventProps(accountId)
                    .contains(ProfileReach.Mode.InApp.toString().toLowerCase());
}
```

The new adapter (`InAppProcessorAdapter`) is chosen when SDK ≥ 60000 (Android/iOS) AND any of `ADVANCED_FCAPS` / `ADVANCED_DISPLAY_TRIGGERS` / `CLIENT_SIDE` / inaction-w-event-props is enabled.

### 1.2 CS-vs-SS gate

`InAppProcessorAdapter.java:306-308`
```java
private Mode getDeliveryMode(int accountId) {
    return featureFlags.isEnabled(InAppsFeature.CLIENT_SIDE, accountId) ? Mode.CS : Mode.SS;
}
```

Any account **without** `CLIENT_SIDE` on but with any of the other three flags on → **SS mode under the new dispatcher**. This is the common case for accounts that adopted advanced fcaps / display triggers without switching to CS delivery.

`Mode` enum at `DeliveryStrategyProvider.java:101-103`:
```java
public enum Mode { CS, SS }
```

---

## 2. The composed strategy set

Unlike CS (which is essentially one bundling strategy + journey), SS is a **set of strategies whose composition depends on the event**.

`core/inapps/service/.../delivery/DeliveryStrategyProvider.java:70-99`
```java
private Optional<InAppDelivery> getSS(ProcessParams params) {
    List<InAppDelivery> deliveries = new ArrayList<>();
    if (EventsHelper.inactionDeliveryRequested(params.getEventName(), params.getEventProperties())) {
        return Optional.of(deliveryStrategySSInactionContentFetch);
    }
    if (metaDeliveryRequested(params.getEventName(), params.getEventProperties())) {
        deliveries.add(deliveryStrategyMetaSS);            // metadata for all SS campaigns
    }
    if (APP_LAUNCHED.equals(params.getEventName())) {
        deliveries.add(deliveryStrategyAppLaunchedSS);     // full campaign content
        deliveries.add(deliveryStrategyAppLaunchedSSMeta); // inaction App Launched meta
    } else {
        deliveries.add(deliveryStrategySS);                // per-event server eval
        deliveries.add(deliveryStrategySSInaction);
    }
    if (!params.getInAppSuppressIds().isEmpty()) {
        deliveries.add(deliveryStrategySuppressionAppLaunchedSS);
    }
    return MergeDelivery.of(deliveries);
}
```

`metaDeliveryRequested` (`EventsHelper.java:16-25`) → `APP_LAUNCHED` OR `wzrk_fetch` with `t=5`.
`inactionDeliveryRequested` (`EventsHelper.java:27-31`) → `wzrk_fetch` with `t=inaction`.

Composition matrix:

| Request event | Strategies composed | Payload keys populated |
|---|---|---|
| Inaction content fetch (`wzrk_fetch t=inaction`) | **Only** `SSInactionContentFetch` | `inapp_notifs` |
| `App Launched` | `MetaSS` + `AppLaunchedSS` + `AppLaunchedSSMeta` (+ `SuppressionAppLaunchedSS` if suppressions) | `inapp_notifs_ss` + `inapp_notifs_applaunched`/`inapp_notifs` + `inapp_notifs_applaunched_meta` |
| `wzrk_fetch t=5` (mid-session refresh) | `MetaSS` + `SS` + `SSInaction` (+ suppression if any) | `inapp_notifs_ss` + `inapp_notifs` + `inapp_notifs_meta` |
| Regular event (e.g. `Charged`) | `SS` + `SSInaction` (+ suppression if any) | `inapp_notifs` + `inapp_notifs_meta` |

`MergeDelivery.of(deliveries)` (`core/inapps/service/.../delivery/MergeDelivery.java:29-37`) folds each sub-strategy's `DeliveryResult` into one — merging the `payloads` map and appending `suppressedEvents`.

---

## 3. The two-step dispatch model — metadata + content

The BE TAN puts it plainly:
> On `App Launched`, the SDK will retrieve all in-apps along with their respective display triggers, advanced fcaps, and display rules. For CS in-apps, the content will also be included in the payload.

Under SS, the SDK's role changes from a "receive content and enforce basic caps" client to an "actively participate in dispatch" client:

1. **Metadata bootstrap** — on App Launched / mid-session fetch, the SDK receives triggers + limits for every SS campaign under `inapp_notifs_ss`.
2. **SDK evaluates locally** — on every user event, the SDK evaluates `whenTriggers` + `frequencyLimits` + `occurrenceLimits` against its local impression map.
3. **SDK asks server for content** — winning campaign IDs go in the request meta under `inapps_eval: [id1, id2, ...]`.
4. **Server verifies + returns content** — server re-checks segmentation + CG + global daily cap, then returns content under `inapp_notifs`.
5. **SDK renders + reports back** — normal render path, view/click events flow back through LC.

Legacy simple-fcap campaigns (no advanced rules) don't ride this loop — they're evaluated purely server-side as before (see §4.4 for the "May 2024 optimisation").

Rationale (BE TAN "Note on advanced fcap checks", verbatim):
> The validation of advanced fcaps will be handled solely on the SDK. This approach is based on the assumption that a comparable implementation to the Leanplum SDK will be developed, where a `{occurrenceId, timestamp}` map of the last 100 in-app occurrences is stored on the device. The transfer of such amount of data back and forth seems impractical.

---

## 4. Each SS strategy in detail

All six SS strategies live under `core/inapps/service/src/main/java/com/wizrocket/inapps/lc/delivery/ss/`.

### 4.1 `DeliveryStrategyMetaSS` — bulk metadata bootstrap

`DeliveryStrategyMetaSS.java` (38 lines)

**Fires:** on meta-delivery events (App Launched or `wzrk_fetch t=5`).

**Target list** — `DeliveryTargetsMetaSS.java:20-44`:
```java
List<TargetingInfo> targets = t.getWebInApp(ProfileReach.Mode.InApp, params.getDevice());
Set<Integer> targetsOnAppLaunch =
        t.getWebInAppNonNull(APP_LAUNCHED, InApp, device).stream()
                .filter(ti -> !ti.isMultiTrigger())
                .map(TargetingInfo::getID)
                .collect(Collectors.toSet());

return targets.stream()
        .filter(ti -> ti.getAdvancedLimits().map(AdvancedLimits::hasAdvancedRules).orElse(false))
        .filter(ti -> !ti.isJourneyTarget())
        .filter(ti -> !ti.isClientSide())
        .filter(ti -> !targetsOnAppLaunch.contains(ti.getID()))
        .toList();
```
Filters:
- `hasAdvancedRules` — has `frequencyLimits` or `occurrenceLimits` (per `AdvancedLimits.hasAdvancedRules` at `AdvancedLimits.java:63-67`).
- Non-journey (journeys always go via journey strategy).
- Not CS.
- Not a single-trigger App-Launched target (those are handled by `AppLaunchedSS` with full content).

**Body** (`.java:23-37`) — trivial: no segmentation, no CG, no cap check. Just build `PayloadMeta` via `PayloadProviderMetaSS.getPayload(ti, params)` for each target.

**Payload** — metadata only (no content field). `PayloadProviderMetaSS.java:20-31`:
```java
BasicDBObject payload = contentUtil.getCommonFields(params, ti);
addExtraMetadata(payload, ti, params);
return DeliveryResult.PayloadMeta.builder().sdkPayload(payload).ti(ti).build();
```
Adds `templateName` (custom-code template) and `inactionDuration` (if any) on top of the common fields (`ti`, `wzrk_id`, content key, priority, `whenTriggers`, `frequencyLimits`/`legacy limits`, `excludeGlobalFCaps`, `occurrenceLimits` if enabled).

**Response key:** `inapp_notifs_ss` (`PayloadKey.java:10`).

**No `markAsSent`** — this is bootstrap metadata, not a render signal.

### 4.2 `DeliveryStrategyAppLaunchedSS` — first-render pick

`DeliveryStrategyAppLaunchedSS.java` (159 lines)

**Fires:** on `App Launched` only.

**Purpose:** pick what to render on session-start in one round-trip — either **one journey inapp** or **all qualifying campaign inapps** (never both).

**Target list** — `DeliveryTargetsAppLaunchedSS.java:24-44`:
Union of App-Launched-triggered campaigns AND no-trigger campaigns, non-CS, non-`InstantEventClosure`, sorted by priority desc + id asc.

**Algorithm** (`.java:52-136`):
```
isCampaignPayload = false
continueTargetSearch = true
payloadKey = INAPP_NOTIFS_APPLAUNCHED

for ti in ordered targets:
    if (isCampaignPayload && ti.isJourneyTarget()) continue     // once a campaign wins, journeys are blocked
    if (already in targetsQualifiedSet)                continue
    if (!inAppTargetEval.processTarget(params, ti))    continue    // segmentation
    if (combinedQuotaCheck.targetQuotaReached(params, ti)) continue    // caps

    payload = PayloadProviderSS.getPayload(ti, params)          // full content
    targetsQualifiedSet.add(ti.getID())

    result = ControlGroupSuppressor.shouldSuppress(ti, i, j, notifClickId, contentKey)
    switch (result):
        SKIP     -> nothing
        SUPPRESS -> if journey:
                        emit SuppressEvent (journey suppression is first-class)
                    else:
                        isCampaignPayload = true          // "campaign lock-in" — future journeys blocked
                        payload' = PayloadProviderSuppressedCS.getPayload(ti, params, cgId)
                        deliverPayloads.add(payload')
        DELIVER  -> payload.markAsSent = true
                    deliverPayloads.add(payload)
                    if journey:
                        continueTargetSearch = false      // one journey wins alone
                        payloadKey = INAPP_NOTIFS         // journey uses inapp_notifs
                    else:
                        isCampaignPayload = true          // campaign path — collect ALL eligible
                        payloadKey = INAPP_NOTIFS_APPLAUNCHED

if deliverPayloads non-empty: payloads[payloadKey] = deliverPayloads
```

Key rules:
- **Journey wins** → sent alone under `inapp_notifs` (SDK renders as the sole App Launched inapp).
- **Campaign wins** → **all** eligible campaigns (not just one) go out under `inapp_notifs_applaunched`. Journeys after that point are ignored.
- **Suppressed campaign** still locks in the campaign path — subsequent journeys blocked. Stubbed suppressed payload is emitted via `PayloadProviderSuppressedCS` so SDK reports the CG event.

**Quota check integration** — `combinedQuotaCheck.targetQuotaReached(params, ti)` (`.java:157`). For campaigns under `ADVANCED_FCAPS` this checks only global daily; for journeys it uses legacy per-target caps.

Response keys used depending on outcome:
- `inapp_notifs_applaunched` (campaigns win) — `PayloadKey.java:11`
- `inapp_notifs` (journey wins) — `PayloadKey.java:9`

### 4.3 `DeliveryStrategyAppLaunchedSSMeta` — inaction bootstrap on App Launched

`DeliveryStrategyAppLaunchedSSMeta.java` (113 lines)

**Fires:** on `App Launched`, alongside 4.2.

**Purpose:** ship **metadata** for inaction campaigns triggered on App Launched, while atomically persisting the qualification to FStore so it survives the delay.

**Target list** — inaction targets on App Launched (per doc-comment: *"inaction target payloads on App Launched events"*).

**Algorithm** (`.java:40-102`):
1. Filter targets: `shouldProcessTarget = !ti.isJourneyTarget() && !alreadyQualified && inAppTargetEval.processTarget(...)`.
2. Compute qualified target IDs.
3. **FStore check:**
   - If `params.isFreshLaunch()`: `fsClientUtils.deleteOnlineTargetQualificationsForUser(accountId, iid)` (a fresh session wipes prior state) and treat all as valid.
   - Else: `validTargets = fsClientUtils.onlineTargetQualificationNotPresent(accountId, iid, targetIds)` — only unqualified ones eligible.
4. For each valid target, stage an FStore put entry: `fsClientUtils.updatePutBatchEntriesForOnlineTargets(accountId, iid, ti, eventName, eventProperties, putEntries)`.
5. **Batch write:** `fsClientUtils.targetQualificationBatchUpdate(accountId, iid, Context.ONLINE_INACTION, putEntries)`.
6. On success → emit metadata under `inapp_notifs_applaunched_meta` (`PayloadKey.java:13`). On failure → report P1, return `DeliveryResult.EMPTY`.

Payload built via `PayloadProviderMetaSS` (same as 4.1).

**Design rationale:** metadata only leaves LC if the qualification is durable. Otherwise the SDK could arm a timer for a campaign we forgot we qualified them for, causing over-delivery.

**No quota check here** — metadata-only, SDK enforces on inaction fetch (4.6).

### 4.4 `DeliveryStrategySS` — per-event dispatch (the workhorse)

`DeliveryStrategySS.java` (115 lines)

**Fires:** on any non-App-Launched event that isn't a meta-delivery / inaction fetch. I.e., real user events like `Charged`, `Notification Clicked`.

**Target list** — `DeliveryTargetsSS.java:20-37`:
```java
List<TargetingInfo> targets =
        t.getWebInAppNonNull(params.getEventName(), ProfileReach.Mode.InApp, params.getDevice());
return targets.stream()
        .filter(ti -> !ti.isClientSide())
        .filter(ti -> ti.getTargetType() != TargetingDao.TargetType.InstantEventClosure)
        .sorted(Comparator.comparing(TargetingInfo::getPriority, Comparator.reverseOrder())
                .thenComparing(TargetingInfo::getID))
        .collect(Collectors.toList());
```
Targets keyed by the request event, non-CS, non-InstantEventClosure, priority-desc + id-asc.

**Bound as** `@Named(DELIVERY_SS)` at `InAppsCommonModule.java:96-105` — factory-injected via `DeliveryStrategyFactory` for target-provider injection.

**Algorithm** (`.java:37-88`):
```
for ti in ordered targets:
    if (shouldSkipTarget(params, ti)) continue
    payload = PayloadProviderSS.getPayload(ti, params)             // full content
    if (payload empty) continue
    targetsQualifiedSet.add(ti.getID())
    result = ControlGroupSuppressor.shouldSuppress(ti, ..., contentKey)
    switch (result):
        SKIP     -> keep looking
        SUPPRESS -> add SuppressEvent; keep looking (unlike App Launched — no lock-in)
        DELIVER  -> payload.markAsSent = true
                    payloads[INAPP_NOTIFS] = [payload]
                    return                                          // one per event
```

**`shouldSkipTarget`** (`.java:106-114`) — the critical guard:
```java
boolean skipCampaign = !ti.isJourneyTarget()
        && !params.getInAppEvalIds().contains(ti.getID())
        && ti.getAdvancedLimits().map(AdvancedLimits::hasAdvancedRules).orElse(false);
return skipCampaign
        || params.getTargetsQualifiedSet().contains(ti.getID())
        || !inAppTargetEval.processTarget(params, ti)
        || combinedQuotaCheck.targetQuotaReached(params, ti);
```

The `skipCampaign` line is the **May 2024 optimisation** referenced in the BE TAN:
> *Reduce the expected increase in SDK traffic for the SS case, where post mvp we will send payload meta for all campaign in-apps. This will affect all clients which currently use in-apps due to the advanced limits / occurrences feature. The idea is to not send meta for campaigns that don't use these advanced features. These campaign can be handled purely server-side as we process journey in-apps. They compete with those requested from SDK and the BE finds the one with highest priority that qualifies (inapps_eval list + 'simple' campaigns + journey campaigns).*

Translation:
- **Advanced-rule campaigns**: only delivered when the SDK explicitly asks (`inAppEvalIds.contains(targetId)`). Otherwise the SDK is expected to have gated locally using metadata from earlier — server refuses to double-fire.
- **Simple (non-advanced) campaigns**: still evaluated purely server-side per event. No metadata was ever shipped; server picks by priority + segmentation + caps.
- **Journey targets**: always considered per event regardless of `inAppEvalIds`.

**Response key:** `inapp_notifs` (`PayloadKey.java:9`). Same key as legacy — one payload per event.

### 4.5 `DeliveryStrategySSInaction` — inaction qualification per event

`DeliveryStrategySSInaction.java` (138 lines)

**Fires:** on non-App-Launched events, alongside 4.4.

**Purpose:** when a normal event occurs, arm any inaction campaigns whose entry trigger matches — persist qualification to FStore so the SDK's inaction timer starts.

**Target list** — non-App-Launched inaction targets, event-keyed.

**`shouldSkipTarget`** (`.java:131-137`):
```java
boolean isDisqualifiedBySDK = !params.getInAppEvalIds().contains(ti.getID()) &&
        ti.getAdvancedLimits().map(AdvancedLimits::hasAdvancedRules).orElse(false);
return isDisqualifiedBySDK
        || params.getTargetsQualifiedSet().contains(ti.getID())
        || !inAppTargetEval.processTarget(params, ti);
```
Same "advanced-rule needs `inAppEvalIds`" gate as 4.4.

**Algorithm** (`.java:42-87`):
```
qualifiedTargets = fetchTargets(params) filtered by shouldSkipTarget=false
targetIds = qualifiedTargets.map(id)
validTargets = fsClientUtils.onlineTargetQualificationNotPresent(accountId, iid, targetIds)  // dedup

for ti in qualifiedTargets (priority order):
    if not in validTargets: skip
    fStoreUpdate = fsClientUtils.addOnlineTargetQualification(accountId, uid, ti, eventName, eventProperties)
    if !fStoreUpdate: skip and report P1
    targetsQualifiedSet.add(ti.getID())
    if hasAdvancedRules: return Optional.empty()      // SDK already has meta from AppLaunchedSSMeta
    payload = PayloadProviderMetaSS.getPayload(ti, params)
    payloads[INAPP_NOTIFS_META] = [payload]
    return   // ONE per event, priority-ordered
```

- **Per-target single FStore write**, unlike 4.3 which uses a batch write for App-Launched inaction.
- **No CG check** — metadata only, SDK handles CG at render time on fetch.
- **No caps check** — SDK enforces on the inaction-content-fetch path (4.6).

**Response key:** `inapp_notifs_meta` (`PayloadKey.java:14`). Only one payload per event.

### 4.6 `DeliveryStrategySSInactionContentFetch` — delayed content delivery

`DeliveryStrategySSInactionContentFetch.java` (152 lines)

**Fires:** on the explicit inaction fetch event (`wzrk_fetch t=inaction`). This is the composer's short-circuit case — `getSS` returns *only* this strategy, skipping everything else.

**Purpose:** the SDK's inaction timer fired; deliver the actual content now. Re-validate the world because it may have changed since qualification was written.

**Algorithm** (`.java:47-127`):
```
targets = fetchTargets(params)      // expected: exactly one target ID (the one the SDK is fetching)
if empty: return EMPTY
target = targets[0]

if (shouldSkipTarget(params, target)):
    addToDeleteBatchTask(accountId, iid, targetId)          // schedule FStore cleanup
    return EMPTY

// Look up the original qualification proto
targetRecordMap = fsClientUtils.getRecordsForOnlineTargets(accountId, iid, [targetId])
qualification = targetRecordMap.get(targetId)               // Inaction.InactionQualificationProto
if empty: return EMPTY

actionEventName  = qualification.getEvtName()
actionEventProps = qualification.getEvtProps()  (parsed JSON)

// Rebuild params with the ORIGINAL trigger event context, not the fetch event
newParams = cloneParamsWithActionEventDetails(params, actionEventName, actionEventProps)

payload = PayloadProviderSS.getPayload(target, newParams)   // full content, personalized against original event
if empty: addToDeleteBatchTask(...); return EMPTY

result = ControlGroupSuppressor.shouldSuppress(target, params.i, params.j, ...)
switch (result):
    SKIP     -> nothing
    SUPPRESS -> add SuppressEvent
    DELIVER  -> payload.markAsSent = true
                payloads[INAPP_NOTIFS] = [payload]

addToDeleteBatchTask(accountId, iid, targetId)              // ALWAYS clean up
return result
```

**`shouldSkipTarget`** (`.java:148-151`):
```java
private boolean shouldSkipTarget(ProcessParams params, TargetingInfo ti) {
    return !inAppTargetEval.processTargetExitQuery(params, ti)     // exit query re-check
            || combinedQuotaCheck.targetQuotaReached(params, ti);
}
```
Two checks:
- **Exit query re-check** — is the user still segment-eligible? Uses `processTargetExitQuery` (`InAppTargetEvalImpl.java:56-66`) which evaluates the target's **`xQuery`** ("mini-who" query) against the current profile. If the user has exited the segment during the delay, drop.
- **Quota check** — daily/lifetime caps re-validated.

Key details:
- **Event-context replay** via `cloneParamsWithActionEventDetails` (`.java:129-141`): the personalization/replacements in `PayloadProviderSS.prepareContent` need to see the *original* event's properties (e.g., the `Charged` cart contents that qualified the user), not the `wzrk_fetch` event.
- **Always delete** the FStore qualification after handling — success or skip. `InactionBatchDeleteTask.addToBatchTask(accountId, uid, Context.ONLINE_INACTION, [key])` schedules the batch delete.
- **Response key:** `inapp_notifs` (delivery). Suppression flows through `DeliveryResult.suppressedEvents`.

### 4.7 `DeliveryStrategySuppressionAppLaunchedSS` — SDK-reported suppressions

Bound at `InAppsCommonModule.java:117-125` — it's an instance of `DeliveryStrategySuppressionsCS` (from the CS package, but reused here) with `DeliveryTargetsAppLaunchedSS` as its target list. Piggybacks when the SDK includes `inapps_suppressed` in the request meta, converts each into a first-class `SuppressEvent` so LC → LP → ES stats remain consistent.

Since journeys are SS, App-Launched suppression tracking must be supported even in "pure SS" accounts.

---

## 5. The `inapps_eval` protocol — SDK-driven server dispatch

Central to how advanced-rules SS campaigns get their content.

### 5.1 The contract

Per the BE TAN:
> On each user action the SDK computes which SS in-apps need to be displayed, taking into account global/local limits, and includes their IDs in a new `inapps_eval` property in the `meta` payload.

Request meta (from SDK on a regular event):
```json
[
  {
    "g": "__d5e0cae60ea14978b908d717c8aa82bf",
    "type": "meta",
    "inapps_eval": [55001, 55002]
  }
]
```

### 5.2 The server side

`inapps_eval` is unpacked into `ProcessParams.inAppEvalIds` at `InAppProcessorAdapter.java:180`:
```java
.inAppEvalIds(mobileContainer.getEvalInApps())
```

In `DeliveryStrategySS.shouldSkipTarget` (`.java:106-114`):
```java
boolean skipCampaign = !ti.isJourneyTarget()
        && !params.getInAppEvalIds().contains(ti.getID())
        && ti.getAdvancedLimits().map(AdvancedLimits::hasAdvancedRules).orElse(false);
```

Meaning:
- Journey targets are always in play.
- Simple campaigns (no advanced rules) are always in play.
- **Advanced-rule campaigns are gated on `inAppEvalIds`** — server refuses to fire them unless the SDK explicitly asserts they've passed local rules.

### 5.3 The double-fire guard

This is the whole reason the guard exists: metadata for advanced-rule campaigns has already been shipped via `MetaSS` / `AppLaunchedSSMeta`. The SDK is expected to enforce `frequencyLimits`/`occurrenceLimits` locally. If the SDK says "eligible" (via `inapps_eval`), the server takes its word.

If a buggy SDK asserts `inAppEvalIds` when it shouldn't, the server has no way to detect that — this is a documented trust assumption.

Simple (non-advanced) campaigns don't need this because the server holds authoritative fcap state for them (via legacy `tlc`/`tdc` + SDK-supplied `TARGET_SHOWN_LIST`).

---

## 6. Fcap enforcement in SS

Both the composed strategies and the quota-check plumbing.

### 6.1 The gate — `InAppCombinedQuotaCheck`

`core/inapps/service/.../delivery/common/InAppCombinedQuotaCheck.java:24-33`
```java
public boolean targetQuotaReached(ProcessParams params, TargetingInfo ti) {
    boolean fCapsEnabled = featureFlags
            .isEnabled(InAppsFeature.ADVANCED_FCAPS, params.getAccountId());
    if (fCapsEnabled && !ti.isJourneyTarget()) {
        return newQuotaCheck.targetQuotaReached(params, ti);   // global-only
    }
    return legacyQuotaCheck.targetQuotaReached(params, ti);    // legacy per-target + global
}
```

Routing:
- `ADVANCED_FCAPS` on AND target is not a journey → **advanced** (`InAppAdvancedFCapsGlobalQuotaCheck`).
- Otherwise → **legacy** (`TargetEvalManager.targetQuotaReached` via the lambda binding at `InAppsLcModule.java:50-63`).

### 6.2 Advanced quota check — global daily only

`InAppAdvancedFCapsGlobalQuotaCheck.java:16-33`
```java
boolean excludeGlobalFCaps = ti.getAdvancedLimits().map(AdvancedLimits::excludeGlobalFCaps).orElse(false);
if (excludeGlobalFCaps) return false;

return Optional.ofNullable(accountProvider.apply(params.getAccountId()))
        .map(Account::getInappMaxPerUserPerDay)
        .map(l -> shownToday >= l)
        .orElse(true);        // fail-closed if account unresolvable
```

Per-target daily/lifetime is **not** checked here — SDK owns it. Server only guards the global daily cap (with per-campaign opt-out via `AdvancedLimits.excludeGlobalFCaps`).

### 6.3 Legacy quota check — per-target + global

`TargetEvalManager.targetQuotaReached` at `.java:297-345` — the same routine used by the legacy dispatcher. Checks `efc`, per-target `tdc`/`tlc` (against SDK-supplied `TARGET_SHOWN_LIST`), then global daily (against SDK-supplied `imp`). See [`legacy_flow.md`](./legacy_flow.md) §5 for the full walkthrough.

### 6.4 Which SS strategies invoke the quota check

**Yes (content-emitting):**
- `DeliveryStrategyAppLaunchedSS` (`.java:157`)
- `DeliveryStrategySS` (`.java:113`)
- `DeliveryStrategySSInactionContentFetch` (`.java:150`)

**No (metadata-only):**
- `DeliveryStrategyMetaSS`
- `DeliveryStrategySSInaction`
- `DeliveryStrategyAppLaunchedSSMeta`
- `DeliveryStrategySuppressionAppLaunchedSS`

The pattern is uniform: **quota check fires only when actual content is about to ship**. Metadata payloads don't count against caps because they don't cause a render — the SDK re-checks at render time.

### 6.5 Cap counters — SDK-supplied, not server-persisted

Same as legacy — the SDK ships `TARGET_SHOWN_LIST` (`tlc`) and `imp` in every request. Read at `MobileAppRequestContainer.java:670-675`, threaded through `ProcessParams.inappTargetUserCounts` + `shownToday`. See `legacy_flow.md` §3 for the details.

Under advanced-fcap SS, the SDK's `{occurrenceId, timestamp}` map (last 100 impressions) tracks per-campaign counts locally. The `TARGET_SHOWN_LIST` is still sent for the simple-fcap fallback and for `inapp_stale` housekeeping.

---

## 7. FStore usage (inaction only)

All FStore operations in SS are through `com.wizrocket.inaction.InactionFSClientUtils`, all under `Context.ONLINE_INACTION`. No fcap counter is ever stored in FStore.

| Strategy | FStore operation | Timing |
|---|---|---|
| `AppLaunchedSSMeta` | `deleteOnlineTargetQualificationsForUser` | If `isFreshLaunch` — before validation |
| `AppLaunchedSSMeta` | `onlineTargetQualificationNotPresent` | Filter already-qualified |
| `AppLaunchedSSMeta` | `targetQualificationBatchUpdate` | Batch put before shipping metadata |
| `SSInaction` | `onlineTargetQualificationNotPresent` | Filter |
| `SSInaction` | `addOnlineTargetQualification` | Per-target put before shipping metadata |
| `SSInactionContentFetch` | `getRecordsForOnlineTargets` | Fetch stored qualification proto |
| `SSInactionContentFetch` | `InactionBatchDeleteTask.addToBatchTask` | Cleanup after delivery / skip |

Fail-safe rule: any write failure drops the payload rather than proceed. Batch writes for App-Launched inaction; per-target writes on regular events. See [`ss_deep_dive.md`](../ss_deep_dive.md) §5 for the earlier deep dive.

---

## 8. Content-payload build (`PayloadProviderSS`)

`core/inapps/service/.../payload/PayloadProviderSS.java:26-75`

```java
String contentKey = params.getContentKey(ti, contentUtil);            // memoised per target
BasicDBObject originalMessage = ti.message(params.getDevice(), contentKey, null);
if (originalMessage == null) return Optional.empty();

BasicDBObject payload = (BasicDBObject) originalMessage.copy();
dimensionsFix.apply((BasicDBObject) payload.get("w"), payload, accountId, ti.getID());

if (ti.isJourneyTarget()) {
    payload.putAll(contentUtil.getJourneyFields(params, ti).toMap());
} else {
    payload.putAll(contentUtil.getCommonFields(params, ti).toMap());
    contentUtil.addInappDelayOnTriggerIfEnabled(accountId, payload, ti);
    payload.put(WZRK_TTL, contentUtil.getInAppTTLWithDelay(accountId, ti));
    contentUtil.removeLegacyMdcLimit(payload, accountId);
}

if (!contentBuilder.prepareContent(...)) return Optional.empty();      // personalization + replacements

String notificationClickedId = contentUtil.getNotificationClickId(ti.getID());
return PayloadMeta{batchId, contentKey, notificationClickedId, sdkPayload=payload, ti};
```

Fields shipped:
- Full content (copy of `ti.message(...)`)
- `ti`, `wzrk_id`, `NOTIFICATION_CONTENT_KEY`, `priority`, `whenTriggers`
- Under `ADVANCED_FCAPS`: `frequencyLimits`, `excludeGlobalFCaps`
- Under `ADVANCED_DISPLAY_TRIGGERS`: `occurrenceLimits`
- Otherwise: legacy `tlc`/`tdc`/`mdc`/`efc`
- `wzrk_ttl` (absolute epoch seconds — not offset, unlike CS)
- Optional `inappDelayAfterTrigger` (feature-flag gated)
- **Journey targets get `getJourneyFields`** (`ContentUtil.java:96-110`) — a slimmer set including `wzrk_ttl` and legacy limits only (no advanced limits or `whenTriggers`).

Metadata-only strategies (`MetaSS`, `SSInaction`, `AppLaunchedSSMeta`) call `PayloadProviderMetaSS` instead (`.java:20-31`) — same `getCommonFields` output plus template name and inaction duration, but **no content copy, no `wzrk_ttl`**.

---

## 9. Response assembly

`InAppProcessorAdapter.evalAndFillResponse` orchestrates the whole thing.

### 9.1 Composition & merge

`.java:142-216`:
```java
ProcessParams params = paramsBuilder.build();
return Profiler.reportTime("...",
        () -> inAppProcessor.process(params).map(deliveryResult -> {
            fillResponse(deliveryResult, response);              // writes payloads to response
            markAsSent(deliveryResult, accountId, ...);          // bumps TargetingCountManager
            return convertSuppressEvents(...);                    // supplementary CG events
        }).orElse(null));
```

### 9.2 Dedup by target id

`.java:218-236`:
```java
Set<Integer> ids = payload.stream()
        .map(o -> (BasicDBObject) o)
        .map(dbObject -> dbObject.getInt(ContentUtil.PAYLOAD_TARGET_ID /* "ti" */, 0))
        .collect(Collectors.toSet());
for (PayloadMeta meta : payloadMeta) {
    if (!ids.contains(meta.getTi().getID())) {
        payload.add(meta.getSdkPayload());
        ids.add(meta.getTi().getID());
    }
}
```
Same target from multiple sub-strategies → emitted once. Uses `targetsQualifiedSet` on `ProcessParams` (a mutable set shared across strategies during one request) for pre-dedup, then a payload-level dedup by target id here as belt-and-braces.

### 9.3 markAsSent

`.java:277-291`:
```java
deliveryResult.getPayloads().values().stream().flatMap(List::stream)
        .filter(PayloadMeta::isMarkAsSent).forEach(payloadMeta -> {
            addToQualifiedJourneySteps(baseRequestDto, ti, journeySteps, response, TARGET_GROUP_ID);
            TargetingCountManager.getInstance().markAsSent(1, accountId,
                    payloadMeta.getTi().getID(),
                    payloadMeta.getBatchId(), device, ProfileReach.Mode.InApp,
                    payloadMeta.getContentKey());
        });
```
Only content-emitting strategies set `markAsSent=true`. Aggregate stats only — not fcap state.

### 9.4 fillMetadata — ceilings + delivery mode

`.java:80-84` (adapter override):
```java
response.put(DELIVERY_MODE_PAYLOAD_KEY, getDeliveryMode(accountId).name());
```
Plus `InAppManager.fillMetadata` (`.java:120-130`) writes `imc` and `imp`.

Response fields overall:

| Key | Set by | Notes |
|---|---|---|
| `imc` | `InAppManager.fillMetadata` | Account session ceiling — SDK-enforced |
| `imp` | `InAppManager.fillMetadata` (only if `!= -1`) | Account daily ceiling |
| `inapp_delivery_mode` | `InAppProcessorAdapter.fillMetadata` | Value: `"SS"` |
| `inapp_notifs_ss` | `MetaSS`, `SS` (via merge — but SS uses `INAPP_NOTIFS`, not `_SS`) | Metadata bundle |
| `inapp_notifs_applaunched` | `AppLaunchedSS` (campaign path) | Full campaign content |
| `inapp_notifs_applaunched_meta` | `AppLaunchedSSMeta` | Inaction metadata on App Launched |
| `inapp_notifs_meta` | `SSInaction` | Inaction metadata on regular events |
| `inapp_notifs` | `SS`, `SSInactionContentFetch`, `AppLaunchedSS` (journey path) | Full content to render |

### 9.5 Sample response — App Launched under SS with advanced fcaps

```json
{
  "inapp_notifs_ss": [
    { "ti": 60010, "wzrk_id": "60010_20260720", "priority": 50,
      "whenTriggers": [ {"eventName": "Charged", ...} ],
      "frequencyLimits": [ {"type": "session", "limit": 1} ],
      "occurrenceLimits": [ {"type": "onEvery", "limit": 3} ],
      "excludeGlobalFCaps": false, "templateName": "..." }
  ],
  "inapp_notifs_applaunched": [
    { "ti": 55001, "wzrk_id": "55001_20260720", "wzrk_ttl": 1691758604,
      "priority": 42, "whenTriggers": [{"eventName": "App Launched"}],
      "frequencyLimits": [...], "excludeGlobalFCaps": false,
      "title": {...}, "message": {...}, "buttons": [...],
      "type": "half-interstitial", "w": {...} }
  ],
  "inapp_notifs_applaunched_meta": [
    { "ti": 60022, "wzrk_id": "60022_20260720", "whenTriggers": [...],
      "inactionDuration": 300 }
  ],
  "inapp_delivery_mode": "SS",
  "imc": 5,
  "imp": 10
}
```

### 9.6 Sample response — `Charged` event under SS

Assume SDK previously asked for inapp `60010` via `inapps_eval` and the server had shipped its metadata on the last App Launched.

```json
{
  "inapp_notifs": [
    { "ti": 60010, "wzrk_id": "60010_20260720", "wzrk_ttl": 1691758604,
      "priority": 50, "title": {...}, "message": {...}, ...,
      "frequencyLimits": [...], "excludeGlobalFCaps": false }
  ],
  "inapp_notifs_meta": [
    { "ti": 60022, "wzrk_id": "60022_20260720",
      "whenTriggers": [{"eventName": "Charged"}], "inactionDuration": 300 }
  ],
  "inapp_delivery_mode": "SS",
  "imc": 5,
  "imp": 10
}
```

---

## 10. Trigger + segmentation eval

`InAppTargetEvalImpl` at `core/lc/src/java/com/wizrocket/lc/targeting/inapps/InAppTargetEvalImpl.java`. Three entry points:

- `processTarget(params, ti)` — evaluates the target's entry query for the current request event.
- `processTarget(triggerEvent, params, ti)` — same but with an overridden trigger event name (used by CS's whenTriggers filter and potentially other flows).
- `processTargetExitQuery(params, ti)` — evaluates the target's **exit query** (`xQuery`) — used by `SSInactionContentFetch` to re-verify eligibility before serving delayed content.

All three delegate to `TargetEvalManager.processTarget → TargetingUtils.processOnlineTarget` (`core/lc/.../targeting/TargetEvalManager.java:79-96`) — the shared predicate engine. Same one legacy uses.

Inputs consumed:
- `eventName`, `eventPropertiesForEval` (event + normalized props)
- `profile`, `epHandlerProps` (app/device fields merged in)
- `journeySteps` (journey qualification)
- `ip`, `isIPEnabled` (geo evaluation via `BasicEPHandler.GEO`)
- `firstSession`, `device`

**Profile is loaded from ES** only when the target's `LOAD_PROFILE` flag is on (via `TriggerQueryDto.needsProfile`). That gate is driven by segmentation-query needs, not by fcap needs.

---

## 11. `ProcessParams` — the mutable state carrier

`core/inapps/service/.../ProcessParams.java:27-60`

Fields relevant across the SS composition:

| Field | Set by | Consumed by |
|---|---|---|
| `accountId`, `i`, `j`, `guid`, `device`, `sdkVersion` | Request parsing | All strategies |
| `eventName`, `eventProperties`, `eventPropertiesForEval` | Request | Trigger eval, meta-delivery gate, content-fetch replay |
| `profile`, `epHandlerProps`, `journeySteps`, `ipEnabled`, `firstSession` | Request | Trigger eval, personalization |
| `mode` | `getDeliveryMode(accountId)` in adapter | `DeliveryStrategyProvider.get(params)` |
| `inAppEvalIds` (Set<Integer>) | SDK meta `inapps_eval` | `shouldSkipTarget` in SS + SSInaction |
| `inAppSuppressIds` | SDK meta `inapps_suppressed` | Suppression strategies |
| `shownToday` | Precomputed from SDK `imp` | Advanced global quota check |
| `inappTargetUserCounts` | SDK `TARGET_SHOWN_LIST` | Legacy per-target quota check |
| `targetsQualifiedSet` | **Mutated across strategies during one request** | All strategies — dedup |
| `isFreshLaunch` | LC container | `AppLaunchedSSMeta` — fresh-launch FStore wipe |
| `contentKeys` (private map) | Populated by `getContentKey` | Dedup content-variant resolution |

`targetsQualifiedSet` is the critical shared state — every strategy adds to it and every strategy reads it, so a target that already fired via one path never fires again via another in the same request.

---

## 12. Gotchas / edge cases

1. **Advanced-rules campaigns are invisible to the server without `inapps_eval`.** A buggy SDK that doesn't populate the list won't get them — but the SDK also won't get their content until it locally decides to ask. The whole flow relies on the SDK being an honest broker.
2. **`AppLaunchedSS` has "campaign lock-in".** Once a non-journey campaign is picked (delivered or suppressed), journeys are blocked for the remainder of that App Launched pass.
3. **Journey caps are always legacy** — even under `ADVANCED_FCAPS`. `InAppCombinedQuotaCheck` explicitly routes journey targets to the legacy check.
4. **`SSInactionContentFetch` replays the original event context** — content personalization sees the qualifying event's properties, not the `wzrk_fetch` event.
5. **FStore write failures drop payloads** — never risk over-delivery on partial state. Look for `P1` log lines with "Dropping messages for Inaction..." in ops.
6. **`isFreshLaunch` wipes prior inaction qualifications** in `AppLaunchedSSMeta`. The intent: a fresh session should not re-fire inaction timers for campaigns qualified in a prior session.
7. **`InstantEventClosure` targets** are excluded from `DeliveryTargetsSS` (per `.java:32`) — those are a different targeting type with their own flow.
8. **`ContentUtil.removeLegacyMdcLimit`** strips `mdc` from the `w` block when `ADVANCED_FCAPS` is on — so old SDK cache-hit paths don't accidentally double-cap.
9. **`fillCustomProfileFieldsToFetch`** (adapter `.java:104-140`) only pre-fetches all-in-app profile fields when the SDK is on the CS path AND a meta-delivery event is happening. Pure SS accounts don't get this bulk pre-fetch — per-target profile fetches happen as segmentation demands.
10. **`markAsSent` bumps campaign stats, not per-user caps.** SDK increments its own counters after render; server has no per-user × per-campaign counter.
11. **Response `inapp_delivery_mode: "SS"`** signals to the SDK not to switch stored state (per SDK TAN: on mode change, wipe the other mode's cache).
12. **`AdvancedLimits.excludeGlobalFCaps` encoding differs by device.** Android gets `1`/`-1`, iOS boolean (`ContentUtil.java:196-201`). Historical, treat both when consuming.

---

## 13. Key file / line reference

| Concern | File | Line |
|---|---|---|
| Manager selector | `core/lc/.../targeting/inapps/InAppManagerProvider.java` | 21-35 |
| SDK/device gate | `core/lc/.../targeting/inapps/InAppSupportChecker.java` | 6-14 |
| CS-vs-SS mode gate | `core/lc/.../targeting/inapps/InAppProcessorAdapter.java` | 306-308 |
| SS composer | `core/inapps/service/.../delivery/DeliveryStrategyProvider.java` | 70-99 |
| Meta / inaction event gate | `core/inapps/service/.../EventsHelper.java` | 16-31 |
| `MetaSS` strategy | `core/inapps/service/.../delivery/ss/DeliveryStrategyMetaSS.java` | 23-37 |
| `DeliveryTargetsMetaSS` filters | `core/inapps/service/.../targets/DeliveryTargetsMetaSS.java` | 20-44 |
| `AppLaunchedSS` main loop | `core/inapps/service/.../delivery/ss/DeliveryStrategyAppLaunchedSS.java` | 52-136 |
| `AppLaunchedSS` skip guard | `core/inapps/service/.../delivery/ss/DeliveryStrategyAppLaunchedSS.java` | 152-158 |
| `AppLaunchedSSMeta` inaction batch write | `core/inapps/service/.../delivery/ss/DeliveryStrategyAppLaunchedSSMeta.java` | 40-102 |
| `DeliveryStrategySS` main loop | `core/inapps/service/.../delivery/ss/DeliveryStrategySS.java` | 37-88 |
| `DeliveryStrategySS.shouldSkipTarget` | `core/inapps/service/.../delivery/ss/DeliveryStrategySS.java` | 106-114 |
| `DeliveryTargetsSS` filters | `core/inapps/service/.../targets/DeliveryTargetsSS.java` | 20-37 |
| `SSInaction` FStore qualification | `core/inapps/service/.../delivery/ss/DeliveryStrategySSInaction.java` | 42-137 |
| `SSInactionContentFetch` exit-query + replay | `core/inapps/service/.../delivery/ss/DeliveryStrategySSInactionContentFetch.java` | 47-151 |
| `SuppressionAppLaunchedSS` binding | `core/inapps/service/.../guice/InAppsCommonModule.java` | 117-125 |
| Combined quota check routing | `core/inapps/service/.../delivery/common/InAppCombinedQuotaCheck.java` | 24-33 |
| Advanced quota check (global only) | `core/inapps/service/.../delivery/common/InAppAdvancedFCapsGlobalQuotaCheck.java` | 16-33 |
| Legacy quota check body | `core/lc/.../targeting/TargetEvalManager.java` | 264-345 |
| Trigger + exit-query eval | `core/lc/.../targeting/inapps/InAppTargetEvalImpl.java` | 28-66 |
| `PayloadProviderSS` content build | `core/inapps/service/.../payload/PayloadProviderSS.java` | 26-75 |
| `PayloadProviderMetaSS` metadata build | `core/inapps/service/.../payload/PayloadProviderMetaSS.java` | 20-31 |
| `ContentUtil.getCommonFields` | `core/inapps/service/.../payload/common/ContentUtil.java` | 67-94 |
| `ContentUtil.getJourneyFields` | `core/inapps/service/.../payload/common/ContentUtil.java` | 96-110 |
| Adapter entry | `core/lc/.../targeting/inapps/InAppProcessorAdapter.java` | 142-216 |
| Adapter fillResponse dedup | `core/lc/.../targeting/inapps/InAppProcessorAdapter.java` | 218-236 |
| Adapter markAsSent | `core/lc/.../targeting/inapps/InAppProcessorAdapter.java` | 277-291 |
| Adapter convertSuppressEvents | `core/lc/.../targeting/inapps/InAppProcessorAdapter.java` | 238-275 |
| Adapter fillMetadata (delivery mode) | `core/lc/.../targeting/inapps/InAppProcessorAdapter.java` | 80-84 |
| Base fillMetadata (imc / imp) | `core/lc/.../targeting/InAppManager.java` | 120-130 |
| Response keys enum | `core/inapps/service/.../delivery/common/PayloadKey.java` | 9-14 |
| `AdvancedLimits` model | `core/inapps/models/.../models/AdvancedLimits.java` | 22-93 |
| `ProcessParams` | `core/inapps/service/.../ProcessParams.java` | 27-60 |

---

## 14. Take-aways

1. **SS mode = new-generation dispatcher with per-event server involvement, but with metadata-first delivery for advanced-rules campaigns.** Not to be confused with legacy dispatch.
2. **`getSS` is a composer, not a single strategy.** The request's event determines which of the six strategies participate. `MergeDelivery` folds their outputs.
3. **`inapps_eval` is the SDK's contract to the server** — "these advanced-rule campaigns passed my local checks, please send content." Server verifies segmentation + CG + global daily cap and returns content.
4. **Simple (non-advanced) SS campaigns are still purely server-side per event.** They compete with journey targets and SDK-requested advanced campaigns in `DeliveryStrategySS`.
5. **Advanced fcaps enforce only global daily on the server** (`InAppAdvancedFCapsGlobalQuotaCheck`). Per-campaign rules ride the payload as metadata for SDK enforcement.
6. **Journey targets always fall to legacy quota check** — `InAppCombinedQuotaCheck` explicitly forces this.
7. **Metadata-only strategies bypass the quota check.** The SDK re-checks caps at render time using the ceilings shipped in the response.
8. **Inaction correctness depends on FStore.** `AppLaunchedSSMeta` uses batch writes; `SSInaction` uses per-target writes; `SSInactionContentFetch` cleans up. Any write failure drops the payload rather than risk over-delivery.
9. **Content-fetch replays the qualifying event context.** `SSInactionContentFetch` rebuilds `ProcessParams` with the stored original event, so personalization resolves against the right event, not the fetch event.
10. **App Launched is special.** It gets three simultaneous strategies (`MetaSS` + `AppLaunchedSS` + `AppLaunchedSSMeta`) to minimize round-trips for the session's opening pass.
11. **Response mixes many keys.** `inapp_notifs_ss` (metadata), `inapp_notifs_applaunched` (App Launched content), `inapp_notifs_applaunched_meta` (App Launched inaction meta), `inapp_notifs_meta` (per-event inaction meta), and `inapp_notifs` (content to render, either journey or SS event). Plus `inapp_delivery_mode: "SS"`, `imc`, `imp`.
12. **The BE TAN's May 2024 optimisation** — "not send meta for campaigns that don't use these advanced features" — is exactly the `skipCampaign` guard in `DeliveryStrategySS.shouldSkipTarget`. Simple campaigns don't need metadata; they go through server-side per-event dispatch like journeys.
