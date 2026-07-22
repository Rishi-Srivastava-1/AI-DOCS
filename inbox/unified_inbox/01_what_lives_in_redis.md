# 1. The Big Picture: What Lives in Redis

## The Storage Model

Like App Inbox, Unified Inbox stores data in Redis — **one key-value pair per user**. But the design is fundamentally different in structure.

```
Key:   VLQ(namespace) + VLQ(inboxId)      ← variable-length, space-efficient
Value: Serialized UserInbox object         ← has envelope: version, message count, message IDs
```

## Key Differences from App Inbox

| Aspect | App Inbox | Unified Inbox |
|--------|-----------|---------------|
| Key encoding | Fixed-size namespace (1-3B) + fixed-size IID (4-8B) | VLQ namespace + VLQ inboxId (both variable) |
| Identity | IID only (device-level) | IID or IDN (device OR user-level) |
| Value structure | Raw concatenated notifications, no envelope | Envelope with protocol version, lastMessageId, message count |
| Message identity | None — identified by (targetId, epoch) | Each message has a unique auto-incremented `id` |
| Concurrency | No protection — last writer wins | WATCH/MULTI/EXEC optimistic locking |
| Status tracking | None | `isRead`, `isDeliveredOnce` flags per message |
| Redis client | Jedis (sync write) + Lettuce (async read) | Lettuce only (sync for writes, sync for reads) |

## The `UserInbox` Envelope

**File**: `core/server-side-inbox/models/src/com/wizrocket/inbox/models/UserInbox.java`

```java
@Jacksonized @Builder @Data
public class UserInbox {
    private int lastMessageId;                              // auto-incrementing counter
    @Default
    private final List<InboxMessage> messages = new ArrayList<>();
}
```

Unlike App Inbox (just raw bytes), Unified Inbox has a proper envelope:
- `lastMessageId` — a counter that increments with each new message. Used to assign unique IDs.
- `messages` — an ordered list of `InboxMessage` objects.

## The Binary Format Overview

```
USER_INBOX :=
    VLQ (protocol version = 0)
    VLQ (lastMessageId)
    VLQ (message count)
    MESSAGE* (one per message)
```

This means the decoder knows upfront:
- What protocol version to use
- How many messages to expect
- What the next available message ID is

## Who Writes vs Who Reads

```
        WRITE (NB)                              READ (LC)
        ----------                              ----------
        Lettuce (sync)                          Lettuce (sync)
        InboxUpdateServiceRedis                 InboxFetchServiceRedis
        WATCH -> GET -> mutate -> MULTI/SET     GET -> deserialize -> expire check
        Per single user (with retry)            Per single user (via /inbox/fetch)
```

- **NB write path**: `core/server-side-inbox/service/src/com/wizrocket/inbox/service/redis/InboxUpdateServiceRedis.java`
- **LC read path**: `core/server-side-inbox/service/src/com/wizrocket/inbox/service/redis/InboxFetchServiceRedis.java`
- **LC servlet**: `core/lc/src/java/com/wizrocket/lc/targeting/inbox/FetchInboxServlet.java`

Both use Lettuce — unlike App Inbox which uses Jedis for NB and Lettuce for LC.

## The Lifecycle of a UserInbox

```
1. User has no inbox messages
   Redis: key doesn't exist
   InboxUpdateServiceRedis.add() → creates new UserInbox with lastMessageId=0

2. Campaign A dispatched to user
   NB: WATCH key → GET → null → create UserInbox(lastMessageId=0)
       → increment lastMessageId to 1 → message.id = 1
       → MULTI → SET → EXEC
   Redis: UserInbox{lastMessageId=1, messages=[msg1]}

3. Campaign B dispatched
   NB: WATCH → GET → UserInbox{lastMessageId=1, messages=[msg1]}
       → increment to 2 → message.id = 2
       → MULTI → SET → EXEC
   Redis: UserInbox{lastMessageId=2, messages=[msg1, msg2]}

4. User opens app → SDK calls /inbox/fetch
   LC: GET → deserialize → check expiry → render messages → return JSON
   Messages stay in Redis (NOT deleted on delivery, unlike App Inbox)

5. SDK marks message as read
   NB/LC: WATCH → GET → find message by id → set isRead=true → MULTI → SET → EXEC

6. Expiry cleanup
   On next fetch: InboxLimits.applyExpiryLimits() removes expired messages
```

**Key difference from App Inbox**: Messages are **NOT deleted after delivery**. They persist until they expire or are explicitly deleted. The SDK can fetch them multiple times.

## Hard Limits

**File**: `core/server-side-inbox/service/src/com/wizrocket/inbox/service/content/InboxLimits.java`

```java
private static final int MAX_PERSONALISATION_VALUE_LEN = 500;   // characters
private static final int MAX_PERSONALISATION_KEYS = 50;
private static final int MAX_MESSAGES = 300;                     // per inbox
private static final long MAX_TTL_SECONDS = Duration.ofDays(30).getSeconds();
```

Compare to App Inbox:
| Limit | App Inbox | Unified Inbox |
|-------|-----------|---------------|
| Max messages per user | ~50-100 (configurable, within 10KB) | **300** |
| Max personalisation value | 255 bytes | **500 characters** |
| Max personalisation keys | 255 | **50** |
| Max TTL | 30 days (configurable) | **30 days** |
| Max blob size | 10,000 bytes | No hard cap |

## Key Source Files

| Component | File Path |
|-----------|-----------|
| UserInbox model | `core/server-side-inbox/models/src/com/wizrocket/inbox/models/UserInbox.java` |
| InboxMessage model | `core/server-side-inbox/models/src/com/wizrocket/inbox/models/InboxMessage.java` |
| UserKey model | `core/server-side-inbox/models/src/com/wizrocket/inbox/models/UserKey.java` |
| Serializer | `core/server-side-inbox/service/src/com/wizrocket/inbox/service/serialization/UserInboxBinarySerializer.java` |
| Deserializer | `core/server-side-inbox/service/src/com/wizrocket/inbox/service/serialization/UserInboxBinaryDeserializer.java` |
| Redis update service | `core/server-side-inbox/service/src/com/wizrocket/inbox/service/redis/InboxUpdateServiceRedis.java` |
| Redis fetch service | `core/server-side-inbox/service/src/com/wizrocket/inbox/service/redis/InboxFetchServiceRedis.java` |
| Redis codec | `core/server-side-inbox/service/src/com/wizrocket/inbox/service/redis/BinaryInboxCodec.java` |
| Key factory | `core/server-side-inbox/service/src/com/wizrocket/inbox/service/redis/RedisInboxKeyFactory.java` |
| NB dispatcher | `core/nb/src/com/wizrocket/nb/dispatchers/notificationinbox/ServerSideInboxDispatcher.java` |
| LC fetch servlet | `core/lc/src/java/com/wizrocket/lc/targeting/inbox/FetchInboxServlet.java` |
| LC renderer | `core/lc/src/java/com/wizrocket/lc/targeting/inbox/InboxRenderer.java` |
| Limits | `core/server-side-inbox/service/src/com/wizrocket/inbox/service/content/InboxLimits.java` |
