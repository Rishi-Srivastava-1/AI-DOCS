# In-app CS (Client-Side) Mode — Fcaps + Triggers Deep Dive

Scope: how in-apps flow **when the account is in CS (client-side) mode** — the new-generation flow where the SDK owns trigger evaluation and per-campaign cap enforcement, and the server ships a metadata + content bundle on App Launched / explicit fetch.

Everything below is verified against `master` code and cross-checked with the BE TAN (`4008706201`), SDK TAN (`4085613262`), and Key Decisions doc (`4052779089`).

---

## 1. When CS mode is chosen

Two independent gates must both hold on the request.

### 1.1 SDK / device gate

`core/lc/src/java/com/wizrocket/lc/targeting/inapps/InAppSupportChecker.java:6-14`
```java
static final int MIN_SDK_VERSION_FOR_ADVANCED_IN_APPS = 60000;
static final int OLD_SDK_BIG_VERSION = 301000;

public boolean advancedInAppsSupported(int sdkVersion, ProfileReach.Device device) {
    return sdkVersion >= MIN_SDK_VERSION_FOR_ADVANCED_IN_APPS &&
            sdkVersion != OLD_SDK_BIG_VERSION &&
            (device == ProfileReach.Device.Android || device == ProfileReach.Device.iOS);
}
```

Required: SDK ≥ 60000 (excluding the sentinel 301000) and device ∈ {Android, iOS}. Anything else → legacy path (see `legacy_flow.md`).

### 1.2 Feature-flag gate

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

`newEvalManager` is `InAppProcessorAdapter extends InAppManager`. Inside it, CS-vs-SS is decided by a **single** flag:

`core/lc/src/java/com/wizrocket/lc/targeting/inapps/InAppProcessorAdapter.java:306-308`
```java
private Mode getDeliveryMode(int accountId) {
    return featureFlags.isEnabled(InAppsFeature.CLIENT_SIDE, accountId) ? Mode.CS : Mode.SS;
}
```

So **CS mode = `InAppsFeature.CLIENT_SIDE` on for the account, on an SDK ≥ 60000 Android/iOS device**.

The Key Decisions doc calls out (verbatim):
> CS in-apps will be enabled on app level … CleverTap Project can be either in In-app Campaigns Client-side mode (CS) or in In-app Campaigns Server-side mode (SS). It cannot be in mixed mode.

The one exception, also from the doc:
> Journeys are server-side only.

Journey in-apps therefore keep flowing via SS logic even inside CS-mode accounts.

---

## 2. The composed strategy

In the new adapter, dispatch is delegated to `InAppProcessor.process(params)` (`core/inapps/service/.../InAppProcessor.java`), which asks `DeliveryStrategyProvider.get(params)` for a strategy. For `Mode.CS`, the composer is:

`core/inapps/service/src/main/java/com/wizrocket/inapps/lc/delivery/DeliveryStrategyProvider.java:51-68`
```java
private Optional<InAppDelivery> getCS(ProcessParams params) {
    List<InAppDelivery> deliveries = new ArrayList<>();

    if (metaDeliveryRequested(params.getEventName(), params.getEventProperties())) {
        // all CS in-apps meta and content
        deliveries.add(deliveryStrategyCS);
    }

    // optionally return one (SS) journey in-app
    deliveries.add(deliveryStrategyJourney);

    if (!params.getInAppSuppressIds().isEmpty()) {
        // track CS suppression events
        deliveries.add(deliveryStrategySuppressionCS);
    }

    return MergeDelivery.of(deliveries);
}
```

Three ingredients, each conditional:

| Ingredient | Fires when | Purpose | Response key |
|---|---|---|---|
| `DeliveryStrategyCS` | `metaDeliveryRequested == true` (App Launched or `wzrk_fetch t=5`) | Bulk-ship all CS campaign in-apps to the SDK | `inapp_notifs_cs` |
| `deliveryStrategyJourney` (`@Named(DELIVERY_SS_JOURNEY)`) | Every event (unconditional) | Server-side pick of at most one journey in-app matching the current event | `inapp_notifs` |
| `deliveryStrategySuppressionCS` | Request meta contains non-empty `inapps_suppressed` | Convert SDK-reported suppression events into `SuppressEvent`s for CG tracking | (no payload; supplementary events only) |

Meta-delivery-triggering events, per `core/inapps/service/.../EventsHelper.java:16-25`:
```java
public static boolean metaDeliveryRequested(String event, BasicDBObject eventProperties) {
    if (APP_LAUNCHED.equals(event)) return true;
    if (WZRK_FETCH.equals(event))
        return eventProperties.getInt(WZRK_FETCH_TYPE_PARAM /* "t" */, -1) == WZRK_FETCH_IN_APP_META /* 5 */;
    return false;
}
```

