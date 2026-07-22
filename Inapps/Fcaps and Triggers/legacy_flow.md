# In-app Legacy Flow — Fcaps + Triggers Deep Dive

Scope: the **pre-6.0** in-app dispatch path — before the "in-app feature parity" project introduced client-side triggering, advanced fcaps, and advanced display triggers. This is the flow every account still hits when SDK version < 60000, when device is Windows/Web, or when none of the three feature flags is on.

Everything below is verified against current `master` code paths and cross-checked with the BE TAN (`4008706201`) and SDK TAN (`4085613262`).

---

## 1. When this flow is chosen

Selection happens **per request** inside the LC servlet:

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

`legacyEvalManager` (the base `InAppManager` class) is used whenever **either** guard fails:

**SDK / device gate** — `InAppSupportChecker.java:6-14`
```java
static final int MIN_SDK_VERSION_FOR_ADVANCED_IN_APPS = 60000;
static final int OLD_SDK_BIG_VERSION = 301000;

public boolean advancedInAppsSupported(int sdkVersion, ProfileReach.Device device) {
    return sdkVersion >= MIN_SDK_VERSION_FOR_ADVANCED_IN_APPS &&
            sdkVersion != OLD_SDK_BIG_VERSION &&
            (device == ProfileReach.Device.Android || device == ProfileReach.Device.iOS);
}
```

**Legacy path taken when:**
- `sdkVersion < 60000`, or `sdkVersion == 301000` (an old iOS build carrying a big version number), or
- device ∈ {Windows, Web, WindowsMobile, …} (anything not Android/iOS), or
- account has none of `ADVANCED_FCAPS` / `ADVANCED_DISPLAY_TRIGGERS` / `CLIENT_SIDE` / inaction-event-props enabled

So an SDK ≥ 60000 on Android/iOS with **no advanced flags on the account** still lands on the legacy path.

Journeys are enforced through the same path for cap purposes — see `InAppCombinedQuotaCheck.java:24-33` where journey targets **always** fall back to the legacy quota check even if `ADVANCED_FCAPS` is on. That check is the same routine used inside the base `InAppManager`.

---

## 2. Data model (Mongo `targets`)

Every legacy in-app target document carries these fcap-related fields:

| Field constant | Mongo key | Type | Semantics |
|---|---|---|---|
| `TargetingDao.INAPP_TGT_LIFETIME_COUNT` | `tlc` | int | Once-per-lifetime cap. `-1` = no cap, `1` = "once ever" |
| `TargetingDao.INAPP_TGT_DAILY_COUNT` | `tdc` | int | Once-per-day cap. `-1` = no cap, `1` = "once per day" |
| `TargetingDao.INAPP_TGT_EXCLUDE_FREQUENCY_CAP` | `efc` | int | `1` = exclude campaign from every cap (target + global) |
| `TargetingDao.MAX_PER_CAMPAIGN` | `mdc` | string/int | Per-session cap. Enforced SDK-side only |

Constants declared at `core/dao/src/com/wizrocket/db/orchestration/dao/TargetingDao.java:360-362`:
```java
public static final String INAPP_TGT_LIFETIME_COUNT = "tlc";
public static final String INAPP_TGT_EXCLUDE_FREQUENCY_CAP = "efc";
public static final String INAPP_TGT_DAILY_COUNT = "tdc";
```

Legacy CleverTap effectively supported **three** cap options plus exclusion — the BE TAN wording (verbatim):
> CleverTap currently supports only "Once per session/day/user"

Anything richer (5 per day, N per week, etc.) was not expressible in legacy.

Account-level ceilings on the `accounts.n` document (`core/dao/.../AccountsDao.java:95, 162, 415-423`):

| Constant | Mongo key | Getter |
|---|---|---|
| `INAPP_MAX_PER_SESSION` | `ism` | `Account.getInAppMaxPerSession()` (default `1`) |
| `INAPP_MAX_PER_USER_PER_DAY` | `imp` | `Account.getInappMaxPerUserPerDay()` (default `10`) |

Default values are declared at `Account.java:71` (`private int imp = 10, wmp = 10;`) and `Account.java:77` (`private int wsm = 1, ism = 1;`).

---

## 3. Where the per-user counters live

They **do not** live on the server. Every SDK request ships them.

Format (verbatim from BE TAN):
```
"tlc": [
  [targetId, todayCount, lifetimeCount]
]
```

