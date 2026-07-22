# 5. How `endIdxForNotification()` Navigates the Blob

## Why This Function Exists

The byte array has **no delimiters, no separators, no length prefixes** between notifications. Every operation needs to answer: **"Where does this notification end and the next one begin?"**

There's no way to jump to the Nth notification. You must **parse from the start**, walking through each notification's variable-length fields. This is what `endIdxForNotification()` does.

## The Function

**File**: `core/inboxstore/src/com/wizrocket/inboxstore/Protocol/Notification.java:163-190`

```java
private int endIdxForNotification(byte[] bArray, int start) {
    int end = start;

    int targetIdFromStorage = Utils.decodeInt(bArray, end);
    end += LENGTH_TARGET_ID;       // end += 4

    int epochFromStorage = Utils.decodeInt(bArray, end);
    end += LENGTH_EPOCH;           // end += 4

    // +ve epoch means no profileVariant
    if (epochFromStorage < 0) {
        end += LENGTH_PROFILE_VARIANT;   // end += 1
    }

    // positive targetId means there is no personalisation.
    if (targetIdFromStorage < 0) {
        int personalisationsCount = Utils.decodeUnsignedByte(bArray, end);
        end += LENGTH_PERSONALISATIONS_COUNT;   // end += 1

        for (int i = 0; i < personalisationsCount; i++) {
            end += LENGTH_PERSONALISATION_ID;   // end += 2 (propId)
            int stringLength = Utils.decodeUnsignedByte(bArray, end);
            end += LENGTH_PERSONALISATION_STRING_SIZE + stringLength;  // end += 1 + N
        }
    }

    return end - 1;    // INCLUSIVE last byte index
}
```

**Returns**: The index of the **last byte** of the notification (inclusive). Next notification starts at `returnValue + 1`.

## Tracing Through All Four Shapes

### Shape A: No variant, no personalisations (8 bytes)

```
Bytes: [00 00 00 2A] [65 E5 D8 00]
Index:  0  1  2  3    4  5  6  7

start = 0, end = 0
  decodeInt(0) -> +42 (positive)    end = 4
  decodeInt(4) -> +1709596800       end = 8
  epoch >= 0 -> skip variant
  targetId >= 0 -> skip pers
return 8 - 1 = 7
```

### Shape B: Has variant, no personalisations (9 bytes)

```
Bytes: [00 00 00 2A] [9A 1A 28 00] [03]
Index:  0  1  2  3    4  5  6  7    8

start = 0, end = 0
  decodeInt(0) -> +42               end = 4
  decodeInt(4) -> -1709596800       end = 8
  epoch < 0 -> end += 1             end = 9
  targetId >= 0 -> skip pers
return 9 - 1 = 8
```

### Shape C: No variant, has personalisations

```
Bytes: [FF FF FF 9C] [65 E5 D8 00] [02] [00 05] [05] [48 65 6C 6C 6F] [00 09] [05] [57 6F 72 6C 64]
Index:  0  1  2  3    4  5  6  7    8    9  10   11   12 13 14 15 16   17 18   19   20 21 22 23 24

start = 0, end = 0
  decodeInt(0) -> -100 (NEGATIVE)   end = 4
  decodeInt(4) -> +1709596800       end = 8
  epoch >= 0 -> skip variant
  targetId < 0 -> personalisations:
    persCount = 2                   end = 9
    i=0: end += 2 -> 11, strlen=5, end += 6 -> 17
    i=1: end += 2 -> 19, strlen=5, end += 6 -> 25
return 25 - 1 = 24
```

### Shape D: Has variant AND personalisations

```
Bytes: [FF FF FF D6] [9A 1A 28 00] [07] [01] [00 0C] [03] [61 62 63]
Index:  0  1  2  3    4  5  6  7    8    9    10 11   12   13 14 15

start = 0, end = 0
  decodeInt(0) -> -42 (NEGATIVE)    end = 4
  decodeInt(4) -> -1709596800       end = 8
  epoch < 0 -> end += 1             end = 9
  targetId < 0 -> personalisations:
    persCount = 1                   end = 10
    i=0: end += 2 -> 12, strlen=3, end += 4 -> 16
return 16 - 1 = 15
```

## Walking Multiple Notifications

The caller iterates through the entire blob:

```java
int start = 0;
while (start < b.length) {
    // ... process notification at 'start' ...
    start = endIdxForNotification(b, start) + 1;
}
```

Example with 3 notifications:
```
Notif A: 8 bytes  (offset 0-7)
Notif B: 16 bytes (offset 8-23)
Notif C: 9 bytes  (offset 24-32)
Total: 33 bytes

Iteration 1: start=0,  endIdx=7,  next start=8
Iteration 2: start=8,  endIdx=23, next start=24
Iteration 3: start=24, endIdx=32, next start=33
Iteration 4: start=33, 33 < 33 -> FALSE -> STOP
```

## The Sibling: `startIdxForTargetID()`

**File**: `core/inboxstore/src/com/wizrocket/inboxstore/Protocol/Notification.java:130-161`

Finds a notification by targetId. Walks notification by notification using the same field-by-field skip logic. Returns the **start** index if found, `-1` if not. This function is actually **unused** in the current codebase — `addNotification()` does its own inline scan.

## Corruption Behavior

If a byte gets corrupted (e.g., `valueLength` reads as 200 when actual value is 5 bytes), the cursor jumps past the array bounds:

```
end += 2 (propId) + 1 (len byte) + 200 (value) = 203 bytes forward
-> ArrayIndexOutOfBoundsException
```

Only `deleteExpiredNotifications()` catches this (`Notification.java:323`). The Decoder and other Encoder methods do NOT — corrupt data crashes the operation.

## Performance

Every operation is **O(N)** where N = total bytes in the blob. `endIdxForNotification()` is the **hottest function** in the protocol — called at least once per notification per operation.
