# LC Backend — Combined PR Plan

**Scope:** enable ND fcap evaluation end-to-end on the LC side. Adds V2 alongside V1 via provider dispatch, wires the cap gate, parses new SDK-supplied request-state, and emits new response keys. Depends on the dashboard PR having landed (needs `ndsm` / `ndmp` on `Account`, `isNdFcapEnabled` and `AdvancedLimits` on the ND target doc).

**Result of this PR at runtime:** zero effect. LD flag `rollout-native-display-fcaps` stays OFF in LaunchDarkly; the provider returns V1 for every account until flipped. Safe to merge behind trunk.

**Result after LD flag flip (per account):** V2 becomes authoritative on the event path (`MobileAppServlet`). `ContentServlet` (content-fetch hydration) and `PullNotificationServlet` (Paytm) continue on V1 unchanged.

---

## 1. Files changed

### 1.1 New V2 + provider + gates (5 new files)

| # | File | Purpose |
|---|---|---|
| N1 | `core/lc/src/java/com/wizrocket/lc/targeting/nativedisplay/NativeDisplayEvalManagerV2.java` | New. `extends NativeDisplayEvalManager` (V1). Overrides the servlet entrypoint (`evalAndFillResponse` or equivalent). Adds new outer loop with: dedup, segmentation, `adUnit_eval` gate (regular event only), cap gate, App-Launched B2 CG check + stub emit, delegation to inherited `super.fillResponse(...)` for the DELIVER path. All V1 helpers (`shouldFetchContent`, `getPerDayLimit`, content build) inherited unchanged. |
| N2 | `core/lc/src/java/com/wizrocket/lc/targeting/nativedisplay/NativeDisplayEvalManagerProvider.java` | New. `@Singleton`. Mirrors `InAppManagerProvider` structurally. Constructor-injected V1 + V2 + `NativeDisplaySupportChecker` + `NativeDisplayFeatureFlags`. One method: `getEvalManager(int sdkVersion, ProfileReach.Device device, int accountId)` returning `NativeDisplayEvalManager` (V2 is-a V1 via extension → return type stays clean). |
| N3 | `core/lc/src/java/com/wizrocket/lc/targeting/nativedisplay/NativeDisplayCombinedQuotaCheck.java` | New. `@Singleton`. Single entrypoint: `boolean targetQuotaReached(TargetingInfo ti, ProcessParams params)`. Routes on `ti.getAdvancedLimits().map(AdvancedLimits::hasAdvancedRules).orElse(false)` → `advancedCapReached` (delegates to `AdvancedRulesEvaluator` + global-ceiling check) OR `legacyCapReached` (`efc` / `tlc.length` / `tdc` / `mdc` / `ndsm` / `ndmp`). Wraps everything in `try/catch(Throwable)` → returns `true` (fail-closed at target level). Emits `nd_v2_cap_gate_exception` metric on catch. |
| N4 | `core/lc/src/java/com/wizrocket/lc/targeting/nativedisplay/NativeDisplaySupportChecker.java` | New. `@Singleton`. Method: `advancedNdSupported(sdkVersion, device)` — checks SDK version thresholds (Android `>= <PLACEHOLDER_ANDROID_VER>`, iOS `>= <PLACEHOLDER_IOS_VER>`, with `OLD_SDK_BIG_VERSION` carveout) AND device is mobile-phone (`Device.iOS || Device.Android`, not tablet variants). Placeholders resolved at implementation time from SDK team. |
| N5 | `core/lc/src/java/com/wizrocket/lc/targeting/nativedisplay/NativeDisplayFeatureFlags.java` | New. `@Singleton`. Thin wrapper around `LaunchDarklyClient` (or equivalent) exposing `isEnabled(NativeDisplayFeature.ROLLOUT_ND_FCAPS, accountId)`. LD flag key: `rollout-native-display-fcaps`. |

### 1.2 Container / ProcessParams (2 files)

