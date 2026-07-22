<style>
body { font-size: 80px; }
h1 { font-size: 2em; }
h2 { font-size: 1.5em; }
h3 { font-size: 1.2em; }
code { font-size: 0.9em; }
pre { font-size: 0.85em; }
table { font-size: 1em; }
</style>

# Native Display Feature - Architecture Documentation

## Overview

Native Display is a real-time, in-app messaging feature that delivers personalized content to users within mobile applications (iOS/Android) and web browsers. Unlike push notifications, Native Display campaigns are evaluated and served in real-time when users perform specific actions (trigger events) within the app.

There are two variants:
- **NativeDisplay** - For mobile apps (iOS/Android)
- **WebNativeDisplay** - For web browsers

## Architecture Components

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           NATIVE DISPLAY DATA FLOW                          │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  ┌──────────────┐                                                           │
│  │  Dashboard   │ ──(Campaign Creation)──► MongoDB (Targeting Collection)  │
│  │   (Vue.js)   │                                                           │
│  └──────────────┘                                                           │
│         │                                                                   │
│         ▼                                                                   │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │                      LOG COLLECTOR (LC)                               │  │
│  │  ┌─────────────────────────┐    ┌────────────────────────────────┐   │  │
│  │  │ NativeDisplayEvalManager│    │ WebNativeDisplayEvalManager    │   │  │
│  │  │   (Mobile Apps)         │    │   (Web Browsers)               │   │  │
│  │  └─────────────────────────┘    └────────────────────────────────┘   │  │
│  │         │                                  │                          │  │
│  │         ▼                                  ▼                          │  │
│  │  ┌─────────────────────────────────────────────────────────────┐     │  │
│  │  │              TargetEvalManager (Base Class)                 │     │  │
│  │  │  - Target evaluation logic                                   │     │  │
│  │  │  - Profile matching                                          │     │  │
│  │  │  - Control group handling                                    │     │  │
│  │  │  - Frequency cap checking                                    │     │  │
│  │  └─────────────────────────────────────────────────────────────┘     │  │
│  │         │                                                             │  │
│  │         ▼                                                             │  │
│  │  Response: { "adUnit_notifs": [...] } (Mobile)                       │  │
│  │  Response: { "inapp_notifs": [...] } (Web)                           │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
│         │                                                                   │
│         ▼                                                                   │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │                      LOG PROCESSOR (LP)                               │  │
│  │  ┌─────────────────────────┐                                          │  │
│  │  │   NotifEventConsumer    │ ── Processes notification events        │  │
│  │  │   - Clicked             │    (Notification Clicked/Viewed)        │  │
│  │  │   - Viewed              │                                          │  │
│  │  │   - Control Group       │                                          │  │
│  │  └─────────────────────────┘                                          │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
│         │                                                                   │
│         ▼                                                                   │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │                       EVENT STORE (ES)                                │  │
│  │  - Stores event data for analytics                                    │  │
│  │  - SDK version validation (isSDKVersionValidForNativeDisplay)        │  │
│  │  - Campaign statistics tracking                                       │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Core Components

### 1. Log Collector (LC)

The LC is responsible for real-time evaluation and delivery of Native Display campaigns.

#### Key Files

| File | Path | Purpose |
|------|------|---------|
| `NativeDisplayEvalManager` | `core/lc/src/java/com/wizrocket/lc/targeting/NativeDisplayEvalManager.java` | Evaluates and serves Native Display campaigns to mobile apps |
| `WebNativeDisplayEvalManager` | `core/lc/src/java/com/wizrocket/lc/targeting/WebNativeDisplayEvalManager.java` | Evaluates and serves Web Native Display campaigns |
| `TargetEvalManager` | `core/lc/src/java/com/wizrocket/lc/targeting/TargetEvalManager.java` | Base class with common evaluation logic |
| `WebEvalManager` | `core/lc/src/java/com/wizrocket/lc/targeting/WebEvalManager.java` | Base class for web targeting evaluation |

#### NativeDisplayEvalManager Details

**Class**: `com.wizrocket.lc.targeting.NativeDisplayEvalManager`

**Key Constants**:
```java
public static final String RESPONSE_NATIVE_DISPLAY_KEY = "adUnit_notifs";
public static final String RESPONSE_REMINDERS_KEY = "reminder_notifs";
public static final int ANDROID_SDK_VERSION = 30602;  // Minimum Android SDK version
public static final int IOS_SDK_VERSION = 30702;      // Minimum iOS SDK version
```

