# Dashboard Backend — Combined PR Plan

**Scope:** enable ND fcaps end-to-end on the dashboard-backend side. FE is assumed to follow the wire contract. LC + SDK are separate later PRs.

**Result of this PR at runtime:** zero effect. Nothing consumes the new fields until LC V2 lands.

---

## 1. Files changed

### Core DAO / model (6 changes across 4 files)

| # | File | Change |
|---|---|---|
| 1 | `core/dao/src/com/wizrocket/db/dao/AccountsDao.java` | Add constants `NATIVE_DISPLAY_MAX_PER_SESSION = "ndsm"` and `NATIVE_DISPLAY_MAX_PER_USER_PER_DAY = "ndmp"`. |
| 2 | `core/dao/src/com/wizrocket/db/dao/AccountsDao.java` | Extend `loadAccount` — read `ndsm` and `ndmp` from the notification bucket (mirror the existing `wndmp` / `wndsm` read block). |
| 3 | `core/dao/src/com/wizrocket/db/dao/AccountsDao.java` | Extend `updateWebInappCapForType` switch — add cases for the two new constants (mirror the WebND cases). |
| 4 | `core/dao/src/com/wizrocket/db/dto/Account.java` | Add fields `nativeDisplayMaxPerSession = 1` and `nativeDisplayMaxPerUserPerDay = 10`. Lombok generates getters. |
| 5 | `core/dao/src/com/wizrocket/db/dto/TargetingInfo.java` (`.java:2132-2159`) | `generateWhenLimitOnInAppCampaign` — add `Mode.NativeDisplay` to the mode allowlist. Preserve the `!isJourneyTarget()` guard so journey ND nodes stay legacy-only. |
| 6 | `core/dao/src/com/wizrocket/db/dao/InAppWebTargetingDao.java` (`.java:60-99`) | Projection: `fields.put("isNdFcapEnabled", 1)` so the field survives the runtime target load into `TargetingInfo.tgt`. |

### Dashboard service / actions (4 changes across 4 files)

| # | File | Change |
|---|---|---|
| 7 | `dashboard/dashboard/src/com/wizrocket/dashboard/spring/services/fcap/FrequencyManagementServiceImpl.java` | Extend `getOnlineChannelConfig(Mode)` switch — add `NativeDisplay` case following the `WebNativeDisplay` shape (session / daily keys used directly as field names on both `gfc_setting` and `accounts.n` update paths). |
| 8 | `dashboard/dashboard/src/com/wizrocket/dashboard/action/target/TargetingNativeDisplayAction.java` (`save`, `.java:124-231`) | (a) Write `isNdFcapEnabled` from request: `t.put("isNdFcapEnabled", old.getBoolean("isNdFcapEnabled", false))`. (b) Invoke content adapters guarded on `!isJourneyTarget()`: `if (inAppContentAdapters != null && !isJourneyTarget()) inAppContentAdapters.forEach(a -> a.onSave(getAccountId(), old, t))`. |
| 9 | `dashboard/dashboard/src/com/wizrocket/dashboard/action/apis/targets/NativeDisplayCreateAction.java` | Same two additions as #8 (API-based create path). |
| 10 | `dashboard/dashboard/src/com/wizrocket/dashboard/action/target/TargetingBaseAction.java` (`fillModeSpecificFields`, `.java:5086-5090`) | Extend the `onLoad` invocation from `mode == Mode.InApp` to `(mode == Mode.InApp || mode == Mode.NativeDisplay) && inAppContentAdapters != null && !isJourneyTarget()`. |

### Journey ND nodes — covered by the same changes (no separate work)

Journey ND nodes are persisted via a proxy-builder that extends `TargetingNativeDisplayAction`, so `TargetingNativeDisplayAction.save()` runs for both standalone ND campaigns AND ND journey nodes. Change #8 covers both cases correctly because `isJourneyTarget()` disambiguates them at runtime.

**Call chain (verified):**