**Consequence:** on regular events (`Charged`, `Notification Clicked`, etc.), the CS composer only runs the journey strategy (+ any suppression event tracking). It ships **nothing new** for campaign in-apps — the SDK is already holding the bundle from the last App Launched.

The `MergeDelivery` wrapper (`core/inapps/service/.../delivery/MergeDelivery.java`) folds each sub-strategy's `DeliveryResult` into a single response — merging `payloads` maps and appending `suppressedEvents`.

---

## 3. `DeliveryStrategyCS` — the bundle shipper

`core/inapps/service/src/main/java/com/wizrocket/inapps/lc/delivery/cs/DeliveryStrategyCS.java` (137 lines)

### 3.1 Target selection

`DeliveryTargetsCS` at `core/inapps/service/.../targets/DeliveryTargetsCS.java:14-30`:
```java
List<TargetingInfo> targets = t.getWebInApp(ProfileReach.Mode.InApp, params.getDevice());
return targets.stream()
        .filter(ti -> !ti.isJourneyTarget())         // journeys always SS
        .filter(TargetingInfo::isClientSide)         // targets with isClientSide=true
        .collect(Collectors.toList());
```

Filters:
1. Non-journey targets (journeys always run through the SS journey strategy).
2. `TargetingInfo.isClientSide == true` — the flag set on save under `InAppsFeature.CLIENT_SIDE` per `AdvancedFeaturesAdapterImpl` + backend save logic. Stored on the target document at `TargetingInfo.java:251` (`private final boolean isClientSide;`), loaded from `IS_CLIENT_SIDE` at `.java:1850`.

Notably: `getWebInApp(mode, device)` returns **all** targets for the mode/device regardless of trigger event — CS bundles the entire catalog, not just those matching the current event.

### 3.2 Per-target evaluation

`DeliveryStrategyCS.java:103-113` — for every candidate:
```java
private EvalResult eval(ProcessParams params, TargetingInfo ti) {
    var result = new EvalResult(ti, new HashSet<>());

    ti.getTriggerEvents().forEach(event -> {
        if (targetEval.processTarget(event, params, ti)) {
            result.qualifiedEvents.add(event);
        }
    });

    return result;
}
```

For each event in the target's trigger set (`ti.getTriggerEvents()` at `TargetingInfo.java:2651-2653`), the server runs `InAppTargetEvalImpl.processTarget(triggerEvent, processParams, ti)`. This delegates to `TargetEvalManager.processTarget → TargetingUtils.processOnlineTarget` (`core/lc/.../targeting/TargetEvalManager.java:79-96`) — the same predicate engine used by legacy dispatch.

The evaluation uses:
- The trigger event **name** from the campaign,
- but the **user context** from the current request (profile, event properties, ip, first-session, etc.).

Meaning: it asks "given who this user is right now, would this campaign qualify if they did trigger X?" — this pre-filters the campaign's trigger list to only those the SDK should still evaluate on-device.

If **no** trigger event qualifies, the target is skipped (see `.java:56` — `.filter(r -> !r.qualifiedEvents().isEmpty())`).

### 3.3 Payload build

If at least one trigger event qualifies, `PayloadProviderCS.getPayload(ti, params)` is called (`core/inapps/service/.../payload/PayloadProviderCS.java:22-61`):
```java
BasicDBObject payload = (BasicDBObject) originalMessage.copy();
payload.putAll(contentUtil.getCommonFields(params, ti).toMap());        // ti, wzrk_id, contentKey, priority, whenTriggers, limits
payload.put(WZRK_TTL_OFFSET, contentUtil.getCSInAppTTL(ti));            // "wzrk_ttl_offset" in seconds
contentUtil.removeLegacyMdcLimit(payload, params.getAccountId());       // drop mdc from w block if ADVANCED_FCAPS on
contentUtil.addInappDelayOnTriggerIfEnabled(params.getAccountId(), payload, ti);

if (!contentBuilder.prepareContent(ContentRequest{...})) return Optional.empty();

return PayloadMeta{batchId, contentKey, notificationClickedId, sdkPayload=payload, ti};
```

The critical difference from legacy/SS: CS ships **content + trigger metadata + all limits** in one payload — the SDK caches it and evaluates future events against it locally.

