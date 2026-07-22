# PBS — Event Tracking PR (App Inbox V2)

**PR:** #42964 — Event Tracking Changes for App Inbox V2  
**Branch:** `ONC-352-event-tracking`  
**Jira:** ONC-352  
**Modules touched:** `core/lc/`, `core/lp/`, `core/utils/`

---

## Overview

Adds the event tracking layer on top of the read path. Two state transitions are tracked per inbox message:

1. **First delivery** — when the SDK receives a message for the first time (`isDeliveredOnce=false → true`)
2. **Read** — when the user views the message via `NOTIFICATION_VIEWED` event (`isRead=false → true`)

Both updates are batched and durable via file-queue overflow. LP owns all FStore writes.

---

## PR Structure

### LC Changes: `core/lc/`

| File | Role |
|------|------|
| `InboxV2DeliveredEventPusher` *(new)* | Pushes `InboxV2Delivered` synthetic event to Redis after `getMessages` surfaces a message for the first time. Uses a dedicated single-thread executor to keep Jackson serialization off the shared request thread pool |
| `InboxV2RedisEventPojo` *(new)* | LC-side DTO serialized to Redis — `eventName`, `messages` (identifier + wzrkId + variant) |
| `InboxV2GetMessagesService` | Updated to call `deliveredEventPusher.createAndPushToRedis(accountId, iid, idn, deliveredFirstTimeMessages)` after render |
| `InboxV2RenderResult` | Adds `InboxV2DeliveredMessageInfo` inner class (identifier + wzrkId + variant) and `deliveredFirstTimeMessages` list |
| `InboxV2Renderer` | Populates `deliveredFirstTimeMessages` for any message where `!msg.isDeliveredOnce()` |
| `InboxV2APIModule` | Binds `InboxV2DeliveredEventPusher` (singleton) |
| `LcInitializer` | Registers `InboxV2DeliveredEventPusher.shutdown()` in `contextDestroyed` |

### LP Changes: `core/lp/`

| File | Role |
|------|------|
| `AppInboxV2EventsConsumer` *(new)* | LP consumer for `APP_INBOX_V2` events. Parses the JSON payload, looks up the handler by `eventName` in the `Map<String, AppInboxV2EventHandler>` factory, delegates |
| `AppInboxV2EventHandler` *(new, interface)* | `handle(accountId, AppInboxV2EventPayloadPojo payload, BatchOutput output)` — strategy interface for event handlers |
| `AppInboxV2DeliveredEventHandler` *(new)* | Handles `InboxV2Delivered` — resolves userId, enqueues `InboxV2BitUpdateDTO` into `InboxV2FStoreDeliveredBitUpdateBatchTask` per message |
| `AbstractInboxV2FStoreBitUpdateBatchTask` *(new)* | Shared infrastructure: thread pool, `BlockingBatchTask`, file-queue overflow, drain scheduler, shutdown. Subclasses implement `isAlreadyApplied()` and `applyMutation()` |
| `InboxV2FStoreDeliveredBitUpdateBatchTask` *(new)* | Extends abstract base — `isAlreadyApplied = msg.isDeliveredOnce()`, `applyMutation = msg.setDeliveredOnce(true)` |
| `InboxV2FStoreReadBitUpdateBatchTask` *(new)* | Extends abstract base — `isAlreadyApplied = msg.isRead()`, `applyMutation = msg.setRead(true)` |
| `InboxV2BitUpdateDTO` *(new)* | Shared DTO for both bit-update tasks: `accountId`, `userId`, `identifier`. Includes binary serialization for file-queue persistence |
| `AppInboxV2EventEntry` *(new)* | LP `PreRecordEntry` for `APP_INBOX_V2` event type — wires `AppInboxV2EventsConsumer` into the event processing chain |
| `AppInboxV2Module` *(new)* | Guice module — installs `FStoreInboxCommonModule`, binds `InboxV2MessageCodec`, `InboxV2BitUpdateDTO` batch tasks, registers `InboxV2Delivered` → `AppInboxV2DeliveredEventHandler` in map binder |
| `AppInboxV2EventPayloadPojo` *(new)* | LP-side deserialization target: `eventName`, `messages`, `isRetry`, `deviceOrdinal` |
| `NotifEventConsumer` | Extended to detect `NOTIFICATION_VIEWED` for InboxV2 campaigns (via `NOTIFICATION_MESSAGE_ID` key) and enqueue `InboxV2BitUpdateDTO` into `InboxV2FStoreReadBitUpdateBatchTask` |
| `LpGuiceModule` | Installs `AppInboxV2Module`, binds `AppInboxV2EventEntry` in pre-record entries map |
| `MainRunner` | Registers `inboxV2FStoreDeliveredBitUpdateBatchTask.shutdown()` and `inboxV2FStoreReadBitUpdateBatchTask.shutdown()` in `addShutDownHook()` |
| `LPMetrics` / `LPMetricsConstants` | New metric constants for inbox V2 tracking |