```
JourneySaveAction.fillOutputForMessageNode                       (.java:3862)
  → JourneyTargetHelper.buildNew(...)                            (.java:3880 / :3890)
    → case NativeDisplay: new NativeDisplayTargetViaJourneyBuilder(rawData)  (.java:407-408)
    → builder.build(request)                                     (.java:430)
      → TargetViaJourneyBuilder.build (interface default)        (TargetViaJourneyBuilder.java:43)
        → getAction().save(...)                                  (.java:46)
          → TargetingNativeDisplayAction.save                    ← change #8 executes here
      → returns targetForDb (captured via overridden saveOrUpdate; no Mongo write yet)
  → child.getOutput().putAll(...)                                (.java:3896)
  → later, publish() phase: TargetingDao.instance.insert(newOutput)  (.java:3419)
```

**Why it works:**

- `NativeDisplayTargetViaJourneyBuilder extends TargetingNativeDisplayAction implements TargetViaJourneyBuilder` — inherits `save()` unchanged.
- `isJourneyTarget()` at `TargetingBaseAction.java:1927` returns `this instanceof TargetViaJourneyBuilder` → `true` inside the proxy.
- The proxy overrides `saveOrUpdate` to capture the DBObject instead of inserting to Mongo; `JourneySaveAction` inserts it later during `publish()`.

**Consequences for the plan:**

| Change | Journey ND node behavior |
|---|---|
| #8a — unconditional `t.put("isNdFcapEnabled", ...)` | Runs. Journey ND target doc gets `isNdFcapEnabled` written. |
| #8b — content-adapter invocation guarded by `!isJourneyTarget()` | Skipped. No `frequencyLimits` / `occurrenceLimits` on journey nodes. Matches design intent. |
| existing `appendFrequencyCapDetails` at `TargetingNativeDisplayAction.java:146` | Runs unchanged. Legacy `efc` / `tlc` / `tdc` / `mdc` written for journey nodes as today. |
| #5 — `TargetingInfo.generateWhenLimitOnInAppCampaign` extended for `NativeDisplay` | Journey guard preserved. Journey ND nodes still get no `AdvancedLimits` at load. |

**No `JourneySaveAction` changes needed** — the file has zero ND-specific save code (only `NodeLink.NATIVE_DISPLAY` for graph-link typing) because all ND-specific save logic is centralized in `TargetingNativeDisplayAction` and reached through the proxy.

---

## 2. Files explicitly not touched

### Paytm / pull-API path (must remain functional)

- `PullNotificationServlet` — pull-API delivery.
- `NativeDisplayFcapPayloadUtils` — FcapV2 payload normalizer.
- `fillFcapSettings` in `TargetingNativeDisplayAction` — writes the `fcapSettings` FcapV2 blob for Paytm.
- `FcapV2Processor` / `FcapV2CampaignConfig` — ES-side FcapV2 evaluator.
- `ProfileHandler.applyOnlineAPIFrequencyCap` — ES-side pull-API check.

### Other shared / channel code (unchanged)