**`ContentUtil.getCommonFields`** (`core/inapps/service/.../payload/common/ContentUtil.java:67-94`) — the common-field builder:
```java
payload.append("ti",                            ti.getID())
       .append(NOTIFICATION_CLICKED_ID,          getNotificationClickId(ti.getID()))
       .append(NOTIFICATION_CONTENT_KEY,         params.getContentKey(ti, this))
       .append("priority",                       ti.getPriority())
       .append("whenTriggers",                   getWhenTriggers(ti, params.getDevice()));

if (ADVANCED_FCAPS enabled for account) {
    payload.put("frequencyLimits",   ti.getAdvancedLimits().map(AdvancedLimits::fCaps).orElse([]));
    payload.put("excludeGlobalFCaps", ...);       // 1/-1 on Android, boolean on iOS
} else {
    payload.putAll(getLegacyLimits(ti));           // efc, tlc, tdc, mdc (if set)
}

if (ADVANCED_DISPLAY_TRIGGERS enabled for account) {
    payload.put("occurrenceLimits",  ti.getAdvancedLimits().map(AdvancedLimits::triggers).orElse([]));
}
```

Fields shipped in a CS payload:

| Key | Source | Purpose (for SDK) |
|---|---|---|
| `ti` | target id | Correlation |
| `wzrk_id` (`NOTIFICATION_CLICKED_ID`) | `contentUtil.getNotificationClickId(ti.getID())` = `"{targetId}_{YYYYMMDD}"` | Impression id — used later on view/click events |
| `NOTIFICATION_CONTENT_KEY` | Message variant key | Variant tracking |
| `priority` | `ti.getPriority()` | 1 (lowest) … 100 (highest) — SDK breaks ties by creation date |
| `whenTriggers` | `ti.getWhenTriggers()` map for the device, **later filtered** to qualified events (§3.4) | Local trigger evaluation |
| `frequencyLimits` (if `ADVANCED_FCAPS`) | `AdvancedLimits.fCaps` | List of `{type, limit, frequency}` — SDK enforces AND'd |
| `excludeGlobalFCaps` (if `ADVANCED_FCAPS`) | `AdvancedLimits.excludeGlobalFCaps` | `1`/`-1` on Android; boolean on iOS (`ContentUtil.java:196-201`) |
| `occurrenceLimits` (if `ADVANCED_DISPLAY_TRIGGERS`) | `AdvancedLimits.triggers` | List of `{type: onEvery/onExactly, limit}` |
| Legacy `tlc`, `tdc`, `mdc`, `efc` (if `ADVANCED_FCAPS` off) | Target doc | Same semantics as legacy — SDK enforces |
| `wzrk_ttl_offset` | `contentUtil.getCSInAppTTL(ti)` = TTL in seconds (not absolute) | SDK computes `wzrk_ttl` locally as `now + offset` |
| `inappDelayAfterTrigger` (feature-flag gated) | `ti.getInappDelayAfterTrigger()` | Delay to hold the in-app after trigger |
| Full content (`title`, `message`, `media`, `buttons`, `w`, `type`, …) | Copy of `ti.message(device, contentKey, null)` | Rendered by SDK |

### 3.4 `adjustPayload` — filtering `whenTriggers` to qualified events

`DeliveryStrategyCS.java:115-133`:
```java
private PayloadMeta adjustPayload(PayloadMeta payloadMeta, EvalResult evalResult) {
    BasicDBObject sdkPayload = payloadMeta.getSdkPayload();

    if (sdkPayload != null
            && sdkPayload.get(PAYLOAD_WHEN_TRIGGERS) instanceof BasicDBList triggers) {
        for (int i = triggers.size() - 1; i >= 0; i--) {
            var whenTrigger = (BasicDBObject) triggers.get(i);
            var event = TriggerQueryDto.getTrimmedEventName(whenTrigger.getString(EVENT_NAME));

            if (!evalResult.qualifiedEvents.contains(event)) {
                triggers.remove(i);
            }
        }
    }

    return payloadMeta;
}
```

Server did segmentation for each event of the campaign's trigger list; here the payload's `whenTriggers` array is trimmed to keep only the events that passed. If the user profile currently disqualifies the campaign for trigger event `Charged` but qualifies for `Notification Clicked`, only the latter reaches the SDK. This is the CS analogue of legacy's audience filter.

Note: even legacy-legacy CS accounts (i.e., `ADVANCED_FCAPS` off but `CLIENT_SIDE` on) get this filtering.

### 3.5 Control group suppression

Before returning the payload, `DeliveryStrategyCS.getPayload` (`.java:67-101`) asks `ControlGroupSuppressor.shouldSuppress(...)`:

