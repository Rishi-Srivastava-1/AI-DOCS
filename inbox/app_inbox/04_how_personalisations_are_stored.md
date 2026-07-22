# 4. How Personalisations Are Stored

## The Problem Personalisations Solve

A marketer creates an inbox campaign with a message like:

```
"Hi! You just purchased {{event.Product Name}} for {{event.Price}}."
```

The `{{event.Product Name}}` and `{{event.Price}}` are **Liquid template placeholders** that need real values. But there's a timing problem:

```
Time T1: User triggers "Charged" event with {Product Name: "iPhone", Price: "$999"}
         NB picks up user for inbox campaign
         NB has access to the event data RIGHT NOW

Time T2: User opens app (maybe hours/days later)
         LC needs to render the message with "iPhone" and "$999"
         But the original event data may be overwritten by newer events
```

**Solution**: NB extracts event property values at dispatch time (T1) and stores them alongside the notification in Redis. LC reads them back at delivery time (T2). These stored values are the **personalisations**.

## Phase 1: NB Builds the Personalisation Object

**File**: `core/nb/src/com/wizrocket/nb/dispatchers/notificationinbox/NotificationInboxDispatcher.java:182-251`

### Step 1: Identify which event properties the campaign template needs

```java
BasicDBObject replacements = ti.replacements();
// Example: {"e:5": "time", "e:9": "SDK Version", "w:language": "en"}

BasicDBList eventReplacementKeys = new BasicDBList();
for (String replacementKey : replacements.keySet()) {
    if (replacementKey.startsWith("e:")) {
        eventReplacementKeys.add(replacementKey.split(":")[1]);  // extract "5", "9"
    }
}
```

Only **event** properties (prefix `"e:"`) are extracted. Profile properties (`"w:"`, `"p:"`) are skipped — they'll be resolved fresh by LC.

A second source — `propIdVals` for online/trigger campaigns (`NotificationInboxDispatcher.java:319-326`):

```java
BasicDBObject propIdVals = (BasicDBObject) ti.getTgtObject()
        .getOrDefault(ID_CONTAINER_FOR_REPLACEMENTS, new BasicDBObject());
// Example: {"9": "SDK Version", "5": "time", "7": "Version", "11": "Make"}
for (String eventPropertyId : propIdVals.keySet()) {
    eventReplacementKeys.add(eventPropertyId);
}
```

### Step 2: Look up actual values from user's profile

```java
BasicDBObject eventPropertiesInProfile = message.getProfileDTO().getResolvedEventSummary();
// Example: {"5": "2024-03-05", "9": "40.0.3", "7": "18.2", "11": "Apple", "3": "WiFi"}
```

NB copies only needed values into `profileE` (`NotificationInboxDispatcher.java:292-313`):

```java
BasicDBObject profileE = new BasicDBObject();

for (Object erk : eventReplacementKeys) {
    String eventReplacementKey = (String) erk;
    Object value = eventPropertiesInProfile.get(eventReplacementKey);

    if (value != null) {
        if (isSizePermissible(value.toString().length(), 1)   // <= 255 bytes
                && !(value instanceof BasicDBList)) {          // not an array
            profileE.put(eventReplacementKey, value);
        } else {
            // SILENTLY DROPPED
            Profiler.report("Event property dropped...");
        }
    }
}
```

### Step 3: Add recommendation data (if applicable)

```java
// NotificationInboxDispatcher.java:207-225
if (!ti.getRecommendationRefs().isEmpty()) {
    BasicDBObject eventJson = message.getProfileDTO().getResolvedEventJson();
    BasicDBObject evtPropsJson = Recommendation.getEvtPropsForRecommendation(eventJson, ...);
    if (ti.attachEventProps()) {
        profileE.putAll((BSONObject) evtPropsJson);
    }
}
```

### Step 4: Add random push ID (if applicable)

```java
// NotificationInboxDispatcher.java:234-238
if (message.getRandomPushId() > 0) {
    profileE.put(MobilePushConstants.RANDOM_PUSH_IDENTIFIER, message.getRandomPushId());
}
```

### Final state of `profileE`

```java
{
    "5": "2024-03-05",         // event property: time
    "9": "40.0.3",             // event property: SDK Version
    "7": "18.2",               // event property: Version
    "11": "Apple",             // event property: Make
}
```

This is passed to `InboxClientSync.appendNotification()` as the `personalisations` parameter.

## Phase 2: Binary Encoding

**File**: `core/inboxstore/src/com/wizrocket/inboxstore/Protocol/Notification.java:251-284`

### Signal via targetId sign bit

```java
Utils.encodeInt(
    personalisations != null && personalisations.size() > 0 ? -targetId : targetId,
    buffer, idx);
```

### Write count and entries

