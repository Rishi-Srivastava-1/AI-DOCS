# In-app in Journeys — Fcaps + Triggers Deep Dive

Scope: how **in-app nodes inside a Journey** are capped and delivered, and how that differs from standalone campaigns. Specifically answers: *is there special fcap handling for journeys, given a journey in-app can be reached via a **live** path or a **PBS** (past-behaviour) path, and PBS delivers at the next `App Launched`?*

Everything below is verified against current `master` and cross-checked with the BE TAN (`4008706201`), SDK TAN (`4085613262`), and Key Decisions doc (`4052779089`). Reads alongside `legacy_flow.md` / `cs_mode.md` / `ss_mode.md`.

> **Scope note — which "frequency caps" this is.** In-app per-campaign fcaps (`tlc`/`tdc`/`mdc`/`efc`), checked via `TargetEvalManager` / the `core/inapps` quota checks. **Not** the credit-based cross-channel **Advanced Frequency Limits (FcapV2)** system, and **not** the Journey engine's own **entry/qualification limits** (see §6). In-app dispatch never touches FcapV2.

---

## 0. TL;DR — the one rule for journey fcaps

**A journey in-app always uses the LEGACY fcap model — never advanced — regardless of account flags or which path (live/PBS) reached it.** Live vs PBS differ only in **when/where the in-app is delivered** (and therefore *when* the legacy cap is evaluated), **not** in the cap rules themselves. There is exactly **one** fcap code-path for journeys; the "special handling" is (a) the forced legacy routing and (b) the delivery/timing split.

---

## 1. Why journeys are always legacy — never advanced fcaps/triggers

Two independent code facts force this:

**(a) Journey targets never get an `AdvancedLimits` object.** `TargetingInfo.generateWhenLimitOnInAppCampaign` (`core/dao/src/com/wizrocket/db/dto/TargetingInfo.java:2132-2159`) builds `AdvancedLimits` **only** for `getMode() == InApp && !isJourneyTarget()` (and Web / WebNativeDisplay). For a journey target it returns `null`, so `ti.getAdvancedLimits()` is empty and `hasAdvancedRules()` is never true.

**(b) The quota router forces legacy for journeys.** `InAppCombinedQuotaCheck.targetQuotaReached` (`core/inapps/service/.../delivery/common/InAppCombinedQuotaCheck.java:24-33`):
```java
boolean fCapsEnabled = featureFlags.isEnabled(ADVANCED_FCAPS, params.getAccountId());
if (fCapsEnabled && !ti.isJourneyTarget()) {
    return newQuotaCheck.targetQuotaReached(params, ti);   // advanced (global-day only)
}
return legacyQuotaCheck.targetQuotaReached(params, ti);    // journeys ALWAYS land here
```
Even on an account fully whitelisted for advanced fcaps, `!ti.isJourneyTarget()` is false for a journey node, so it takes the **legacy** branch → `TargetEvalManager.targetQuotaReached` (`core/lc/.../targeting/TargetEvalManager.java:264-345`).

*Rationale:* a journey in-app **node** is authored once but can sit on a live **or** a PBS path, and the advanced model is an SDK-per-device-counter mechanism keyed to a client display trigger — which the PBS path doesn't have (§4.2). Rather than support it on one path and not the other, journeys were excluded wholesale (BE TAN; `ss_mode.md` §6, `cs_mode.md` §13). See the advanced-vs-journey discussion in `ss_mode.md` §5/§6.

## 2. What a journey in-app payload carries — legacy limits only

Journey in-apps are built with `getJourneyFields`, **not** `getCommonFields`. `ContentUtil.getJourneyFields` (`core/inapps/service/.../payload/common/ContentUtil.java:96-110`):
```java
payload.append(PAYLOAD_TARGET_ID, ti.getID())
       .append(NOTIFICATION_CLICKED_ID, ...)
       .append(NOTIFICATION_CONTENT_KEY, ...)
       .append(WZRK_TTL, getInAppTTLWithDelay(accountId, ti));
payload.putAll(getLegacyLimits(ti));            // efc, tlc, tdc, mdc (if set) — ContentUtil.java:156-182
return payload;
```
So a journey in-app ships **only** the legacy caps (`tlc`/`tdc`/`mdc`/`efc`) — and crucially **no `whenTriggers`, no `frequencyLimits`, no `occurrenceLimits`.** (Contrast `PayloadProviderSS`, `ss_mode.md` §8, which routes journey targets to `getJourneyFields`.) The dashboard exposes these as the 4 delivery-preference options on the node (`BatchDeliveryPreference.vue`; TAN: *Entry Limits in Campaigns/Journeys*):