- `SKIP` → drop the target entirely (no payload).
- `SUPPRESS` → replace the payload with `PayloadProviderSuppressedCS.getPayload(ti, params, cgId)`. This returns a **stub payload** built by `contentUtil.getCommonFields(...)` plus:
  ```java
  payload.put("suppressed", true);          // ContentUtil.PAYLOAD_SUPPRESSED
  payload.put("wzrk_cgId",  cgId);          // ContentUtil.PAYLOAD_SUPPRESSED_CONTROL_GROUP_ID
  ```
  So the SDK still receives metadata for the target (id, wzrk_id, whenTriggers, limits) but with `suppressed: true`. When the SDK's trigger evaluation later matches this target for display, it recognizes the flag and reports a `NotificationSuppressed` event back rather than rendering.

- `DELIVER` → mark `markAsSent=true` on the payload. `InAppProcessorAdapter.markAsSent` (`.java:277-291`) will bump `TargetingCountManager` for aggregate stats when the response is finalized.

The `markAsSent=true` on CS is a slight terminology quirk — the *server* has decided the target is eligible from the CG standpoint. Actual "sent" bookkeeping in campaign stats happens now; actual render is later on the SDK.

### 3.6 Result shape

`DeliveryStrategyCS.generatePayload` builds:
```java
return DeliveryResult.builder()
        .payload(PayloadKey.INAPP_NOTIFS_CS, result)
        .build();
```

All CS payloads (including any suppressed stubs) go under the response key `inapp_notifs_cs` (`core/inapps/service/.../delivery/common/PayloadKey.java:12`).

---

## 4. The Journey strategy — always SS, always at most one per event

`deliveryStrategyJourney` is bound at `InAppsCommonModule.java:85-94`:
```java
@Named(DELIVERY_SS_JOURNEY)
@Provides
public InAppDelivery getDeliveryJourney(
        DeliveryStrategyFactory factory,
        DeliveryTargetsJourney targetsJourney,
        Set<TargetFilter> filters,
        TargetsUsageStatsHandler tracker) {
    return factory.createDeliverySS(decorate(targetsJourney, filters, tracker));
}
```

It's an instance of `DeliveryStrategySS` — the SS "per-event single-target" strategy — but with `DeliveryTargetsJourney` supplying the target list (journey inapps triggered by the current event).

Consequences under CS mode:
- On any event (App Launched or regular), if a journey inapp qualifies, exactly one goes to the SDK under `inapp_notifs`.
- Journey targets pass through the **combined quota check** (`DeliveryStrategySS.java:106-114`), which for `ti.isJourneyTarget()` routes to **legacy** quota check even under `ADVANCED_FCAPS` (`InAppCombinedQuotaCheck.java:26-32`). So journey caps use `tlc`/`tdc`/`efc` + global daily.
- The `AdvancedLimits` optional is null for journey targets (`TargetingInfo.java:2131-2145` — `whenLimits` only generated for `InApp && !isJourneyTarget()`).

The SDK TAN captures this decision: *"Journey's in-apps will continue to be server-side triggered only until further changes outside of the scope of this project."*

Priority handling per the Key Decisions doc:
> By default the CS (campaign) in-apps are with higher priority, so one campaign in-app is displayed based on priority on the trigger event. If equal priority - show the one created the earliest. The Journey is then displayed after the CS in-app.

So the SDK renders the picked CS in-app first, then any journey in-app that piggy-backed.

---

## 5. `DeliveryStrategySuppressionsCS` — SDK-reported suppressions

`core/inapps/service/src/main/java/com/wizrocket/inapps/lc/delivery/cs/DeliveryStrategySuppressionsCS.java` (60 lines)

Fires when the SDK reports suppressions in the request meta:
```json
{
  "type": "meta",
  "inapps_suppressed": [
    {"wzrk_id": "1691570436_20230818", "wzrk_pivot": "wzrk_default", "wzrk_cgId": 0}
  ]
}
```

Algorithm (`.java:37-58`):
```java
targets = targetsProvider.fetchTargets(params);        // event's targets + all App Launched targets
for (sm : params.getInAppSuppressIds()) {
    TargetingInfo ti = targets.get(sm.getId());
    if (ti != null && !targetsQualifiedSet.contains(ti.getID())) {
        targetsQualifiedSet.add(ti.getID());
        result.suppressedEvent(
            DeliveryResult.SuppressMeta.builder()
                .suppressEvent(SuppressEvent.of(sm))
                .ti(ti)
                .build());
    }
}
```