### Shared: `core/utils/`

| Constant | Value | Used for |
|----------|-------|----------|
| `AppConstants.EventType.APP_INBOX_V2` | new enum value | Redis event type ordinal |
| `AppConstants.MockEvents.INBOX_V2_DELIVERED_EVENT_NAME` | `"InboxV2Delivered"` | Event name key in handler factory map |
| `AppConstants.KnownKeys.NOTIFICATION_MESSAGE_ID` | `"wzrk_mid"` | Identifies InboxV2 messages in `NOTIFICATION_VIEWED` events |

---

## LLD

### Flow 1 — Delivered Tracking

```
SDK
 │
 ▼ POST /inbox/v2/getMessages
InboxV2Servlet (LC)
 │
 ▼
InboxV2GetMessagesService (LC)
 │
 ├──► InboxV2FetchService.get()          → top 100 messages from FStore
 │
 ├──► InboxV2Renderer.render()
 │         │
 │         ├── renderedMessages
 │         └── deliveredFirstTimeMessages  (where isDeliveredOnce=false)
 │
 ├──► InboxV2DeliveredEventPusher
 │         │  createAndPushToRedis(accountId, iid, idn, deliveredFirstTimeMessages)
 │         │  [offloaded to dedicated single-thread executor]
 │         │
 │         ▼ Redis  (InboxV2Delivered event)
 │              meta: { account, iid, idn, timestamp, eventSource=BATCH }
 │              payload: { eventName, messages: [{identifier, wzrkId, variant}] }
 │
 ▼ HTTP Response → SDK

                   ↓ async, LP picks up from Redis

          AppInboxV2EventsConsumer (LP)
           │  Jackson.fromJsonString → AppInboxV2EventPayloadPojo
           │  eventHandlerFactory.get("InboxV2Delivered")
           │
           ▼ AppInboxV2DeliveredEventHandler
           │  userId = ProfileUtil.getUid(iid, idn)
           │  for each message:
           │    enqueue InboxV2BitUpdateDTO(accountId, userId, identifier)
           │
           ▼ InboxV2FStoreDeliveredBitUpdateBatchTask
           │  [groups by userId, batchGet → check isAlreadyApplied → setDeliveredOnce(true) → batchPut]
           │
           ▼ FStore  (isDeliveredOnce = true)
```

---

### Flow 2 — Read Tracking

```
SDK
 │
 ▼ NOTIFICATION_VIEWED event (standard event ingestion)
LC → Redis → LP
 │
 ▼ NotifEventConsumer (LP)
 │  detects NOTIFICATION_MESSAGE_ID ("wzrk_mid") in event data
 │  → identifies InboxV2 message
 │
 ▼ InboxV2FStoreReadBitUpdateBatchTask
 │  enqueue InboxV2BitUpdateDTO(accountId, userId, identifier)
 │  [groups by userId, batchGet → check isAlreadyApplied → setRead(true) → batchPut]
 │
 ▼ FStore  (isRead = true)
```

---

### `AbstractInboxV2FStoreBitUpdateBatchTask` — Internal Architecture

