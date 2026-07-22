# 2. Redis Key Encoding

## The Key Structure

**File**: `core/server-side-inbox/service/src/com/wizrocket/inbox/service/redis/RedisInboxKey.java`

```java
@Jacksonized @Builder @Data
public class RedisInboxKey {
    private final int namespace;
    private final long inboxId;
}
```

Two fields:
- **namespace** — identifies the account (from `InboxStoreConfig.getUnifiedInboxNamespace()`)
- **inboxId** — identifies the user (computed from IID/IDN)

## Key Encoding: VLQ (Variable-Length Quantity)

Unlike App Inbox which uses fixed-size encoding, Unified Inbox uses **VLQ** for both fields.

**File**: `core/server-side-inbox/service/src/com/wizrocket/inbox/service/redis/BinaryInboxCodec.java:54-65`

```java
@Override
public ByteBuffer encodeKey(RedisInboxKey inboxKey) {
    ByteArrayOutputStream out = new ByteArrayOutputStream();
    ExtendedDataOutputStream data = new ExtendedDataOutputStream(out);
    data.writeVLQ(inboxKey.getNamespace());
    data.writeVLQ(inboxKey.getInboxId());
    return ByteBuffer.wrap(out.toByteArray());
}
```

And decoding (`BinaryInboxCodec.java:28-38`):

```java
@Override
public RedisInboxKey decodeKey(ByteBuffer byteBuffer) {
    ExtendedDataInputStream in = new ExtendedDataInputStream(new ByteBufferInputStream(byteBuffer));
    return RedisInboxKey.builder()
            .namespace((int) in.readVLQ())
            .inboxId(in.readVLQ())
            .build();
}
```

## How VLQ Encoding Works

**File**: `core/server-side-inbox/service/src/com/wizrocket/inbox/service/serialization/ExtendedDataOutputStream.java:20-33`

```java
public void writeVLQ(long value) {
    if (value < 0) {
        throw new IllegalArgumentException(String.valueOf(value));
    }
    do {
        int sevenBits = (int) value & 0x7F;    // take lowest 7 bits
        value >>>= 7;                           // shift right
        if (value > 0) {
            sevenBits |= 0x80;                 // set continuation bit
        }
        out.write(sevenBits);
    } while (value > 0);
}
```

Each byte uses 7 bits for data + 1 bit (MSB) as continuation flag:
```
Value 0-127:        1 byte   [0xxxxxxx]
Value 128-16383:    2 bytes  [1xxxxxxx] [0xxxxxxx]
Value 16384+:       3+ bytes [1xxxxxxx] [1xxxxxxx] [0xxxxxxx]
```

Examples:
```
VLQ(1)   = [01]            (1 byte)
VLQ(127) = [7F]            (1 byte)
VLQ(128) = [80 01]         (2 bytes)
VLQ(300) = [AC 02]         (2 bytes)
VLQ(12345) = [B9 60]       (2 bytes)
```

Compare to App Inbox: namespace=300 uses 2 bytes in both systems, but IID=5 uses 4 bytes in App Inbox vs 1 byte in Unified Inbox (VLQ).

## The InboxId: IID vs IDN with LSB Tag

**File**: `core/server-side-inbox/service/src/com/wizrocket/inbox/service/redis/RedisInboxKeyFactory.java:15-23`

```java
public Optional<RedisInboxKey> create(UserKey key) {
    return inboxConfigProvider.get(key.getAccountId())
            .map(config -> RedisInboxKey.builder()
                    .namespace(config.getUnifiedInboxNamespace())
                    .inboxId(key.getIdn() == 0
                            ? (key.getIid() << 1) + 1    // iid mode: LSB = 1
                            : key.getIdn() << 1)          // idn mode: LSB = 0
                    .build());
}
```

The LSB (least significant bit) of `inboxId` encodes which identity type is used:

```
If user has IDN (identified user):
  inboxId = idn << 1        (LSB = 0)
  Example: idn=100 → inboxId = 200 (binary: ...11001000)

If user has only IID (anonymous):
  inboxId = (iid << 1) + 1  (LSB = 1)
  Example: iid=5 → inboxId = 11 (binary: ...1011)
```

Decoding back (`RedisInboxKeyFactory.java:25-29`):

```java
public UserKey create(int accountId, RedisInboxKey key) {
    return (key.getInboxId() & 1) != 0
            ? UserKey.builder().accountId(accountId).iid(key.getInboxId() >> 1).build()
            : UserKey.builder().accountId(accountId).idn(key.getInboxId() >> 1).build();
}
```

Check LSB: if 1 → it's an IID, shift right to recover. If 0 → it's an IDN.

**This enables user-level identity**: When a user logs in on a new device, their IDN is the same. Both devices map to the same `inboxId`, so they share one inbox in Redis.

## Complete Key Examples

**Example 1** — namespace=1, iid=5 (anonymous user):
```
inboxId = (5 << 1) + 1 = 11

Key bytes:
  VLQ(1)  = [01]
  VLQ(11) = [0B]
  Total: [01 0B] = 2 bytes
```

**Example 2** — namespace=1, idn=100 (identified user):
```
inboxId = 100 << 1 = 200

Key bytes:
  VLQ(1)   = [01]
  VLQ(200) = [C8 01]
  Total: [01 C8 01] = 3 bytes
```

**Example 3** — namespace=300, idn=50000:
```
inboxId = 50000 << 1 = 100000

Key bytes:
  VLQ(300)    = [AC 02]
  VLQ(100000) = [A0 8D 06]
  Total: [AC 02 A0 8D 06] = 5 bytes
```

## Comparison: App Inbox Key vs Unified Inbox Key

For namespace=1, iid=5:

```
App Inbox:     [01] [00 00 00 05]     = 5 bytes (fixed-size int for IID)
Unified Inbox: [01] [0B]             = 2 bytes (VLQ for inboxId=11)
```

For namespace=300, iid=12345:

```
App Inbox:     [01 2C] [00 00 30 39]  = 6 bytes
Unified Inbox: [AC 02] [F1 C0 01]    = 5 bytes
```

Unified Inbox keys are generally **smaller** due to VLQ encoding, especially for small IIDs/IDNs.

## Namespace Scan Pattern

For the `updateAll()` bulk operation and `SCAN` iteration, the codec provides namespace-only encoding:

```java
// BinaryInboxCodec.java:80-90
public byte[] encodeNamespace(int namespace) {
    ByteArrayOutputStream out = new ByteArrayOutputStream();
    ExtendedDataOutputStream data = new ExtendedDataOutputStream(out);
    data.writeVLQ(namespace);
    return out.toByteArray();
}
```

Used for Redis SCAN pattern matching: `"namespace_bytes*"` — finds all inbox keys for an account.

## Connection Sharding

**File**: `core/server-side-inbox/service/src/com/wizrocket/inbox/service/redis/RedisConnectionProvider.java`

Multiple Redis hosts per account. Sharding is done by:
```
shard = Murmur3_32(inboxId) % shards.size()
```

Each shard has multiple connections (default 20) for throughput, selected via `ThreadLocalRandom`.