| # | File | Change |
|---|---|---|
| N6 | `core/lc/src/java/com/wizrocket/lc/request_containers/MobileAppRequestContainer.java` | (a) Add three `@Getter` fields: `nativeDisplayTargetShownList` (BasicDBList), `nativeDisplayImpMap` (BasicDBObject), `nativeDisplayTargetsShownToday` (int, defaults `-1`). (b) In `case REQ_META` (near `.java:670-675`), parse the three fields from `eachObject` — byte-parallel to the existing in-app `tlc` / `INAPP_MAX_PER_USER_PER_DAY` parse lines. (c) Add `fillNdMeta` method mirroring `fillInAppsMeta` at `.java:1573-1578` — populates `adUnit_meta` response key when the request qualifies for ND meta delivery (App Launched or `wzrk_fetch t=WZRK_FETCH_ND_META`). Existing in-app parses byte-untouched. |
| N7 | `core/lc/src/java/com/wizrocket/lc/data/targeting/ProcessParams.java` (or wherever the class lives — same package as in-app's `ProcessParams`) | Extend with three ND fields: `nativeDisplayTargetsShownList`, `nativeDisplayImpMap`, `nativeDisplayTargetsShownToday`. Populated at `MobileAppServlet` request entry from `MobileAppRequestContainer`. Read only by `NativeDisplayCombinedQuotaCheck` and `AdvancedRulesEvaluator` for the ND path. |

### 1.3 DAO constants (1 file)

| # | File | Change |
|---|---|---|
| N8 | `core/dao/src/com/wizrocket/db/dao/AccountsDao.java` | Add two constants: `NATIVE_DISPLAY_TARGET_SHOWN_LIST = "ndtlc"` and `NATIVE_DISPLAY_IMP_MAP = "nd_imp"`. Note: `NATIVE_DISPLAY_MAX_PER_USER_PER_DAY = "ndmp"` and `NATIVE_DISPLAY_MAX_PER_SESSION = "ndsm"` are already added by the dashboard PR (items #1–#4 in `fcaps_dashboard.md`). |

### 1.4 Meta bootstrap plumbing (2 files)

| # | File | Change |
|---|---|---|
| N9 | New — `core/lc/src/java/com/wizrocket/lc/targeting/nativedisplay/NdEventsHelper.java` (or extend an existing ND-side helper) | Add `WZRK_FETCH_ND_META = 100` (placeholder value — reconcile with SDK team on final integer). Method: `metaDeliveryRequested(String event, BasicDBObject eventProperties)` — returns `true` if `APP_LAUNCHED.equals(event)` OR (`WZRK_FETCH.equals(event) && props.getInt("t", -1) == WZRK_FETCH_ND_META`). Byte-parallel to in-app's `EventsHelper.metaDeliveryRequested`. |
| N10 | New — `core/lc/src/java/com/wizrocket/lc/targeting/nativedisplay/NdDeliveryTargetsMeta.java` (or reuse in-app's `DeliveryTargetsMetaSS` shape) | Filters active ND targets to those with `AdvancedLimits::hasAdvancedRules == true` AND `!isJourneyTarget()` (journey ND stays legacy). No `!isClientSide` filter — mobile ND has no client-side mode. Returns list feeding the `adUnit_meta` response emission. |

### 1.5 Dead-target GC (2 files — NEW mechanism for mobile ND)

Mobile ND currently has **no** stale-target GC (verified: `DeadTargetsEvalManager` at `.java:26-50` has cases only for `Web`, `InApp`, `WebInbox`, `WebNativeDisplay`; `MobileAppServlet.java:137-141` only calls `attachDeadTargets` with `Mode.InApp`). This PR adds it.

| # | File | Change |
|---|---|---|
| N11 | `core/lc/src/java/com/wizrocket/lc/targeting/DeadTargetsEvalManager.java` | Add constant `NATIVE_DISPLAY_MOBILE_DEAD_TARGETS = "adUnit_stale"` (distinct from the existing `NATIVE_DISPLAY_DEAD_TARGETS = "native_display_stale"` which is Web-only). Add `case NativeDisplay:` in the switch at `.java:28-50` calling `attachDeadTargets(response, targetsShownList, NATIVE_DISPLAY_MOBILE_DEAD_TARGETS, activeTargetingIdsProvider.getCurrentNativeDisplayTargets(accountID))`. Web ND case untouched. |
| N12 | `core/lc/src/java/com/wizrocket/lc/data/targeting/ActiveTargetingIdsProvider.java` | Add method `getCurrentNativeDisplayTargets(int accountId)` — mirrors existing `getCurrentWebNativeDisplayTargets` / `getCurrentInAppTargets`. Reads the same TargetingDto path, filtered on `Mode.NativeDisplay`. |

### 1.6 Per-day limit dispatch (1 file)

| # | File | Change |
|---|---|---|
| N13 | `core/lc/src/java/com/wizrocket/lc/targeting/TargetEvalManager.java` (`.java:374-385` `getPerDayLimit`) | Extend switch — add `case NativeDisplay:` returning `account.getNativeDisplayMaxPerUserPerDay()`. Existing Web / WebInbox / WebNativeDisplay cases untouched; `default` (which falls to `inappMaxPerUserPerDay`) unchanged. |

### 1.7 Servlet wiring (1 file)

| # | File | Change |
|---|---|---|
| N14 | `core/lc/src/java/com/wizrocket/lc/upload/MobileAppServlet.java` | (a) Swap `@Inject NativeDisplayEvalManager` field type → `@Inject NativeDisplayEvalManagerProvider provider`. (b) At the ND dispatch call site, wrap with `provider.getEvalManager(sdkVersion, device, accountId).evalAndFillResponse(container, params)`. (c) Add ND dead-target attach call after the existing in-app one at `.java:137-141`: `BasicDBList ndShown = container.getNativeDisplayTargetShownList(); if (ndShown != null) deadTargetsEvalManager.attachDeadTargets(accountId, responseObj, ndShown, ProfileReach.Mode.NativeDisplay);`. |

### 1.8 Guice module (1 file)

| # | File | Change |
|---|---|---|
| N15 | LC's existing ND-side Guice module (e.g. `core/lc/src/java/com/wizrocket/lc/guice/NativeDisplayModule.java` or the umbrella LC module — whichever binds V1 today) | Add bindings: `bind(NativeDisplayEvalManagerV2.class).in(Singleton.class);` `bind(NativeDisplayEvalManagerProvider.class).in(Singleton.class);` `bind(NativeDisplayCombinedQuotaCheck.class).in(Singleton.class);` `bind(NativeDisplaySupportChecker.class).in(Singleton.class);` `bind(NativeDisplayFeatureFlags.class).in(Singleton.class);`. Existing V1 binding (`NativeDisplayEvalManager`) untouched. `AdvancedRulesEvaluator` reused from the in-app module (already bound as `@Singleton` there — no fresh binding). Constructor injection throughout; no `@Provides`; no `Modules.override` outside tests (per CLAUDE.md Guice rules). |

---

## 2. V1 zero-diff guarantee

`NativeDisplayEvalManager.java` (V1, 492 lines) is byte-untouched. V2 extends V1 and inherits all helpers verbatim — `fillResponse`, `shouldFetchContent`, content-key resolution, `TargetingCountManager.markAsSent`, CG check on the DELIVER path. Any V2-specific behavior lives in V2's overridden entrypoint.

Verification checklist at PR time:
- `git diff` on `core/lc/src/java/com/wizrocket/lc/targeting/NativeDisplayEvalManager.java` must be empty.
- V2 must not declare any of V1's methods with the same signature except the one servlet entrypoint being overridden.

---

## 3. Files explicitly not touched

### 3.1 Paytm / pull-API path (must remain functional — hard invariant)

- `core/lc/src/java/com/wizrocket/lc/upload/PullNotificationServlet.java` — pull-API delivery. Injects `NativeDisplayEvalManager` (V1) directly. Never invokes the provider.
- `core/lc/src/java/com/wizrocket/lc/upload/ContentServlet.java` — content-fetch hydration. Injects V1 directly. Serves the pointers V2 queued via V1's inherited `fillResponse` — pointers are V1-compatible by construction (V2 never touches the content-fetch fork).
- `NativeDisplayFcapPayloadUtils` — FcapV2 payload normalizer.
- `FcapV2Processor` / `FcapV2CampaignConfig` — ES-side FcapV2 evaluator.
- `ProfileHandler.applyOnlineAPIFrequencyCap` — ES-side pull-API cap check.

### 3.2 In-app / Web / Web ND / Inbox code

- `core/lc/src/java/com/wizrocket/lc/targeting/inapps/**` — untouched. `InAppManagerProvider`, `InAppProcessorAdapter`, `InAppManager` all byte-identical.
- `AdvancedRulesEvaluator` — reused via DI from the in-app module (Issue #9 D5.A lock). Audit at code-review time for any `Mode.InApp`-specific branch; if clean, inject as-is. If any branch exists, we do a small extraction — do NOT fork the evaluator.
- `core/lc/src/java/com/wizrocket/lc/upload/WebAppServlet.java` — untouched. Web SDK ND path stays on the existing `WebNativeDisplay` mode.
- `DeadTargetsEvalManager.java` — Web / InApp / WebInbox / WebNativeDisplay cases at `.java:29-44` all preserved; only a new `case NativeDisplay:` added (see #N11).
- `TargetEvalManager.evalAndFillResponse` outer `try/catch` at `.java:217-244` — unchanged. Continues to swallow any Throwable at the dispatch level. V2's per-target catches (Issue #10 E3 lock) are additive — a second layer, not a replacement.

### 3.3 In-app SS delivery strategies

- `DeliveryStrategySS`, `DeliveryStrategyAppLaunchedSS`, `DeliveryTargetsMetaSS`, `PayloadProviderSS` — all byte-untouched. ND V2 has structural parallels (§#N10 `NdDeliveryTargetsMeta` mirrors `DeliveryTargetsMetaSS`) but they are separate files with no shared parent.

---

## 4. Locked design decisions

Every decision below was locked issue-by-issue during design (Issues #1–#13, closed 2026-08-11). Implementation must honor them.

### 4.1 Provider dispatch (Issue #12)
- V2 extends V1. No interface extraction. V1 class byte-untouched.
- Provider gate: SDK version thresholds + mobile-phone device + LD flag `rollout-native-display-fcaps`. All three consolidated in `NativeDisplaySupportChecker` + `NativeDisplayFeatureFlags`.
- Only `MobileAppServlet` invokes the provider. `ContentServlet` and `PullNotificationServlet` on V1 direct.

### 4.2 Cap gate placement (Issue #8)
- Cap gate runs at V2's outer-loop skip predicate, **before** V1's `fillResponse`. Cap-capped campaigns never reach the content-fetch fork, never queue pointers, never call `TargetingCountManager.markAsSent`.
- App-Launched CG-suppression (B2): V2 does its own `ControlGroupUtils.getControlGroupId` check before delegating. On SUPPRESS → emit stub (`suppressed:true` + `wzrk_cgId`) into `adUnit_notifs_applaunched`, set `isCampaignPayload=true`, continue. On DELIVER → call `super.fillResponse` (which internally re-does CG — deterministic, cheap).
- Regular-event dispatch (§6.9): V1's return-false-on-CG-suppress + emitted supplementary CG event used as-is. No V2 CG override needed.

### 4.3 Cap gate class shape (Issue #9)
- Single entrypoint `boolean targetQuotaReached(TargetingInfo, ProcessParams)`. Internal routing on `hasAdvancedRules`.
- Fail-closed at target level: inner `try/catch(Throwable)` returns `true`.
- Outer `TargetEvalManager` catch stays as second safety layer.
- All per-user counter state via `ProcessParams`. `AccountsProvider` for account caps.
- Reuse `AdvancedRulesEvaluator` from in-app module verbatim (audit required).

### 4.4 Emission pattern (Issue #10)
- Emit-as-you-go. No local buffer / atomic swap.
- Per-target atomicity: build DBObject in local var, append once.
- Per-target `try/catch(Throwable) → continue` around V2's own CG check + stub emit + `fillResponse` delegation. One bad target on App-Launched does not tank the dispatch.
- Two exception metrics: `nd_v2_cap_gate_exception`, `nd_v2_target_dispatch_exception`.

### 4.5 Wire contract (Issue #11 + #13)
- Request-direction (parsed in `case REQ_META`): `ndtlc` (BasicDBList), `nd_imp` (BasicDBObject), `ndmp` (int, `-1` sentinel = unknown → skip daily ceiling), `adUnit_eval` (list of advanced-rule campaign IDs SDK's `canShow` said OK for THIS event — same request path, parsed same way as in-app's `inapps_eval`), `adUnit_suppressed` (H3.C shape: `[{id, reason: "cg"|"canshow"}, ...]`; fallback to bare id list if SDK pushes back).
- Response-direction: `adUnit_notifs`, `adUnit_notifs_applaunched`, `adUnit_notifs_ss`, `adUnit_stale` (NEW for mobile ND — see #N11), `adUnit_meta` (only on App Launched or `wzrk_fetch t=100`; full payload, empty active-set → `[]` explicit).
- BE tolerates oversized `nd_imp` — logs `CRITICAL: oversized nd_imp` metric, does not reject. Advanced-rule evaluator's window filter naturally ignores stale entries.

### 4.6 Merge order (Issue #12)
1. **Dashboard PR** (`fcaps_dashboard.md`) → adds `ndsm` / `ndmp` on `Account`, `isNdFcapEnabled` and `AdvancedLimits` on ND target doc.
2. **LC PR** (this one) → adds V2, provider, cap check, container parses, dead-target GC, meta bootstrap. LD flag OFF at merge time = zero runtime effect.
3. **SDK PR** → sends `ndtlc` / `nd_imp` / `ndmp` / `adUnit_eval` / `adUnit_suppressed`; honors `adUnit_notifs_ss` / `adUnit_stale` / `adUnit_meta`; `canShow` re-check before render.
4. **LD flag flip** per-account.

---

## 5. Test coverage

### 5.1 Unit tests (no Guice, no MongoBasedTest — per CLAUDE.md Testing rules)

**`NativeDisplayCombinedQuotaCheck`** (constructed manually with `@Mock`ed collaborators):
- Advanced-rule target + `excludeGlobalFCaps=false` + within all `frequencyLimits` + within account ceilings → returns `false` (not capped).
- Advanced-rule target + one `frequencyLimits` breached → returns `true`.
- Advanced-rule target + `excludeGlobalFCaps=true` + account ceilings breached → returns `false` (excluded — global ignored).
- Legacy target + `efc=true` + account ceilings breached → returns `false` (excluded).
- Legacy target + `efc=false` + `ndmp` breached → returns `true`.
- Legacy target + `tlc.length >= tdc` → returns `true`.
- Legacy target + missing `ndmp` (sentinel `-1`) → skips daily ceiling, other checks still run.
- Advanced-rule evaluator throws → returns `true` (fail-closed); `nd_v2_cap_gate_exception` metric incremented.

**`NativeDisplayEvalManagerProvider`**:
- V2-eligible SDK + LD flag ON + mobile-phone device → returns V2 instance.
- V2-eligible SDK + LD flag OFF → returns V1.
- Below SDK version threshold → returns V1 regardless of flag.
- Tablet device → returns V1 regardless of flag.
- `OLD_SDK_BIG_VERSION` carveout case → correctly resolved (specific test per SDK team's spec).

**`NativeDisplayEvalManagerV2`** (with mocked V1 super calls where feasible; may need integration test if super calls are hard to mock):
- Cap-passing target on regular event → inherited `fillResponse` called → `adUnit_notifs` populated.
- Cap-capped target on regular event → `fillResponse` never called; loop continues.
- App-Launched CG-suppressed campaign → stub emitted into `adUnit_notifs_applaunched` with `suppressed:true` + `wzrk_cgId`; `isCampaignPayload=true`.
- App-Launched CG-suppressed journey ND → supplementary CG event only (no stub, no lock-in).
- V2's own CG check throws → target skipped; `nd_v2_target_dispatch_exception` metric incremented; loop continues.
- Advanced-rule campaign NOT in `adUnit_eval` list on regular event → skipped without cap check.
- Content-fetch-eligible campaign that passes cap → inherited `fillResponse` queues pointer into `contentFetchNotifications`.
- Content-fetch-eligible campaign that fails cap → pointer NOT queued (proves the cap-before-content-fetch lock).

**`MobileAppRequestContainer.case REQ_META`** parse:
- Full payload with `ndtlc` / `nd_imp` / `ndmp` present → all three fields populated correctly.
- Missing `ndtlc` / `nd_imp` / `ndmp` → defaults `null` / `null` / `-1`.
- Oversized `nd_imp` (> 500 targets or > 100 entries in any target) → parsed anyway; `CRITICAL` metric emitted.
- In-app `tlc` / `imp` / `INAPP_MAX_PER_USER_PER_DAY` parses unaffected (regression check).

### 5.2 Integration tests (Guice with `Modules.override` for external deps only)

- **End-to-end V2 dispatch, regular event, one advanced-rule campaign eligible** → `adUnit_notifs` has one entry; `TargetingCountManager` incremented; `adUnit_stale` present if any old target IDs in `ndtlc`.
- **End-to-end V2 dispatch, App Launched, three advanced-rule campaigns** → all three either delivered inline or via content-fetch, per-target atomic; `adUnit_meta` in response.
- **End-to-end V2 dispatch, App Launched, campaign + journey mix, one campaign lock-in** → subsequent journey ND targets skipped after first campaign delivers (respects `isCampaignPayload` gate).
- **Provider dispatch, old SDK on new LC** → V1 path used; V2 metrics untouched.
- **Content-fetch fork correctness** → cap-capped campaign never appears in `contentFetchNotifications`; cap-passing content-fetch campaign appears.
- **Paytm regression** → existing `PullNotificationServlet` integration tests pass unchanged.

### 5.3 Regression checks

- In-app dispatch unaffected by V2 additions (in-app tests all green).
- Web ND dispatch unaffected (`WebAppServlet` tests all green).
- V1 direct dispatch via `ContentServlet` / `PullNotificationServlet` unchanged.

---

## 6. Runtime effect on merge

**Zero.** LD flag `rollout-native-display-fcaps` is off in LaunchDarkly. Provider returns V1 for every account. V2 classes are constructed by Guice (lazy or eager depending on binding) but never dispatched to.

New `case REQ_META` parses read fields that are absent in current SDK payloads — the null / `-1` defaults apply. No downstream code path consumes these defaults on V1.

The new `case NativeDisplay:` in `DeadTargetsEvalManager` is not reached because `MobileAppServlet`'s new `attachDeadTargets` call passes a `ndShown` value that current SDKs don't populate — the null-guard at the servlet skips the call. Safe.

## 7. Rollout after merge

1. LD flag stays off.
2. SDK PR ships with the new client-side capabilities (`ndtlc` / `nd_imp` / `ndmp` / `adUnit_eval` / `adUnit_suppressed` on the request; `canShow` re-check; `adUnit_meta` handling).
3. Once minimum SDK adoption reached, flip `rollout-native-display-fcaps` on for a canary account.
4. Monitor `nd_v2_cap_gate_exception` and `nd_v2_target_dispatch_exception` metrics. Both should stay flat at 0 under normal load.
5. Expand rollout progressively.

---

## 8. Explicit non-goals

- No Paytm / pull-API flow changes. Five Paytm touchpoints unmodified.
- No `NativeDisplayEvalManager.java` (V1) diff. Any code inspection at PR time must show V1 byte-identical.
- No in-app changes. `AdvancedRulesEvaluator` reused via DI only, not forked.
- No Web ND changes. `WebNativeDisplay` mode paths (including `native_display_stale` for Web ND) untouched.
- No SDK-side changes in this PR. SDK ships separately.
- No LD flag evaluation removed / bypassed for testing without an explicit test-only override module.
- No Mongo migration. All new fields read via `getBoolean(...)` / `getInt(..., -1)` / `.get(...)` with null-safe defaults.
- No dashboard changes. That work is scoped to `fcaps_dashboard.md`.