```
addToBatchTask(dto)
 │
 ├── batchTask.addData(accountId, dto)  ──success──► task(accountIdStr, List<dto>)
 │                                                        │
 │                                                        ▼
 │                                               FSClientSync.batchGet()
 │                                                        │
 │                                               for each message:
 │                                                 if !isAlreadyApplied(msg)
 │                                                   applyMutation(msg)
 │                                                   add to entries
 │                                                        │
 │                                               FSClientSync.batchPut()
 │
 └── on queue full / InterruptedException
       │
       ▼ SmallerPageSizePersistentQueue (disk overflow)
             │
             ▼ drainExecutor (scheduled every 5s)
                   replays from disk → batchTask.addData()
```

---

### Event Wire Format (Redis payload)

```json
{
  "meta": {
    "a": 1699358338,
    "ts": 1748677200,
    "i": 5647,
    "j": 0,
    "es": 3
  },
  "events": [{
    "t": 42,
    "payload": {
      "eventName": "InboxV2Delivered",
      "messages": [
        {
          "identifier": "1777549260",
          "wzrk_id": "1777549260_20260429",
          "wzrk_pivot": "wzrk_default"
        }
      ],
      "isRetry": false
    }
  }]
}
```

---

## Key Design Decisions

### 1. Map-based handler dispatch in LP
`AppInboxV2EventsConsumer` dispatches via `Map<String, AppInboxV2EventHandler>` keyed on `eventName`. Adding a new synthetic event (e.g., `InboxV2Read` from LC in a future PR) requires only a new handler class + a single `addBinding` in `AppInboxV2Module` — no changes to the consumer.

### 2. Dedicated pusher executor in LC
`InboxV2DeliveredEventPusher` offloads Jackson serialization + Redis push to a single-thread executor with a bounded queue (1000). This keeps the hot `BaseAsyncServlet.exec` thread pool free per `core/lc/CLAUDE.md` threading rules. On queue full, the event push is dropped — `isDeliveredOnce` self-heals on the next `getMessages` call.

### 3. `AbstractInboxV2FStoreBitUpdateBatchTask` — shared infrastructure
Delivered and read bit-update tasks share 100% of their infrastructure (thread pool, file queue, drain, shutdown). Subclasses implement only two hooks: `isAlreadyApplied()` (idempotency guard) and `applyMutation()` (the bit to flip). At-least-once guarantee: drain loop calls `addData` before `fileQueue.dequeue` so a crash between the two replays the item on restart.

### 4. Read tracking via `NOTIFICATION_VIEWED` (not a synthetic event)
Read state is detected inline in `NotifEventConsumer` by checking for `NOTIFICATION_MESSAGE_ID` (`wzrk_mid`) in the event data — no separate Redis round-trip needed. This keeps the read-tracking path lower latency than delivered tracking.

### 5. `iid`/`idn` instead of `guid` in delivered event meta
`InboxV2DeliveredEventPusher` uses `iid`/`idn` (already resolved by `InboxV2GetMessagesService`) rather than `guid`. LP resolves the full user profile from `iid`/`idn` via its standard pipeline — consistent with how all other synthetic events are processed.

---

## Responsibility Split vs Other PRs

| Concern | Inbox PR (#42861) | Event Tracking PR (#42964) | Delete PR (#43681) | Live Trigger PR (#43058) |
|---------|:-----------------:|:--------------------------:|:------------------:|:------------------------:|
| Read path (`getMessages`) | ✓ | — | — | — |
| First-delivery tracking | — | ✓ | — | — |
| Read tracking | — | ✓ | — | — |
| SDK-triggered delete | — | — | ✓ | — |
| Cap-excess / non-persistent delete | — | — | ✓ | — |
| Targeting stats for user deletes | — | — | ✓ | — |
| Persistent live trigger LC→LP push | — | — | — | ✓ |
| LP serialize message → NB queue | — | — | — | ✓ |
| NB dispatch → FStore write path | — | — | — | base branch (ONC-350) |
