# 6. The Three Encoder Operations

All three operations follow the same pattern: **walk the blob, selectively copy notifications into a scratch buffer, return the buffer as the new blob**.

## The Scratch Buffer

**File**: `core/inboxstore/src/com/wizrocket/inboxstore/Protocol/Notification.java:68-72`

```java
public static class Encoder {
    private byte[] b = null;                        // input: current Redis blob
    private byte[] buffer = new byte[MAX_BYTE_ARRAY]; // scratch: 10,000 bytes pre-allocated
    private int idx = 0;                            // write cursor into buffer
```

Every operation reads from `b`, writes into `buffer`, then creates a right-sized `byte[]` from `buffer[0..idx-1]`.

---

## Operation 1: `addNotification()` — Add with Dedup + Overflow

**File**: `core/inboxstore/src/com/wizrocket/inboxstore/Protocol/Notification.java:199-309`

```java
public byte[] addNotification(int targetId, int epoch, int profileVariant,
        BasicDBObject personalisations, boolean override, int maxNotifications)
```

Does **five things** in one method.

### Phase 1: Validation (lines 202-211)

```java
if (personalisations != null && !personalisations.isEmpty()
        && personalisations.size() > MAX_PERSONALISATIONS_COUNT) {   // > 255
    throw MAX_PERSONALIZATION_EXC;
}
if (profileVariant > MAX_PROFILE_VARIANT) {   // > 255
    throw EXCEEDED_MAX_PROFILE_VARIANT;
}
if (maxNotifications <= 0) {
    throw MAX_NOTIFICATIONS_INVALID;
}
```

### Phase 2: Scan for existing notification (lines 219-236)

```java
while (start < b.length) {
    int absTargetIdFromStorage = Math.abs(Utils.decodeInt(b, start));
    int absEpochFromStorage = Math.abs(Utils.decodeInt(b, start + LENGTH_TARGET_ID));
    int endIdxOfCurrent = endIdxForNotification(b, start);

    if (targetId == absTargetIdFromStorage
            && (override || epoch == absEpochFromStorage)) {
        startIdxOfExisting = start;
        endIdxOfExisting = endIdxOfCurrent;
    } else {
        count++;
    }
    start = endIdxOfCurrent + 1;
}
```

**Dedup rules:**

| `override` | Condition | Meaning |
|-----------|-----------|---------|
| `true` | Same targetId, ANY epoch | **Recurring campaign**: replaces previous dispatch |
| `false` | Same targetId AND same epoch | **Exact duplicate**: dedup by (targetId + epoch) |
| `false` | Same targetId, different epoch | **NOT replaced**: both kept |

The `override` flag comes from `isTargetRecurringType` in the dispatcher.

### Phase 3: Remove duplicate from blob (lines 238-249)

```java
// Copy bytes BEFORE the found notification
if (startIdxOfExisting != 0) {
    System.arraycopy(b, 0, buffer, 0, startIdxOfExisting);
    idx = startIdxOfExisting;
}
// Copy bytes AFTER the found notification
if (endIdxOfExisting != b.length - 1) {
    System.arraycopy(b, endIdxOfExisting + 1, buffer, idx,
            b.length - (endIdxOfExisting + 1));
    idx += b.length - (endIdxOfExisting + 1);
}
```

Visualization:
```
Original: [AAAA][BBBBBB][CCCC]     (replacing B)
Step 1: Copy [AAAA] to buffer
Step 2: Copy [CCCC] to buffer
Buffer:   [AAAA][CCCC]
```

### Phase 4: Encode and append new notification (lines 251-285)

```java
// targetId -- negate if has personalisations
Utils.encodeInt(hasPersonalisations ? -targetId : targetId, buffer, idx);
idx += 4;

// epoch -- negate if has variant
Utils.encodeInt(profileVariant >= 0 ? -epoch : epoch, buffer, idx);
idx += 4;

// variant byte (only if >= 0)
if (profileVariant >= 0) {
    buffer[idx] = (byte) profileVariant;
    idx += 1;
}

// personalisations (only if present)
if (personalisations != null && personalisations.size() > 0) {
    buffer[idx] = (byte) personalisations.size();
    idx += 1;
    for (String key : personalisations.keySet()) {
        Utils.encodeShortUnsigned(Integer.parseInt(key), buffer, idx);
        idx += 2;
        byte[] value = personalisations.getString(key, "").getBytes(Utils.UTF8_CHARSET);
        buffer[idx] = (byte) value.length;
        idx += 1;
        System.arraycopy(value, 0, buffer, idx, value.length);
        idx += value.length;
    }
}
```

### Phase 5: Overflow handling (lines 287-305)

```java
int exceededCount = count + 1 - maxNotifications;  // +1 for new
if (exceededCount <= 0) {
    // Everything fits
    res = new byte[idx];
    System.arraycopy(buffer, 0, res, 0, idx);
} else {
    // Drop OLDEST from the FRONT
    int startFromBuffer = 0;
    while (exceededCount != 0) {
        startFromBuffer = endIdxForNotification(buffer, startFromBuffer) + 1;
        exceededCount--;
    }
    res = new byte[idx - startFromBuffer];
    System.arraycopy(buffer, startFromBuffer, res, 0, idx - startFromBuffer);
}
```

Oldest notifications are at the front (written first). New ones appended at end. Dropping from front = dropping oldest.

```
maxNotifications = 3, existing = [A][B][C], new = D
buffer: [A][B][C][D]   exceededCount = 3+1-3 = 1
Drop A: startFromBuffer -> start of B
Result: [B][C][D]
```

