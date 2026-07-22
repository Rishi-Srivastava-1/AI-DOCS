# In-App Campaigns — Dispatch Flow

Scope: **dispatch only** — how an eligible in-app gets to the client SDK. Campaign creation, event feedback (viewed/clicked), and stats writeback are out of scope.

All in-app dispatch happens in **LC** (`core/lc`) with heavy lifting in the shared **`core/inapps/service`** module. NB does **not** dispatch in-apps in the current architecture — the SDK pulls them on every request via LC servlets (`MobileAppServlet`, `WebAppServlet`, `MobileWebAppServlet`).

---

## 1. The top-level selector — old vs new

Every mobile/web request that carries an event flows through the LC servlet, which asks `InAppManagerProvider` for the eval manager to use for that request:

`core/lc/src/java/com/wizrocket/lc/targeting/inapps/InAppManagerProvider.java:21-35`

```java
public InAppManager getEvalManager(int sdkVersion, ProfileReach.Device device, int accountId) {
    boolean useAdvancedEvalManager =
            supportChecker.advancedInAppsSupported(sdkVersion, device) &&
                    localTriggeringRequired(accountId);
    return useAdvancedEvalManager ? newEvalManager : legacyEvalManager;
}

private boolean localTriggeringRequired(int accountId) {
    return featureFlag.isEnabled(InAppsFeature.ADVANCED_FCAPS, accountId) ||
            featureFlag.isEnabled(InAppsFeature.ADVANCED_DISPLAY_TRIGGERS, accountId) ||
            featureFlag.isEnabled(InAppsFeature.CLIENT_SIDE, accountId) ||
            inactionDynamicConfig.getChannelsInactionWithEventProps(accountId)
                    .contains(ProfileReach.Mode.InApp.toString().toLowerCase());
}
```

Two conditions must both hold to pick the **new** manager:

1. **SDK/device capability** — `InAppSupportChecker.advancedInAppsSupported` (`InAppSupportChecker.java:9-14`)
   - `sdkVersion >= 60000` (`MIN_SDK_VERSION_FOR_ADVANCED_IN_APPS`)
   - `sdkVersion != 301000` (`OLD_SDK_BIG_VERSION`)
   - `device` ∈ {Android, iOS} — Windows/Web fall back to legacy
2. **Any of these per-account feature flags** must be on:
   - `InAppsFeature.ADVANCED_FCAPS`
   - `InAppsFeature.ADVANCED_DISPLAY_TRIGGERS`
   - `InAppsFeature.CLIENT_SIDE`
   - Inaction inapp with event-props (via `InactionDynamicConfig`)

If either check fails → **legacy `InAppManager`**. Otherwise → **`InAppProcessorAdapter`** (the "new" manager).

Both classes are types of `InAppManager`; `InAppProcessorAdapter extends InAppManager` and overrides `evalAndFillResponse`, `fillMetadata`, `requiresProfile`, `fillCustomProfileFieldsToFetch`.

---

## 2. Flow A — Legacy in-app dispatch (base `InAppManager`)

Entry: `InAppManager.evalAndFillResponse(...)` — `core/lc/src/java/com/wizrocket/lc/targeting/InAppManager.java:157`

### What it does
1. Loads targets from the in-memory cache: `AccountWorker.getInAppWebTargeting(accountId)` (`InAppManager.java:194`).
2. Iterates every in-app target for the account.
3. For each target, runs `processTarget(...)` — the trigger/predicate/frequency evaluation inherited from `TargetEvalManager` (`InAppManager.java:237-249`).
4. On qualification:
   - Reads target-level caps: `INAPP_TGT_EXCLUDE_FREQUENCY_CAP`, `INAPP_TGT_LIFETIME_COUNT`, `INAPP_TGT_DAILY_COUNT` (`InAppManager.java:256-259`).
   - Picks message variant: fallback / legacy / native / A-B-tested (`InAppManager.java:274-296`).
   - Builds SDK payload via `contentBuilder` and appends to `enabledTargets`.
5. Writes `enabledTargets` into response under key **`inapp_notifs`** (`PayloadKey.INAPP_NOTIFS` = `"inapp_notifs"`, `core/inapps/service/.../PayloadKey.java:9`).

### Payload shape (SDK sees)
```
{
  "inapp_notifs": [ { "wzrk_id": "...", "w": {...}, "d": {...}, "wzrk_ttl": N, ... }, ... ],
  "imc": 5,     // account.inAppMaxPerSession, filled by fillMetadata (InAppManager.java:120-130)
  "imp": 10     // account.inappMaxPerUserPerDay (optional)
}
```

