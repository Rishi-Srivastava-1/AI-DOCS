<style>
body { font-size: 12px; }
h1 { font-size: 2em; }
h2 { font-size: 1.5em; }
h3 { font-size: 1.2em; }
code { font-size: 0.9em; }
pre { font-size: 0.85em; }
table { font-size: 1em; }
</style>

# Native Display Content Preparation Flow

This document provides a deep dive into how content is prepared and personalized for Native Display campaigns in the LC (Log Collector).

## Overview

Content preparation is the process of:
1. Retrieving raw message content from the target configuration
2. Applying personalization (profile replacements, liquid templates, recommendations)
3. Building the final response object sent to the client SDK

## Content Preparation Entry Point

### NativeDisplayEvalManager.getNativeDisplayNotificationContent()

**Location**: `core/lc/src/java/com/wizrocket/lc/targeting/NativeDisplayEvalManager.java:326-378`

This is the main method that orchestrates content preparation:

```java
@Nullable
public BasicDBObject getNativeDisplayNotificationContent(int accountId,
        TargetingInfo ti,
        ProfileReach.Device device,
        BasicDBObject profile,
        BasicDBObject eventProperties,
        String key,
        String notifClickedId,
        boolean wrapWzrKObj,
        long idn,
        String subcontext) throws Exception {

    // Step 1: Retrieve raw message content
    BasicDBObject m = ti.message(device, key, null);

    // Step 2: Prepare recommendation replacements
    Map<String, Integer> currEventMap = ti.getPartialEvMaster().get(ti.getCurrentEventId());
    BasicDBObject eventPropMap = Recommendation.getEvtPropsForRecommendation(eventProperties, currEventMap);
    BasicDBObject recommendations = ReplacementsUtils.prepareRecommendationReplacements(
            ti.getRecommendationRefs(), eventPropMap,
            (BasicDBObject) profile.get(LAST_EVENT_DATA), ti.getMapLastActionIdToAlias(),
            accountId + "", ti.getRecommendationFilter(), profile);
    profile.put("recommendations", recommendations);

    // Step 3: Apply all replacements
    InboxAdViewUtils.fillMessage(m, profile, ti, key);

    // Step 4: Add notification metadata
    m.put(AppConstants.KnownKeys.NOTIFICATION_CLICKED_ID, notifClickedId);
    m.put(AppConstants.KnownKeys.NOTIFICATION_CONTENT_KEY, key);
    m.put("ti", ti.getID());

    return m;
}
```

## Content Retrieval Flow

### Step 1: TargetingInfo.message()

**Location**: `core/dao/src/com/wizrocket/db/dto/TargetingInfo.java:2515-2536`

```java
public BasicDBObject message(ProfileReach.Device device, String messageKey,
        BasicDBObject fileRow) {
    BasicDBObject msg = get(device, TargetingDao.MSG, fileRow);

    if (msg != null) {
        BasicDBObject out = null;
        if (messageKey != null) {
            out = (BasicDBObject) msg.get(messageKey);  // Get specific variant
        }
        if (out == null) {
            messageKey = TargetingDao.MSG_DEFAULT;      // Fallback to default
            out = (BasicDBObject) msg.get(messageKey);
        }
        if (out == null) {
            messageKey = MultipleMessageInfo.BACKUP_KEY; // Fallback to backup
            out = (BasicDBObject) msg.get(messageKey);
        }
        return out;
    }
    return null;
}
```

### Content Storage Structure

Content is stored in the `contents` array indexed by device ordinal:

```java
private final BasicDBList[] contents = new BasicDBList[ProfileReach.Device.size()];
// Index: 0=Web, 1=Android, 2=iOS, etc.
```

**Initialization** (`TargetingInfo.java:1206-1257`):

```java
// For each delivery device
for (int i = 0; i < deliverToDevices.size(); i++) {
    int deliveryOrdinal = (Integer) deliverToDevices.get(i);
    Device device = Device.val(deliveryOrdinal);

    Object contentObj = tgt.get(TargetingDao.CONTENT);
    if (contentObj instanceof BasicDBList content) {
        // Drip content - multiple steps
        for (int j = 0; j < content.size(); j++) {
            BasicDBObject dripContent = (BasicDBObject) content.get(j);
            BasicDBList bucket = contents[device.ordinal()];
            // ... store device-specific content
        }
    } else {
        // Non-drip content - single message
        BasicDBObject content = (BasicDBObject) contentObj;
        BasicDBObject deviceContent = (BasicDBObject) content.get(deliveryOrdinalStr);
        BasicDBList list = new BasicDBList();
        list.add(deviceContent);
        contents[device.ordinal()] = list;
    }
}
```