**Key Methods**:

1. `evalAndFillResponse()` - Main evaluation pipeline
   - Retrieves targets for the trigger event
   - Evaluates each target against user profile and event properties
   - Handles control group logic
   - Fills response with qualified notifications

2. `fillResponse()` - Populates the response object
   - Checks target qualification
   - Handles control group assignment
   - Creates notification content
   - Marks campaign as sent in statistics

3. `getNativeDisplayNotificationContent()` - Builds notification content
   - Applies personalization replacements
   - Handles recommendations
   - Adds notification metadata (clicked ID, content key)

4. `isSDKVersionValid()` - Validates SDK version
   - Android: >= 30602
   - iOS: >= 30702

#### WebNativeDisplayEvalManager Details

**Class**: `com.wizrocket.lc.targeting.WebNativeDisplayEvalManager`

Extends `WebEvalManager` and handles Web Native Display campaigns.

**Key Features**:
- No capping on number of campaigns per response
- Handles liquid replacements for personalization
- Adds campaign priority to display object
- Processes banner details with replacements

### 2. Log Processor (LP)

The LP processes notification events (clicks, views) for analytics and statistics.

#### Key Files

| File | Path | Purpose |
|------|------|---------|
| `NotifEventConsumer` | `core/lp/src/com/wizrocket/lp/events/consumers/NotifEventConsumer.java` | Processes notification events |
| `ConsumerUtils` | `core/lp/src/com/wizrocket/lp/events/utils/ConsumerUtils.java` | Utility methods for event consumption |

#### NotifEventConsumer Details

**Mode to Download Type Mapping**:
```java
case NativeDisplay:
    return Download.nativedisplay;
case WebNativeDisplay:
    return Download.webnativedisplay;
```

**UTM Medium Values**:
- NativeDisplay: `UTM_MEDIUM_NATIVE_DISPLAY`
- WebNativeDisplay: `UTM_MEDIUM_WEB_NATIVEDISPLAY`

**Event Processing**:
- Validates `wzrk_id` (notification clicked ID)
- Maps events to statistics (clicked, viewed, control group)
- Handles session tracking for Native Display (preserves session TS)

### 3. Event Store (ES)

ES handles SDK version validation and event storage.

#### Key Files

| File | Path | Purpose |
|------|------|---------|
| `App.java` | `core/es/src/com/wizrocket/eventsstore/protocol/App.java` | SDK version validation |

**SDK Version Validation** (`App.DecoderV2.isSDKVersionValidForNativeDisplay()`):
```java
public boolean isSDKVersionValidForNativeDisplay(String accountId, AppPlatform appPlatform) {
    // Special handling for Paytm family accounts
    if (AppConstants.PAYTM_FAMILY.contains(accountId)) {
        return appPlatform == AppPlatform.ANDROID || appPlatform == AppPlatform.IOS;
    }
    // Standard version checks
    int baseSDKVersion;
    if (appPlatform == AppPlatform.ANDROID) {
        baseSDKVersion = AppConstants.NATIVE_DISPLAY_ANDROID_SDK_VERSION;
    } else if (appPlatform == AppPlatform.IOS) {
        baseSDKVersion = AppConstants.NATIVE_DISPLAY_IOS_SDK_VERSION;
    } else {
        return false;
    }
    return isSDKVersionValid(appPlatform, baseSDKVersion);
}
```

### 4. DAO Layer

The DAO layer handles data access for Native Display campaigns.

#### Key Files

| File | Path | Purpose |
|------|------|---------|
| `InAppWebTargetingDao` | `core/dao/src/com/wizrocket/db/dao/InAppWebTargetingDao.java` | DAO for targeting campaigns |
| `TargetingDto` | `core/dao/src/com/wizrocket/db/dto/TargetingDto.java` | DTO for target data |
| `TriggerQueryDto` | `core/dao/src/com/wizrocket/db/dto/TriggerQueryDto.java` | Base DTO for trigger-based targeting |
| `TargetingInfo` | `core/dao/src/com/wizrocket/db/dto/TargetingInfo.java` | Wrapper for individual target data |

#### TargetingDto Target Storage

