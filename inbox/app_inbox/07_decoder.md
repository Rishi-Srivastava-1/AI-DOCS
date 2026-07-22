# 7. The Decoder

## What the Decoder Does

The Decoder takes a raw byte array from Redis and produces a list of structured notification objects that LC can work with.

```
Input:  byte[] from Redis (raw concatenated notifications)
Output: BasicDBList of BasicDBObject (each representing one notification)
```

Used exclusively on the **LC side** — when a user opens the app and LC fetches their inbox from Redis.

## The Main Method

**File**: `core/inboxstore/src/com/wizrocket/inboxstore/Protocol/Notification.java:432-492`

```java
public BasicDBList getNotificationData(HashSet<Integer> excludeTargetIds) {
    if (b == null || b.length == 0) {
        return null;   // no inbox messages -- returns null, not empty list
    }

    BasicDBList notificationData = new BasicDBList();
    int start = 0;

    while (start < b.length) {
        BasicDBObject notification = new BasicDBObject();

        // Step 1: Read targetId (always 4 bytes)
        int targetIdFromStorage = Utils.decodeInt(b, start);
        int targetId = Math.abs(targetIdFromStorage);
        notification.append(TARGET_ID, targetId);
        start += LENGTH_TARGET_ID;

        // Step 2: Read epoch (always 4 bytes)
        int epochFromStorage = Utils.decodeInt(b, start);
        notification.append(EPOCH, Math.abs(epochFromStorage));
        start += LENGTH_EPOCH;

        // Step 3: Conditionally read profileVariant (0 or 1 byte)
        if (epochFromStorage < 0) {
            notification.append(PROFILE_VARIANT, Utils.decodeUnsignedByte(b, start));
        }
        start += epochFromStorage < 0 ? LENGTH_PROFILE_VARIANT : 0;

        // Step 4: Conditionally read personalisations (variable length)
        if (targetIdFromStorage < 0) {
            int personalisationsCount = Utils.decodeUnsignedByte(b, start);
            start += LENGTH_PERSONALISATIONS_COUNT;

            BasicDBObject personalisations = new BasicDBObject();
            for (int i = 0; i < personalisationsCount; i++) {
                int id = Utils.decodeShortUnsigned(b, start);
                start += LENGTH_PERSONALISATION_ID;

                int stringLength = Utils.decodeUnsignedByte(b, start);
                start += LENGTH_PERSONALISATION_STRING_SIZE;

                String value = new String(
                        Arrays.copyOfRange(b, start, start + stringLength),
                        Utils.UTF8_CHARSET);
                start += stringLength;

                personalisations.append(Integer.toString(id), value);
            }
            notification.append(PERSONALISATIONS, personalisations);
        }

        // Step 5: Filter and collect
        if (excludeTargetIds == null || !excludeTargetIds.contains(targetId)) {
            notificationData.add(notification);
        }
    }

    return notificationData;
}
```

## Step-by-Step Output

For each notification, the decoder produces:

```java
// With variant and personalisations:
{
    "targetId": 42,
    "epoch": 1709596800,
    "profileVariant": 3,
    "personalisations": {
        "5": "Hello",
        "9": "World"
    }
}

// Without variant, without personalisations:
{
    "targetId": 42,
    "epoch": 1709596800
}
// Note: "profileVariant" and "personalisations" keys are ABSENT, not null
```

## The `excludeTargetIds` Parameter

Lets LC skip certain campaigns without modifying the byte array:
- **Stopped campaigns**: LC knows a campaign was stopped from `TargetingDto` cache but Redis hasn't been cleaned yet
- **Already processed**: Prevents double-processing in edge cases

If `null`, all notifications are included.

## How LC Consumes the Output

**File**: `core/lc/src/java/com/wizrocket/lc/targeting/InboxManager.java:568-577`

```java
BasicDBObject eventPersonalizations = rresp.personalizations;

// Inject personalisations into profile
if (eventPersonalizations != null) {
    profile.append(ProfilesDao.EVT, eventPersonalizations);
}

// Render message content using profile (with personalisations injected)
BasicDBObject content = getInboxNotificationContent(ti, device, messageKey, profile, ...);
inboxNotificationList.add(content);

// Schedule deletion from Redis
container.addInboxStoreTargetForDeletion(targetId, epoch);
```

## What the Decoder Does NOT Do

| Thing | Who does it instead |
|-------|-------------------|
| Check TTL expiry | `InboxManager` checks `epoch + ttl < now` |
| Filter stopped campaigns | `InboxManager` checks against `TargetingDto` |
| Validate data integrity | Nobody -- corrupt data crashes the parser |
| Order notifications | `InboxManager` processes newest-first |
| Limit result count | `InboxManager` enforces `maxInboxTargets` |
| Resolve personalisation IDs to names | Liquid template engine via event master lookup |
| Remove delivered notifications | Separate `removeTargetsFromNotification` call |

The Decoder is a pure **byte-to-object translator**. All business logic lives in `InboxManager`.

## Corruption Behavior

The Decoder has **zero error handling**:

```java
// No try-catch anywhere in the decode loop
int targetIdFromStorage = Utils.decodeInt(b, start);  // can ArrayIndexOutOfBoundsException
String value = new String(Arrays.copyOfRange(b, start, start + stringLength));  // can too
```

| Corruption | Effect |
|-----------|--------|
| Truncated blob | `ArrayIndexOutOfBoundsException` -- entire request fails |
| Inflated `personalisationsCount` | Reads garbage, eventually ArrayIndexOutOfBounds |
| Inflated `stringLength` | Reads past array end |
| Bit flip in targetId sign bit | **Silent misparse** -- thinks personalisations exist when they don't, cascading misalignment |
| Bit flip in epoch sign bit | **Silent misparse** -- reads 1 extra byte, all subsequent notifications shifted |

Sign-bit corruption is most dangerous: no crash, just **silently wrong data**.

## Decoder vs Encoder Asymmetry

| Aspect | Encoder | Decoder |
|--------|---------|--------|
| **ThreadLocal** | Yes -- reuses 10KB scratch buffer | Yes -- but no buffer (lightweight) |
| **Mutable state** | `b`, `buffer`, `idx` | Only `b` |
| **Error handling** | `deleteExpiredNotifications` catches ArrayIndexOutOfBounds | None |
| **Output** | `byte[]` | `BasicDBList` of `BasicDBObject` |
| **Used by** | NB + DeleteExpiryCron | LC only |