### Message Key Selection (A/B Testing & Multi-Variant)

**Location**: `TargetingInfo.java:2421-2504`

```java
public String getMessageContentKey(BasicDBObject profileFields, String guid, long iid,
        boolean skipWinner) {
    String pivotName = TargetingDao.MSG_DEFAULT;

    if (pivotInfo != null && pivotKeyId != -1 && pivotKeys != null) {
        // Multi-variant: Select based on profile pivot field
        pivotName = getPivotFromProfileFields(profileFields);
    } else if (multipleMessageInfo != null && !multipleMessageInfo.getMessageIds().isEmpty()) {
        // A/B Testing: Select winner or distribute based on hash
        if (!skipWinner && multipleMessageInfo.getWinnerId() != null) {
            return multipleMessageInfo.getWinnerId();
        }
        // Hash-based distribution
        String key = guid.hashCode() % 100 < splitPercent ? keyA : keyB;
        return key;
    }
    return pivotName;
}
```

## Personalization Flow

### Step 2: Prepare Recommendation Replacements

**Location**: `core/dao/src/com/wizrocket/db/utils/ReplacementsUtils.java:75-84`

```java
public static BasicDBObject prepareRecommendationReplacements(
        List<String> recommendationRefs, BasicDBObject evtProps,
        BasicDBObject lastActionData, Map<String, Integer> lastActions, String accountId,
        BasicDBObject recommendationFilter, BasicDBObject profile) {

    BasicDBObject commonParam = new BasicDBObject(TargetingDao.ACCOUNT_ID, accountId);
    commonParam.put(TargetingDao.PROFILE, profile);
    commonParam.put(TargetingDao.RECOMMENDATION_FILTER, recommendationFilter);

    return prepareReplacements(recommendationRefs, evtProps, lastActionData, lastActions,
            null, null, commonParam);
}
```

**Recommendation Reference Format**:
```
r:<catalogId>:<refType>:<eventId>:<propertyId>:<refColId>:<rank>:<showColId>
```

**Reference Types**:
- `IH` (In-Hand) - Use event property value directly
- `LA` (Last Action) - Use last action data from profile
- `PR` (Profile) - Use user profile property
- `CP` (Constant Property) - Use a constant value

### Step 3: Apply All Replacements

**Location**: `core/dao/src/com/wizrocket/db/utils/InboxAdViewUtils.java:42-261`

#### InboxAdViewUtils.fillMessage()

```java
public static void fillMessage(BasicDBObject m, BasicDBObject profile, TargetingInfo ti,
        String key) throws InvalidDataException, RecommendationReplacementException,
        LiquidReplacementException {

    // Process Custom KV Pairs
    processCustomKVPairs(m, profile, ti, isPreview, variantKey);

    // Process content array
    BasicDBList content = (BasicDBList) m.get(TargetingDao.CONTENT);
    if (content != null && content.size() > 0) {
        for (int i = 0; i < content.size(); i++) {
            BasicDBObject singleContent = (BasicDBObject) content.get(i);
            String liquidCacheSuffix = variantKey + AppConstants.UNDERSCORE + i;

            // Process message
            BasicDBObject messageContent = (BasicDBObject) singleContent.get(MESSAGE);
            if (messageContent != null) {
                messageContent.put("text", TargetingUtils.cleanAndReplaceReplacements(
                        MESSAGE + liquidCacheSuffix, messageContent, ti, profile, isPreview));
            }

            // Process title
            BasicDBObject titleContent = (BasicDBObject) singleContent.get(TITLE);
            if (titleContent != null) {
                titleContent.put("text", TargetingUtils.cleanAndReplaceReplacements(
                        TITLE + liquidCacheSuffix, titleContent, ti, profile, isPreview));
            }

            // Process action URLs
            BasicDBObject actionContent = (BasicDBObject) singleContent.get(ACTION);
            if (actionContent != null) {
                // Process Android URL
                actionContentURLAndroid.put("text",
                    TargetingUtils.cleanAndReplaceReplacements(...));
                // Process iOS URL
                actionContentURLIOS.put("text",
                    TargetingUtils.cleanAndReplaceReplacements(...));
            }

            // Process Personalized Media (Recommendation)
            boolean isMediaSourceRecommended = singleContent.getBoolean("isMediaSourceRecommended", false);
            if (isMediaSourceRecommended) {
                BasicDBObject mediaContentRecommendation =
                    (BasicDBObject) singleContent.get("recommendedText");
                String mediaText = TargetingUtils.cleanAndReplaceReplacements(
                        "media" + liquidCacheSuffix, mediaContentRecommendation, ti, profile, isPreview);
                mediaContent.put("url", mediaText);
            }

            // Process Personalized Icon (Recommendation)
            boolean isIconSourceRecommended = singleContent.getBoolean("isIconSourceRecommended", false);
            if (isIconSourceRecommended) {
                BasicDBObject iconContentRecommendation =
                    (BasicDBObject) singleContent.get("recommendedIconText");
                String iconText = TargetingUtils.cleanAndReplaceReplacements(
                        "icon" + liquidCacheSuffix, iconContentRecommendation, ti, profile, isPreview);
                iconContent.put("url", iconText);
            }
        }
    }
}
```