---

## Operation 2: `removeTargetsFromNotification()` — Delete Specific

**File**: `core/inboxstore/src/com/wizrocket/inboxstore/Protocol/Notification.java:94-128`

Called by LC after delivering notifications to the SDK.

```java
public byte[] removeTargetsFromNotification(
        final List<RedisTargetAndEpoch> targetIdWithEpochs)
```

Walks the blob, copies everything EXCEPT matching notifications:

```java
while (start < b.length) {
    int absTargetIdFromStorage = Math.abs(Utils.decodeInt(b, start));
    int absEpochFromStorage = Math.abs(Utils.decodeInt(b, start + LENGTH_TARGET_ID));
    targetAndEpochFromStorage = new RedisTargetAndEpoch(absTargetIdFromStorage, absEpochFromStorage);
    endIdxOfCurrent = endIdxForNotification(b, start);

    if (!targetIdWithEpochs.contains(targetAndEpochFromStorage)) {
        System.arraycopy(b, start, buffer, idx, endIdxOfCurrent - start + 1);
        idx += endIdxOfCurrent - start + 1;
        count++;
    }
    start = endIdxOfCurrent + 1;
}
```

**Match uses `RedisTargetAndEpoch.equals()`** (`RedisTargetAndEpoch.java:23-33`) — checks BOTH targetId AND epoch:
```java
return (this.targetId == objTargetId && this.epochTs == objEpochTs);
```

So deleting `(targetId=42, epoch=1709596800)` will NOT delete `(targetId=42, epoch=1709600000)`.

If all notifications deleted: returns `new byte[0]` (empty, not null).

---

## Operation 3: `deleteExpiredNotifications()` — TTL Cleanup

**File**: `core/inboxstore/src/com/wizrocket/inboxstore/Protocol/Notification.java:311-410`

Called by `DeleteExpiryCron`. Most complex because of multiple TTL scenarios.

```java
public byte[] deleteExpiredNotifications(HashMap<Integer, Object> ttlMap, int maxTtl)
```

### The `ttlMap` structure

```
key > 0  -> value is int (TTL seconds) for regular inbox campaign
             value = 0 means infinite TTL
key < 0  -> push amplification target:
             value is int: pushAmp-only TTL
             value is int[]: push+inbox linked ([0]=TTL, [1..N]=associated targets)
```

### Copy strategy: range-based lazy copy

Instead of copying notification-by-notification, tracks ranges of consecutive survivors:

```java
int idxToCopyFrom = 0;  // start of current "surviving range"
int start = 0;

// When deleting:
if (delete) {
    if (start != idxToCopyFrom) {
        System.arraycopy(b, idxToCopyFrom, buffer, idx, start - idxToCopyFrom);
        idx += start - idxToCopyFrom;
    }
    idxToCopyFrom = endOfCurrentNotification + 1;  // skip past deleted
}
```

Visualization:
```
Blob: [A][B][C][D][E]    Delete B and D:

A: keep     -> extend range
B: delete   -> copy [A] to buffer, skip B
C: keep     -> extend range
D: delete   -> copy [C] to buffer, skip D
E: end      -> copy [E] to buffer

Buffer: [A][C][E]
```

### Deletion decision tree

```java
boolean isStopped = DeleteExpiryCron.stoppedTargets.contains(targetId);
boolean delete = isStopped || (epoch + maxTtl < now);  // safety net

if (ttlMap.containsKey(targetId)) {
    // Regular inbox -- per-campaign TTL
    if (delete || ((int) ttlMap.get(targetId) != 0
            && (epoch + (int) ttlMap.get(targetId) < now))) {
        delete = true;
    }
} else if (ttlMap.containsKey(-targetId)) {
    // Push amplification
    Object val = ttlMap.get(-targetId);
    if (val instanceof Integer) {
        // Pure pushAmp
        if ((int) val != 0 && epoch + (int) val < now) delete = true;
    } else {
        // Push+Inbox linked
        int[] arr = (int[]) val;
        if (arr[0] != 0 && epoch + arr[0] < now) delete = true;
    }
} else {
    // Orphaned -- kept until maxTtl
    DeleteExpiryCron.notFoundTargetIds.add(targetId);
}
```

### Corruption guard (unique to this operation)

```java
try {
    endOfCurrentNotification = endIdxForNotification(b, start);
} catch (ArrayIndexOutOfBoundsException e) {
    Profiler.report("end idx exceeds the array size, deleting the remaining bytes");
    break;   // truncate at corruption point
}
```

### Return value

Returns `null` (not empty byte array) if all notifications deleted. Caller deletes the Redis key entirely.

---

## Summary: Three Operations Compared

| Aspect | `addNotification` | `removeTargets` | `deleteExpired` |
|--------|-------------------|-----------------|-----------------|
| **Called by** | NB (dispatch) | LC (after delivery) | DeleteExpiryCron |
| **Match criteria** | targetId + (override OR same epoch) | targetId + epoch (exact) | TTL expiry, stopped campaigns |
| **Copy strategy** | Copy before + after match, append new | Copy non-matching one by one | Range-based lazy copy |
| **Overflow handling** | Yes -- drops oldest from front | No | No |
| **Corruption guard** | No | No | Yes -- catches ArrayIndexOutOfBounds |
| **Empty result** | Never | `new byte[0]` | `null` |
| **Dedup** | Yes | N/A | N/A |
