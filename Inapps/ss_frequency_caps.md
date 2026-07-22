# SS In-App Frequency Caps — Deep Dive

Companion to [`ss_deep_dive.md`](./ss_deep_dive.md). Scope: **frequency caps for server-side (SS) in-apps only**. Covers the two cap levels (global + campaign), how they're stored, how they're counted, how they're enforced, and how the `ADVANCED_FCAPS` feature flag splits enforcement between server and SDK.

Session-level caps are **not enforced on LC** (the code even calls this out — `TargetEvalManager.java:294-295`). They live entirely on the SDK. Everything below is about **daily** and **lifetime** enforcement.

---

## 1. The two levels

### 1.1 Global (account-wide) cap
- **What:** "How many in-apps this user has already seen today, across ALL campaigns."
- **Value source:** `accounts` collection → `n.imp` (Mongo). Loaded by `AccountsDao` (`core/dao/.../AccountsDao.java:420-423`) into `Account.inappMaxPerUserPerDay`.
- **Also loaded:** `n.ism` → `Account.inAppMaxPerSession` (session cap, SDK-enforced only).
- Fetched in enforcement via `getPerDayLimit(mode, account)` (`TargetEvalManager.java:347-358`).
- Advertised to SDK in every response as `imc` (session) and `imp` (day) via `InAppManager.fillMetadata` (`InAppManager.java:120-130`).

### 1.2 Campaign / target-level caps
- **What:** Per-campaign daily count + per-campaign lifetime count + per-campaign exclusion from caps.
- **Value source:** the target's own Mongo document. Field constants in `TargetingDao.java:360-362`:
  ```java
  public static final String INAPP_TGT_LIFETIME_COUNT = "tlc";
  public static final String INAPP_TGT_EXCLUDE_FREQUENCY_CAP = "efc";
  public static final String INAPP_TGT_DAILY_COUNT = "tdc";
  ```
- Read at eval time from `ti.getTgtObject().getInt(...)` inside the legacy quota check binding (`InAppsLcModule.java:57-59`).

Semantics recap (from the doc-comment on the legacy check — `TargetEvalManager.java:290-295`):
```
1. ✓ [target level] Exclude from all         (efc)
2. x [target level] Once per session         (SDK-only)
3. ✓ [target level] Once per day             (tdc)
4. ✓ [target level] Once per lifetime        (tlc)
5. x [global level] X campaigns per session  (SDK-only)
6. ✓ [global level] X campaigns per day      (imp)
```
✓ = server-enforced. x = SDK-enforced.

---

## 2. Where the counters live (the read side)

Two counters must be **read at request time** to answer "has this campaign been shown enough already?":
1. Per-target daily + lifetime count for this user
2. Global daily count for this user

**Neither is fetched from Mongo on the request thread.** LC never blocks on IO for this — so both counters are supplied **by the SDK in the request payload**.

`MobileAppRequestContainer.java:670-675`:
```java
this.targetShownList = (BasicDBList) eachObject.get(TARGET_SHOWN_LIST);
this.targetsShownToday =
        eachObject.getInt(AccountsDao.INAPP_MAX_PER_USER_PER_DAY, -1);
```

Format:
- **`TARGET_SHOWN_LIST`** — list of `[targetId, dailyCount, lifetimeCount]` triples for every campaign the SDK has ever shown to this user (or at least remembers). Structure enforced by `TargetEvalManager.targetQuotaReached` at line `312-337`:
  ```java
  BasicDBList tgtObj = (BasicDBList) o;
  if (Integer.parseInt((String) tgtObj.get(0)) == tgtId) {
      int dailyCount = (int) tgtObj.get(1);
      int lifetimeCount = (int) tgtObj.get(2);
      ...
  }
  ```
- **`INAPP_MAX_PER_USER_PER_DAY` (= `"imp"`)** — the SDK's own "how many in-apps I've shown today across all campaigns" counter. Repurposes the same JSON key as the *ceiling* the server hands back — confusing but historical. On the request it means *shown today*; on the response it means *max today*.

These are passed through the LC servlet as:
- `container.getTargetShownList()` → `inappTargetUserCounts` argument
- `container.getTargetsShownToday()` → `shownToday` argument