### Characteristics
- **Server-side eval only.** Every dispatch decision (trigger match, freq caps, variant) is made on LC per request.
- No metadata-vs-content split — SDK gets fully rendered content in the same response.
- No `inapp_delivery_mode` key in response (that key is added only by the new adapter's `fillMetadata`).
- Frequency caps enforced with `shownToday` and `inappTargetUserCounts` supplied by the caller.

---

## 3. Flow B — New in-app dispatch (`InAppProcessorAdapter`)

Entry: `InAppProcessorAdapter.evalAndFillResponse(...)` — `core/lc/src/java/com/wizrocket/lc/targeting/inapps/InAppProcessorAdapter.java:142-216`

The adapter is a thin LC-side shim that:
1. Builds a `ProcessParams` from the request (`InAppProcessorAdapter.java:163-191`).
2. Delegates the actual work to `inAppProcessor.process(params)` — the shared processor in `core/inapps/service`.
3. Writes each returned payload back to the response (`fillResponse`, `InAppProcessorAdapter.java:218-236`).
4. Emits `markAsSent` + suppression events (`InAppProcessorAdapter.java:277-291`).

Also augments the metadata block with the delivery-mode key (`InAppProcessorAdapter.java:80-84`):
```java
response.put(DELIVERY_MODE_PAYLOAD_KEY, getDeliveryMode(accountId).name());
// DELIVERY_MODE_PAYLOAD_KEY = "inapp_delivery_mode" (InAppManager.java:80)
```

### 3.1 The CS-vs-SS gate

`InAppProcessorAdapter.getDeliveryMode` — `InAppProcessorAdapter.java:306-308`:
```java
private Mode getDeliveryMode(int accountId) {
    return featureFlags.isEnabled(InAppsFeature.CLIENT_SIDE, accountId) ? Mode.CS : Mode.SS;
}
```

`Mode` is defined in `DeliveryStrategyProvider.java:101-103`:
```java
public enum Mode { CS, SS }
```

Mode is passed via `ProcessParams.mode` and dispatches inside the processor to `DeliveryStrategyProvider.get(params)` (`DeliveryStrategyProvider.java:44-49`), which routes to `getCS` or `getSS`.

---

### 3.2 Flow B1 — Client-Side (CS)

Router: `DeliveryStrategyProvider.getCS` — `core/inapps/service/src/main/java/com/wizrocket/inapps/lc/delivery/DeliveryStrategyProvider.java:51-68`

```java
private Optional<InAppDelivery> getCS(ProcessParams params) {
    List<InAppDelivery> deliveries = new ArrayList<>();
    if (metaDeliveryRequested(params.getEventName(), params.getEventProperties())) {
        deliveries.add(deliveryStrategyCS);          // all CS in-apps meta+content
    }
    deliveries.add(deliveryStrategyJourney);         // optional one SS journey in-app
    if (!params.getInAppSuppressIds().isEmpty()) {
        deliveries.add(deliveryStrategySuppressionCS);
    }
    return MergeDelivery.of(deliveries);
}
```

Key ideas:
- **Meta-delivery gate.** `metaDeliveryRequested` returns true only on `App Launched` or an explicit "fetch inapps" event — so LC ships the full CS catalogue only on those triggers, not on every event.
- **Bundle everything.** `DeliveryStrategyCS` (`.../delivery/cs/DeliveryStrategyCS.java`) assembles every CS-eligible in-app for the account into one response block. Trigger conditions, `whenTriggers`, `whenLimits`, `frequencyLimits`, and creative content are all attached — the SDK owns runtime evaluation.
- **Journey in-apps stay SS.** Even in CS mode, journey-based in-apps are dispatched server-side via `deliveryStrategyJourney` (bound with `NamedConstants.DELIVERY_SS_JOURNEY` in `DeliveryStrategyProvider.java:32-33`).
- **Suppressions.** When the SDK reports suppressed inapps in the request, `DeliveryStrategySuppressionsCS` mirrors those into the response so downstream counters stay consistent.

Response keys used:
- `inapp_notifs_cs` — the CS bundle (`PayloadKey.java:12`)
- `inapp_notifs_ss` — for the piggy-backed journey in-app (`PayloadKey.java:10`)
- `inapp_delivery_mode: "CS"`
- `imc`, `imp` — freq-cap ceilings

**Cap enforcement:** target-level `tlc` (lifetime), `tdc` (daily), `efc` (exclude-from-cap), and account-level `imc`/`imp` are shipped inside the payload — the SDK enforces them locally.

**Profile fetching:** `InAppProcessorAdapter.fillCustomProfileFieldsToFetch` (`InAppProcessorAdapter.java:104-140`) pre-fetches all profile fields that any CS in-app references, but only when `metaDeliveryRequested` is true — batch-scanning the target set once per bundle instead of per event.

---

### 3.3 Flow B2 — Server-Side (SS)

Router: `DeliveryStrategyProvider.getSS` — `DeliveryStrategyProvider.java:70-99`

```java
private Optional<InAppDelivery> getSS(ProcessParams params) {
    List<InAppDelivery> deliveries = new ArrayList<>();
    if (EventsHelper.inactionDeliveryRequested(params.getEventName(), params.getEventProperties())) {
        return Optional.of(deliveryStrategySSInactionContentFetch);
    }
    if (metaDeliveryRequested(params.getEventName(), params.getEventProperties())) {
        deliveries.add(deliveryStrategyMetaSS);            // metadata only (no content)
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

SS is not one strategy — it's a set that composes for the request. Strategies (`core/inapps/service/.../delivery/ss/`):

| Strategy | When it fires | What it ships |
|---|---|---|
| `DeliveryStrategyMetaSS` | Any meta-delivery event (e.g. App Launched, fetch) | Metadata for all SS campaigns — triggers, caps — under `inapp_notifs_meta` (`PayloadKey.java:14`) |
| `DeliveryStrategyAppLaunchedSS` | `App Launched` event | Full campaign content ready to render, under `inapp_notifs_applaunched` (`PayloadKey.java:11`) |
| `DeliveryStrategyAppLaunchedSSMeta` | `App Launched` event | Inaction-campaign metadata, under `inapp_notifs_applaunched_meta` (`PayloadKey.java:13`) |
| `DeliveryStrategySS` (`@Named(DELIVERY_SS)`) | Any non-App-Launched event | Server-evaluated in-app content for matched triggers, under `inapp_notifs_ss` (`PayloadKey.java:10`) |
| `DeliveryStrategySSInaction` | Non-App-Launched events | Inaction inapps arming/disarming |
| `DeliveryStrategySSInactionContentFetch` | Explicit inaction-delivery event | Delayed content for inaction campaigns |
| `DeliveryStrategySuppressionAppLaunchedSS` | Non-empty `inAppSuppressIds` | App-Launched suppression events |

Characteristics:
- **Server owns eval.** Trigger match, frequency caps (`imc`, `imp`, `tlc`, `tdc`), CG membership, personalisation, TTL — all decided on LC per request.
- **Metadata / content split.** On non-App-Launched events LC returns metadata only; the SDK requests actual content in a follow-up call (avoids paying content-assembly cost when the campaign might not display).
- **App Launched is special.** The very first request of a session gets full content in one shot, so the app can render without a second round-trip.

Response keys used depending on strategies fired:
`inapp_notifs_ss`, `inapp_notifs_applaunched`, `inapp_notifs_meta`, `inapp_notifs_applaunched_meta`, `inapp_delivery_mode: "SS"`, plus `imc`/`imp`.

---

## 4. Payload writeback & bookkeeping (shared for CS + SS)

Every `InAppDelivery` returns a `DeliveryResult` mapping `PayloadKey → List<PayloadMeta>`. The adapter merges them into the HTTP response with per-target dedup:

`InAppProcessorAdapter.fillResponse` — `InAppProcessorAdapter.java:218-236`

```java
Set<Integer> ids = payload.stream()
        .map(o -> (BasicDBObject) o)
        .map(dbObject -> dbObject.getInt(ContentUtil.PAYLOAD_TARGET_ID, 0))
        .collect(Collectors.toSet());
for (PayloadMeta meta : payloadMeta) {
    if (!ids.contains(meta.getTi().getID())) {
        payload.add(meta.getSdkPayload());
        ids.add(meta.getTi().getID());
    }
}
```

Then `markAsSent` (`InAppProcessorAdapter.java:277-291`) increments per-target counters via `TargetingCountManager.getInstance().markAsSent(...)` and journey step state — this is what closes the loop with campaign stats.

---

## 5. Dispatch decision tree (concise)

```
LC servlet (MobileAppServlet / WebAppServlet / MobileWebAppServlet)
    │
    ▼
InAppManagerProvider.getEvalManager(sdkVersion, device, accountId)
    │
    ├─ advancedInAppsSupported == false  OR  no local-triggering flag on
    │        │
    │        ▼
    │   Flow A — Legacy InAppManager.evalAndFillResponse
    │        · Iterate targets, evaluate server-side, ship content
    │        · Response key: "inapp_notifs"
    │
    └─ advancedInAppsSupported == true  AND  localTriggeringRequired == true
             │
             ▼
        InAppProcessorAdapter.evalAndFillResponse
             │
             ▼
        InAppsFeature.CLIENT_SIDE ?
             │
             ├── ON  → Mode.CS → DeliveryStrategyProvider.getCS
             │           · DeliveryStrategyCS (bundle all CS inapps on meta-delivery events)
             │           + DELIVERY_SS_JOURNEY (journey inapp SS-evaluated)
             │           + DeliveryStrategySuppressionsCS (if suppressions)
             │           · Response keys: "inapp_notifs_cs", "inapp_notifs_ss"
             │           · SDK evaluates triggers + enforces caps locally
             │
             └── OFF → Mode.SS → DeliveryStrategyProvider.getSS
                         · MetaSS on meta-delivery events
                         · AppLaunchedSS + AppLaunchedSSMeta on App Launched
                         · SS + SSInaction on other events
                         · SSInactionContentFetch on inaction-delivery events
                         · DeliveryStrategySuppressionAppLaunchedSS (if suppressions)
                         · Response keys: "inapp_notifs_ss" / "_applaunched" / "_meta" / "_applaunched_meta"
                         · Server evaluates triggers + enforces caps

    All paths also add: "inapp_delivery_mode" (only from InAppProcessorAdapter),
                        "imc", "imp"  (via InAppManager.fillMetadata:120-130)
```

---

## 6. Key files quick reference

| Concern | File | Line |
|---|---|---|
| Old-vs-new selector | `core/lc/.../targeting/inapps/InAppManagerProvider.java` | 21–35 |
| SDK/device capability gate | `core/lc/.../targeting/inapps/InAppSupportChecker.java` | 6–14 |
| Legacy dispatch entry | `core/lc/.../targeting/InAppManager.java` | 157 |
| Legacy metadata (`imc`/`imp`) | `core/lc/.../targeting/InAppManager.java` | 120–130 |
| New dispatch entry (adapter) | `core/lc/.../targeting/inapps/InAppProcessorAdapter.java` | 142–216 |
| CS-vs-SS mode decision | `core/lc/.../targeting/inapps/InAppProcessorAdapter.java` | 306–308 |
| Response fill + dedup | `core/lc/.../targeting/inapps/InAppProcessorAdapter.java` | 218–236 |
| markAsSent bookkeeping | `core/lc/.../targeting/inapps/InAppProcessorAdapter.java` | 277–291 |
| Strategy router | `core/inapps/service/.../delivery/DeliveryStrategyProvider.java` | 44–99 |
| `Mode` enum | `core/inapps/service/.../delivery/DeliveryStrategyProvider.java` | 101–103 |
| Response keys | `core/inapps/service/.../delivery/common/PayloadKey.java` | 9–14 |
| CS strategy | `core/inapps/service/.../delivery/cs/DeliveryStrategyCS.java` | — |
| SS strategies | `core/inapps/service/.../delivery/ss/` | — |

---

## 7. Take-aways

- The **old flow is not an NB-based dispatch** — both flows live in LC. The "old" one is `InAppManager` doing server-side eval + full-content-in-response for every request; the "new" one is `InAppProcessorAdapter` delegating to the shared `core/inapps/service` processor with pluggable delivery strategies.
- The **old→new selector** is a per-request check based on SDK version, device, and account feature flags. Any account without the advanced flags — or any older SDK / non-Android/iOS device — keeps hitting the legacy path.
- Within the new flow, **CS vs SS is a per-account switch** (`InAppsFeature.CLIENT_SIDE`).
  - **CS** ships trigger metadata + content in bulk on App-Launched-like events; SDK owns evaluation.
  - **SS** keeps evaluation on LC; content is sent full on App Launched, metadata-only for other events (with content fetched separately).
- Journey and inaction in-apps stay server-side even under CS mode — they get bolted on to the CS response as extra `inapp_notifs_ss` items.
