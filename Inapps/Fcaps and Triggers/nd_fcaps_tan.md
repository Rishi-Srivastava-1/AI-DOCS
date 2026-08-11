# TAN — Frequency Caps in ND (Native Display) Campaigns

Scope: introduces per-campaign frequency caps on the **mobile Native Display** event-triggered path for the first time. Backend + SDK design. Modeled on in-app **SS mode** (see `ss_mode.md`).

Related index: [Fcaps in ND campaigns](https://wizrocket.atlassian.net/wiki/spaces/EN/pages/6972964894/Fcaps+in+ND+campaigns).

---

## 0. TL;DR

- New Mongo key **`isNdFcapEnabled`** on the target doc — LD-gated at **save-time only**, frozen at creation. `true` → all fcap checks run (server + SDK). `false`/absent → no fcap check, always deliver. Backward compat comes for free — no key on existing docs = uncapped, as today.
- Fcap schema on the doc is **identical to in-app** (`AdvancedLimits.fCaps`, `AdvancedLimits.triggers`, `excludeGlobalFCaps`, legacy `efc`/`tlc`/`tdc`/`mdc`) — no new schema, dashboard reuses in-app widgets.
- LC starts shipping fcap metadata (`whenTriggers`, `frequencyLimits`, `occurrenceLimits`, legacy limits) to the SDK for ND **for the first time in this project**. SDK owns per-campaign cap enforcement (mirrors SS).
- New request-meta field **`nd_eval`** — SDK votes advanced-rule ND campaigns that cleared local caps; LC gates content ship on membership. Simple campaigns remain purely server-side.
- **No inaction flow** for ND — drops 3 of the 6 in-app SS strategies (`AppLaunchedSSMeta`, `SSInaction`, `SSInactionContentFetch`) and all FStore machinery.
- **No CS mode** — ND runs SS-only.
- Coexists with the existing **Paytm / pull-API** FcapV2 path (`PullNotificationServlet` → `FcapV2Processor`) — different code path, different schema, no overlap.

---

## 1. Scope

### 1.1 In scope
- Mobile Native Display: `ProfileReach.Mode.NativeDisplay`.
- Event-triggered LC path: `NativeDisplayEvalManager.evalAndFillResponse`, invoked from `MobileAppServlet`.
- Both **advanced** fcaps (`frequencyLimits`, `occurrenceLimits`, `excludeGlobalFCaps`) and **legacy** fcaps (`efc`, `tlc`, `tdc`, `mdc`).

### 1.2 Out of scope
- **Web Native Display** (`Mode.WebNativeDisplay`) — different dispatcher (`WebNativeDisplayEvalManager`); its fcap path already exists via `TargetEvalManager.getPerDayLimit` + `getWebNativeDisplayMaxPerUserPerDay`.
- **Paytm / fetch-via-API pull path** (`PullNotificationServlet` → `ProfileHandler.applyOnlineAPIFrequencyCap`) — continues to use `FcapV2CampaignConfig`. Unchanged.
- **Inaction flow** — ND has no inaction campaigns; SS's inaction strategies + FStore are not implemented for ND.
- **CS mode** for ND — SS-only.
- **Cross-channel credit-based FcapV2** — orthogonal system; not this TAN.

---

## 2. Product Intent

Today, mobile ND event-triggered campaigns fire on every qualifying event with only priority-lock arbitration and control-group suppression — **no per-campaign, per-user, or global-daily caps run on this path** (verified: `NativeDisplayEvalManager` has zero `targetQuotaReached` / `Fcap` / `frequency` references). This TAN closes that gap by introducing, at feature-parity with in-app SS:

- Per-campaign session / day / week / month / ever caps (`frequencyLimits`).
- N-per-X occurrence rules (`occurrenceLimits` — `onEvery` / `onExactly`).
- Global daily cap with per-campaign `excludeGlobalFCaps` opt-out.
- Legacy `efc` / `tlc` / `tdc` / `mdc` for older-SDK / basic customers.

The dashboard's ND campaign form starts exposing the same fcap widget in-apps have; LC starts shipping fcap metadata + limits to the SDK; the SDK gains a local cap-eval loop and impression map.

---

## 3. Backward Compatibility

- **Existing running ND campaigns are grandfathered.** They don't have `isNdFcapEnabled` on the doc → every LC read defaults to `false` → no fcap check → deliver as today.
- **`isNdFcapEnabled` is frozen at creation.** LD flag is evaluated once during the save action; edits/duplicates carry the original value. Prevents surprise cap-enforcement on already-live customer campaigns.
- **Old SDKs** (pre-ND-fcap release) continue to receive the existing payload shape — no fcap metadata, no `nd_eval` protocol, no client-side caps. LC gates fcap-metadata emission on SDK version.
- **No Mongo migration required** at any phase.

---

## 4. Data Model (Mongo `targets`)

### 4.1 New field
| Field | Type | Values | Semantics |
|---|---|---|---|
| `isNdFcapEnabled` | boolean | `true` / `false` / absent | Master switch. `true` → run all fcap checks (server + SDK). `false` / absent → skip everything, always deliver. Frozen at creation. |

### 4.2 Existing fcap fields (mirrored from in-app schema)
| Legacy constant | Key | Semantics |
|---|---|---|
| `INAPP_TGT_EXCLUDE_FREQUENCY_CAP` | `efc` | `1` → exclude campaign from every cap (target + global). Short-circuit. |
| `INAPP_TGT_LIFETIME_COUNT` | `tlc` | Once-per-lifetime |
| `INAPP_TGT_DAILY_COUNT` | `tdc` | Once-per-day |
| `MAX_PER_CAMPAIGN` | `mdc` | Once-per-session (SDK-only) |

Advanced-fcaps sub-doc (gated on `InAppsFeature.ADVANCED_FCAPS` / `ADVANCED_DISPLAY_TRIGGERS`, same as in-app):
- `AdvancedLimits.fCaps` — shipped as `frequencyLimits: [{type, limit, frequency}]`
- `AdvancedLimits.triggers` — shipped as `occurrenceLimits: [{type: onEvery|onExactly, limit}]`
- `AdvancedLimits.excludeGlobalFCaps` — shipped as `excludeGlobalFCaps` (platform-dependent encoding: `1`/`-1` on Android, boolean on iOS, per `ContentUtil.java:196-201`)

Precedence rules identical to in-app: `efc == 1` short-circuits everything; `excludeGlobalFCaps == true` bypasses only global. `isNdFcapEnabled == false` overrides both (nothing runs at all).

---

## 5. Dashboard Flow

### 5.1 UI (frontend — Vue/TSX)
- Extend the ND campaign creation form to render the **same** fcap widget used by in-app campaigns (`frequencyLimits` / `occurrenceLimits` / `excludeGlobalFCaps` UI). Visibility gated on the LD flag.
- No new schema on the front end; reuse the in-app fcap component.

### 5.2 Save path (backend — dashboard)
- `TargetingNativeDisplayAction.save` / `NativeDisplayCreateAction`:
  - Read the LD flag `nd-fcaps-enabled` for the account.
  - Write `isNdFcapEnabled: true|false` on the new target doc (always write explicitly — never rely on absence).
  - Existing call to `appendFrequencyCapDetails(old, t)` (`TargetingBaseAction.java:7662-7672`) is unchanged — it already writes `efc`/`tlc`/`tdc`/`mdc`.
- Extend `AdvancedFeaturesAdapterImpl.onSave` (`core/inapps/service/.../adapters/AdvancedFeaturesAdapterImpl.java:44-113`) so it also processes `Mode.NativeDisplay`. Today it fires only for `Mode.InApp` — this is the single most-easy-to-miss extension, without it `frequencyLimits`/`occurrenceLimits` never land on the ND doc even if the UI sends them.
- **Edit / duplicate paths must not re-evaluate the LD flag** — persist `isNdFcapEnabled` as read from the existing doc.

### 5.3 Coexistence with FcapV2 (pull-API/Paytm path)
- `NativeDisplayFcapPayloadUtils` (`dashboard/.../NativeDisplayFcapPayloadUtils.java`) continues to write the `FcapV2CampaignConfig` blob for the pull-API path — untouched.
- The new event-triggered fcap fields (`isNdFcapEnabled`, `AdvancedLimits`, legacy fcap fields) coexist on the same target doc; the two schemas do not overlap.
- The two paths never cross at runtime: `PullNotificationServlet` → `ProfileHandler.applyOnlineAPIFrequencyCap` → `FcapV2Processor` vs `MobileAppServlet` → `NativeDisplayEvalManager` → new cap gate.

---

## 6. Runtime Flow

### 6.1 Data model load-time
- `TargetingInfo.generateWhenLimitOnInAppCampaign` (`core/dao/.../TargetingInfo.java:2132-2159`) currently builds `AdvancedLimits` only for `InApp && !isJourneyTarget()` and Web/WebNativeDisplay. **Extend to include `Mode.NativeDisplay`**, preserving the journey-target guard.
- Extend the field projection in `InAppWebTargetingDao.java:60-99` to include `isNdFcapEnabled`.

### 6.2 LC entry point — `NativeDisplayEvalManager` (SS-inspired composer)
Composer strategies for ND (three-piece, no inaction):

| ND strategy | Fires on | Purpose | Response key |
|---|---|---|---|
| `NDStrategyMeta` | App Launched (+ optional mid-session refresh) | Bulk metadata bootstrap for all `isNdFcapEnabled` ND campaigns (regardless of segmentation match — user may qualify later) | `adUnit_notifs_ss` |
| `NDStrategyAppLaunched` | App Launched | Content-in-advance for App-Launched-triggered ND campaigns that pass segmentation + CG + global daily | `adUnit_notifs_applaunched` |
| `NDStrategyEvent` | Any regular event | Per-event dispatch; gates advanced-rule campaigns on `nd_eval` membership | `adUnit_notifs` (existing) |
| `NDStrategySuppression` | On any event when SDK meta carries `nd_suppressed` | Convert SDK-reported suppressions into supplementary CG events | — |

The App Launched strategy exists for the same reason SS's `AppLaunchedSS` does: on first launch after campaign creation the SDK has no metadata to name in `nd_eval`, so LC must ship content up-front.

### 6.3 Cap gate — `NativeDisplayCombinedQuotaCheck` (new)
Analog of `InAppCombinedQuotaCheck` (`core/inapps/service/.../delivery/common/InAppCombinedQuotaCheck.java:24-33`):
```
if (!ti.get("isNdFcapEnabled", false)) return false;                // MASTER SWITCH — deliver
if (ADVANCED_FCAPS enabled && !ti.isJourneyTarget()) {
    return advancedGlobalOnlyCheck(params, ti);                     // global daily only
}
return legacyQuotaCheck(params, ti);                                // efc / tlc / tdc / global daily
```
- Journey ND nodes always route to legacy (matches in-app precedent — `TargetingInfo` no longer emits `AdvancedLimits` for journey targets).
- The check invocation slots in before `fillResponse` in `NativeDisplayEvalManager.evalAndFillResponse` — after segmentation eval, before content build.

### 6.4 Per-event SDK protocol (mirrors in-app SS `inapps_eval`)
- SDK ships in request meta:
  ```json
  { "type": "meta", "nd_eval": [70001, 70002], "nd_suppressed": [...] }
  ```
- `MobileAppRequestContainer` parses `nd_eval` into `Set<Integer> ndEvalIds`. `NativeDisplayEvalManager` receives it and:
  - Advanced-rule ND campaign → **only** delivered if `ndEvalIds.contains(ti.getID())`.
  - Simple ND campaign (no `AdvancedLimits`) → server evaluates fully (segmentation + legacy caps + CG), same as today.
  - Journey ND node → legacy path, unconditional server eval.

### 6.5 SDK responsibilities (all new for ND)
- Persist `isNdFcapEnabled` bundle from App Launched response.
- Local `{occurrenceId, timestamp}` impression map (last N ND impressions) — analog to in-app SDK's map.
- Local counters `ndlc = [[targetId, todayCount, lifetimeCount]]` (analog to `tlc` for in-app) shipped in every request.
- Local `ndp` (daily count) shipped every request; ceiling from account.
- Local `ndm` (session count) — SDK-side only.
- On event: evaluate cached metadata → match `whenTriggers` → check `frequencyLimits` + `occurrenceLimits` locally → pick winner by priority (desc, then earliest created) → push to `nd_eval`.
- On response: render, bump counters, fire `Notification Viewed`.
- Honor `native_display_stale` (already emitted by `DeadTargetsEvalManager.attachDeadTargets`; `.java:19-21`) to GC counter storage for stopped/deleted campaigns.

---

## 7. Cap Enforcement Matrix (server vs SDK)

| Cap | Server-enforced | SDK-enforced |
|---|---|---|
| `isNdFcapEnabled == false` short-circuit | ✓ (deliver always) | ✓ (no local eval) |
| `efc == 1` | ✓ (legacy path) | ✓ |
| `tlc` (lifetime) | ✓ (legacy) | ✓ |
| `tdc` (daily) | ✓ (legacy) | ✓ |
| `mdc` (session) | ✗ | ✓ |
| `frequencyLimits` | ✗ (SDK owns; server trusts `nd_eval` vote) | ✓ |
| `occurrenceLimits` | ✗ | ✓ |
| Global daily (`ndp` / account ceiling) | ✓ | ✓ (`canShow` re-check) |
| Global session (`ndm`) | ✗ | ✓ |

Rationale for the split is unchanged from the BE TAN's in-app note: transferring the last-100-impression map every request is impractical, so per-campaign advanced state lives on the device.

---

## 8. Response Payload Shape

### 8.1 App Launched
```json
{
  "adUnit_notifs_ss": [
    { "ti": 70001, "wzrk_id": "70001_20260810", "priority": 50,
      "whenTriggers": [ {"eventName": "Charged", "eventProperties": [...]} ],
      "frequencyLimits": [ {"type": "session", "limit": 1} ],
      "occurrenceLimits": [ {"type": "onEvery", "limit": 3} ],
      "excludeGlobalFCaps": false }
  ],
  "adUnit_notifs_applaunched": [
    { "ti": 70002, "wzrk_id": "70002_20260810", "wzrk_ttl": 1691758604,
      "priority": 42, "whenTriggers": [{"eventName": "App Launched"}],
      "frequencyLimits": [...], "title": {...}, "message": {...}, "media": {...} }
  ],
  "native_display_stale": [65001, 62030],
  "ndp": 10
}
```

### 8.2 Regular event with `nd_eval: [70001]` in request
```json
{
  "adUnit_notifs": [
    { "ti": 70001, "wzrk_id": "70001_20260810", ...content }
  ],
  "ndp": 10
}
```

---

## 9. Design Decisions

### 9.1 Freeze `isNdFcapEnabled` at creation
LD flag is evaluated once at save time and persisted; edits do not re-evaluate. Ops can flip the LD flag safely — no retroactive cap-enforcement on running campaigns. Grandfather guarantee.

### 9.2 No inaction flow
Drops `AppLaunchedSSMeta`, `SSInaction`, `SSInactionContentFetch`, all FStore machinery (`InactionFSClientUtils`, `Context.ONLINE_INACTION`, batch delete tasks), the exit-query re-check (`processTargetExitQuery`), and event-context replay. Cleaner design, fewer failure modes.

### 9.3 Journey ND nodes routed to legacy
Mirror in-app precedent (`InAppCombinedQuotaCheck.java:24-33` — `!ti.isJourneyTarget()` gates advanced). Journey targets never emit `AdvancedLimits` (guarded in `TargetingInfo.generateWhenLimitOnInAppCampaign`).

### 9.4 Coexistence with FcapV2 (pull-API)
Two independent fcap systems on the same doc, never crossing at runtime. `FcapV2CampaignConfig` continues to serve the Paytm pull-API path unchanged.

### 9.5 Fail-closed on cap-check exception
Mirror `TargetEvalManager.targetQuotaReached` (`.java:264-284`, `legacy_flow.md` §5.1): any exception returns `true` (capped) → target dropped. Prefer no delivery over double delivery.

### 9.6 `NDStrategyMeta` ships regardless of segmentation match
Metadata for all `isNdFcapEnabled` ND campaigns is shipped on App Launched irrespective of whether the user currently qualifies — user may qualify mid-session, and the SDK needs the catalog to evaluate against. Mirrors Key Decisions doc for in-app SS.

### 9.7 Content-in-advance for App-Launched-triggered ND campaigns only
`NDStrategyAppLaunched` ships full content for campaigns whose trigger is `App Launched`, because the SDK has no prior metadata to name them in `nd_eval` on first launch after creation. All other ND campaigns wait for the SDK to vote via `nd_eval` on subsequent events.

---

## 10. Feature Flags

| Flag | Layer | Role |
|---|---|---|
| `nd-fcaps-enabled` (new, LaunchDarkly) | Dashboard save | Whether new ND campaigns get `isNdFcapEnabled: true` at creation |
| `InAppsFeature.ADVANCED_FCAPS` (existing) | Save + LC | Determines whether `frequencyLimits`/`excludeGlobalFCaps` are emitted vs legacy fields |
| `InAppsFeature.ADVANCED_DISPLAY_TRIGGERS` (existing) | Save + LC | Determines whether `occurrenceLimits` are emitted |
| SDK-version gate | LC | Whether to emit fcap metadata at all — old SDKs still see current payload shape |

---

## 11. Rollout Plan

| Phase | What lands | Runtime effect |
|---|---|---|
| 1. Dashboard save | `isNdFcapEnabled` write, `AdvancedFeaturesAdapterImpl` extension | None (nothing reads the field yet) |
| 2. Data-model load | `TargetingInfo.generateWhenLimitOnInAppCampaign` + projection extended for ND | None (LC ignores the loaded limits) |
| 3. LC evaluator | `NativeDisplayCombinedQuotaCheck` + metadata emission on App Launched | Metadata visible to SDK; SDK ignores until updated |
| 4. SDK release | Cap-eval loop, `nd_eval` vote, `nd_suppressed` ack | End-to-end enforcement for opted-in accounts on new SDK |
| 5. Dashboard UI | ND fcap widget exposed on campaign form | Customers can author with caps |
| 6. LD rollout | Enable per-account after SDK adoption threshold | Progressive enablement |

Each phase is independently deployable and reversible (no lockstep coupling, no cross-service contract break — per the platform's independent-deployment rule).

---

## 12. Metrics & Observability

- Profile metric: `nd_fcap_gate_hit` per cap type (efc / tlc / tdc / advanced / global).
- Profile metric: `nd_fcap_delivered_uncapped` (i.e. `isNdFcapEnabled == false` path) — tracks grandfathering scope.
- P1 log line on `NativeDisplayCombinedQuotaCheck` exception (fail-closed drop).
- Campaign stats: `TargetingCountManager.markAsSent` continues to fire per delivery (existing behavior, no change).
- SDK impression counters: not persisted server-side. Reset on uninstall / clear-data — same trade-off as in-app.
- `native_display_stale` output size on App Launched — proxy for counter-GC health.

---

## 13. Open Questions

1. **Global daily ceiling for ND** — reuse `Account.inappMaxPerUserPerDay` (today's silent fallback in `TargetEvalManager.getPerDayLimit` for `NativeDisplay`) or introduce a dedicated `Account.nativeDisplayMaxPerUserPerDay`?
2. **Session ceiling `ndm`** — SDK-only; do we need one distinct from in-app `ism`?
3. **SDK impression-map size** — mirror in-app's 100, or ND-specific?
4. **`nd_suppressed` ack path** — needed if App-Launched content-in-advance can include CG-suppressed ND campaigns. Recommendation: yes, symmetric with `inapps_suppressed`.
5. **Mid-session metadata refresh** — is there an ND equivalent of `wzrk_fetch t=5`, or is App Launched the only bootstrap point?

---

## 14. References

**In-app fcaps (reference implementation this TAN mirrors):**
- `legacy_flow.md` — legacy dispatch, cap-check body, dead targets
- `ss_mode.md` — SS-mode composer, `inapps_eval` protocol, quota-check routing
- `cs_mode.md` — for contrast (ND is SS-only)
- `journeys_fcaps.md` — legacy-forced routing for journey targets

**Key code paths this TAN touches:**
- `core/lc/.../targeting/NativeDisplayEvalManager.java` — event-triggered ND dispatcher (new fcap gate slots in here)
- `core/inapps/service/.../delivery/common/InAppCombinedQuotaCheck.java:24-33` — pattern to mirror for the ND analog
- `core/inapps/service/.../adapters/AdvancedFeaturesAdapterImpl.java:44-113` — extend for `Mode.NativeDisplay`
- `core/dao/.../TargetingInfo.java:2132-2159` — extend to include `NativeDisplay`
- `core/dao/.../InAppWebTargetingDao.java:60-99` — extend projection with `isNdFcapEnabled`
- `dashboard/.../TargetingNativeDisplayAction.java:146` — save-side LD-flag write
- `dashboard/.../NativeDisplayCreateAction.java:512` — create-action LD-flag write
- `dashboard/.../NativeDisplayFcapPayloadUtils.java` — coexisting FcapV2 path (untouched)
- `core/lc/.../targeting/DeadTargetsEvalManager.java` — `native_display_stale` emission (already exists)

**Confluence:**
- Fcaps in ND campaigns (index): [6972964894](https://wizrocket.atlassian.net/wiki/spaces/EN/pages/6972964894/Fcaps+in+ND+campaigns)
- BE TAN — in-app feature parity: [4008706201](https://wizrocket.atlassian.net/wiki/spaces/MC/pages/4008706201)
- SDK TAN — in-app feature parity: [4085613262](https://wizrocket.atlassian.net/wiki/spaces/EN/pages/4085613262)
- Key decisions: [4052779089](https://wizrocket.atlassian.net/wiki/spaces/MC/pages/4052779089)
- PRD — advanced fcaps: [3944448006](https://wizrocket.atlassian.net/wiki/spaces/MC/pages/3944448006)
- PRD — advanced display triggers: [3842637854](https://wizrocket.atlassian.net/wiki/spaces/MC/pages/3842637854)