Handoff site (legacy `InAppManager.evalAndFillResponse`): `InAppManager.java:160-161` — same signature is inherited by `InAppProcessorAdapter`.

They are then packed into `ProcessParams` for the SS strategies (`InAppProcessorAdapter.java:180-184`):
```java
.inappTargetUserCounts(inappTargetUserCounts)
.shownToday(shownToday)
.targetsQualifiedSet(targetsQualifiedSet)
```

**Consequence:** frequency-cap accuracy is bounded by SDK honesty. If the SDK loses its local counter (uninstall/reinstall, storage clear), the user can re-cross a per-target lifetime cap. Ultron accepts this in exchange for zero read latency on the hot path.

---

## 3. Where the counters live (the write side)

When SS actually delivers a payload, `InAppProcessorAdapter.markAsSent` (`InAppProcessorAdapter.java:277-291`) bumps a **server-side** counter:
```java
TargetingCountManager.getInstance().markAsSent(1, String.valueOf(accountId),
        payloadMeta.getTi().getID(),
        payloadMeta.getBatchId(), device, ProfileReach.Mode.InApp,
        payloadMeta.getContentKey());
```

That counter (`core/delivery/stats/.../TargetingCountManager.java:320-354`) writes into an in-memory `addToKey` structure, keyed by `accountId + targetId + batchId + device + pivot + SENT`. It is later flushed on a scheduler (line 134) to durable **stats storage** — this feeds Dashboard analytics and per-campaign reporting, **not** the per-user cap check. The per-user cap check reads the SDK's counters, not this.

So the model is:
- **Reads for enforcement:** SDK-supplied counters in the request.
- **Writes for stats:** server-side counters via `TargetingCountManager`.

They are intentionally different data paths — a user's "shown-today" is user state (SDK-owned), whereas the stats aggregate is business state (server-owned).

---

## 4. The enforcement gate — `InAppCombinedQuotaCheck`

Every SS strategy that emits actual content (`DeliveryStrategySS`, `DeliveryStrategyAppLaunchedSS`, `DeliveryStrategySSInactionContentFetch`) calls:

```java
@Named(NamedConstants.COMBINED_QUOTA_CHECK)
private final InAppQuotaCheck combinedQuotaCheck;
...
combinedQuotaCheck.targetQuotaReached(params, ti)
```

Metadata-only strategies (`DeliveryStrategyMetaSS`, `DeliveryStrategySSInaction`, `DeliveryStrategyAppLaunchedSSMeta`) **do not** call it — they ship the cap ceilings in the payload for the SDK to enforce locally when the campaign eventually renders.

Combined check body (`core/inapps/service/.../delivery/common/InAppCombinedQuotaCheck.java:24-33`):
```java
public boolean targetQuotaReached(ProcessParams params, TargetingInfo ti) {
    boolean fCapsEnabled = featureFlags
            .isEnabled(InAppsFeature.ADVANCED_FCAPS, params.getAccountId());
    if (fCapsEnabled && !ti.isJourneyTarget()) {
        return newQuotaCheck.targetQuotaReached(params, ti);
    }
    return legacyQuotaCheck.targetQuotaReached(params, ti);
}
```

So the routing is:

| Account has `ADVANCED_FCAPS` on? | Target is a journey? | Which check runs? |
|---|---|---|
| No | any | Legacy |
| Yes | No | Advanced |
| Yes | Yes | Legacy |

Journeys always use legacy because they carry their own per-node semantics — the "advanced fcaps" model doesn't apply cleanly to journey nodes.

---

## 5. Legacy quota check — enforces BOTH levels on the server

Binding (`core/lc/.../targeting/inapps/InAppsLcModule.java:50-63`) — this is a lambda, not a class:
```java
@Named(NamedConstants.LEGACY_QUOTA_CHECK)
@Provides @Singleton
public InAppQuotaCheck inAppLegacyQuotaCheck() {
    return (processParams, ti) -> TargetEvalManager.targetQuotaReached(
            processParams.getAccountId(),
            ti,
            ti.getTgtObject().getInt(TargetingDao.INAPP_TGT_EXCLUDE_FREQUENCY_CAP, -1),
            ti.getTgtObject().getInt(TargetingDao.INAPP_TGT_LIFETIME_COUNT, -1),
            ti.getTgtObject().getInt(TargetingDao.INAPP_TGT_DAILY_COUNT, -1),
            processParams.getInappTargetUserCounts(),
            processParams.getShownToday(),
            ProfileReach.Mode.InApp, CTLogger.Feature.IN_APP);
}
```

