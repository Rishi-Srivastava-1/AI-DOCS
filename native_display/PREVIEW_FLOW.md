<style>
body { font-size: 12px; }
h1 { font-size: 2em; }
h2 { font-size: 1.5em; }
h3 { font-size: 1.2em; }
code { font-size: 0.9em; }
pre { font-size: 0.85em; }
table { font-size: 1em; }
</style>

# Native Display Preview Flow

This document describes the preview/test flow for Native Display campaigns, which allows users to preview how the campaign will look on their devices before publishing.

## Overview

The Native Display preview flow sends a test push notification to selected devices containing the campaign content wrapped in a special key (`wzrk_adunit`). The SDK on the device recognizes this key and renders the Native Display content.

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                        NATIVE DISPLAY PREVIEW FLOW                              │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                 │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │                         DASHBOARD (Frontend)                             │   │
│  │  ┌─────────────────────────────────────────────────────────────────┐    │   │
│  │  │ NativeDisplayContent.vue                                         │    │   │
│  │  │   └── "Preview & Test" button → openTestModal()                 │    │   │
│  │  │         └── TestModal.vue                                        │    │   │
│  │  │               └── PushtestForm.vue (select profile/tokens)      │    │   │
│  │  │                     └── convertAndSendTest()                     │    │   │
│  │  │                           └── prepareOptions()                   │    │   │
│  │  │                                 └── Constructs wzrk_adunit KV   │    │   │
│  │  └─────────────────────────────────────────────────────────────────┘    │   │
│  │                             │                                            │   │
│  │                             ▼                                            │   │
│  │  ┌─────────────────────────────────────────────────────────────────┐    │   │
│  │  │ api.sendPushLikeTest(options)                                    │    │   │
│  │  │   POST /{accountId}/json/push/interact/previewTarget             │    │   │
│  │  │   Payload: { title, message, gcmIds, apnsTokens, uids,          │    │   │
│  │  │              kv: { 1: {wzrk_adunit: "{...}"}, 2: {...}, 3: {...}} │    │   │
│  │  │            }                                                      │    │   │
│  │  └─────────────────────────────────────────────────────────────────┘    │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
│                                    │                                            │
│                                    ▼                                            │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │                     DASHBOARD (Backend - Struts)                         │   │
│  │  ┌─────────────────────────────────────────────────────────────────┐    │   │
│  │  │ TargetingPushAction.previewTarget()                              │    │   │
│  │  │   - Validates input                                              │    │   │
│  │  │   - Fetches profiles/tokens from ES                             │    │   │
│  │  │   - Forwards request to NB (Notification Backend)               │    │   │
│  │  └─────────────────────────────────────────────────────────────────┘    │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
│                                    │                                            │
│                                    ▼                                            │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │                      NOTIFICATION BACKEND (NB)                           │   │
│  │  ┌─────────────────────────────────────────────────────────────────┐    │   │
│  │  │ PreviewProcessor.java                                            │    │   │
│  │  │   └── updateIndividualKv()                                       │    │   │
│  │  │         └── if key == "wzrk_adunit":                             │    │   │
│  │  │               └── InboxAdViewUtils.fillPreviewMessage(msg, profile)│    │   │
│  │  │                     └── fillMessage(m, profile, null, true, null)│    │   │
│  │  │                           └── isPreview = true                   │    │   │
│  │  │                                 └── TargetingUtils.cleanAndReplaceReplacements()│
│  │  │                                       (with isPreview=true)      │    │   │
│  │  └─────────────────────────────────────────────────────────────────┘    │   │
│  │                             │                                            │   │
│  │                             ▼                                            │   │
│  │  ┌─────────────────────────────────────────────────────────────────┐    │   │
│  │  │ Push Notification Dispatch                                       │    │   │
│  │  │   - Send to FCM (Android) / APNs (iOS)                          │    │   │
│  │  │   - Payload includes processed wzrk_adunit content              │    │   │
│  │  └─────────────────────────────────────────────────────────────────┘    │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
│                                    │                                            │
│                                    ▼                                            │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │                         MOBILE SDK                                       │   │
│  │  - Receives push notification with wzrk_adunit key                      │   │
│  │  - Recognizes Native Display content                                     │   │
│  │  - Renders the campaign UI to the user                                   │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
│                                                                                 │
└─────────────────────────────────────────────────────────────────────────────────┘
```

## Detailed Flow

### Step 1: Frontend - User Initiates Preview

**File**: `dashboard/dashboard/web/src/apps/campaigns/components/editor/what/native-display/NativeDisplayContent.vue`

```vue
<a v-if="isValidForTest && showPreviewAndTest"
   href="javascript:void(0);"
   class="d-flex ct-link t-14 mr-4"
   @click="openTestModal">
  <ct-icon class="mr-2" color="primary">cticon-Preview-Test</ct-icon>
  Preview & Test