Parsed at `MobileAppRequestContainer.java:670-675`:
```java
this.targetShownList = (BasicDBList) eachObject.get(TARGET_SHOWN_LIST);   // JSON key "tlc"
this.targetsShownToday =
        eachObject.getInt(AccountsDao.INAPP_MAX_PER_USER_PER_DAY, -1);    // JSON key "imp"
```

Key constant at `AbstractMobileAndWebAppRequestContainer.java:119`:
```java
protected static final String TARGET_SHOWN_LIST = "tlc";
```

Confusingly the SDK reuses two keys with opposite meanings:
- `"tlc"` **on request** = per-campaign shown counts (list). **On response (per-campaign)** = the target-lifetime-count *ceiling* (int).
- `"imp"` **on request** = user's daily total. **On response** = the account daily *ceiling*.

The container getters (`AbstractMobileAndWebAppRequestContainer.java:231-236`) expose `getTargetShownList()` and `getTargetsShownToday()`, which the legacy dispatcher passes down as method arguments.

No Mongo write, no Redis write, no ES write, no FStore write ever persists these counters server-side. `TargetingCountManager.markAsSent(...)` is the only server-side "counter" touched during dispatch and it feeds **aggregate campaign stats** (`MongoStatsCountSink` → Mongo `campaign_stats` at `TargetingCountManager.java:65-66`), not per-user cap state.

---

## 4. End-to-end dispatch

The legacy dispatcher's entry point is inherited from `TargetEvalManager` but implemented in the base `InAppManager`.

### 4.1 Entry point

`core/lc/src/java/com/wizrocket/lc/targeting/InAppManager.java:157`
```java
public ArrayList<BaseRequestDto> evalAndFillResponse(int accountId, HttpServletRequest req,
        BasicDBObject response, ProfileReach.Device device, String eventName,
        BasicDBObject eventProperties, BasicDBObject profile, boolean firstSession,
        BasicDBObject appFields, BasicDBObject deviceFields, BasicDBList inappTargetUserCounts,
        int shownToday, HashSet<Integer> targetsQualifiedSet, BaseRequestDto baseRequestDto,
        BasicDBObject journeySteps, boolean isIPEnabled, String guid,
        AbstractMobileAndWebAppRequestContainer container) { ... }
```

Called once per event in the batch by `TargetEvalManager.processResponseWithEsCall` (with ES profile, `.java:190-219`) or `TargetEvalManager.processResponseWithoutEsCall` (no ES profile, `.java:150-184`). The container fields `container.getTargetShownList()` and `container.getTargetsShownToday()` become the `inappTargetUserCounts` and `shownToday` args (see the wiring at `TargetEvalManager.java:166-167, 202-203`).

### 4.2 Algorithm

Pseudocode extracted from `InAppManager.java:157-455`:

```
if (eventName == null) return;
if (event is AttrTrigger but no attribute change) return;

t = AccountWorker.getInAppWebTargeting(accountId);       // in-memory target cache (line 194)
targets = loadTargets(t, params, container);             // filtered/ordered list

for (ti in targets, ordered by priority-desc + id-asc):
    if (processTarget(container, EvalParams{...})):     // line 237 — trigger + segmentation eval
        excludeFromCaps  = ti.tgt.getInt("efc", -1);
        targetLifeTimeCap = ti.tgt.getInt("tlc", -1);
        targetDailyCap   = ti.tgt.getInt("tdc", -1);

        timezone = timezoneProvider.getTimeZone(accountId);
        if (timezone == null) return;      // hard stop for the whole account
        DateTime.setTZ(timezone);
        batchId = DateTime.getYMD();

        // pick message variant (line 274-296)
        if (device == WindowsMobile || sdkVersion < 14361 /* SDK_VERSION_FALLBACK_CONTENT */):
            m = ti.message(device, MSG_DEFAULT, null); key = MSG_DEFAULT;
        else if (ti.isLegacyInapp()):
            key = sdkVersion < 30300 ? "Legacy In-app" : "New In-app"; m = ti.message(...);
        else:
            key = ti.getMessageContentKey(profileFields, guid, -1); m = ti.message(...);

        if (m == null) continue;
        m = deep-copy(m);
        fixInAppDimensions(m."w", m, accountId, targetId);

        if (!applyCompatibilityCaps(sdkVersion, device, ti, tlc, tdc, efc, m."w", ...)):
            continue;                                    // legacy-legacy compat, see 4.4

        if (targetQuotaReached(accountId, ti, efc, tlc, tdc,
                               inappTargetUserCounts, shownToday, InApp, IN_APP)):
            continue;                                    // capped — see §5

        mergeJourneyContextNamespace(...);

        if (!contentBuilder.prepareContent(ContentRequest{...})):
            continue;                                    // personalization/replacement failed

        m.put("wzrk_id", targetId + "_" + batchId);
        m.put("key",     key);
        m.put("wzrk_ttl", contentUtil.getInAppTTLWithDelay(accountId, ti));
        m.put("ti",      targetId);
        if (tlc != -1) m.put("tlc", tlc);
        if (tdc != -1) m.put("tdc", tdc);
        addMdcIfNeeded(ti, m, sdkVersion, device);       // adds mdc=1 for advanced SDK if legacy is once-per-session
        if (efc == 1)  m.put("efc", 1);

        if (targetsQualifiedSet.contains(targetId)) continue;
        targetsQualifiedSet.add(targetId);

        cgId = ControlGroupUtils.getControlGroupId(iid, idn, ti.getControlGroupList(), ...);
        if (cgId != TARGET_GROUP_ID):
            // user is in control group — emit supplementary event, DO NOT deliver content
            supplementary.add(LCUtils.createControlGroupEvent(baseRequestDto, notifClickedId, cgType, key));
            addToQualifiedJourneySteps(...);
        else:
            enabledTargets.add(m);
            addToQualifiedJourneySteps(...);
            TargetingCountManager.markAsSent(1, accountId, targetId, batchId, device, InApp, key);
            break;    // ★ only one in-app per event (line 441)

fillResponse(device, enabledTargets, req, response);      // writes response["inapp_notifs"] = enabledTargets
return supplementary;
```