Actual enforcement body — `TargetEvalManager.targetQuotaReached(...)` (`core/lc/.../targeting/TargetEvalManager.java:297-345`):

```java
if (excludeFromFreqCaps > 0) {
    return false;                                     // efc=1 → skip all caps
}

// per-target counters from SDK
if (inappTargetUserCounts != null) {
    for (Object o : inappTargetUserCounts) {
        BasicDBList tgtObj = (BasicDBList) o;
        if (Integer.parseInt((String) tgtObj.get(0)) == tgtId) {
            int dailyCount    = (int) tgtObj.get(1);
            int lifetimeCount = (int) tgtObj.get(2);

            if (tgtDailyCap != -1 && dailyCount != -1
                    && dailyCount >= tgtDailyCap) {
                targetDailyLimitReached = true;
            }
            if (tgtLifeTimeCap != -1 && lifetimeCount != -1
                    && lifetimeCount >= tgtLifeTimeCap) {
                targetLifetimeLimitReached = true;
            }
            break;
        }
    }
}

int perDayLimit = getPerDayLimit(mode, account);      // account.imp
globalDailyLimitReached = shownToday >= perDayLimit;

return targetDailyLimitReached || targetLifetimeLimitReached || globalDailyLimitReached;
```

Rules:
- **Sentinel `-1`** means "cap not set" — must be checked on both the ceiling and the count before comparing. Otherwise `-1 >= -1` would trip.
- **`efc > 0`** wins over everything else — the campaign is exempt from every cap (target and global).
- **First-match iteration** — the count list is scanned linearly until the target's row is found; the loop breaks after the first match.
- **Global check runs unconditionally** — even if no per-target row is found. Which means a brand-new campaign, unseen by the SDK, can still be blocked by the global cap.
- **Exception handling** — if the whole calculation throws, the outer wrapper (`.java:264-284`) logs P1 and returns `true` (blocked) — fail-closed under uncertainty.

---

## 6. Advanced quota check — enforces ONLY the global cap

Binding: `bind(InAppQuotaCheck.class).annotatedWith(Names.named(NamedConstants.ADVANCED_FCAPS_GLOBAL_QUOTA_CHECK)).to(InAppAdvancedFCapsGlobalQuotaCheck.class)` — `InAppsLcModule.java:41-43`.

Body (`core/inapps/service/.../delivery/common/InAppAdvancedFCapsGlobalQuotaCheck.java:16-33`):
```java
public boolean targetQuotaReached(ProcessParams params, TargetingInfo ti) {
    boolean excludeGlobalFCaps = ti.getAdvancedLimits()
            .map(AdvancedLimits::excludeGlobalFCaps)
            .orElse(false);
    if (excludeGlobalFCaps) {
        return false;
    }

    return Optional.ofNullable(accountProvider.apply(params.getAccountId()))
            .map(Account::getInappMaxPerUserPerDay)
            .map(l -> targetGlobalQuotaReached(params.getShownToday(), l))
            .orElse(true);
}

private boolean targetGlobalQuotaReached(int shownToday, int globalDayLimit) {
    return shownToday >= globalDayLimit;
}
```

Key differences vs legacy:
- **No per-target daily/lifetime check.** Those are the SDK's job under `ADVANCED_FCAPS` — the server ships the cap definitions inside the metadata payload and the SDK enforces them locally against its own history.
- **`excludeGlobalFCaps` skip** — sourced from `AdvancedLimits.excludeGlobalFCaps` (`core/inapps/models/.../AdvancedLimits.java:25, 39`), which reads `New.EXCLUDE_GLOBAL_FCAPS` from the target JSON. The advanced-mode analogue of legacy `efc`.
- **Fail-closed on missing account** — `orElse(true)` returns "quota reached" if the account can't be resolved, matching the legacy try/catch fail-closed philosophy.