- `FrequencyManagementController` — endpoint URL and dispatch preserved.
- `OnlineChannelLimitRequest` DTO — payload shape preserved.
- `AccountMetaDao.updateFrequencyCap` — dual-write logic unchanged.
- `AdvancedFeaturesAdapterImpl` — already mode-agnostic; only the invocation is extended (via #8, #9, #10). No file change to the adapter itself.
- `appendFrequencyCapDetails` in `TargetingBaseAction` — already writes `efc` / `tlc` / `tdc` / `mdc` for ND at `.java:146`, unchanged.
- `TargetingInAppAction`, `TargetingWebInboxAction`, `TargetingWebNativeDisplayAction` — untouched.

---

## 3. Test coverage

### Global fcaps (changes #1–#4, #7)

- `AccountsDao.loadAccount` — new fields read correctly; defaults applied when absent from Mongo.
- `AccountsDao.updateWebInappCapForType` — new constants routed to `accounts.n.ndsm` / `accounts.n.ndmp`.
- `FrequencyManagementServiceImpl.saveOnlineChannelLimit(channel=12, sessionLimit=X, messageLimit=Y)` — dual-writes to both `accounts_meta.gfc_setting.12` AND `accounts.n.ndsm` / `accounts.n.ndmp`.
- Integration:
  - `POST /nx/v2/frequency-management/online-channel {"channel":12, "sessionLimit":X, "messageLimit":Y}` → verify Mongo state.
  - `GET /nx/v2/frequency-management/settings` → returns ND channel with defaults (1 / 10) when unset, saved values when set.

### Campaign save flow (changes #5, #6, #8, #9, #10)

- Save standalone ND campaign with `isNdFcapEnabled=true` + `frequencyLimits` + `occurrenceLimits` in request → target doc has `isNdFcapEnabled=true`, `AdvancedLimits` populated, legacy mirrors written by `AdvancedFeaturesAdapterImpl.onSave`.
- Save ND journey node with `isNdFcapEnabled=true` → content adapter NOT invoked; only legacy fields (`efc` / `tlc` / `tdc` / `mdc`) written via `appendFrequencyCapDetails`; `AdvancedLimits` absent.
- Save without `isNdFcapEnabled` in request → target doc has `isNdFcapEnabled=false`.
- `TargetingInfo.generateWhenLimitOnInAppCampaign(Mode.NativeDisplay, isJourneyTarget=false)` → returns `AdvancedLimits`.
- `TargetingInfo.generateWhenLimitOnInAppCampaign(Mode.NativeDisplay, isJourneyTarget=true)` → returns null.
- Projection: Mongo doc with `isNdFcapEnabled` field survives target load into `TargetingInfo.tgt`.
- Integration: dashboard save → verify Mongo state → dashboard load edit form → verify all fcap fields present in response.

### Paytm regression check

- Save an ND campaign via `TargetingNativeDisplayAction.save` with `fcapSettings` in the request (FcapV2 blob) → `fillFcapSettings` still writes it correctly. New fields do not overwrite or conflict.
- Existing pull-API integration test suite passes unchanged.

---

## 4. Runtime effect on merge

Zero. No consumer reads the new fields yet. LC continues on its current path. Safe to merge behind trunk without any flag rollout. LC V2 in a later PR will start consuming `isNdFcapEnabled`, `AdvancedLimits` on ND targets, and `ndmp` / `ndsm` on the account doc.

## 5. Rollout after merge

The LD flag `rollout-native-display-fcaps` is not evaluated by this PR. It stays off in LaunchDarkly. FE turns it on per-account only after the LC + SDK work is also live.

---

## 6. Locked design decisions

1. **Same LD flag** — `rollout-native-display-fcaps` gates both dashboard surfaces (campaign form + account-settings channel selector) and the campaign-doc `isNdFcapEnabled` write. One flag for the whole feature.
2. **Load defaults** — `GET /nx/v2/frequency-management/settings` returns ND defaults (`ndsm=1`, `ndmp=10`) when unset. Mirrors in-app behavior.
3. **Non-journey guard** — `TargetingBaseAction.isJourneyTarget()` (defined `.java:1927`) is already used in `TargetingNativeDisplayAction.java:142`. Works in the ND action's context.
4. **Validation** — reuse the in-app validator chain for `frequencyLimits` / `occurrenceLimits` bounds. No new validators for ND.
5. **Legacy mirror on save** — keep `AdvancedFeaturesAdapterImpl.onSave` behavior. Writes `efc` / `tlc` / `tdc` / `mdc` mirrors when `ADVANCED_FCAPS` is enabled. Ensures target doc has both advanced + legacy shapes for basic-SDK compatibility.

## 7. Explicit non-goals

- No Paytm / pull-API flow changes. All five Paytm touchpoints unmodified.
- No Mongo migration. New fields absent on existing docs get Java defaults (Part 1 additions) or `false` (Part 2 additions via `getBoolean` fallback).
- No LC changes. V2 eval manager + provider is a separate later PR.
- No changes to InApp / WebND / WebInbox / Web flows. Extensions are purely additive: new mode case, new account fields, new target-doc field.