### 4.3 The critical "one per event" rule

At `InAppManager.java:441` — the loop breaks after the first delivered target. The doc comment is literal:
```java
break; // Since we only send down one inapp.
```

This is why the BE TAN says: *"For each event the BE iterates over all in-apps and finds the first one that … The BE returns the content of the qualified in-apps to the SDK."*

If a user is in the control group (SCG or custom CG) for the first-eligible target, the loop **continues** — the next eligible target may deliver.

### 4.4 The compatibility guard for very old SDKs

`InAppManager.java:499-537` — `applyCompatibilityCaps`:

```java
boolean deviceCapsSupported = sdkVersion >= SDK_VERSION_CAPS_SUPPORT   // 13513
        && device != ProfileReach.Device.WindowsMobile;

if (deviceCapsSupported) return true;                                   // modern-enough, no compat needed

if (ti.journeyId() > 0)          return false;   // journeys never go to SDKs without cap support
if (only-once-lifetime setup)    return false;   // "only once" is never sent to SDKs w/o cap support
```

So for SDKs older than 13513 (or WindowsMobile), journeys and lifetime-cap in-apps are silently dropped. This is legacy-legacy, mostly historical.

---

## 5. Cap enforcement — `TargetEvalManager.targetQuotaReached`

The single cap gate in the legacy path.

### 5.1 The public wrapper (fail-closed)

`core/lc/src/java/com/wizrocket/lc/targeting/TargetEvalManager.java:264-284`
```java
public static boolean targetQuotaReached(int accountId, TargetingInfo targetingInfo,
        int excludeFromFreqCaps, int tgtLifeTimeCap, int tgtDailyCap,
        BasicDBList inappTargetUserCounts, int shownToday, ProfileReach.Mode mode,
        CTLogger.Feature feature) {
    try {
        Account account = AccountWorker.getAccount(accountId);
        if (account == null) return true;   // fail-closed
        return targetQuotaReached(targetingInfo, ..., account);
    } catch (Throwable ex) {
        Profiler.report("CRITICAL: exception in accounting frequency caps for tgtId=%d ...", 1, ex, ...);
        return true;                        // fail-closed on any exception
    }
}
```

Any error → treated as "capped" → target dropped. This prevents over-delivery under uncertainty.

### 5.2 The body (verbatim doc comment + code)

`TargetEvalManager.java:290-345` — the comment lists the six cases and marks which are server-enforced:

> Cases:
> 1. ✓ [target level] Exclude from all
> 2. ✗ [target level] Once per session
> 3. ✓ [target level] Once per day
> 4. ✓ [target level] Once per lifetime
> 5. ✗ [global level] X campaigns per session
> 6. ✓ [global level] X campaigns per day
>
> Note: This method does not handle session level caps at all. It's left to be evaluated at the client side.

