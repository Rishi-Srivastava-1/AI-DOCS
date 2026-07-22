# 7. LC Delivery Flow

## Entry Point

Unlike App Inbox (which delivers via the `/a1` MobileAppServlet on App Launched), Unified Inbox has its own **dedicated endpoint**:

**File**: `core/lc/src/java/com/wizrocket/lc/targeting/inbox/FetchInboxServlet.java`

```
Endpoint: GET /inbox/fetch
```

The SDK calls this endpoint explicitly to fetch inbox messages — not on every App Launched, but when the app wants to display the inbox.

## The Fetch Flow

```
SDK calls /inbox/fetch
    |
    +-> FetchInboxServlet.processRequest()
    |     +-> Parse request: accountId, iid, idn, lastKnownId
    |     +-> Create UserKey(accountId, iid, idn)
    |
    +-> InboxFetchServiceRedis.get(userKey)
    |     +-> Create RedisInboxKey from UserKey
    |     +-> Get Redis connection via RedisConnectionProvider
    |     +-> conn.sync().get(redisKey) → UserInbox
    |     +-> InboxLimits.applyExpiryLimits() → remove expired messages
    |     +-> Return Optional<UserInbox>
    |
    +-> InboxRenderer.personalise(userKey, inbox, lastKnownId)
    |     +-> For each message:
    |     |     +-> If message.id > lastKnownId → full render
    |     |     |     +-> Decode personalisation keys back to strings
    |     |     |     +-> Run Liquid template with profile + stored personalisations
    |     |     |     +-> Build rendered message JSON
    |     |     +-> Else → status update only (isRead, isDeliveredOnce)
    |     +-> Return List<RenderedInboxMessage>
    |
    +-> Build response JSON:
          { "messages": [...], "lastMessageId": N }
```

## Key Differences from App Inbox LC Flow

| Aspect | App Inbox | Unified Inbox |
|--------|-----------|---------------|
| **Endpoint** | `/a1` (MobileAppServlet, shared with all App Launched events) | `/inbox/fetch` (dedicated) |
| **Trigger** | Every App Launched event | Explicit SDK call when inbox is opened |
| **Pre-fetch** | Async Redis GET before target evaluation | Sync Redis GET on demand |
| **After delivery** | Messages DELETED from Redis | Messages STAY in Redis |
| **Re-fetch** | Not possible (messages gone) | Possible — SDK can call again, gets only updates |
| **lastKnownId** | Not applicable | SDK sends last known message ID → LC sends only new messages fully, old ones as status-only |
| **Status sync** | Not possible (no isRead tracking) | SDK can mark as read → server stores flag → reflects on next fetch |
| **Personalisation** | LC resolves from profile + stored event props | LC decodes stored personalisations (both profile + event), re-renders Liquid |

## The `lastKnownId` Optimisation

The SDK tracks the highest message ID it has received. On subsequent fetches:

```
First fetch:   lastKnownId = 0 → LC sends all messages (full content)
                response: lastMessageId = 5, messages = [msg1(full), msg2(full), ..., msg5(full)]

SDK stores: lastKnownId = 5

Second fetch:  lastKnownId = 5 → LC sends:
                - msg6, msg7 (full content — new since last fetch)
                - msg1-5 (status only — isRead, isDeliveredOnce)
                response: lastMessageId = 7, messages = [msg1(status), ..., msg5(status), msg6(full), msg7(full)]
```

This reduces payload size — old messages don't need full content re-sent.

## Expiry on Fetch

**File**: `core/server-side-inbox/service/src/com/wizrocket/inbox/service/content/InboxLimits.java:57-68`

```java
public UserInbox applyExpiryLimits(UserInbox inbox, IntConsumer callback) {
    inbox.getMessages().removeIf(message -> {
        boolean shouldDelete = clock.instant()
                .isAfter(Instant.ofEpochSecond(message.getExpiryTime()));
        if (shouldDelete) {
            callback.accept(message.getTargetingId());
        }
        return shouldDelete;
    });
    return inbox;
}
```

Expired messages are removed **in-memory during the fetch** — they're filtered out before rendering. The Redis blob is NOT updated on fetch (lazy cleanup). The next write operation (add, markAsRead) will include the cleaned-up state.

This is different from App Inbox where `DeleteExpiryCron` actively scans all Redis keys to remove expired notifications.

## Messages Persist After Delivery

This is the biggest behavioral difference. In App Inbox:
```
NB writes → user opens app → LC reads → LC DELETES from Redis → gone
```

In Unified Inbox:
```
NB writes → SDK fetches → LC reads → messages STAY → SDK can fetch again
              → SDK marks as read → LC updates isRead flag in Redis
              → messages eventually expire (based on expiryTime)
```

This enables:
- **Cross-device sync**: Same inbox on all devices (via IDN-based key)
- **Read receipts**: Server knows which messages the user has read
- **Persistent inbox**: Messages survive app restarts, reinstalls, device changes