| Node option | Field | Enforced |
|---|---|---|
| Exclude from campaign limit | `efc` | server + SDK |
| Once per session | `mdc` | **SDK only** |
| Once per day | `tdc` | server + SDK |
| Once per user for campaign duration | `tlc` | server + SDK |

Advanced "N per X" / onEvery-onExactly are **not** offered on journey nodes.

## 3. Fcap enforcement for journeys — the legacy quota check

Same routine as legacy campaigns (`legacy_flow.md` §5). `TargetEvalManager.targetQuotaReached` evaluates, for the journey in-app node:

- `efc > 0` → short-circuit (never capped),
- target daily (`tdc`) and lifetime (`tlc`) vs the SDK-reported `TARGET_SHOWN_LIST` (`tlc` array `[targetId, todayCount, lifetime]`),
- global per-day (`shownToday` vs `account.getInappMaxPerUserPerDay`).
- **Session (`mdc`) and global session (`ism`) are never server-side** — SDK enforces (`legacy_flow.md` §5.2 javadoc).

Counters are **SDK-supplied** (`tlc`/`imp` in every request), server-persists nothing per-user. Global↔node coordination (pass-all / tighter-binds / `efc` precedence) is identical to `legacy_flow.md` §5.4.

**Journeys are never gated on `inapps_eval`.** `DeliveryStrategySS.shouldSkipTarget` (`.java:106-114`): `skipCampaign` requires `!ti.isJourneyTarget()`, so a journey is never skipped for lack of an SDK vote. The server evaluates and delivers the journey in-app **directly** — there is no SDK "vote" step for journeys, because the SDK holds no advanced metadata for them (§1, §2). (`ss_mode.md` §5.)

## 4. The two paths — same caps, different delivery moment

This is where live and PBS diverge — in **delivery path and timing**, which is *when* §3's legacy check runs.

### 4.1 Live-behaviour journey in-app — delivered on the trigger event

The user does the qualifying event → LC processes it → the journey in-app fires **on that event**. Target selection: `DeliveryTargetsJourney.fetchTargets` (`core/inapps/service/.../targets/DeliveryTargetsJourney.java:22-44`) keys on `params.getEventName()`, filters `isJourneyTarget`, sorts priority-desc + id.
- **SS-mode account:** handled by `DeliveryStrategySS` (its target list includes journey targets; not gated on `inapps_eval` per §3).
- **CS-mode account:** handled by the dedicated `deliveryStrategyJourney` (`@Named(DELIVERY_SS_JOURNEY)`, `InAppsCommonModule.java:85-94` — a `DeliveryStrategySS` wired with `DeliveryTargetsJourney`), because the CS campaign path never touches journeys (`cs_mode.md` §4).
- Legacy quota (§3) is evaluated **at that event**, against the counts the SDK just reported. Content returned in `inapp_notifs`.

### 4.2 PBS journey in-app — delivered at the **next App Launched**

A PBS-qualified user is moved into the journey/step by the ES batch — there is **no client trigger event** for the in-app node. So the node is stored as a **no-trigger target**, and it's picked up on the next `App Launched` by the **null-trigger union**:

`DeliveryTargetsJourney.fetchTargets` (`.java:31-35`):
```java
List<TargetingInfo> targets = t.getWebInAppNonNull(params.getEventName(), InApp, device);
if (APP_LAUNCHED.equals(params.getEventName())) {
    targets = ListUtils.union(targets, t.getWebInAppNonNull(null, InApp, device));  // ← no-trigger targets
}
```
The same union exists in `DeliveryTargetsAppLaunchedSS` (SS mode). On App Launched the journey competes by priority and, if it wins, is returned **alone** in `inapp_notifs` (`DeliveryStrategyAppLaunchedSS`, `ss_mode.md` §4.2 — "journey wins → single in-app under `inapp_notifs`").

**Fcap consequence:** the legacy check (§3) runs **at App Launch**, against the counts the SDK reports on that launch:
- `tdc`/`tlc`/global-day behave exactly as for any other in-app.
- **`mdc` ("once per session")** is naturally evaluated against the **session that just started** — App Launch is a session boundary — so a PBS journey in-app respects once-per-session per the delivering session. (Session cap is SDK-side; `mdc` is shipped in `getLegacyLimits` for the SDK to enforce.)

> The PBS batch/qualification decides *whether the user is at the node*; the **fcap** only decides *whether to actually deliver on this App Launch*. They are independent gates. (Delivery-window "Set Frequency" and start/end active-period are separate again — server-side segmentation, see the in-app docs.)

## 5. One in-app per event / per App-Launch

Journeys return **at most one** in-app per triggering event (priority-arbitrated); on App Launched, if a journey wins the priority race against campaign in-apps it is returned alone in `inapp_notifs`, else campaigns go out in `inapp_notifs_applaunched` (`ss_mode.md` §4.2). The winning in-app still has to pass §3's legacy quota before it ships.

