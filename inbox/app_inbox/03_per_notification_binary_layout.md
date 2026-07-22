# 3. Per-Notification Binary Layout

## The Core Problem

Each notification needs to store these fields:

| Field | Always present? | Typical value |
|-------|----------------|---------------|
| targetId | Yes | Campaign ID, e.g. 42 |
| epoch | Yes | Unix timestamp when NB dispatched it |
| profileVariant | Sometimes | A/B test variant or profile variant ID. Default = -1 (none) |
| personalisations | Sometimes | Event properties stored for Liquid replacement, e.g. `{"5": "Hello"}` |

The challenge: **how to encode optional fields without wasting bytes** when they're absent.

## The Sign-Bit Trick — The Central Design Decision

**File**: `core/inboxstore/src/com/wizrocket/inboxstore/Protocol/Notification.java:26-31`

Instead of adding a boolean byte for each optional field, the protocol **steals the sign bit** of an existing required field:

```
targetId field:
  Stored as POSITIVE -> "no personalisations follow"
  Stored as NEGATIVE -> "personalisations block follows"
  Actual targetId recovered via Math.abs()

epoch field:
  Stored as POSITIVE -> "no profileVariant follows"
  Stored as NEGATIVE -> "profileVariant byte follows"
  Actual epoch recovered via Math.abs()
```

Java's `int` is 32 bits. The MSB (bit 31) is the sign bit:
```
Positive int:  0xxxxxxx xxxxxxxx xxxxxxxx xxxxxxxx  (bit 31 = 0)
Negative int:  1xxxxxxx xxxxxxxx xxxxxxxx xxxxxxxx  (bit 31 = 1)
```

### Why This Works (For Now)

**For targetId:** Campaign IDs are positive integers (1-10,000,000). Negating them is always safe.

**For epoch:** Unix timestamps are currently ~1.7 billion. Max positive int32 is 2,147,483,647 = **January 19, 2038 03:14:07 UTC**. After 2038, real epochs will overflow into negative territory, and the decoder will misinterpret every notification as having a profileVariant. **This is a ticking time bomb.**

### How `encodeInt` Handles Negative Numbers

For `targetId = 42`, negation trace:

```
+42 in binary:  00000000 00000000 00000000 00101010

-42 in two's complement:
  Invert:       11111111 11111111 11111111 11010101
  Add 1:        11111111 11111111 11111111 11010110

encodeInt(-42) -> [FF FF FF D6]
decodeInt([FF FF FF D6]) -> -42
Math.abs(-42) -> 42
```

**Critical detail about `decodeInt`** (`Utils.java:129-132`): The first byte `d[off]` is NOT masked with `& 0xFF`. This preserves sign extension, making negative numbers decode correctly.

## Field Size Constants

**File**: `core/inboxstore/src/com/wizrocket/inboxstore/Protocol/Notification.java:41-47`

```java
private static final short LENGTH_TARGET_ID = 4;               // int
private static final short LENGTH_EPOCH = 4;                    // int
private static final short LENGTH_PROFILE_VARIANT = 1;          // byte
private static final short LENGTH_PERSONALISATIONS_COUNT = 1;   // byte
private static final short LENGTH_PERSONALISATION_ID = 2;       // short
private static final short LENGTH_PERSONALISATION_STRING_SIZE = 1; // byte
```

## The Four Possible Notification Shapes

### Shape A: No variant, no personalisations (8 bytes minimum)

```
[+targetId: 4B] [+epoch: 4B]
```

Example — targetId=42, epoch=1709596800:
```
[00 00 00 2A] [65 E5 D8 00]
```

### Shape B: Has variant, no personalisations (9 bytes)

```
[+targetId: 4B] [-epoch: 4B] [variant: 1B]
```

Example — targetId=42, epoch=1709596800, variant=3:
```
[00 00 00 2A] [9A 1A 28 00] [03]
 +42           -1709596800    v=3
```

### Shape C: No variant, has personalisations (10+ bytes)

```
[-targetId: 4B] [+epoch: 4B] [count: 1B] [pers_0]...[pers_N]
```

Each personalisation entry:
```
[propId: 2B unsigned short] [valueLen: 1B unsigned byte] [value: valueLen bytes UTF-8]
```

Example — targetId=100, epoch=1709596800, personalisations=`{"5":"Hello","9":"World"}`:
```
[FF FF FF 9C] [65 E5 D8 00] [02] [00 05] [05] [48 65 6C 6C 6F] [00 09] [05] [57 6F 72 6C 64]
 -100          +1709596800    cnt   id=5   len=5  "Hello"          id=9   len=5  "World"
```
Total: 25 bytes

### Shape D: Has variant AND personalisations (11+ bytes)

```
[-targetId: 4B] [-epoch: 4B] [variant: 1B] [count: 1B] [pers_0]...[pers_N]
```

Example — targetId=42, epoch=1709596800, variant=7, personalisations=`{"12":"abc"}`:
```
[FF FF FF D6] [9A 1A 28 00] [07] [01] [00 0C] [03] [61 62 63]
 -42           -1709596800    v=7  cnt   id=12  len=3  "abc"
```
Total: 16 bytes

## Complete Field Layout Reference

```
NOTIFICATION :=
    +------------------------------------------------------------+
    | FIELD           SIZE      ENCODING          ALWAYS PRESENT? |
    +------------------------------------------------------------+
    | targetId        4 bytes   big-endian int32  YES              |
    |                           sign bit = has personalisations    |
    |                                                             |
    | epoch           4 bytes   big-endian int32  YES              |
    |                           sign bit = has profileVariant      |
    |                                                             |
    | profileVariant  1 byte    unsigned byte     ONLY IF epoch<0  |
    |                           (0-255)                            |
    |                                                             |
    | persCount       1 byte    unsigned byte     ONLY IF tgtId<0  |
    |                           (0-255)                            |
    |                                                             |
    | personalisations[0..persCount-1]:           ONLY IF tgtId<0  |
    |   +- propId     2 bytes   big-endian uint16                  |
    |   +- valueLen   1 byte    unsigned byte (0-255)              |
    |   +- value      N bytes   UTF-8 string (N = valueLen)        |
    +------------------------------------------------------------+
```

## Personalisation Encoding Limitations

| Constraint | Limit | Why |
|-----------|-------|-----|
| Keys must be numeric | 0-65535 | `Integer.parseInt(key)` then `encodeShortUnsigned()` |
| Values must be strings | N/A | `personalisations.getString(key)` — no numbers, booleans, or nested objects |
| Value max length | 255 bytes | 1 byte for `valueLen` |
| Max personalisations | 255 | 1 byte for `persCount` |
| No nesting | N/A | Flat `{id: value}` only |

The dispatcher checks value size before storing (`NotificationInboxDispatcher.java:61-63`):
```java
static boolean isSizePermissible(int size, int permissibleSizeInBytes) {
    return size < Math.pow(2, permissibleSizeInBytes * 8.0);
    // permissibleSizeInBytes = 1 -> size < 256
}
```
Values of 256+ bytes are **silently dropped** — not truncated, not stored at all.