Targets are stored in a multi-dimensional map structure:
```java
// HashMap index mapping:
// 0 = Web
// 1 = InApp
// 2 = Webhook
// 3 = NativeDisplay
// 4 = NotificationInbox
// 5 = WebNativeDisplay
// 6 = WebInbox
final HashMap<String, InAppWebEntry[]>[] deviceToInAppWebEntryMap = new HashMap[7];
```

For each mode, targets are indexed by:
1. Trigger event name
2. Device type (Android/iOS/Web)

### 5. Dashboard (Campaign Management)

#### Key Files

| File | Path | Purpose |
|------|------|---------|
| `TargetingNativeDisplayAction` | `dashboard/dashboard/src/com/wizrocket/dashboard/action/target/TargetingNativeDisplayAction.java` | Mobile Native Display campaign management |
| `TargetingWebNativeDisplayAction` | `dashboard/dashboard/src/com/wizrocket/dashboard/action/target/TargetingWebNativeDisplayAction.java` | Web Native Display campaign management |

#### TargetingNativeDisplayAction

**Key Content Fields**:
```java
public static final String NATIVE_DISPLAY_KEY_MESSAGE = "message";
public static final String NATIVE_DISPLAY_KEY_TITLE = "title";
public static final String NATIVE_DISPLAY_KV = "customKVData";
public static final String NATIVE_DISPLAY_ACTION_URL = "url";
public static final String NATIVE_DISPLAY_PERSONALIZED_MEDIA_SOURCE = "isMediaSourceRecommended";
public static final String NATIVE_DISPLAY_PERSONALIZED_ICON_SOURCE = "isIconSourceRecommended";
```

**Campaign Creation Flow**:
1. `edit()` - Display editor UI
2. `save()` - Save campaign
   - `fillSpecific()` - Set device and mode
   - `fillContent()` - Process message content
   - `fillQuery()` - Build targeting query

## Mode Definitions

Defined in `ProfileReach.java`:

```java
public enum Mode {
    // ... other modes ...
    NativeDisplay(nativedisplay, "Native Display", false, false),
    WebNativeDisplay(webnativedisplay, "Web Native Display", false, false),
    // ...
}

// Bitmask values:
private final static int nativedisplay = 1 << 25;
private final static int webnativedisplay = 1 << 29;
```

**Web Target Type Enum**:
```java
public enum WebTargetType {
    POPUP, EXITINTENT, WEBNATIVEDISPLAY, WEBINBOX;
}
```

## Request/Response Flow

### Mobile Native Display Request Flow

1. **App Event** → LC receives event (e.g., "App Launched", "Product Viewed")
2. **Target Retrieval** → `TargetingDto.getWebInApp(eventName, NativeDisplay, device)`
3. **Target Evaluation** → `processTarget()` evaluates:
   - Time/day constraints
   - Event property matching
   - Profile-based targeting
   - Journey step qualification
4. **SDK Version Check** → `isSDKVersionValid()`
5. **Control Group Check** → `ControlGroupUtils.getControlGroupId()`
6. **Response Building** → `fillResponse()` creates notification content
7. **Statistics Update** → `TargetingCountManager.markAsSent()`

### Response Structure

**Mobile Native Display Response**:
```json
{
  "adUnit_notifs": [
    {
      "wzrk_id": "12345_20240301",
      "wzrk_ck": "default",
      "ti": 12345,
      "message": { "text": "...", "color": "..." },
      "title": { "text": "...", "color": "..." },
      "action": { "url": { "android": "...", "ios": "..." } },
      "media": { ... },
      "customKVData": [ { "key": "...", "value": "..." } ]
    }
  ]
}
```

**Web Native Display Response** (via `inapp_notifs`):
```json
{
  "inapp_notifs": [
    {
      "wzrk_id": "12345_20240301",
      "wzrk_ck": "default",
      "msgContent": { ... },
      "display": {
        "priority": 1,
        "details": [ ... ],
        "wtarget_type": 2
      }
    }
  ]
}
```

## Personalization Features

### 1. Profile Replacements
- Uses `${}` syntax for profile field replacements
- Handled by `TargetingUtils.doReplacements()`

### 2. Liquid Personalization
- Advanced templating with Liquid syntax
- Marked with `HAS_LIQUID` flag
- Processed by `doLiquidReplacements()`

### 3. Recommendations
- Product recommendations based on user behavior
- Uses `ReplacementsUtils.prepareRecommendationReplacements()`
- Supports recommended media and icon sources