#### Custom KV Pairs Processing

**Location**: `InboxAdViewUtils.java:263-292`

```java
public static void processCustomKVPairs(BasicDBObject m, BasicDBObject profile,
        TargetingInfo ti, boolean isPreview, String variantKey) {

    BasicDBList customKVList = (BasicDBList) m.get(REQUEST_CUSTOM_KV);  // "customKVData"

    if (customKVList != null && !customKVList.isEmpty()) {
        BasicDBObject customKVObj = new BasicDBObject();
        for (int i = 0; i < customKVList.size(); i++) {
            BasicDBObject kvPair = (BasicDBObject) customKVList.get(i);
            String key = kvPair.getString(CUSTOM_KV_KEY);
            if (!StringUtil.isEmpty(key)) {
                BasicDBObject valObj = (BasicDBObject) kvPair.getOrDefault(CUSTOM_KV_VALUE,
                        new BasicDBObject());
                customKVObj.put(key,
                        TargetingUtils.cleanAndReplaceReplacements(
                                CUSTOM_KV_VALUE + variantKey + i, valObj, ti, profile, isPreview));
            }
        }
        m.remove(REQUEST_CUSTOM_KV);        // Remove "customKVData"
        m.put(RESPONSE_CUSTOM_KV, customKVObj);  // Add "custom_kv"
    }
}
```

### TargetingUtils.cleanAndReplaceReplacements()

**Location**: `core/dao/src/com/wizrocket/db/orchestration/utils/TargetingUtils.java:1304-1340`

```java
public static String cleanAndReplaceReplacements(String keyPrefix, BasicDBObject content,
        TargetingInfo ti, BasicDBObject profile, boolean isPreview) {

    // Get text with replacements syntax
    String replacedText = getReplacedText(content);

    // Step 1: Liquid replacement (if enabled)
    if (ti != null && ti.hasLiquid()) {
        try {
            if (content.getBoolean(IS_LIQUID, false)) {
                replacedText = doLiquidReplacements(ti, keyPrefix, replacedText, profile, isPreview);
            }
        } catch (Exception ex) {
            throw new LiquidReplacementException(ex);
        }
    }

    // Step 2: Profile replacements
    if ((ti != null && ti.replacements() != null && ti.replacements().size() > 0) || isPreview) {
        replacedText = TargetingUtils.doReplacements(replacedText, profile, false, false, isPreview);

        // Validation: Check for incomplete replacements
        boolean nullMessage = (replacedText == null);
        boolean replacementsNotDone = replacedText.contains(TargetingUtils.REPLACE_DELIM_START);
        if (nullMessage || replacementsNotDone) {
            throw new InvalidDataException("Data contains null or replacement ");
        }
    }
    return replacedText;
}
```

### TargetingUtils.doReplacements()

**Location**: `core/dao/src/com/wizrocket/db/orchestration/utils/TargetingUtils.java:879-940`

```java
public static String doReplacements(String str, Object profile, boolean useFallback,
        boolean doURIEncode, boolean isPreview) {

    if (str == null) return null;

    // Replace escaped brackets
    str = str.replace(OPEN_SQUARE_BRACKET, "[").replace(CLOSE_SQUARE_BRACKET, "]");

    // Pattern: $<bucket>:<propId>$ or $<bucket>:<propId>|<fallback>$
    Matcher m = replacePattern.matcher(str);

    while (m.find()) {
        String bucket = m.group(1);    // e.g., "p" (profile), "e" (event), "recommendations"
        String propId = m.group(2);    // Property ID
        String fallback = m.group(4);  // Optional fallback value

        BasicDBObject bucketObj = (BasicDBObject) ((BasicDBObject) profile).get(bucket);
        Object value = bucketObj != null ? bucketObj.get(propId) : null;

        if (value != null) {
            str = str.replace(m.group(0), value.toString());
        } else if (useFallback && fallback != null) {
            str = str.replace(m.group(0), fallback);
        }
    }

    return str;
}
```