```java
if (excludeFromFreqCaps > 0) return false;   // case 1 — short-circuit everything

boolean targetDailyLimitReached = false, targetLifetimeLimitReached = false, globalDailyLimitReached;

if (inappTargetUserCounts != null) {
    for (Object o : inappTargetUserCounts) {
        BasicDBList tgtObj = (BasicDBList) o;                    // [ "targetId", daily, lifetime ]
        if (Integer.parseInt((String) tgtObj.get(0)) == tgtId) {
            int dailyCount    = (int) tgtObj.get(1);
            int lifetimeCount = (int) tgtObj.get(2);

            if (tgtDailyCap != -1 && dailyCount    != -1 && dailyCount    >= tgtDailyCap)  targetDailyLimitReached    = true;
            if (tgtLifeTimeCap != -1 && lifetimeCount != -1 && lifetimeCount >= tgtLifeTimeCap) targetLifetimeLimitReached = true;
            break;
        }
    }
}

int perDayLimit = getPerDayLimit(mode, account);                 // account.imp for InApp
globalDailyLimitReached = shownToday >= perDayLimit;

return targetDailyLimitReached || targetLifetimeLimitReached || globalDailyLimitReached;
```

Rules:
- **Sentinel `-1`** means "cap not set" on either the ceiling or the count. Both must be non-`-1` for the comparison to fire.
- The SDK counter list is scanned linearly for the first match by `targetId` (string-encoded). No match → per-target checks skipped, only global daily fires.
- **Global check runs unconditionally** — a brand-new campaign the SDK has never seen can still be blocked by `imp`.

Per-day ceiling by mode (`TargetEvalManager.java:347-358`):
```java
private static int getPerDayLimit(ProfileReach.Mode mode, Account account) {
    switch (mode) {
        case Web:              return account.getWebMaxPerUserPerDay();
        case WebInbox:         return account.getWebInboxMaxPerUserPerDay();
        case WebNativeDisplay: return account.getWebNativeDisplayMaxPerUserPerDay();
        default:               return account.getInappMaxPerUserPerDay();
    }
}
```

### 5.3 Enforcement matrix

| Cap | Where enforced |
|---|---|
| `efc > 0` (exclude) | Server + SDK |
| `tlc` (per-campaign lifetime) | Server + SDK |
| `tdc` (per-campaign daily) | Server + SDK |
| `mdc` (per-campaign session) | SDK only |
| Account `imp` (per-user daily) | Server + SDK |
| Account `ism` (per-user session) | SDK only |

The doc comment is authoritative: sessions are never a server concern in legacy.

---

## 6. Trigger + segmentation evaluation

Legacy triggers were **single-event with property filters**, evaluated **server-side** on every request event.

Actual eval (called inside the loop at `InAppManager.java:237-249`):
```java
processTarget(container, EvalParams.builder()
        .accountId(accountId)
        .info(ti)
        .eventName(eventName)
        .eventProperties(eventPropertiesForEval)
        .firstSession(firstSession)
        .device(device)
        .profile(profile)
        .epHandlerProps(epHandlerProps)
        .journeySteps(journeySteps)
        .ip(Helper.getRemoteIP(req))
        .isIPEnabled(isIPEnabled)
        .build())
```

This resolves through `TargetEvalManager.processTarget` (`.java:79-96`) → `TargetingUtils.processOnlineTarget(evalParams, logInfo)` — the shared predicate engine used by push/email/etc. Under the hood it evaluates the target's segmentation query (`sq`/`wc` format) against the event, event properties, and — if the target has profile-referencing predicates — the user profile.

