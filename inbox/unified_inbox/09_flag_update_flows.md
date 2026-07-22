# 9. Flag Update Flows — Read, Delivered, Clicked, Delete

## Overview

Unified Inbox tracks message state via flags stored inside each `InboxMessage` in Redis.
Unlike App Inbox (fire-and-forget, delete after delivery), Unified Inbox persists messages and
keeps their state updated as the user interacts.

```
InboxMessage flags:
  isRead          → true after user reads the message
  isDeliveredOnce → true after first SDK fetch (delivery tracking)

Deletion:
  Physical removal from UserInbox.messages list (no isDeleted flag)
```

---

## 1. Endpoints (LC)

**Servlet**: `UnifiedInboxServlet` — `@WebServlet("/1/inbox/*")`

| URI | Service | Action |
|-----|---------|--------|
| `/1/inbox/getMessages` | `GetMessagesService` | Fetch messages; pushes `InboxDelivered` LP event for first-time delivery |
| `/1/inbox/markMessagesAsRead` | `MarkAsReadService` | Mark messages as read; pushes `InboxMarkAsRead` LP event |
| `/1/inbox/markMessagesAsClicked` | `MarkAsClickedService` | Track click; spawns `NOTIFICATION_CLICKED` event via LP |

> **No SDK-facing delete endpoint exists** in `UnifiedInboxServlet`. The servlet routes only the
> three URIs above. Delete is handled internally (see §5).

---

## 2. Read Flag Flow

```
SDK calls /1/inbox/markMessagesAsRead
    |
    +-> MarkAsReadService.handleRequest()
    |     +-> Extract guid from request body (meta.g)
    |     +-> esHelper.getJForK(guid) → iid, idn
    |     +-> Build event payload: { eventName: "InboxMarkAsRead", messages: [{messageId, isRead}...] }
    |     +-> Push to LP Redis queue via UnifiedInboxCustomEventRedisService
    |
    +-> LP: UnifiedInboxEventsConsumer.doConsume()
    |     +-> Deserialise payload → UnifiedInboxEventPayloadPojo
    |     +-> eventHandlerFactory.get("InboxMarkAsRead") → UnifiedInboxViewedEventHandler
    |
    +-> UnifiedInboxViewedEventHandler.handle()
    |     +-> Resolve idn from profile (IDN-based key for cross-device identity)
    |     +-> Filter messages where !isRead (skip already-read messages)
    |     +-> redisFlagUpdateBatchTask.addToBatchTask(READ_BIT_UPDATE, userKey, messageIds)
    |     +-> Spawn NOTIFICATION_VIEWED event for analytics (wzrk_id, variant, ct_source)
    |
    +-> InboxRedisFlagUpdateBatchTask (BlockingBatchTask, 5s window, batch=100)
    |     +-> On flush: InboxUpdateServiceRedis.updateFlags(userKeyToMsgIds, READ_BIT_UPDATE)
    |     +-> On partial failure: push failed keys back to LP Redis via UnifiedInboxLPRedisUtils
    |
    +-> InboxUpdateServiceRedis.updateFlags()
          +-> messageMutator = updateFlag(InboxMessage::isRead, InboxMessage::setRead)
          +-> updateBatch() with WATCH/MULTI/EXEC (see §6)
          +-> Only sets flag if not already true (idempotent)
```

**Spawned analytics event**: `NOTIFICATION_VIEWED` — tracked in ES for campaign analytics.

---

## 3. Delivered Flag Flow

Triggered **on first SDK fetch**, not on NB dispatch.

```
SDK calls /1/inbox/getMessages
    |
    +-> GetMessagesService.handleRequest()
    |     +-> InboxFetchServiceRedis.get(userKey) → UserInbox
    |     +-> InboxRenderer.personalise() → List<RenderedInboxMessage>
    |     +-> For each message where deliveredFirstTime == true (isDeliveredOnce == false):
    |           Push "InboxDelivered" LP event payload
    |     +-> Return rendered messages to SDK
    |
    +-> LP: UnifiedInboxEventsConsumer.doConsume()
    |     +-> eventHandlerFactory.get("InboxDelivered") → UnifiedInboxDeliveredEventHandler
    |
    +-> UnifiedInboxDeliveredEventHandler.handle()
    |     +-> redisFlagUpdateBatchTask.addToBatchTask(DELIVERED_BIT_UPDATE, userKey, messageIds)
    |
    +-> InboxUpdateServiceRedis.updateFlags()
          +-> messageMutator = updateFlag(InboxMessage::isDeliveredOnce, InboxMessage::setDeliveredOnce)
          +-> WATCH/MULTI/EXEC write
```

**Purpose**: `isDeliveredOnce` enables the SDK to distinguish "first delivery" from re-fetches
(e.g., after app restart). Returned as `firstDelivery` in the response.

---

## 4. Clicked Flag Flow

Clicked tracking works differently — there is **no Redis flag update**.
Click is recorded as a spawned event in LP, not stored in InboxMessage.

```
SDK calls /1/inbox/markMessagesAsClicked
    |
    +-> MarkAsClickedService.handleRequest()
    |     +-> Extract guid, resolve iid/idn
    |     +-> Build payload: { eventName, messages: [{messageId, wzrk_id, variant}] }
    |     +-> Push to LP Redis via UnifiedInboxNotificationEventRedisService
    |           (uses NOTIFICATION_EVENT Redis service, different from CUSTOM_EVENT)
    |
    +-> LP: processes as NOTIFICATION_CLICKED event
          +-> Spawns NOTIFICATION_CLICKED in ES (standard notification engagement tracking)
          +-> No inbox Redis mutation
```