`AdvancedLimits` also carries `fCaps` (list of `FREQUENCY_LIMITS`) and `triggers` (list of `OCCURRENCE_LIMITS`). Those are **not** consulted server-side by this quota check — they're shipped to the SDK as metadata for local enforcement. If the SDK later sends a content-fetch request for such a campaign, the server trusts the SDK's decision (the campaign must be in `params.inAppEvalIds` — see `DeliveryStrategySS.shouldSkipTarget` at `.java:106-114`).

---

## 7. The two enforcement models side by side

| Concern | Legacy (`ADVANCED_FCAPS` off, or journey) | Advanced (`ADVANCED_FCAPS` on, non-journey) |
|---|---|---|
| Per-target daily | Server-enforced (`tdc` vs `TARGET_SHOWN_LIST[i][1]`) | SDK-enforced (advanced `FREQUENCY_LIMITS`) |
| Per-target lifetime | Server-enforced (`tlc` vs `TARGET_SHOWN_LIST[i][2]`) | SDK-enforced (advanced `FREQUENCY_LIMITS`) |
| Per-target session | SDK-enforced | SDK-enforced |
| Global daily | Server-enforced (`account.imp` vs `shownToday`) | Server-enforced (same) |
| Global session | SDK-enforced (`account.ism`) | SDK-enforced |
| Exclude-from-caps | `INAPP_TGT_EXCLUDE_FREQUENCY_CAP` (`efc`) | `AdvancedLimits.excludeGlobalFCaps` |
| Read source | `TARGET_SHOWN_LIST` + `INAPP_MAX_PER_USER_PER_DAY` from SDK request | Only `INAPP_MAX_PER_USER_PER_DAY` from SDK request |
| Fail-closed on error | Yes (try/catch → `true`) | Yes (`orElse(true)`) |

The `ADVANCED_FCAPS` migration essentially **narrows** what LC has to enforce — global daily only — and pushes richer rules ("show this campaign at most 2 times per week", "cap all promo campaigns to 3 per day combined", etc.) onto the SDK, where they can respect device-side signals the server doesn't have.

---

## 8. Where the check fires in each SS strategy

`combinedQuotaCheck.targetQuotaReached(params, ti)` is a term inside `shouldSkipTarget` for the three strategies that ship content:

| Strategy | Skip if quota reached? | Reference |
|---|---|---|
| `DeliveryStrategySS` | Yes | `DeliveryStrategySS.java:106-114` |
| `DeliveryStrategyAppLaunchedSS` | Yes | `DeliveryStrategyAppLaunchedSS.java:152-158` |
| `DeliveryStrategySSInactionContentFetch` | Yes | `DeliveryStrategySSInactionContentFetch.java:148-151` (with exit-query too) |
| `DeliveryStrategyMetaSS` | No — metadata only | `DeliveryStrategyMetaSS.java:23-37` |
| `DeliveryStrategySSInaction` | No — metadata only | `DeliveryStrategySSInaction.java:97-125` (qualification write only) |
| `DeliveryStrategyAppLaunchedSSMeta` | No — metadata only | `DeliveryStrategyAppLaunchedSSMeta.java:104-112` |

The pattern is uniform: **the server only quota-checks when it's about to hand actual content to the SDK**. Metadata drops don't count against caps because they don't cause a render.

For `SSInactionContentFetch` (the delayed content path) two things are checked together (`.java:148-151`):
```java
private boolean shouldSkipTarget(ProcessParams params, TargetingInfo ti) {
    return !inAppTargetEval.processTargetExitQuery(params, ti)
            || combinedQuotaCheck.targetQuotaReached(params, ti);
}
```
The exit-query re-validates that the user still qualifies now (they may have exited the segment during the delay); the quota check re-validates that they haven't blown through the daily cap while the timer was ticking. If either fails, the qualification is deleted from FStore via `addToDeleteBatchTask` — quota-blocked content isn't kept dangling.

---

## 9. The full cap ceilings ship to the SDK — always

Every response (legacy + new, CS + SS) gets these three ceilings in the top-level object:

| Key | Value source | Meaning |
|---|---|---|
| `imc` | `account.getInAppMaxPerSession()` (Mongo `n.ism`) | Session cap for global inapps |
| `imp` | `account.getInappMaxPerUserPerDay()` (Mongo `n.imp`) | Daily cap for global inapps (optional, only if set) |
| `inapp_delivery_mode` | `getDeliveryMode(accountId)` → CS / SS | (New adapter only) tells SDK which enforcement contract to honor |

