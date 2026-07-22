# 2. Redis Key Encoding

## What Identifies a User's Inbox

The Redis key answers: **"Which device's inbox am I looking at?"**

Two pieces of information form the key:
- **Namespace** — identifies the account (CleverTap customer). Each account gets a unique namespace number.
- **IID** — identifies the device within that account.

The key is pure binary — not a string like `"account:12:iid:98765"`. This saves bytes since Redis stores millions of these keys.

## The `getRedisKey()` Function

**File**: `core/inboxstore/src/com/wizrocket/inboxstore/Utils.java:67`

```java
public static byte[] getRedisKey(int namespace, long iid)
```

This function produces a byte array by concatenating the namespace bytes followed by the IID bytes. Both parts have **variable lengths**.

## Namespace Encoding (Variable: 0-3 bytes)

**File**: `core/inboxstore/src/com/wizrocket/inboxstore/Utils.java:43-65`

```java
private static byte[] getRedisKey(int namespace, long iid, EncodeKey encoder, int size) {
    if (namespace == 0) {
        // 0 namespace == blank -- no prefix bytes at all
        redisKeyBytes = new byte[size];
        encoder.encode(iid, redisKeyBytes, 0);     // IID starts at offset 0
    } else if (namespace <= 255) {
        // 1 byte prefix
        redisKeyBytes = new byte[size + 1];
        redisKeyBytes[0] = (byte) namespace;
        encoder.encode(iid, redisKeyBytes, 1);
    } else if (namespace <= 65535) {
        // 2 byte prefix, big-endian
        redisKeyBytes = new byte[size + 2];
        Utils.encodeShortUnsigned(namespace, redisKeyBytes, 0);
        encoder.encode(iid, redisKeyBytes, 2);
    } else {  // namespace <= 16777216
        // 3 byte prefix, big-endian
        redisKeyBytes = new byte[size + 3];
        Utils.encode3BytesUnsigned(namespace, redisKeyBytes, 0);
        encoder.encode(iid, redisKeyBytes, 3);
    }
    return redisKeyBytes;
}
```

**Concrete examples:**

```
namespace=0:    No bytes. Key starts directly with IID.
namespace=12:   1 byte: [0C]
namespace=300:  2 bytes: [01 2C]
namespace=100000: 3 bytes: [01 86 A0]
```

## How `encodeShortUnsigned` Works

**File**: `core/inboxstore/src/com/wizrocket/inboxstore/Utils.java:94-98`

```java
public static void encodeShortUnsigned(int i, byte[] d, int off) {
    d[off]     = (byte) (i >> 8);   // high byte
    d[off + 1] = (byte) i;          // low byte
}
```

Standard big-endian encoding. For `i = 300`:
```
300 in binary: 00000001 00101100
i >> 8:        00000001          -> byte = 0x01
(byte) i:      00101100          -> byte = 0x2C
Result: [01][2C]
```

Decoding (`Utils.java:101-103`):
```java
public static int decodeShortUnsigned(byte[] d, int off) {
    return (((d[off] & 0xFF) << 8) | (d[off + 1] & 0xFF)) & 0xFFFF;
}
```

The `& 0xFF` masks are needed because Java bytes are signed (-128 to 127). Without masking, a byte like `0x9C` would sign-extend to `0xFFFFFF9C` when promoted to int.

## IID Encoding (Variable: 4 or 8 bytes)

**File**: `core/inboxstore/src/com/wizrocket/inboxstore/Utils.java:67-88`

```java
public static byte[] getRedisKey(int namespace, long iid) {
    if (iid <= Integer.MAX_VALUE) {
        // Fits in 4 bytes -- use int encoding
        Utils.encodeInt((int) iid, redisKeyBytes, offset);
    } else {
        // Needs 8 bytes -- use long encoding
        Utils.encodeLong(iid, redisKeyBytes, offset);
    }
}
```

## How `encodeInt` Works

**File**: `core/inboxstore/src/com/wizrocket/inboxstore/Utils.java:117-122`

```java
public static void encodeInt(int i, byte[] d, int off) {
    d[off]     = (byte) (i >> 24);  // byte 3 (most significant)
    d[off + 1] = (byte) (i >> 16);  // byte 2
    d[off + 2] = (byte) (i >> 8);   // byte 1
    d[off + 3] = (byte) i;          // byte 0 (least significant)
}
```

For `iid = 98765`:
```
98765 in hex: 0x000181CD
Result: [00 01 81 CD]
```

Decoding (`Utils.java:129-132`):
```java
public static int decodeInt(byte[] d, int off) {
    return d[off] << 24
         | (d[off + 1] & 0xFF) << 16
         | (d[off + 2] & 0xFF) << 8
         | (d[off + 3] & 0xFF);
}
```

**Critical detail**: `d[off]` (first byte) is NOT masked with `& 0xFF`. This preserves the sign bit — essential for how the notification protocol uses negative integers as boolean flags.

## Complete Key Examples

**Example 1** — namespace=5, iid=12345:
```
namespace=5: 1 byte  -> [05]
iid=12345:   4 bytes -> [00 00 30 39]
Key: [05] [00 00 30 39]
Total: 5 bytes
```

**Example 2** — namespace=0, iid=12345:
```
namespace=0: 0 bytes (blank)
iid=12345:   4 bytes -> [00 00 30 39]
Key: [00 00 30 39]
Total: 4 bytes
```

**Example 3** — namespace=300, iid=5000000000 (large IID):
```
namespace=300:      2 bytes -> [01 2C]
iid=5000000000:     8 bytes -> [00 00 00 01 2A 05 F2 00]
Key: [01 2C] [00 00 00 01 2A 05 F2 00]
Total: 10 bytes
```

## Why Variable-Length Keys?

From `InboxClientSync.java:22-33`:

```java
// We have an Async client in LC and Jedis is not Async. So, Lettuce is used to make
// async get calls there. Therefore, not using redis namespaces concept as lettuce
// select method uses "await" and therefore blocks.
// So appending one byte as prefix to store the namespace with each iid in redis.

// The advantage of giving each account a separate namespace irrespective of whether
// they have same redis or not is that while moving accounts, we don't need to worry
// about changing the namespaces in mongo for the account.
```

Two reasons:
1. **Lettuce (LC's Redis client) doesn't support Redis `SELECT` well in async mode** — so instead of using Redis database namespaces, they prefix the key with account namespace bytes.
2. **Account migration** — when moving an account from one Redis to another, the namespace stays the same. Keys can be migrated as-is.

## The Key is Write-Only

The key format is **ambiguous on the reader side** (variable namespace + variable IID lengths). But it doesn't matter because:
- The writer and reader always know the account's namespace from MongoDB (`AccountMetaDao.InboxStoreConfig.getNamespace()`)
- The IID is always known from the request context
- Keys are never "decoded" — they're only constructed for GET/SET operations

The key is essentially a **write-only format** — generated to address the Redis slot, but never parsed back.