Target list from `DeliveryTargetsSuppressionsCS` (`.java:21-43`):
- targets triggered by the request's current event, plus
- targets triggered by `APP_LAUNCHED` (because App Launched suppressions can't be reported inside the App Launched request itself — the SDK reports them on the *next* event)
- filtered to non-journey + `isClientSide`

Each suppression becomes a `SuppressEvent` in `DeliveryResult.suppressedEvents`. Downstream, `InAppProcessorAdapter.convertSuppressEvents` (`.java:238-275`) converts these into supplementary `NotificationSuppressed`-style events that flow through LC → LP → ES for A/B stats consistency.

The doc-comment on this class is worth reading (verbatim) for the batching edge case:
> App Launched triggers are a special case as no 'App Launched' event is triggered after the in-app was shown/suppressed. These specific suppressions are tracked on the next event sent to the server, therefore the targeting provider returns includes such in-apps.

---

## 6. Cap enforcement — who checks what

Very short answer: **the SDK does almost all of it**.

`DeliveryStrategyCS` itself never calls `combinedQuotaCheck.targetQuotaReached(...)`. Grep the file — no reference. It ships every eligible-by-segmentation campaign with its `frequencyLimits`/`occurrenceLimits` metadata attached, and the SDK enforces them at trigger time and again in `canShow` before render (see SDK TAN §6 "canShow").

The only server-side quota check that runs in CS mode is inside the **journey** sub-strategy (which is really `DeliveryStrategySS` with a journey target list): `DeliveryStrategySS.java:106-114` calls `combinedQuotaCheck.targetQuotaReached(params, ti)` per journey target. That check, for journey targets, routes to legacy (`InAppCombinedQuotaCheck.java:26-32`).

So under CS:

| Cap | Where enforced |
|---|---|
| Campaign `frequencyLimits` (session/day/week/hour/ever) | **SDK only** |
| Campaign `occurrenceLimits` (onEvery / onExactly) | **SDK only** |
| Campaign `excludeGlobalFCaps` opt-out | **SDK only** (server doesn't invoke the check) |
| Legacy `tdc`, `tlc`, `mdc`, `efc` (if `ADVANCED_FCAPS` off but `CLIENT_SIDE` on) | **SDK only** for campaigns |
| Global `imp` per day | **SDK only** for campaigns; server-enforced for journeys via legacy quota check |
| Global `ism` per session | **SDK only** |
| Journey `tdc`/`tlc`/`efc` + global daily | Server + SDK (via journey strategy → legacy quota check) |

The BE TAN nails this in "Note on advanced fcap checks":
> *The validation of advanced fcaps will be handled solely on the SDK.*

Rationale (verbatim):
> *This approach is based on the assumption that a comparable implementation to the Leanplum SDK will be developed, where a `{occurrenceId, timestamp}` map of the last 100 in-app occurrences is stored on the device. The transfer of such amount of data back and forth seems impractical.*

The SDK stores that impression map locally and uses it for `frequencyLimits`/`occurrenceLimits` evaluation. Nothing about it ever reaches the server.

---

## 7. What the SDK does with the CS response

Per SDK TAN §"Client-side triggering" and §"Evaluation":

### 7.1 On App Launched / `wzrk_fetch t=5`

1. Server sends `inapp_notifs_cs` (bundle) + `inapp_delivery_mode: "CS"` + updated `imc`/`imp` ceilings.
2. SDK **persists** the CS bundle to local storage — it must survive across sessions and be usable offline.
3. Optional: preload images to filesystem so the render is instant when triggered (SDK TAN's "Image preloading" section — post-MVP).
4. If any CS campaign has an `App Launched` trigger, the SDK immediately evaluates it now (SDK TAN's "Client-side / App Launched" flow: *"Wait for the App Launched response to be received and then call the evaluation."*).

### 7.2 On regular events

For every event the SDK records:
1. Iterate cached CS in-apps.
2. Match `whenTriggers` — event name (with `TriggerQueryDto.getNormalizedString` normalization) + `eventProperties` filters using the operator table (SDK TAN §"Operators", `CTTriggerOperator*`).
3. For every campaign whose triggers matched, **increment a trigger counter** (independent of caps).
4. Evaluate `occurrenceLimits`: does the current trigger-count match `onEvery Nth` or `onExactly Nth`?
5. Evaluate `frequencyLimits`: is the campaign still under all of its rules? (Uses local `{occurrenceId, timestamp}` map of last 100 impressions.)
6. Pick the winning campaign by **priority** (descending). Equal priority → the one created earliest.
7. Check `suppressed` flag on the picked payload:
    - `true` → append to `inapp_suppressed` meta in the next request; try the next eligible campaign; suppress all suppressed ones as you go (SDK TAN §"Control group and Suppression events").
    - `false` → compute `wzrk_ttl = now + wzrk_ttl_offset` and enqueue for display.
8. Before render (in `canShow`): re-check `frequencyLimits` and global caps (SDK TAN §"canShow"). Occurrence limits are **not** re-checked here.
9. On successful render:
    - `impressionManager.recordImpressions(id)` (adds `{campaignId, timestamp}` to the local map)
    - Bump local `imp` (global today count)
    - Send a `Notification Viewed` event on the next request

### 7.3 Cap ceilings & `inapp_stale`

Each response includes:
- `imc` — session ceiling (SDK-enforced only)
- `imp` — daily ceiling (SDK-enforced only, in CS mode for campaigns)
- `inapp_delivery_mode: "CS"` — added by `InAppProcessorAdapter.fillMetadata` (`.java:80-84`)
- Optionally `inapp_stale` — target ids whose campaigns are stopped/completed. The SDK uses this to purge them from its local impression map and counter list.

---

## 8. Response payload shape

The InAppProcessorAdapter merges strategy outputs and writes them to the HTTP response.

`InAppProcessorAdapter.fillResponse` (`.java:218-236`):
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
Batch-dedup by target id (safe when the same target appears in multiple sub-strategy outputs).

Typical CS-mode response body:
```json
{
  "inapp_notifs_cs": [
    {
      "ti": 55001,
      "wzrk_id": "55001_20260720",
      "priority": 42,
      "whenTriggers": [ { "eventName": "Charged", "eventProperties": [...] } ],
      "frequencyLimits": [
        {"type": "session", "limit": 1},
        {"type": "days",    "limit": 2, "frequency": 1}
      ],
      "occurrenceLimits": [ {"type": "onEvery", "limit": 5} ],
      "excludeGlobalFCaps": false,
      "wzrk_ttl_offset": 172800,
      "title": {...}, "message": {...}, "buttons": [...], "media": {...},
      "type": "half-interstitial", "w": {...}
    },
    {
      "ti": 55002,
      "wzrk_id": "55002_20260720",
      "suppressed": true, "wzrk_cgId": 0,
      "whenTriggers": [...], "frequencyLimits": [...], ...
    }
  ],
  "inapp_notifs": [
    // At most one journey in-app if event triggered one (from deliveryStrategyJourney)
  ],
  "inapp_delivery_mode": "CS",
  "imc": 5,
  "imp": 10
}
```

If the event is a regular one (not `App Launched` / `wzrk_fetch t=5`), the `inapp_notifs_cs` key is absent — only the journey (if any) shows up. That's normal: the SDK doesn't need refreshed metadata every request.

---

## 9. Suppression response flow — meta + supplementary events

Two-part flow to keep control-group A/B stats correct even though the SDK owns rendering.

### 9.1 Server signal in `inapp_notifs_cs`

For a suppressed CS campaign, the payload has `"suppressed": true` and `"wzrk_cgId": <cgId>` per `PayloadProviderSuppressedCS.generateSuppressContent`.

### 9.2 SDK ack via `inapps_suppressed` meta

When the SDK's trigger + priority logic later chooses that suppressed campaign for what would have been display, it instead adds to the next request's meta:
```json
"inapps_suppressed": [
  {"wzrk_id": "55002_20260720", "wzrk_pivot": "wzrk_default", "wzrk_cgId": 0}
]
```

### 9.3 Server converts to supplementary event

`InAppProcessorAdapter.convertSuppressEvents` (`.java:238-275`) — turns each `SuppressMeta` from `DeliveryStrategySuppressionsCS` into a `NotificationSuppressed` control-group event:
```java
LCUtils.createControlGroupEvent(baseRequestDto,
        e.getSuppressEvent().getNotificationClickedId(),
        e.getSuppressEvent().getCgType(),
        e.getSuppressEvent().getKey())
```
Deduped against any suppression already present via the `NOTIFICATION_CLICKED_ID` key.

This event flows LC → LP → ES so downstream campaign-stats bookkeeping is consistent. The doc explains the split intent:
> The server must register the suppression event **after** the SDK has determined that the in-app needs to be displayed. Therefore, suppressed in-apps are included to `inapp_notifs_cs` with special payload.

---

## 10. Feature-flag interaction

Three flags with independent effect (BE TAN "Backward compatibility"):

| Flag | Effect in CS mode |
|---|---|
| `IN_APPS_CLIENT_SIDE` (aka `InAppsFeature.CLIENT_SIDE`) | Master switch. Off → account reverts to SS (per Key Decisions: converted, not preserved) |
| `IN_APPS_ADVANCED_FCAPS` (aka `InAppsFeature.ADVANCED_FCAPS`) | Off → CS payload ships legacy `tlc`/`tdc`/`mdc`/`efc` fields instead of `frequencyLimits`/`excludeGlobalFCaps` (`ContentUtil.java:80-85`); SDK still enforces client-side |
| `IN_APPS_ADVANCED_DISPLAY_TRIGGERS` (aka `InAppsFeature.ADVANCED_DISPLAY_TRIGGERS`) | Off → CS payload does not include `occurrenceLimits` (`ContentUtil.java:87-91`) |

On save, `AdvancedFeaturesAdapterImpl.onSave` (`core/inapps/service/.../adapters/AdvancedFeaturesAdapterImpl.java:44-61`) still writes both formats to Mongo when `ADVANCED_FCAPS` is on — so a flag flip doesn't lose the campaign's capping intent:
- Only-`session-1` → `mdc=1, tdc=-1, tlc=-1, efc=-1`
- Only-`days-1` → `tdc=1, mdc=1000, tlc=-1, efc=-1`
- Only-`ever-1` → `tlc=1, mdc=1000, tdc=-1, efc=-1`
- Only `excludeGlobalFCaps=true` with no rules → `efc=1`
- Anything richer → default to "once per session" (`mdc=1`)

Per Key Decisions ("Backwards compatibility & rollout"):
> SDK needs to ensure support for the old fCaps to solve for the use case: customer upgrades to the new SDK but is not whitelisted for the new functionalities.

---

## 11. Gotchas / edge cases

1. **CS ≠ zero server work.** The server still (a) runs full segmentation per candidate campaign, (b) evaluates control groups, (c) filters `whenTriggers` down to segment-qualified events. Only *cap* evaluation moves to the device.
2. **First-install App Launched.** SDK TAN calls out the CS App Launched flow specifically: *"Wait for the App Launched response to be received and then call the evaluation. This ensures that the client-side in-apps are received and takes care of in-apps triggered on the first install."* — regular App Launched inapps need the CS bundle first.
3. **Mode switch semantics.** Per SDK TAN: on `inapp_delivery_mode` change the SDK wipes the *other* mode's cache (CS→SS clears CS cache, etc.). Bundle re-fetch happens on the next App Launched.
4. **`excludeGlobalFCaps` encoding is platform-dependent.** Android gets `1`/`-1`, iOS gets a boolean (`ContentUtil.java:196-201`). Historical quirk — treat both formats when reading.
5. **`whenTriggers` normalization.** SDK's local matcher applies `TriggerQueryDto.getNormalizedString` to trigger event names (BE TAN Nov 2024 note). Match on the normalized form, not the raw string.
6. **Legacy `mdc` in the `w` block.** When `ADVANCED_FCAPS` is on, `ContentUtil.removeLegacyMdcLimit` (`.java:227-234`) strips `MAX_PER_CAMPAIGN` from the dimensional-info block to avoid old SDK code paths honoring it twice.
7. **Journeys can suppress-lock a slot even in CS.** The composed strategy always adds `deliveryStrategyJourney`, and `DeliveryStrategySS` there enforces the legacy quota check (§4). A caps-blocked journey doesn't leak into `inapp_notifs`, but a CG-suppressed one still generates a supplementary event.
8. **`InAppProcessorAdapter.fillCustomProfileFieldsToFetch`** (`.java:104-140`) pre-fetches profile fields needed by any CS in-app, but **only** on meta-delivery events — so bulk profile lookup happens once per session (App Launched), not on every event.
9. **CG suppression payload still counts as a `targetsQualifiedSet` add.** Once a suppressed target is emitted, other SS strategies in the same request won't double-emit it (§5 in the SS doc has more).
10. **First-time trigger under CS.** BE TAN Nov 2024 note: SDKs get an "exactly once" rule injected into `frequencyLimits` for first-time-only campaigns, since first-time state can't be verified across devices. The BE injection happens inside `ContentUtil.getFCaps` (`.java:206-216`).

---

## 12. Key file / line reference

| Concern | File | Line |
|---|---|---|
| Manager selector | `core/lc/.../targeting/inapps/InAppManagerProvider.java` | 21-35 |
| SDK/device gate | `core/lc/.../targeting/inapps/InAppSupportChecker.java` | 6-14 |
| CS-vs-SS mode gate | `core/lc/.../targeting/inapps/InAppProcessorAdapter.java` | 306-308 |
| CS composer | `core/inapps/service/.../delivery/DeliveryStrategyProvider.java` | 51-68 |
| Meta-delivery gate (event type check) | `core/inapps/service/.../EventsHelper.java` | 16-25 |
| `DeliveryStrategyCS` main loop | `core/inapps/service/.../delivery/cs/DeliveryStrategyCS.java` | 50-65 |
| `DeliveryStrategyCS` per-target eval | `core/inapps/service/.../delivery/cs/DeliveryStrategyCS.java` | 103-113 |
| `DeliveryStrategyCS` whenTriggers filter | `core/inapps/service/.../delivery/cs/DeliveryStrategyCS.java` | 115-133 |
| CG suppression in CS | `core/inapps/service/.../delivery/cs/DeliveryStrategyCS.java` | 67-101 |
| `DeliveryTargetsCS` target filter | `core/inapps/service/.../targets/DeliveryTargetsCS.java` | 14-30 |
| `DeliveryStrategySuppressionsCS` | `core/inapps/service/.../delivery/cs/DeliveryStrategySuppressionsCS.java` | 37-58 |
| `DeliveryTargetsSuppressionsCS` target scope | `core/inapps/service/.../targets/DeliveryTargetsSuppressionsCS.java` | 21-43 |
| Journey binding | `core/inapps/service/.../guice/InAppsCommonModule.java` | 85-94 |
| `PayloadProviderCS` payload build | `core/inapps/service/.../payload/PayloadProviderCS.java` | 22-61 |
| `PayloadProviderSuppressedCS` stub payload | `core/inapps/service/.../payload/PayloadProviderSuppressedCS.java` | 15-32 |
| `ContentUtil.getCommonFields` | `core/inapps/service/.../payload/common/ContentUtil.java` | 67-94 |
| `ContentUtil.getAdvancedLimits` | `core/inapps/service/.../payload/common/ContentUtil.java` | 188-204 |
| `ContentUtil.getLegacyLimits` | `core/inapps/service/.../payload/common/ContentUtil.java` | 156-182 |
| `ContentUtil.removeLegacyMdcLimit` | `core/inapps/service/.../payload/common/ContentUtil.java` | 227-234 |
| Adapter fillResponse (dedup) | `core/lc/.../targeting/inapps/InAppProcessorAdapter.java` | 218-236 |
| Adapter markAsSent (stats bump) | `core/lc/.../targeting/inapps/InAppProcessorAdapter.java` | 277-291 |
| Adapter convertSuppressEvents | `core/lc/.../targeting/inapps/InAppProcessorAdapter.java` | 238-275 |
| Adapter fillMetadata (imc / imp / delivery_mode) | `core/lc/.../targeting/inapps/InAppProcessorAdapter.java` | 80-84 |
| Base fillMetadata | `core/lc/.../targeting/InAppManager.java` | 120-130 |
| Response keys enum | `core/inapps/service/.../delivery/common/PayloadKey.java` | 9-14 |
| `AdvancedLimits` model | `core/inapps/models/src/main/java/com/wizrocket/inapps/models/AdvancedLimits.java` | 22-93 |
| Save-time mirror to legacy fields | `core/inapps/service/.../adapters/AdvancedFeaturesAdapterImpl.java` | 44-113 |

---

## 13. Take-aways

1. **CS mode = SDK owns per-campaign fcaps + all trigger eval; server owns segmentation + CG + payload bundling.**
2. **`DeliveryStrategyCS` runs only on App Launched / `wzrk_fetch t=5`.** On regular events, the CS composer ships only a journey inapp (if any). The SDK evaluates cached campaigns locally.
3. **Journey inapps are always SS**, even in CS mode. They use the legacy quota check because `InAppCombinedQuotaCheck` forces journey targets there.
4. **The `whenTriggers` filter is subtle.** Server pre-filters the trigger list to those the user currently qualifies for by segmentation. The SDK only ever sees "possible" triggers.
5. **CG suppression rides along in `inapp_notifs_cs`.** The SDK reports back via `inapps_suppressed` meta; the server converts to supplementary `NotificationSuppressed` events for LP/ES stats.
6. **No per-user × per-campaign counter is stored server-side, ever.** All counters live in device storage.
7. **`markAsSent` in CS is a stats-side signal, not a render signal.** It bumps `TargetingCountManager` when the server hands over a payload — actual rendering happens on the SDK.
8. **Feature-flag decomposition matters.** `CLIENT_SIDE` alone gives you CS delivery with legacy fields; add `ADVANCED_FCAPS` for the new fcap vocabulary; add `ADVANCED_DISPLAY_TRIGGERS` for `occurrenceLimits`. The three ship independently.
9. **`wzrk_ttl_offset` (not `wzrk_ttl`) is what CS ships.** SDK computes `wzrk_ttl = now + offset` locally on receipt — because the server can't know the exact display time.
10. **The BE TAN's rationale is worth remembering:** *"The transfer of such amount of data back and forth seems impractical."* — every server-side simplification in CS traces back to that constraint.