**Replacement Syntax**:
- `$p:<propId>$` - Profile property
- `$e:<propId>$` - Event property
- `$recommendations:<key>$` - Recommendation value
- `$p:<propId>|fallback$` - With fallback value

## Content Structure in Response

### Native Display Response Format

```json
{
  "adUnit_notifs": [
    {
      "wzrk_id": "12345_20240301",
      "wzrk_ck": "default",
      "ti": 12345,
      "type": "simple",
      "content": [
        {
          "message": {
            "text": "Hello John!",
            "color": "#000000"
          },
          "title": {
            "text": "Special Offer",
            "color": "#333333"
          },
          "action": {
            "url": {
              "android": "app://deep/link",
              "ios": "app://deep/link"
            }
          },
          "media": {
            "url": "https://cdn.example.com/image.jpg",
            "content_type": "image/jpeg"
          },
          "icon": {
            "url": "https://cdn.example.com/icon.png"
          }
        }
      ],
      "custom_kv": {
        "offer_id": "SALE2024",
        "discount": "20%"
      }
    }
  ]
}
```

## Key Classes and Methods Summary

### Content Retrieval Chain

```
NativeDisplayEvalManager.getNativeDisplayNotificationContent()
    └── TargetingInfo.message(device, key, null)
            └── TargetingInfo.get(device, TargetingDao.MSG, fileRow)
                    └── contents[device.ordinal()] → BasicDBList
                            └── msg.get(messageKey) → BasicDBObject (the actual message)
```

### Personalization Chain

```
NativeDisplayEvalManager.getNativeDisplayNotificationContent()
    │
    ├── Recommendation.getEvtPropsForRecommendation()
    │       └── Maps event property names to IDs
    │
    ├── ReplacementsUtils.prepareRecommendationReplacements()
    │       └── Fetches recommended product values from catalogs
    │
    └── InboxAdViewUtils.fillMessage()
            │
            ├── processCustomKVPairs()
            │       └── Personalizes custom key-value pairs
            │
            └── For each content item:
                    ├── TargetingUtils.cleanAndReplaceReplacements(message)
                    ├── TargetingUtils.cleanAndReplaceReplacements(title)
                    ├── TargetingUtils.cleanAndReplaceReplacements(action URLs)
                    ├── If isMediaSourceRecommended:
                    │       └── TargetingUtils.cleanAndReplaceReplacements(recommendedText → media.url)
                    └── If isIconSourceRecommended:
                            └── TargetingUtils.cleanAndReplaceReplacements(recommendedIconText → icon.url)
```

### Replacement Processing Chain

```
TargetingUtils.cleanAndReplaceReplacements()
    │
    ├── getReplacedText(content)
    │       └── Returns content.replacements or content.text
    │
    ├── If ti.hasLiquid() && content.IS_LIQUID:
    │       └── doLiquidReplacements(ti, keyPrefix, text, profile, isPreview)
    │
    └── If ti.replacements() exists:
            └── doReplacements(text, profile, useFallback, doURIEncode, isPreview)
                    └── Regex pattern matching for $bucket:propId$ syntax
```

## Key Files Reference

| File | Location | Purpose |
|------|----------|---------|
| `NativeDisplayEvalManager.java` | `core/lc/src/java/com/wizrocket/lc/targeting/` | Main content preparation orchestration |
| `TargetingInfo.java` | `core/dao/src/com/wizrocket/db/dto/` | Content storage and retrieval |
| `InboxAdViewUtils.java` | `core/dao/src/com/wizrocket/db/utils/` | Message filling and personalization |
| `TargetingUtils.java` | `core/dao/src/com/wizrocket/db/orchestration/utils/` | Replacement execution |
| `ReplacementsUtils.java` | `core/dao/src/com/wizrocket/db/utils/` | Recommendation replacements |
| `Recommendation.java` | `core/utils/src/com/wizrocket/utils/` | Event property to ID mapping |

## Where to Make Changes

Based on the flow above, if you need to modify content preparation:

1. **Change message retrieval logic** → `TargetingInfo.message()` or `TargetingInfo.get()`
2. **Add new content fields** → `InboxAdViewUtils.fillMessage()`
3. **Modify replacement syntax** → `TargetingUtils.doReplacements()`
4. **Add new personalization source** → `ReplacementsUtils.prepareReplacements()`
5. **Change response structure** → `NativeDisplayEvalManager.getNativeDisplayNotificationContent()`
6. **Add new KV processing** → `InboxAdViewUtils.processCustomKVPairs()`