Expressiveness (from the SDK TAN's "Triggers" section):
- Single trigger event per campaign
- Event property operators: `equals`, `not equals`, `exists`, `does not exist`, `contains`, `does not contain`, `between`, `greater than`, `less than`
- Multiple properties AND'd together
- Special-case handling for `Charged` event items, notification-props aliasing (`Campaign id` ↔ `wzrk_id`, `Variant` ↔ `wzrk_pivot`), and system properties (`CT App Version` → `Version`, etc.)
- Optional geo-radius (Haversine on backend's fixed earth-diameter constant, `EDUtils.java:698-712`)

The BE evaluates the full `sq`/`wc` query, meaning trigger event + property filters + past-behaviour (miniWho) + segmentation are all resolved server-side per event.

---

## 7. Content preparation and response

### 7.1 Variant selection

`InAppManager.java:274-302`:
- SDK < 14361 or WindowsMobile → fallback content (`MSG_DEFAULT`)
- Else, if `isLegacyInapp()`: `"Legacy In-app"` for SDK < 30300, `"New In-app"` otherwise
- Else: `ti.getMessageContentKey(profileFields, guid, -1)` for A/B or profile-based content variants

### 7.2 Content build

`contentBuilder.prepareContent(...)` at `.java:355-367` — runs personalization/replacements/recommendations against the profile. Returns false if resolution fails → target dropped.

### 7.3 Fields the SDK sees

`InAppManager.java:371-390`:
- `wzrk_id` = `targetId + "_" + batchId` (batchId = `DateTime.getYMD()` in the account timezone)
- `NOTIFICATION_CONTENT_KEY` = message-variant key
- `wzrk_ttl` = `contentUtil.getInAppTTLWithDelay(accountId, ti)` (absolute epoch seconds)
- `ti` = target id
- Optionally `inappDelayAfterTrigger` (feature-flag gated)
- `tlc`, `tdc`, `mdc`, `efc` — echoed only when set (`addMdcIfNeeded` adds `mdc=1` for advanced-capable SDKs even in legacy mode if the legacy session cap is 1)

### 7.4 Response payload

`TargetEvalManager.fillResponse` (`.java:127-145`) writes `enabledTargets` to `response["inapp_notifs"]` (or `RESPONSE_INAPP_KEY_WINDOWS` for WindowsMobile).

The `fillMetadata` method (called separately from the servlet before evalAndFillResponse) sets the account-level ceilings on the response — `InAppManager.java:120-130`:
```java
response.put(INAPP_MAX_PER_SESSION, account.getInAppMaxPerSession());       // "imc"
if (account.getInappMaxPerUserPerDay() != -1) {
    response.put(AccountsDao.INAPP_MAX_PER_USER_PER_DAY,                     // "imp"
                 account.getInappMaxPerUserPerDay());
}
```

Legacy responses do **not** include `inapp_delivery_mode` (that key is added only by the new `InAppProcessorAdapter.fillMetadata` at `.java:80-84`).

Response payload for a legacy dispatch:
```json
{
  "inapp_notifs": [
    {
      "ti": 55001,
      "wzrk_id": "55001_20260720",
      "wzrk_pivot": "wzrk_default",
      "wzrk_ttl": 1691758604,
      "title": {...},
      "message": {...},
      "media": {...},
      "buttons": [...],
      "type": "half-interstitial",
      "tlc": 1,
      "tdc": -1,
      "efc": -1
    }
  ],
  "imc": 5,
  "imp": 10
}
```

At most **one** element in `inapp_notifs` per triggering event (see §4.3).

---

## 8. Control group handling

`InAppManager.java:412-441` handles CG suppression:

- `ControlGroupUtils.getControlGroupId(iid, idn, controlGroupList, excludedUserListCG, cgAbHashDistEnabled)` returns:
    - `TARGET_GROUP_ID` → user in target group, deliver.
    - `SYSTEM_CG_ID` (= `0`) → system control group.
    - other → custom control group.
- If **not** target group: emit a supplementary `NotificationSuppressed`-style event via `LCUtils.createControlGroupEvent(...)`, mark journey step, **don't** call `markAsSent`, **don't** break — keep looking at the next target.
- If target group: append to `enabledTargets`, `markAsSent` (bumps `TargetingCountManager` for aggregate stats), break.

The `id <= 0` guard at `.java:405-408` blocks CG evaluation for anonymous devices (no identity → no CG membership → target dropped if any CG is configured).

---

## 9. What the SDK does with the response

Per SDK TAN "Sample In-app notification payload" and "Global Caps":
1. Read `imc` / `imp` if present — update local ceilings.
2. Enqueue each `inapp_notifs[]` entry for display.
3. Before rendering, re-check `mdc` (session), `tdc`, `tlc`, `efc`, `imc`, `imp` against local counters.
4. On successful render:
   - Increment local `imp` (global-today)
   - Increment local `tlc[targetId]` (both dailyCount + lifetimeCount)
   - Fire `Notification Viewed` event back to LC on next request
5. Send both counters in the *next* request as `tlc` (list of triples) and `imp` (int).
6. On `wzrk_id` receipt, the id encodes `campaignId + "_" + YYYYMMDD` — the SDK uses the date part to know when to reset per-day counters.

Server side: `inapp_stale` in the response tells the SDK which target IDs to purge from local counter storage — targets that have been stopped/completed. The SDK sends its full `tlc` array; the server returns a subset of stale IDs so the device can shrink its list.

---

## 10. Multi-request-thread interactions

Legacy dispatch runs per-request on LC's async servlet thread pool. It never blocks on IO:
- Target list comes from `AccountWorker.getInAppWebTargeting(accountId)` — an in-memory cache refreshed on a background thread every ~30s.
- Account object from `AccountWorker.getAccount(accountId)` — same cache.
- Timezone from `TimezoneProvider`/`AccountWorker.getTimeZone` — cached.
- Profile (if `needsProfile` on any target) — fetched from ES via LC's ES lookup pool **before** entering `evalAndFillResponse`, then passed as an argument.
- `TargetingCountManager.markAsSent` writes to an in-memory map and flushes periodically to Mongo (`.java:134` — scheduled every N seconds).

Fail-closed everywhere: null profile, null account, missing timezone, exception during cap check — all return early or set `blocked = true`.

---

## 11. Key file / line reference

| Concern | File | Line |
|---|---|---|
| Legacy-vs-new selector | `core/lc/.../targeting/inapps/InAppManagerProvider.java` | 21-35 |
| SDK/device support gate | `core/lc/.../targeting/inapps/InAppSupportChecker.java` | 6-14 |
| Legacy entry point | `core/lc/.../targeting/InAppManager.java` | 157 |
| Variant selection | `core/lc/.../targeting/InAppManager.java` | 274-302 |
| Compatibility caps for old SDKs | `core/lc/.../targeting/InAppManager.java` | 499-537 |
| Cap check invocation | `core/lc/.../targeting/InAppManager.java` | 336-342 |
| Content build + payload fields | `core/lc/.../targeting/InAppManager.java` | 355-390 |
| CG suppression + delivery break | `core/lc/.../targeting/InAppManager.java` | 412-441 |
| fillMetadata (imc / imp ceilings) | `core/lc/.../targeting/InAppManager.java` | 120-130 |
| Trigger/segmentation eval engine | `core/lc/.../targeting/TargetEvalManager.java` | 79-96 |
| Cap check body | `core/lc/.../targeting/TargetEvalManager.java` | 264-345 |
| Per-day-limit accessor | `core/lc/.../targeting/TargetEvalManager.java` | 347-358 |
| fillResponse (inapp_notifs writer) | `core/lc/.../targeting/TargetEvalManager.java` | 127-145 |
| Legacy target-doc constants | `core/dao/.../orchestration/dao/TargetingDao.java` | 360-362 |
| Account-level caps | `core/dao/.../dao/AccountsDao.java` | 95, 162, 415-423 |
| Account default caps | `core/dao/.../dto/Account.java` | 71, 77, 209, 417 |
| SDK-supplied counters (request parse) | `core/lc/.../request_containers/MobileAppRequestContainer.java` | 670-675 |
| `TARGET_SHOWN_LIST` = `"tlc"` constant | `core/lc/.../request_containers/AbstractMobileAndWebAppRequestContainer.java` | 119 |
| Stats writer (aggregate only) | `core/delivery/stats/.../TargetingCountManager.java` | 320-354 |

---

## 12. Take-aways

1. **The legacy dispatcher fires per event, delivers at most one in-app per event.** The doc line `break; // Since we only send down one inapp.` is literal.
2. **Server + SDK enforce daily/lifetime caps together.** Server does its check against SDK-supplied counters, then the SDK re-checks before render. Session caps are always SDK-only.
3. **`efc > 0` short-circuits every cap** — including global. This is stronger than the advanced-mode `excludeGlobalFCaps`, which only bypasses global.
4. **The per-user counter never lives server-side.** The SDK is the source of truth; loss of device state (uninstall / clear data) resets counters. `TargetingCountManager` writes are aggregate only.
5. **Fail-closed** — null account, missing timezone, exception → target dropped. Ultron would rather ship nothing than double-deliver.
6. **Trigger + segmentation are full server-side.** Each event pays for a `TargetingUtils.processOnlineTarget` evaluation against every in-app target (in priority order). This is the load model the newer flows are trying to shed.
7. **Journeys always ride the legacy quota check** (`InAppCombinedQuotaCheck.java:24-33`), even under `ADVANCED_FCAPS`. The rich `frequencyLimits`/`occurrenceLimits` model doesn't apply cleanly to journey nodes and journeys have their own per-node semantics.
8. **`applyCompatibilityCaps`** silently drops journeys and once-per-lifetime in-apps for pre-13513 SDKs / WindowsMobile — legacy-legacy behaviour.
