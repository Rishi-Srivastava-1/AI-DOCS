# 1. The Big Picture: What Lives in Redis

## The Storage Model

Every device in CleverTap's system has an internal identity called **IID** (Internal Identity ID). For App Inbox, Redis stores **one key-value pair per device**:

```
Key:   binary bytes derived from (namespace + IID)
Value: a single byte[] blob containing ALL inbox notifications for that device
```

There is no JSON, no protobuf, no schema. Just raw bytes.

## What "All notifications" means

When a marketer sends 3 inbox campaigns to a user, and that user hasn't opened the app yet, Redis holds:

```
Key: [05][00 00 30 39]

Value: [Notif_1_bytes][Notif_2_bytes][Notif_3_bytes]
        <- campaign A -> <- campaign B -> <- campaign C ->
```

The notifications are **concatenated back to back** — no delimiters, no separators, no length headers, no message count. It's like writing three sentences in a book without any punctuation or spacing.

## No Envelope / No Header

This is a critical design decision. Compare to how most systems store lists:

```
Typical system:
  [version: 1B] [count: 4B] [msg1_length: 4B] [msg1_data] [msg2_length: 4B] [msg2_data] ...

App Inbox:
  [msg1_data][msg2_data][msg3_data]...
  (nothing else)
```

**Consequences of no envelope:**

| Question | How to answer it |
|----------|-----------------|
| How many notifications does this user have? | Walk the entire byte array, parsing each notification until you reach the end |
| What's the 3rd notification? | Parse notification 1, skip. Parse notification 2, skip. Parse notification 3, return. |
| Is this byte array valid? | Try to parse it. If you hit `ArrayIndexOutOfBoundsException`, it's corrupt (line 323 in `Notification.java` actually catches this!) |
| What protocol version was used? | Unknown — there's no version field. The format is hardcoded. |

## The 10KB Hard Cap

**File**: `core/inboxstore/src/com/wizrocket/inboxstore/Protocol/Notification.java:65-71`

```java
private static final int MAX_BYTE_ARRAY = 10000;

public static class Encoder {
    private byte[] b = null;
    private byte[] buffer = new byte[MAX_BYTE_ARRAY];
    private int idx = 0;
```

The `Encoder` pre-allocates a 10,000 byte scratch buffer. All mutations happen within this buffer. If a user's total notification data exceeds 10KB, the buffer overflows.

What does 10KB hold in practice? A minimal notification (no variant, no personalisations) is **8 bytes**. A typical one with variant + 3 personalisations of ~20 bytes each is roughly **80 bytes**. So:

```
Minimal:  10,000 / 8  ~ 1,250 notifications (theoretical max)
Typical:  10,000 / 80 ~ 125 notifications
Heavy:    10,000 / 200 ~ 50 notifications
```

But `maxNotifications` (configurable per account via `InboxStoreConfig`) kicks in before this — typically 50-100. When exceeded, the **oldest notifications are dropped from the front** of the blob.

## Who Writes vs Who Reads

```
        WRITE (NB)                              READ (LC)
        ----------                              ----------
        Jedis (sync)                            Lettuce (async)
        InboxClientSync                         InboxClientAsync
        Pipeline GET -> mutate -> Pipeline SET  Async GET -> Decoder
        Per batch of users                      Per single user (on App Launched)
```

- **NB write path**: `core/inboxstore/src/com/wizrocket/inboxstore/Redis/InboxClientSync.java`
- **LC read path**: `core/inboxstore/src/com/wizrocket/inboxstore/Redis/InboxClientAsync.java`

NB writes in batches (multiple users' blobs in one Redis pipeline). LC reads one user at a time when they open the app. They use **different Redis client libraries** — Jedis (blocking) for NB because batched writes are simpler in sync mode, and Lettuce (non-blocking) for LC because the `/a1` endpoint needs async I/O.

## The Lifecycle of a Byte Array

```
1. User has no inbox messages
   Redis: key doesn't exist (or null value)

2. Campaign A dispatched to this user
   NB: GET -> null -> Encoder.addNotification(A) -> [A_bytes]
   NB: SET -> Redis now holds [A_bytes]

3. Campaign B dispatched
   NB: GET -> [A_bytes] -> Encoder.addNotification(B) -> [A_bytes][B_bytes]
   NB: SET -> Redis now holds [A_bytes][B_bytes]

4. User opens app
   LC: GET -> [A_bytes][B_bytes]
   LC: Decoder.getNotificationData() -> [{targetId:A, ...}, {targetId:B, ...}]
   LC: Sends both to SDK
   LC: Encoder.removeTargetsFromNotification([A, B]) -> [] (empty)
   LC: SET -> Redis key now holds empty byte[] or is deleted

5. DeleteExpiryCron runs periodically
   Scans ALL IIDs (1 to maxIid, in batches of 1000)
   For each: GET -> decode -> check TTL -> delete expired -> SET back
   If all notifications expired -> DELETE key entirely
```

## Key Source Files

| Component | File Path |
|-----------|-----------|
| Protocol (Encoder/Decoder) | `core/inboxstore/src/com/wizrocket/inboxstore/Protocol/Notification.java` |
| Redis sync client (NB writes) | `core/inboxstore/src/com/wizrocket/inboxstore/Redis/InboxClientSync.java` |
| Redis async client (LC reads) | `core/inboxstore/src/com/wizrocket/inboxstore/Redis/InboxClientAsync.java` |
| Key encoding utilities | `core/inboxstore/src/com/wizrocket/inboxstore/Utils.java` |
| NB dispatcher | `core/nb/src/com/wizrocket/nb/dispatchers/notificationinbox/NotificationInboxDispatcher.java` |
| NB store manager | `core/nb/src/com/wizrocket/nb/providers/InboxStoreManager.java` |
| LC inbox manager | `core/lc/src/java/com/wizrocket/lc/targeting/InboxManager.java` |
| TTL cleanup cron | `core/inboxstore/src/com/wizrocket/inboxstore/DeleteExpiryCron.java` |