### 4. Event Property Personalization
- Trigger event properties can be used in content
- Stored in `ID_CONTAINER_FOR_REPLACEMENTS`

## Frequency Capping

### Target-Level Caps
- `INAPP_TGT_DAILY_COUNT` - Maximum per day
- `INAPP_TGT_LIFETIME_COUNT` - Maximum lifetime
- `INAPP_TGT_EXCLUDE_FREQUENCY_CAP` - Exclude from caps

### Global Caps
- Per-day limit based on account settings
- Retrieved via `Account.getWebNativeDisplayMaxPerUserPerDay()`

### Web Native Display
- No default session/day frequency caps (set to `Integer.MAX_VALUE`)
- Custom frequency caps via `InAppMongoConstants.New.FREQUENCY_LIMITS`

## Control Groups

Control group handling is built into the evaluation flow:

1. **System Control Group** - Automatic percentage-based exclusion
2. **Custom Control Group** - User-defined control groups

When a user falls into a control group:
- A `NOTIFICATION_CONTROL` event is generated
- Statistics are tracked separately for control group users
- No notification is sent to the user

## Campaign Types

### Mobile Native Display Templates
- Simple Message
- Carousel Message
- Message with Icon
- Carousel Image Only
- Custom Key-Value
- Simple Image Only

### Web Native Display
- Custom HTML banners
- Details array for multiple banners
- Priority-based ordering

## Integration Points

### Journey Integration
- Native Display can be part of journey campaigns
- Journey step tracking via `addToQualifiedJourneySteps()`
- Entry criteria support via `isEntryCriteria()`

### Cascade/Sequence Delivery
- `CASCADE_MESSAGE_DELIVERY` - Supports sequential message delivery
- `FETCH_VIA_API_SUB_CONTEXT` - Content fetch API integration

### Reminders
- `HAS_REMINDER_QUERY` - Support for reminder campaigns
- `RESPONSE_REMINDERS_KEY` - Separate response key for reminders

## Key Database Fields

| Field | Description |
|-------|-------------|
| `TYPE` | Mode ordinal (NativeDisplay or WebNativeDisplay) |
| `DEVICE` | Target devices (Android=1, iOS=2, Web=0) |
| `CONTENT` | Message content per device |
| `QUERY` | Targeting queries |
| `TARGET_TYPE` | Campaign type (InstantEventTriggered for native display) |
| `PRIORITY` | Campaign priority for ordering |
| `HAS_LIQUID` | Liquid personalization flag |
| `REPLACEMENT` | Profile field replacements |
| `WEB_TARGET_TYPE` | Web-specific target type (WEBNATIVEDISPLAY=2) |

## Metrics and Monitoring

### Profiler Reports
- `"Native Display Num of successful profile evals..."` - Successful evaluations
- `"failed to send nativedisplay!"` - Delivery failures

### Statistics Tracked
- Sent count via `TargetingCountManager.markAsSent()`
- Clicked/Viewed via `NotifEventConsumer`
- Control group stats via `markAsCG()`

## Feature Flags and Configuration

| Feature Flag | Purpose |
|--------------|---------|
| `rollout-nd-linked-content` | Linked content API support |
| `contentApiForOnlineTargets` | Content API for online targets |
| `nativedisplay_channel` | Native Display feature access |
| `webnativedisplay_channel` | Web Native Display feature access |

## Related Files Summary

### LC (Log Collector)
- `NativeDisplayEvalManager.java` - Mobile evaluation
- `WebNativeDisplayEvalManager.java` - Web evaluation
- `TargetEvalManager.java` - Base evaluation logic
- `WebEvalManager.java` - Web base evaluation

### LP (Log Processor)
- `NotifEventConsumer.java` - Event processing
- `ConsumerUtils.java` - Utility functions

### ES (Event Store)
- `App.java` - SDK version validation

### DAO
- `InAppWebTargetingDao.java` - Data access
- `TargetingDto.java` - Target data structure
- `TriggerQueryDto.java` - Trigger-based targeting
- `TargetingInfo.java` - Individual target wrapper

### Dashboard
- `TargetingNativeDisplayAction.java` - Mobile campaign management
- `TargetingWebNativeDisplayAction.java` - Web campaign management

### Constants
- `ProfileReach.java` - Mode and device definitions
- `AppConstants.java` - SDK version constants
