# PIP (Picture-in-Picture) InApp Template — Backend Implementation Plan

## Context

ONC-276 adds a new InApp template type `pip` (PIP). The frontend is already built on branch `ONC-276-pipInAppsTemplate`. PIP is a floating video/image/gif overlay InApp with no title/message/buttons. This plan covers the backend changes needed for:
1. **Campaign save** — marking `text_liquid` on personalizable PIP fields
2. **LC dispatch** — personalizing PIP fields at delivery time

---

## FE Payload Structure (msg)

```json
{
  "type": "pip",
  "is_native": true,
  "close": true,
  "media": {
    "url": "", "content_type": "", "alt_text": "",
    "fallback_url": "", "key": "", "poster": ""
  },
  "pip": {
    "animation": { "type": "instant", "direction": "left", "duration": 15, "easing": "linear" },
    "aspectRatio": { "numerator": 1.78, "denominator": 1 },
    "border": { "color": "#E1E4E5", "enabled": false, "width": 1 },
    "controls": { "drag": true, "expandCollapse": false, "mute": false, "playPause": true },
    "cornerRadius": 4,
    "margins": { "horizontal": 5, "vertical": 5 },
    "onClick": { "type": "url", "android": "", "ios": "", "kv": {}, "close": false },
    "position": "bottom-right",
    "width": 40
  }
}
```

Key observations:
- `type` is `"pip"` (not `"picture-in-picture"`)
- Media uses the **standard root-level `media` object** — not inside `pip`
- `pip` contains only layout/behavior config + `onClick`
- `pip.onClick` has the same shape as button actions: `{type, android, ios, kv, close}`

---

## Key Decisions

- PIP is a **native template** (`is_native=true`) — follows the native path in `InAppContentBuilderImpl`, NOT the HTML path
- PIP has **no title, message, or buttons** — but uses the standard `media` field at root level
- `pip.onClick` reuses **existing `fillButtonActions()`** for personalization (android/ios URLs + KV pairs)
- PIP does **NOT skip** `parseAndPopulateMediaFields()` — media is standard
- PIP **skips** title/message text replacement (no such fields) and button processing (no buttons array)
- **No SDK version gating** — all native-InApp-capable SDKs handle PIP
- The DB stores SDK-ready format — **no transformation needed** in LC dispatch

---

## Changes

### 1. Add PIP constants — `Constants.java`
**File:** `core/inapps/service/src/main/java/com/wizrocket/inapps/imageinterstitial/Constants.java`

```java
public static final String PIP_TYPE = "pip";
public static final String PIP = "pip";
public static final String ON_CLICK = "onClick";
```

### 2. Add PIP type check — `InAppTemplateType.java`
**File:** `core/inapps/service/src/main/java/com/wizrocket/inapps/utils/InAppTemplateType.java`

```java
public static boolean isPip(String type) {
    return Constants.PIP_TYPE.equals(type);
}
```

### 3. Dashboard Save — liquid marking for PIP onClick
**File:** `dashboard/dashboard/src/com/wizrocket/dashboard/action/target/TargetingInAppAction.java`

#### 3a. Add new method `checkAndMarkLiquidForPipData()`
- Gets `message.type`; if not `"pip"`, return `hasLiquid` unchanged
- Gets `pip` BasicDBObject from message
- Gets `pip.onClick` and delegates to `checkAndMarkLiquidForActions()` for android/ios/KV

#### 3b. Extract shared helper `checkAndMarkLiquidForActions()`
The android/ios/KV liquid-check logic is shared between `checkAndMarkLiquidForButton()` and `checkAndMarkLiquidForPipData()`, extracted into:
```java
protected boolean checkAndMarkLiquidForActions(BasicDBObject actions, BasicDBObject old,
        boolean hasLiquid)
```

#### 3c. Integrate into `checkAndMarkLiquidContentForAllFields()`
Add at the end (before return):
```java
hasLiquid = checkAndMarkLiquidForPipData(message, old, hasLiquid);
```

**Note:** Existing message/title/media checks still run — message/title return false harmlessly (empty), media check handles the standard `media` field correctly.