**Key difference from read**: Click is an analytics event only. The inbox message itself has no
`isClicked` flag — the clicked state is not returned to the SDK on subsequent fetches.

---

## 5. Delete Flow

Deletion removes the message physically from the `UserInbox.messages` list in Redis.

### 5a. Delete via NB (broadcast replacement / TTL expiry at fetch time)

```
InboxLimits.applyExpiryLimits(inbox) [called during fetch]
    +-> inbox.getMessages().removeIf(message ->
            Instant.now().isAfter(Instant.ofEpochSecond(message.getExpiryTime())))
    +-> Redis blob is NOT updated immediately (lazy cleanup)
    +-> Cleaned state is persisted on the next write (add/markAsRead/updateFlags)
```

### 5b. Direct delete (NB dispatcher, e.g., replaced broadcast)

```
InboxUpdateServiceRedis.delete(userKey, messageId)
    +-> update(userKey, inbox -> inbox.getMessages()
            .removeIf(message -> message.getId() == messageId))
    +-> Same WATCH/MULTI/EXEC transaction as flag updates
```

> There is no SDK-initiated delete endpoint in Unified Inbox. If a delete API is needed in the
> future, it would follow the same LP event → batch task → `InboxUpdateServiceRedis.delete()` pattern.

---

## 6. Redis Write Pattern (WATCH/MULTI/EXEC)

All flag updates and deletes use the same transactional pattern:

```
synchronized(connection) {
    commands.watch(inboxKey)            // 1. Watch for concurrent modification
    UserInbox inbox = commands.get(key) // 2. Read current value
    mutate(inbox)                       // 3. Apply flag/delete mutation in memory
    limits.apply(inbox)                 // 4. Enforce size/count limits
    commands.multi()                    // 5. Start transaction
    commands.set(key, inbox)            // 6. Stage write
    TransactionResult result = commands.exec()  // 7. Commit
    // If result.wasDiscarded() → WATCH conflict → retry via RetryDecorator
}
```

**Idempotency**: `updateFlag()` checks the current value before setting. If already true, no
mutation is applied and the key is not marked as changed. This prevents unnecessary Redis writes.

**Batch variant**: `updateBatch()` groups keys by Redis connection, issues a single `mget` for all
keys on that connection, applies mutations, then writes back with a single `mset` under MULTI/EXEC.

**Decorators**:
- `RetryDecorator` — retries on WATCH conflict (optimistic concurrency failure)
- `CircuitBreakerDecorator` — opens on repeated Redis failures to prevent cascade

---

## 7. Flag Update Operation Types

```java
// core/server-side-inbox/service/src/com/wizrocket/inbox/service/UnifiedInboxFlagUpdateOperationType.java
public enum UnifiedInboxFlagUpdateOperationType {
    READ_BIT_UPDATE("InboxMarkAsRead"),
    DELIVERED_BIT_UPDATE("InboxDelivered");
}
```

The enum value (LP event name) is used to route to the correct handler in `UnifiedInboxEventsConsumer`.

---

## 8. Batching in LP (InboxRedisFlagUpdateBatchTask)

Flag updates from LP are **not applied immediately** to Redis — they are batched:

| Parameter | Value |
|-----------|-------|
| Batch window | 5 seconds |
| Batch size | 100 items |
| Thread pool | 1 thread, queue=1000 |
| Failure handling | Push failed keys back to LP Redis queue |

This decouples LP throughput from Redis write latency. Multiple `markAsRead` events for the
same user within the 5s window are merged into one Redis write.

---

## 9. Analytics Events Spawned

| Action | Spawned Event | Where |
|--------|--------------|-------|
| Message fetched (first time) | `InboxDelivered` → LP event | GetMessagesService |
| Message marked as read | `NOTIFICATION_VIEWED` | UnifiedInboxViewedEventHandler |
| Message clicked | `NOTIFICATION_CLICKED` | MarkAsClickedService → LP |

---

## 10. Key Source Files

| File | Role |
|------|------|
| `core/lc/.../apis/targeting/send/UnifiedInboxServlet.java` | Servlet — routes to per-action service |
| `core/lc/.../service/unifiedinbox/MarkAsReadService.java` | Pushes InboxMarkAsRead LP event |
| `core/lc/.../service/unifiedinbox/MarkAsClickedService.java` | Pushes NOTIFICATION_CLICKED LP event |
| `core/lc/.../service/unifiedinbox/GetMessagesService.java` | Pushes InboxDelivered LP event on first fetch |
| `core/lp/.../unified_inbox/UnifiedInboxEventsConsumer.java` | LP consumer — routes by event name |
| `core/lp/.../unified_inbox/UnifiedInboxViewedEventHandler.java` | Handles read flag + NOTIFICATION_VIEWED spawn |
| `core/lp/.../unified_inbox/UnifiedInboxDeliveredEventHandler.java` | Handles delivered flag |
| `core/lp/.../unified_inbox/InboxRedisFlagUpdateBatchTask.java` | BlockingBatchTask — batches Redis writes |
| `core/server-side-inbox/service/.../redis/InboxUpdateServiceRedis.java` | WATCH/MULTI/EXEC flag + delete writes |
| `core/server-side-inbox/service/.../UnifiedInboxFlagUpdateOperationType.java` | Enum: READ_BIT_UPDATE, DELIVERED_BIT_UPDATE |
| `core/server-side-inbox/models/.../InboxMessage.java` | Message model: isRead, isDeliveredOnce flags |