Set in `InAppManager.fillMetadata` (`.java:120-130`) + `InAppProcessorAdapter.fillMetadata` (`.java:80-84`):
```java
response.put(INAPP_MAX_PER_SESSION, account.getInAppMaxPerSession());  // "imc"
if (account.getInappMaxPerUserPerDay() != -1) {
    response.put(AccountsDao.INAPP_MAX_PER_USER_PER_DAY,               // "imp"
            account.getInappMaxPerUserPerDay());
}
response.put(DELIVERY_MODE_PAYLOAD_KEY, getDeliveryMode(accountId).name());
```

Plus, **per campaign**:
- Legacy mode: the payload includes `tlc`, `tdc`, `efc` (via `ContentUtil.getCommonFields`)
- Advanced mode: the payload includes `frequencyLimits`, `whenLimits`, `excludeGlobalFCaps` (via `AdvancedLimits`)

So even in advanced mode, the server always sends the SDK **both** the campaign-level rules and the account-level ceilings — because the SDK needs them to make its own decisions when advanced rules govern the render.

---

## 10. Worked example — legacy mode

Setup:
- Account `12345`, `ADVANCED_FCAPS` off.
- Global `imp = 5`.
- Campaign `A`: `tdc = 2`, `tlc = 10`, `efc = -1`.
- Campaign `B`: `efc = 1` (excluded from all caps).
- User has already been shown A twice today (5 lifetime), B once today.

Request from SDK:
```
targetShownList = [["55001", 2, 5], ["55002", 1, 1]]   // [tgtId, dailyCount, lifetimeCount]
targetsShownToday = 3                                    // total user-day count
```

Eval for campaign A (id=55001):
```
efc = -1 → don't short-circuit
row for 55001 found → dailyCount=2, lifetimeCount=5
tdc=2, dailyCount=2 → targetDailyLimitReached = true
tlc=10, lifetimeCount=5 → not reached
perDayLimit = 5, shownToday = 3 → globalDailyLimitReached = false
→ TRUE (blocked by target daily cap)
```

Eval for campaign B (id=55002):
```
efc = 1 → return FALSE immediately (excluded)
```

Eval for campaign C (id=55003, never seen):
```
efc = -1 → don't short-circuit
no row for 55003 → target counts unchecked (both false)
perDayLimit = 5, shownToday = 3 → globalDailyLimitReached = false
→ FALSE (proceed to deliver)
```

If user sees C, LC calls `markAsSent` → `TargetingCountManager` records it → SDK bumps its local `TARGET_SHOWN_LIST` + `imp` for the next request.

---

## 11. Worked example — advanced mode

Same setup but account has `ADVANCED_FCAPS` on. Campaign A has advanced rules configured (a `FREQUENCY_LIMITS` block) instead of legacy `tdc`/`tlc`.

Request from SDK includes:
```
inAppEvalIds = [55001]          // SDK is asserting "I locally decided A can render"
targetsShownToday = 3
```

Eval:
- Combined check → advanced → `InAppAdvancedFCapsGlobalQuotaCheck`
- `excludeGlobalFCaps` on A: false
- `shownToday=3` vs `imp=5` → not reached
- Return FALSE → deliver

Note the server never looked at per-target counts. It trusted the SDK's `inAppEvalIds` assertion that campaign A's advanced rules allow it now.

If A had no advanced rules (`hasAdvancedRules=false`), `DeliveryStrategySS.shouldSkipTarget` (`.java:106-114`) would let the target through even without being in `inAppEvalIds`, and the advanced quota check would still allow global-only enforcement.

If the SDK is buggy and asserts `inAppEvalIds = [55001]` when A's local rule says "no", the server has no way to detect that — again, honesty of the SDK is a foundational assumption.

---

## 12. Failure modes and gotchas