### 4. LC Dispatch — content building for PIP
**File:** `core/lc/src/java/com/wizrocket/lc/targeting/InAppContentBuilderImpl.java`

#### 4a. Add PIP branch in `fillInAppContent()`
```java
} else if (InAppTemplateType.isPip(type)) {
    fillPipContent(profile, ti, m, variantKey);
}
```
**No early return** — PIP falls through to `parseAndPopulateMediaFields()` (standard media) and `fillButtons()` (harmless on empty buttons list).

#### 4b. Add new method `fillPipContent()`
Only handles `pip.onClick` personalization:
```java
private void fillPipContent(BasicDBObject profile, TargetingInfo ti,
                            BasicDBObject m, String variantKey) {
    BasicDBObject pip = (BasicDBObject) m.get(PIP);
    if (pip == null) return;

    if (pip.get(ON_CLICK) instanceof BasicDBObject onClick) {
        fillButtonActions(profile, ti, onClick,
                variantKey + UNDERSCORE + PIP + UNDERSCORE + ON_CLICK);
    }
}
```

---

## Files Modified

| # | File | Change |
|---|------|--------|
| 1 | `core/inapps/service/.../imageinterstitial/Constants.java` | Add `PIP_TYPE`, `PIP`, `ON_CLICK` constants |
| 2 | `core/inapps/service/.../utils/InAppTemplateType.java` | Add `isPip()` method |
| 3 | `dashboard/.../action/target/TargetingInAppAction.java` | Add `checkAndMarkLiquidForPipData()`, extract `checkAndMarkLiquidForActions()`, integrate into `checkAndMarkLiquidContentForAllFields()` |
| 4 | `core/lc/.../targeting/InAppContentBuilderImpl.java` | Add `fillPipContent()`, add PIP branch in `fillInAppContent()` |

## Implementation Order

1. `Constants.java` — no dependencies
2. `InAppTemplateType.java` — depends on Constants
3. `TargetingInAppAction.java` — depends on InAppTemplateType
4. `InAppContentBuilderImpl.java` — depends on InAppTemplateType

## Existing Utilities Reused

| Utility | File | Used For |
|---------|------|----------|
| `TargetingUtils.checkAndMarkFieldSpecificLiquidContent()` | `core/dao/.../utils/TargetingUtils.java` | Marking text_liquid on save |
| `TargetingUtils.cleanAndReplaceReplacements()` | same | Personalizing fields at dispatch |
| `fillButtonActions()` | `InAppContentBuilderImpl.java` | Personalizing onClick android/ios/kv |
| `cleanAndReplaceReplacementsForKVPairs()` | `InAppContentBuilderImpl.java` | Converting KV array->object with replacements |
| `checkAndMarkLiquidContentForKVPairs()` | `TargetingInAppAction.java` | Marking liquid on KV values |
| `checkAndMarkLiquidForActions()` | `TargetingInAppAction.java` | Shared android/ios/KV liquid check (extracted) |
| `parseAndPopulateMediaFields()` | `InAppContentBuilderImpl.java` | Standard media personalization (reused as-is) |

## Verification

1. **Unit tests for dashboard save** (in `TargetingInAppActionTest.java`):
   - PIP message with liquid in `pip.onClick.android` -> assert `text_liquid=true`
   - PIP message with liquid in `pip.onClick.ios` -> assert `text_liquid=true`
   - PIP message with liquid in `pip.onClick.kv` values -> assert `text_liquid=true`
   - PIP message with no personalization -> assert no liquid marked
   - Non-PIP type -> assert PIP check skipped

2. **Unit tests for LC dispatch** (in `InAppContentBuilderTest.java`):
   - PIP with `pip.onClick` containing BasicDBObject android/ios -> assert personalized
   - PIP with `pip.onClick.kv` array -> assert converted to object with resolved values
   - PIP with standard `media` field -> assert media personalization works
   - PIP message -> assert no NPE from missing title/message/buttons

3. **Manual E2E test:**
   - Save a PIP campaign with liquid personalization in onClick via dashboard
   - Verify MongoDB document has `text_liquid` flags and `HAS_LIQUID=true`
   - Trigger LC dispatch for a matching user and verify personalized SDK payload
