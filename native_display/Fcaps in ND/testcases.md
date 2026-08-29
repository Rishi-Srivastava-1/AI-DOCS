# Native Display Fcaps — Test Cases

## ND strategies

### Multiple ND: **OFF**

| # | Strategy | Output key |
|---|---|---|
| 1 | `deliveryStrategyMetaSS` | `adunit_notifs_ss` |
| 2 | `deliveryStrategySS` | `adunit_notifs` |
| 3 | `deliveryStrategyAppLaunchedSS` | `adunit_notifs` / `adunit_notifs_app_launched` |
| 4 | `deliveryStrategySuppressionAppLaunchedSS` | *(none — raises supplementary events)* |

### Multiple ND: **ON**

Batch vs single — *TBD*.

---

## Test cases

### Backward compatibility

1. **Already-running campaigns must be excluded from fcaps by default.**
   - There is a key `isNDFcaps` (enabled). If this key is **not present**, BE should assume these are old campaigns and treat them as `excludeFromFcaps`.

---

## Multiple ND — OFF

### 1. Non-app-launch targets

- At app launch, BE sends `adunit_notifs_ss` (trigger info).
- During event trigger, BE sends `adunit_notifs` **or** `content_fetch` (any one; can be campaign or journey).
- Handle **single vs batch** events.
- Handle **single vs multi-trigger** cases.
- CG handling at BE.
- **BE checks:**
  - If target has `advanceFcaps` / trigger → the target must be present in `adunit_evals`, else ignore the target.
  - If the above passes, check `legacyFcaps` (if `excludeFromFcap` is not `true`).
- BE marks the **single** target as *mark as sent*.

### 2. App-launch targets

- At `app_launch`, BE sends `adunit_notifs` (single journey, similar to in-app) **or** `adunit_notifs_app_launched`.
  > **In in-app:** if the highest-priority + oldest combination turns out to be a **journey**, BE sends the data in `inapp_notifs`; else all the data goes in `inapp_notifs_app_launched`.
- BE marks **all** targets as *mark as sent*.
- SDK shows the campaign as per the fcaps.
- If there is a **CG case**, the SDK sends `adunit_suppressed`; BE should consume this and raise subsequent CG events.

### 3. `WZRK_FETCH` `t = 7`

- Just send the meta over to the SDK.

---

## Multiple ND — ON

### 1. Non-app-launch targets

- At app launch, BE sends `adunit_notifs_ss` (trigger info) — **no change** whether the flag is ON or OFF.
- During event trigger, BE sends `adunit_notifs` / `content_fetch` — **all qualifying at the highest priority** (campaign or journey).
- Handle **single vs batch** events.
- Handle **single vs multi-trigger** cases.
- CG handling at BE.
- **BE checks:**
  - If target has `advanceFcaps` / trigger → the target must be present in `adunit_evals`, else ignore the target.
  - If the above passes, check `legacyFcaps` (if `excludeFromFcap` is not `true`).
- BE marks the **single** target as *mark as sent*.

### 2. App-launch targets

- At `app_launch`, BE sends `adunit_notifs` (**all** journeys) / `adunit_notifs_app_launched` (**all** campaigns).
  > ⚠️ **Discuss this once with SDK.**
  > In in-app at the AL event: if a journey event wins, `inapp_notifs` is sent; else campaign data goes in `_app_launched` (irrespective of whether the target has adv fcaps selected or not).
- BE marks **all** targets as *mark as sent*.
- SDK shows the campaign as per the fcaps.
- If there is a **CG case**, the SDK sends `adunit_suppressed`; BE should consume this and raise subsequent CG events.

### 3. `WZRK_FETCH` `t = 7`

- Just send the meta over to the SDK.