```java
buffer[idx] = (byte) personalisations.size();   // count byte
idx += 1;

for (String key : personalisations.keySet()) {
    // Property ID as 2-byte unsigned short
    Utils.encodeShortUnsigned(Integer.parseInt(key), buffer, idx);
    idx += 2;

    // Value as length-prefixed UTF-8 string
    byte[] value = personalisations.getString(key, "").getBytes(Utils.UTF8_CHARSET);
    buffer[idx] = (byte) value.length;    // length byte
    idx += 1;
    System.arraycopy(value, 0, buffer, idx, value.length);  // value bytes
    idx += value.length;
}
```

### Concrete encoding example

`profileE = {"5": "2024-03-05", "9": "40.0.3"}`

```
Count: [02]

Entry "5" -> "2024-03-05":
  propId:   [00 05]
  valueLen: [0A] (10 bytes)
  value:    [32 30 32 34 2D 30 33 2D 30 35]

Entry "9" -> "40.0.3":
  propId:   [00 09]
  valueLen: [06] (6 bytes)
  value:    [34 30 2E 30 2E 33]
```

## Phase 3: Binary Decoding

**File**: `core/inboxstore/src/com/wizrocket/inboxstore/Protocol/Notification.java:460-483`

```java
if (targetIdFromStorage < 0) {
    int personalisationsCount = Utils.decodeUnsignedByte(b, start);
    start += 1;

    BasicDBObject personalisations = new BasicDBObject();
    for (int i = 0; i < personalisationsCount; i++) {
        int id = Utils.decodeShortUnsigned(b, start);   // propId
        start += 2;
        int stringLength = Utils.decodeUnsignedByte(b, start);  // value length
        start += 1;
        String value = new String(
            Arrays.copyOfRange(b, start, start + stringLength), Utils.UTF8_CHARSET);
        start += stringLength;
        personalisations.append(Integer.toString(id), value);  // key as string "5", "9"
    }
    notification.append(PERSONALISATIONS, personalisations);
}
```

## Phase 4: LC Uses Personalisations for Content Rendering

**File**: `core/lc/src/java/com/wizrocket/lc/targeting/InboxManager.java:780-792`

```java
public BasicDBObject getInboxNotificationContent(..., BasicDBObject personalisations, ...) {
    getRecommendationReplacements(ti, profile, personalisations, accountId);
    if (personalisations != null) {
        profile.append(ProfilesDao.EVT, personalisations);  // inject into profile.EVT
    }
    BasicDBObject inboxContent = getInboxNotificationContent(ti, device, messageKey, profile, ...);
    return inboxContent;
}
```

The Liquid engine resolves `{{event.Product Name}}` by:
1. Looking up "Product Name" in event master -> finds property ID `11`
2. Looking up `"11"` in `profile.EVT` -> finds `"Apple"`
3. Replaces the placeholder

## What's Stale vs What's Fresh

| Property type | Stored in Redis? | Resolved when? | Can be stale? | Why? |
|--------------|-----------------|----------------|---------------|------|
| **Event** (`{{event.X}}`) | Yes | NB dispatch time | Yes — intentionally | Notification is about THAT specific event |
| **Profile** (`{{profile.X}}`) | No | LC delivery time | No — always current | Profile props should reflect latest state |
| **Recommendations** | Partial context | LC delivery time | Results are fresh | LC calls recommendation engine at delivery time |

## The Data Flow Diagram

```
CAMPAIGN CREATION (Dashboard):
  Template: "Bought {{event.Product Name}} at {{event.Price}}"
  Event master maps: "Product Name" -> propId 11, "Price" -> propId 7
  TargetingInfo.replacements: {"e:11": "Product Name", "e:7": "Price"}

NB DISPATCH:
  User's profile.eventSummary: {"11": "iPhone", "7": "$999", "3": "WiFi", ...}
  Filter for needed keys:     eventReplacementKeys = ["11", "7"]
  Extract values:             profileE = {"11": "iPhone", "7": "$999"}

BINARY ENCODING:
  [02] [00 0B] [06] [iPhone bytes] [00 07] [04] [$999 bytes]

REDIS:
  SET [ns][iid] -> [...targetId...epoch...][personalisations]

LC DELIVERY:
  GET -> decode -> personalisations: {"11": "iPhone", "7": "$999"}
  Inject into profile.EVT
  Liquid render: "Bought iPhone at $999"
  Send to SDK
```

## What Can Go Wrong

| Issue | What happens |
|-------|-------------|
| Value > 255 bytes | `isSizePermissible` rejects it. Property silently dropped. Template shows raw placeholder. |
| Value is a `BasicDBList` (array) | Explicitly rejected. Dropped silently. |
| Key is not numeric | `Integer.parseInt(key)` throws `NumberFormatException`. Whole notification fails. |
| More than 255 properties | `MAX_PERSONALISATIONS_COUNT` check throws exception. Notification not stored. |
| Multi-byte UTF-8 | Length byte stores byte count, not character count. Emoji-heavy strings could exceed 255-byte limit. |