## 6. Node fcaps vs journey-level limits — don't conflate

Two different throttles apply to a journey:

| Concern | What it limits | Where enforced |
|---|---|---|
| **In-app node fcap** (this doc) | how often *this in-app node* renders for a user | LC — legacy `targetQuotaReached` + SDK `mdc`/`ism` |
| **Journey entry / qualification limits** | how often a user can **enter / re-enter** the journey, journey-level frequency, conversion/exit windows | Journey engine — ES qualification / LP step commit (out of scope here; TAN: *Entry Limits in Campaigns/Journeys*) |

A user can be blocked from *entering* the journey (journey-level) **or** qualify and enter but have the in-app *not rendered* (node fcap). The node fcap is the only piece this doc covers.

## 7. Gotchas / special-handling summary

1. **Advanced fcaps/triggers never apply to journeys** — forced legacy by `InAppCombinedQuotaCheck` (`!ti.isJourneyTarget()`) and by journeys having no `AdvancedLimits` (`TargetingInfo.java:2135`). No `frequencyLimits`/`occurrenceLimits`/`whenTriggers` in the payload.
2. **Live vs PBS = delivery timing only.** Live → on the trigger event (`DeliveryStrategySS` / CS `deliveryStrategyJourney`); PBS → next App Launch via the **null-trigger union** in `DeliveryTargetsJourney`/`DeliveryTargetsAppLaunchedSS`. The legacy cap rule is identical; only the moment it runs changes.
3. **PBS session cap is evaluated in the delivering session** — App Launch is the session boundary, so `mdc` once-per-session is honoured per that launch's session.
4. **No `inapps_eval` vote for journeys** — server evaluates and delivers directly; the SDK holds no journey metadata to gate on.
5. **One journey in-app per event / App-Launch**, priority-arbitrated; still subject to §3 quota.
6. **CS-mode accounts still deliver journeys server-side** via `deliveryStrategyJourney`; journey caps there also route to legacy (`cs_mode.md` §4, §6).
7. **Node fcap ≠ journey entry limit** (§6) — separate systems.
8. **Session/global-session caps are SDK-only** even for journeys (`mdc`, `ism`); server checks only `tdc`/`tlc`/global-day (`legacy_flow.md` §5.2).

## 8. Key file / line reference

| Concern | File | Line |
|---|---|---|
| Journeys get no `AdvancedLimits` | `core/dao/.../dto/TargetingInfo.java` | 2132-2159 |
| Quota router forces legacy for journeys | `core/inapps/.../delivery/common/InAppCombinedQuotaCheck.java` | 24-33 |
| Legacy quota body | `core/lc/.../targeting/TargetEvalManager.java` | 264-345 |
| Journey payload fields (legacy limits only) | `core/inapps/.../payload/common/ContentUtil.java` | 96-110 |
| Legacy limits builder | `core/inapps/.../payload/common/ContentUtil.java` | 156-182 |
| Journey target selection + App-Launch null-trigger union | `core/inapps/.../targets/DeliveryTargetsJourney.java` | 22-44 |
| Journeys not gated on `inapps_eval` | `core/inapps/.../delivery/ss/DeliveryStrategySS.java` | 106-114 |
| PBS journey delivered on App Launch (journey wins → `inapp_notifs`) | `core/inapps/.../delivery/ss/DeliveryStrategyAppLaunchedSS.java` | 52-136 |
| CS journey strategy binding | `core/inapps/.../guice/InAppsCommonModule.java` | 85-94 |
| SS vs CS composer (journey handling per mode) | `core/inapps/.../delivery/DeliveryStrategyProvider.java` | 51-99 |

## 9. Take-aways

1. **One fcap model for journeys: legacy.** Forced, regardless of account flags or path.
2. **Journey in-apps carry only `tlc`/`tdc`/`mdc`/`efc`** — never advanced limits or `whenTriggers`.
3. **Live and PBS share the exact same cap rules**; they differ only in *when* the in-app is delivered (trigger event vs next App Launch) and therefore *when* the legacy check runs.
4. **PBS's App-Launch delivery** rides the **null-trigger union** in `DeliveryTargetsJourney` — that's the concrete mechanism behind "delivered at next App Launched".
5. **`mdc` for a PBS journey in-app is per the delivering session** (App Launch = session start); session/global-session caps are SDK-side for journeys too.
6. **No `inapps_eval` handshake for journeys** — the server owns evaluation + delivery end to end.
7. **Keep node fcaps separate from journey entry/qualification limits** — different subsystems (LC vs the journey engine).
8. Coordination with global caps and the `efc` vs `excludeGlobalFCaps` precedence are identical to `legacy_flow.md` §5.4.