- **SDK lost/reset local counters** → per-target cap resets. Global cap resets too (because `shownToday` comes from the SDK). Only server stats remain intact.
- **`efc` field misused** — legacy check tests `efc > 0`. Setting `efc = 0` or `efc = -1` both mean "not excluded"; only positive values exempt the campaign.
- **New-quota-check without `AdvancedLimits`** — the code reads `ti.getAdvancedLimits()` as Optional; an empty Optional treats `excludeGlobalFCaps` as false. So legacy targets promoted into advanced-mode accounts still get global-capped normally.
- **Journeys under `ADVANCED_FCAPS`** — always fall back to legacy quota check via `!ti.isJourneyTarget()` guard in the combined check. Journey nodes therefore still need `tlc`/`tdc`/`efc` set if you want per-node capping.
- **`DeliveryStrategyMetaSS` and `DeliveryStrategySSInaction` bypass the quota check.** That's intentional — they only ship metadata. But it means those payloads are visible to a user who has already blown the global cap; the SDK is expected to also honor the ceilings before choosing to render.
- **Exception under legacy check → treated as capped.** The wrapper (`TargetEvalManager.java:275-283`) reports P1 and returns `true`. Under advanced check, unresolved account also returns `true` (`orElse(true)`). Both fail-closed to prevent over-delivery under uncertainty.

---

## 13. Key files quick reference

| Concern | File | Line |
|---|---|---|
| Global cap fields on `accounts` doc | `core/dao/.../AccountsDao.java` | 95, 162, 415–423 |
| Target cap fields on `targets` doc | `core/dao/.../TargetingDao.java` | 360–362 |
| Advanced-mode cap model | `core/inapps/models/.../AdvancedLimits.java` | 22–68 |
| Counter read (SDK request → container) | `core/lc/.../MobileAppRequestContainer.java` | 672–675 |
| Container getters | `core/lc/.../AbstractMobileAndWebAppRequestContainer.java` | 231–236 |
| Handoff into ProcessParams | `core/lc/.../inapps/InAppProcessorAdapter.java` | 180–184 |
| Combined quota check routing | `core/inapps/service/.../delivery/common/InAppCombinedQuotaCheck.java` | 24–33 |
| Legacy quota check binding | `core/lc/.../targeting/inapps/InAppsLcModule.java` | 50–63 |
| Legacy quota check body | `core/lc/.../targeting/TargetEvalManager.java` | 297–345 |
| Legacy wrapper (fail-closed) | `core/lc/.../targeting/TargetEvalManager.java` | 264–284 |
| `getPerDayLimit` (mode → account field) | `core/lc/.../targeting/TargetEvalManager.java` | 347–358 |
| Advanced quota check body | `core/inapps/service/.../delivery/common/InAppAdvancedFCapsGlobalQuotaCheck.java` | 16–33 |
| Cap ceilings shipped to SDK | `core/lc/.../targeting/InAppManager.java` | 120–130 |
| Delivery-mode key added | `core/lc/.../inapps/InAppProcessorAdapter.java` | 80–84 |
| Quota check call sites (SS strategies) | `DeliveryStrategySS.java:106-114`, `DeliveryStrategyAppLaunchedSS.java:152-158`, `DeliveryStrategySSInactionContentFetch.java:148-151` | — |
| Server-side stats writer | `core/delivery/stats/.../TargetingCountManager.java` | 320–354 |

---

## 14. Take-aways

- **Two levels, two owners.** Global daily is server-enforced; per-target daily/lifetime is server-enforced under legacy but SDK-enforced under `ADVANCED_FCAPS`. Session caps are always SDK-only.
- **No Mongo reads for cap counters.** Counters ride in every SDK request (`TARGET_SHOWN_LIST`, `INAPP_MAX_PER_USER_PER_DAY`). Server writes go to `TargetingCountManager` for **stats aggregation only** — not for per-user cap decisions.
- **Feature-flag routing lives in `InAppCombinedQuotaCheck`.** Everything upstream just injects `@Named(COMBINED_QUOTA_CHECK)` and asks — the flag is invisible to the strategies.
- **Quota check fires only when actual content is about to ship.** Metadata-only strategies deliberately skip it so the SDK still learns about the campaign; the SDK re-checks the caps at render time.
- **Both branches fail-closed.** Any error or unresolvable account → treat as "capped" and drop the payload rather than risk over-delivery.
- **Exclusion works differently in each branch.** Legacy uses `efc` on the target doc; advanced uses `AdvancedLimits.excludeGlobalFCaps` inside the target's advanced-limits sub-object. Both bypass only the *global* cap (in legacy, `efc` also skips per-target caps by short-circuiting the entire method).