</a>

<test-modal :modal.sync="testModal" @sendTest="convertAndSendTest" :loading="sendingTest">
  <template #form>
    <pushtest-form ref="pushTestForm" :params.sync="testParams" :is-push="false" />
  </template>
</test-modal>
```

### Step 2: Frontend - Prepare Content for Preview

**File**: `NativeDisplayContent.vue:147-169`

```javascript
prepareOptions () {
  let tempMsg = {}
  tempMsg.msg = formatMsg(this.templateInEdit)
  let formatted = JSON.stringify(tempMsg)
  let kv = {
    1: {wzrk_adunit: formatted},  // Android
    2: {wzrk_adunit: formatted},  // iOS
    3: {wzrk_adunit: formatted}   // Windows
  }
  const options = {
    ...this.testParams,
    title: 'Clevertap',
    message: 'Touch this notification to open your app and view your native-display notification',
    kv,
    mode: 'push',
    isAndroid: true,
    isIOS: true
  }
  if (this.testParams.channel) {
    options.kv[1].wzrk_cid = this.testParams.channel
  }
  return options
}
```

**Key Points**:
- Content is wrapped in `{msg: {...}}` structure
- JSON stringified and placed in `wzrk_adunit` key
- Device-specific KV map (1=Android, 2=iOS, 3=Windows)
- Channel ID can be added for Android notification channels

### Step 3: Frontend - Send Test Request

**File**: `dashboard/dashboard/web/src/apps/campaigns/components/editor/what/mixins/PushTestMixin.js:30-45`

```javascript
convertAndSendTest (options_ = null) {
  if (!this.$refs.pushTestForm.validate()) return
  let options = options_ || this.prepareOptions()
  if (options) {
    this.sendingTest = true
    api.sendPushLikeTest(options).then((data) => {
      this.queueSnackbar(new SnackbarItem({
        title: data.result,
        duration: 5000,
        type: data?.state || SnackbarType.DEFAULT
      }))
    }).finally(() => {
      this.sendingTest = false
    })
  }
}
```

### Step 4: Frontend - API Call

**File**: `dashboard/dashboard/web/src/apps/campaigns/store/api.js:223-306`

```javascript
sendPushLikeTest (params) {
  let options = {
    isAndroid: false,
    isIOS: false,
    type: 'token',
    uids: [],
    apnsTokens: '',
    gcmIds: '',
    title: '',
    message: '',
    kv: {},
    appId: params?.appId ?? 0,
    mode: 'push'
  }
  options = {...options, ...params}

  // Build devices string
  let devices = ''
  if (options.isAndroid) devices = devices + ',android'
  if (options.isIOS) devices = devices + ',ios'

  const url = `/${ACCOUNT_ID}/json/${options.mode}/interact/previewTarget?devices=${devices}`

  return api.post(url, {
    title: options.title,
    message: options.message,
    gcmIds: options.gcmIds,
    apnsTokens: options.apnsTokens,
    type: options.type,
    uids: options.uids,
    kv: options.kv,
    appId: options.appId,
    // ... other params
  })
}
```

**API Endpoint**: `POST /{accountId}/json/push/interact/previewTarget?devices=android,ios`

### Step 5: Backend (Dashboard) - TargetingPushAction

**File**: `dashboard/dashboard/src/com/wizrocket/dashboard/action/target/TargetingPushAction.java`

The `previewTarget` method:
1. Validates input parameters
2. Fetches user profiles from Event Store (ES)
3. Constructs the NB payload
4. Forwards request to Notification Backend (NB)

### Step 6: Backend (NB) - PreviewProcessor

**File**: `core/nb/src/com/wizrocket/nb/preview/processors/PreviewProcessor.java:1042-1063`

```java
protected void updateIndividualKv(String k, String v, ProfileDTO profile, BasicDBObject kvs,
        PushMessage m) {

    if (k.equalsIgnoreCase(WZRK_INBOX) || k.equalsIgnoreCase(WZRK_ADUNIT)) {
        // adunit is nativedisplay
        BasicDBObject ibNotif = BasicDBObject.parse(v);
        BasicDBObject msg =
                (BasicDBObject) ibNotif.getOrDefault(TargetingDao.MSG, new BasicDBObject());
        try {
            InboxAdViewUtils.fillPreviewMessage(msg, profile.getProfileObject());

            // For adview, SDK doesn't expect "msg" key, whereas in
            // "inbox" the msg key is present.
            if (k.equalsIgnoreCase(WZRK_INBOX)) {
                // Handle inbox-specific processing...
                ibNotif.put(TargetingDao.MSG, msg);
            } else {
                // For Native Display (adunit), put msg content directly
                ibNotif.put(TargetingDao.MSG, msg);
            }
        } catch (Exception e) {
            // Handle error
        }
        kvs.put(k, ibNotif.toString());
    }
}
```

**Key Points**:
- `WZRK_ADUNIT` is the key used for Native Display
- `WZRK_INBOX` is used for App Inbox (similar flow)
- `fillPreviewMessage` is called with `isPreview=true`

### Step 7: Content Processing with Preview Flag

**File**: `core/dao/src/com/wizrocket/db/utils/InboxAdViewUtils.java:36-39`

```java
public static void fillPreviewMessage(BasicDBObject m, BasicDBObject profile)
        throws InvalidDataException, RecommendationReplacementException,
        LiquidReplacementException {
    fillMessage(m, profile, null, true, null);  // isPreview = true
}
```

The `fillMessage` method with `isPreview=true`:
1. Processes custom KV pairs
2. Applies profile replacements
3. Handles recommendations (but removes `recommendedText` and `recommendedIconText` fields in preview mode)

**File**: `InboxAdViewUtils.java:253-258`

```java
if (isPreview) {
    // If its preview make sure to remove the recommendText and recommendIconText as
    // we are not performing doReplacements on these fields
    singleContent.removeField("recommendedText");
    singleContent.removeField("recommendedIconText");
}
```

### Step 8: Replacement Processing in Preview Mode

**File**: `core/dao/src/com/wizrocket/db/orchestration/utils/TargetingUtils.java:1304-1340`

```java
public static String cleanAndReplaceReplacements(String keyPrefix, BasicDBObject content,
        TargetingInfo ti, BasicDBObject profile, boolean isPreview) {

    String replacedText = getReplacedText(content);

    // In preview mode, liquid replacement is skipped (ti is null)
    if (ti != null && ti.hasLiquid()) {
        if (content.getBoolean(IS_LIQUID, false)) {
            replacedText = doLiquidReplacements(ti, keyPrefix, replacedText, profile, isPreview);
        }
    }

    // Profile replacements are always applied (with isPreview flag)
    if ((ti != null && ti.replacements() != null && ti.replacements().size() > 0) || isPreview) {
        replacedText = TargetingUtils.doReplacements(replacedText, profile, false, false, isPreview);

        // In preview mode, incomplete replacements are allowed (no validation)
        // In production, incomplete replacements would throw an exception
    }
    return replacedText;
}
```

**Key Differences in Preview Mode**:
- `TargetingInfo` is `null` (no campaign metadata)
- Liquid replacement is skipped (no liquid cache)
- Profile replacements use the selected test profile
- Incomplete replacements don't cause errors
- `recommendedText` and `recommendedIconText` fields are removed

## Legacy Flow (JSP-based)

For older implementations, the flow is triggered from:

**File**: `dashboard/dashboard/web/src/pages/what-nativedisplay/what-nativedisplay.jsp:174-224`

```javascript
function _sendTestPushNotification(devices, content) {
  var boolAndroid = devices.indexOf(1) > -1;
  var boolIos = devices.indexOf(2) > -1;
  var boolWindows = devices.indexOf(3) > -1;

  var options = {
    android: boolAndroid,
    ios: boolIos,
    windows: boolWindows,
    uids: '',
    type: 'profile',
    callback: function (type, uids, androidTokens, iosTokens, windowTokens) {
      var clonedContent = JSON.parse(JSON.stringify(content))
      var adViewDetails = {wzrk_adunit: JSON.stringify(clonedContent)};
      var androidDetails = $.extend(true, {}, adViewDetails);
      var channelId = $("#testaochannel").val();
      if (channelId != "" && channelId != null) {
        androidDetails.wzrk_cid = channelId;
      }

      $.fn.sendTestPushNotification(boolAndroid, boolIos, boolWindows, boolChrome,
        type, uids, androidTokens, iosTokens, windowTokens, chromeTokens, firefoxTokens, safariTokens,
        "CleverTap",
        "Touch this notification to open your app and view your notification adView message",
        {
          "1": androidDetails,
          "2": $.extend(true, {}, adViewDetails),
          "3": $.extend(true, {}, adViewDetails)
        }, "", boolFirefox, boolSafari, appId, null, boolKaios, kaiosTokens);
    },
    gcmTokens: __WHAT_ADVIEW_GLOBALS.gcmTokens,
    apnsTokens: __WHAT_ADVIEW_GLOBALS.apnsTokens,
    winTokens: __WHAT_ADVIEW_GLOBALS.winTokens
  };

  var $adViewTestModal = $("#adview-test-modal");
  var testModal = $adViewTestModal.wzrktestpnwidget(options, "nativedisplay");
  testModal.testPushNotificationButtonClick();
}
```

## Content Structure in Preview

### Input (Frontend → Dashboard)

```json
{
  "title": "CleverTap",
  "message": "Touch this notification to open your app...",
  "type": "profile",
  "uids": "identity1,identity2",
  "gcmIds": "token1,token2",
  "apnsTokens": "token1,token2",
  "appId": 123,
  "kv": {
    "1": {
      "wzrk_adunit": "{\"msg\":{\"default\":{\"bg\":\"#ffffff\",\"content\":[{\"message\":{\"text\":\"Hello $Name$!\",\"color\":\"#000000\"},\"title\":{\"text\":\"Special Offer\",\"color\":\"#333333\"},\"media\":{\"url\":\"https://...\"},\"action\":{\"url\":{\"android\":\"app://...\",\"ios\":\"app://...\"}}}]}}}"
    },
    "2": { "wzrk_adunit": "..." },
    "3": { "wzrk_adunit": "..." }
  }
}
```

### Output (NB → Push Provider)

After processing with profile data:

```json
{
  "wzrk_adunit": {
    "msg": {
      "default": {
        "bg": "#ffffff",
        "content": [{
          "message": {"text": "Hello John!", "color": "#000000"},
          "title": {"text": "Special Offer", "color": "#333333"},
          "media": {"url": "https://..."},
          "action": {"url": {"android": "app://...", "ios": "app://..."}}
        }]
      }
    }
  }
}
```

## Key Files Reference

| Component | File | Purpose |
|-----------|------|---------|
| Frontend Entry | `NativeDisplayContent.vue:22-24` | Preview & Test button |
| Frontend Options | `NativeDisplayContent.vue:147-169` | Prepare preview options |
| Frontend Mixin | `PushTestMixin.js:30-45` | Send test logic |
| Frontend API | `api.js:223-306` | API call to backend |
| Backend Action | `TargetingPushAction.java` | Handle previewTarget request |
| NB Processor | `PreviewProcessor.java:1042-1063` | Process wzrk_adunit KV |
| Content Processing | `InboxAdViewUtils.java:36-39` | fillPreviewMessage entry |
| Replacements | `TargetingUtils.java:1304-1340` | Apply replacements with isPreview flag |
| Legacy JSP | `what-nativedisplay.jsp:174-224` | Old preview flow |

## Differences: Preview vs Production

| Aspect | Preview Mode | Production Mode |
|--------|--------------|-----------------|
| TargetingInfo | `null` | Populated with campaign data |
| Liquid Replacement | Skipped | Applied |
| Profile Source | Selected test profile | Real user profile from ES |
| Recommendation Fields | Removed from response | Processed and included |
| Replacement Validation | No validation | Throws exception on failure |
| Route | via Push notification KV | Direct LC response |

## Relevant Constants

```java
// In PreviewProcessor.java
public static final String WZRK_ADUNIT = "wzrk_adunit";
public static final String WZRK_INBOX = "wzrk_inbox";

// In NativeDisplayContent.vue
const KV_DEVICES = {
  1: 'Android',
  2: 'iOS',
  3: 'Windows'
}
```

## Error Handling

1. **Invalid Profile**: If the test profile doesn't exist, NB returns an error
2. **Invalid Token**: If push tokens are invalid, the push provider returns an error
3. **Replacement Failure**: In preview mode, incomplete replacements are allowed (but shown as raw syntax)
4. **SDK Version**: Preview bypasses SDK version checks (will render on any SDK version)

## Notes for Implementation Changes

When making changes to preview content preparation:

1. **Frontend Changes**: Modify `prepareOptions()` in `NativeDisplayContent.vue`
2. **Content Structure Changes**: Update both frontend and `InboxAdViewUtils.fillPreviewMessage()`
3. **Replacement Logic Changes**: Modify `TargetingUtils.cleanAndReplaceReplacements()` with `isPreview` flag handling
4. **New Fields**: Add to `fillMessage()` processing and ensure preview-specific handling if needed
