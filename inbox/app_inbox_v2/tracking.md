# InboxV2 Delivered Bit Tracking — Implementation Plan

## Context

InboxV2 messages are stored in FStore with `isDeliveredOnce=false`. When LC serves messages via `/inbox/v2` for the first time, we need to:
1. Flip `isDeliveredOnce=true` in FStore (so subsequent fetches show `deliveredFirstTime=false`)
2. This counts as "sent" in stats

The approach mirrors **unified inbox's delivered flow** (`InboxDelivered` → `UnifiedInboxEventsConsumer` → `InboxRedisFlagUpdateBatchTask` → Redis flag update), but adapted for V2:
- New event type `APP_INBOX_V2` (like `UNIFIED_INBOX`)
- New consumer `AppInboxV2EventsConsumer` (like `UnifiedInboxEventsConsumer`)
- New handler `AppInboxV2DeliveredEventHandler` (like `UnifiedInboxDeliveredEventHandler`)
- New batch task with `SmallerPageSizePersistentQueue` overflow → flushes to FStore (not Redis)
- Event is NOT punched into ES

## Flow

```
LC (InboxV2GetMessagesService)
  → renders messages, identifies first-delivery ones (!isDeliveredOnce)
  → builds InboxV2Delivered event payload (identifier per message)
  → pushes to LP Redis with eventType = APP_INBOX_V2.ordinal()

LP (PreRecordMapEntry routing)
  → APP_INBOX_V2 ordinal → AppInboxV2EventEntry → AppInboxV2EventsConsumer
  → dispatches by eventName: "InboxV2Delivered" → AppInboxV2DeliveredEventHandler
  → handler adds to InboxV2FStoreDeliveredBitUpdateBatchTask (in-memory BlockingBatchTask)
  → if batch task full → overflow to SmallerPageSizePersistentQueue (disk)
  → batch flushes: batchGet → deserialize → set isDeliveredOnce=true → serialize → batchPut
```

## Decisions Made

- **LP dependency**: Add `inboxv2-service` to LP's pom.xml (LP already depends on `inbox-service` V1)
- **FileQueue**: `SmallerPageSizePersistentQueue` from `nb-common` (LP already depends on it) — 8MB pages, LZ4 compression
- **Payload**: Pass `identifier` string per message (from `FStoreInboxKey.getIdentifier()`). LP reconstructs full FStore key via `FStoreInboxKeyUtils.encodeKey(accountId, userId, identifier)`
- **No ES**: Event is not stored in EventStore

## Changes

### 1. New EventType — `core/utils/`

**Modify: `AppConstants.java`** (`core/utils/src/com/wizrocket/utils/constants/AppConstants.java`)
- Add `APP_INBOX_V2` to `EventType` enum (append at end, preserving ordinals)
- Add constant: `INBOX_V2_DELIVERED_EVENT_NAME = "InboxV2Delivered"` in `MockEvents`

### 2. LC — Push InboxV2Delivered Event

**Modify: `InboxV2RenderResult.java`** (`core/lc/src/java/com/wizrocket/lc/service/inboxv2/InboxV2RenderResult.java`)
- Add field: `List<InboxV2DeliveredMessageInfo> deliveredFirstTimeMessages`
- `InboxV2DeliveredMessageInfo` — lightweight DTO with `identifier`, `wzrkId`, `variant`

**Modify: `InboxV2Renderer.java`** (`core/lc/src/java/com/wizrocket/lc/service/inboxv2/InboxV2Renderer.java`)
- In render loop, when `!msg.isDeliveredOnce()`, collect into `deliveredFirstTimeMessages`

**New: `InboxV2DeliveredEventPusher.java`** (`core/lc/src/java/com/wizrocket/lc/service/inboxv2/InboxV2DeliveredEventPusher.java`)
- Follows `UnifiedInboxCustomEventLPRedisService` pattern exactly
- Builds payload:
  ```json
  {
    "eventName": "InboxV2Delivered",
    "messages": [
      { "identifier": "abc123", "wzrk_id": "...", "wzrk_cid": "..." }
    ]
  }
  ```
- Wraps in `RedisPayloadDto` with `eventType = APP_INBOX_V2.ordinal()`
- Calls `BaseAsyncServlet.pushToRedis()`

**Modify: `InboxV2GetMessagesService.java`** (`core/lc/src/java/com/wizrocket/lc/service/inboxv2/InboxV2GetMessagesService.java`)
- Inject `InboxV2DeliveredEventPusher`
- After render, call `deliveredEventPusher.createAndPushToRedis(accountId, guid, renderResult.getDeliveredFirstTimeMessages())`

**Modify: `InboxV2APIModule.java`** (`core/lc/src/java/com/wizrocket/lc/guice/InboxV2APIModule.java`)
- Bind `InboxV2DeliveredEventPusher`, `Clock`

### 3. LP — New Consumer, Handler, Batch Task

**New: `AppInboxV2EventsConsumer.java`** (`core/lp/src/com/wizrocket/lp/events/consumers/app_inbox_v2/AppInboxV2EventsConsumer.java`)
- Follows `UnifiedInboxEventsConsumer` pattern exactly
- Extends `BaseConsumer<SinglePushEvent>`
- Uses `Map<String, AppInboxV2EventHandler>` (Guice MapBinder) to dispatch by event name
- Parses JSON payload, delegates to handler

**New: `AppInboxV2EventHandler.java`** (interface, same package)
- `void handle(int accountId, AppInboxV2EventPayloadPojo payload, BatchOutput output)`

**New: `AppInboxV2EventPayloadPojo.java`** (`core/lp/src/com/wizrocket/lp/events/dto/appinboxv2/`)
- Follows `UnifiedInboxEventPayloadPojo` pattern
- Fields: `eventName`, `List<Message> messages`, `boolean isRetry`
- `Message`: `identifier` (String), `wzrkId`, `variant`

**New: `AppInboxV2DeliveredEventHandler.java`** (same package as consumer)
- Follows `UnifiedInboxDeliveredEventHandler` pattern
- Gets `idn` from `output.profile`
- Builds key info and adds to `InboxV2FStoreDeliveredBitUpdateBatchTask`

**New: `InboxV2FStoreDeliveredBitUpdateBatchTask.java`** (same package)
- Follows `InboxRedisFlagUpdateBatchTask` pattern with overflow:
  - `@Singleton`, `BlockingBatchTask<InboxV2DeliveredBitDTO>`, batch=100, flush=5s
  - `ThreadPoolExecutor` with `ArrayBlockingQueue(1000)` + `ReEnqueueTaskRejectedExecutionHandler`
  - **Overflow**: When `addData()` throws (queue full after blocking), serialize DTO to `SmallerPageSizePersistentQueue` on disk
  - **Drain**: Background thread reads from FileQueue when in-memory queue has capacity
  - `task()` callback: uses `FStoreInboxKeyUtils.encodeKey()` to reconstruct full key → `batchGet` → deserialize via `InboxV2MessageCodec` → set `isDeliveredOnce=true` → serialize → `batchPut`
  - Idempotent: skip rewrite if already delivered
  - `shutdown()`: flush batch task + close FileQueue

**New: `InboxV2DeliveredBitDTO.java`** (same package)
- `identifier` (String), `idn` (long), `accountId` (int)

**New: `AppInboxV2EventEntry.java`** (`core/lp/src/com/wizrocket/lp/events/consumers/prerecordentries/`)
- Follows `UnifiedInboxEventEntry` exactly
- Maps `EventType.APP_INBOX_V2` → `AppInboxV2EventsConsumer`

**New: `AppInboxV2Module.java`** (`core/lp/src/com/wizrocket/lp/`)
- Follows `UnifiedInboxModule` pattern
- MapBinder: `"InboxV2Delivered"` → `AppInboxV2DeliveredEventHandler`
- Binds `FStoreInboxKeyUtils`, `InboxV2MessageCodec`, `FSClientInboxSyncManager`

### 4. LP Wiring

**Modify: `LpGuiceModule.java`** (`core/lp/src/com/wizrocket/lp/LpGuiceModule.java`)
- `bindConsumers()`: add `AppInboxV2EventsConsumer`
- `bindPreRecordMapEntries()`: add `AppInboxV2EventEntry`
- `configure()`: install `AppInboxV2Module`

**Modify: `MainRunner.java`** (`core/lp/src/com/wizrocket/lp/events/MainRunner.java`)
- `addShutDownHook()`: add `inboxV2FStoreDeliveredBitUpdateBatchTask.shutdown()`

**Modify: `core/lp/pom.xml`**
- Add dependency: `inboxv2-service`

## Reference Files (patterns to follow)

| Pattern | Source File |
|---------|------------|
| Consumer | `core/lp/.../unified_inbox/UnifiedInboxEventsConsumer.java` |
| Handler | `core/lp/.../unified_inbox/UnifiedInboxDeliveredEventHandler.java` |
| Batch task | `core/lp/.../unified_inbox/InboxRedisFlagUpdateBatchTask.java` |
| PreRecordMapEntry | `core/lp/.../prerecordentries/UnifiedInboxEventEntry.java` |
| Guice module | `core/lp/UnifiedInboxModule.java` |
| LC event pusher | `core/lc/.../lpredisservice/UnifiedInboxCustomEventLPRedisService.java` |
| LC payload DTO | `core/lc/src/.../dtos/unifiedinbox/UnifiedInboxRedisEventPojo.java` |
| LP payload DTO | `core/lp/.../dto/unifiedinbox/UnifiedInboxEventPayloadPojo.java` |
| FStore key utils | `core/inboxv2/service/.../fstore/FStoreInboxKeyUtils.java` |
| Message codec | `core/inboxv2/service/.../serialization/InboxV2MessageCodec.java` |
| FileQueue | `core/nb-common/.../fileq/SmallerPageSizePersistentQueue.java` |
| FStore batch delete | `core/inboxv2/service/.../fstore/InboxV2BatchDeleteTask.java` |

## File Summary

| File | Change |
|------|--------|
| `core/utils/.../AppConstants.java` | Add `APP_INBOX_V2` enum + `INBOX_V2_DELIVERED_EVENT_NAME` |
| `core/lc/.../InboxV2RenderResult.java` | Add `deliveredFirstTimeMessages` field |
| `core/lc/.../InboxV2Renderer.java` | Collect first-delivery message info |
| `core/lc/.../InboxV2DeliveredEventPusher.java` | **New** — push to LP Redis |
| `core/lc/.../InboxV2GetMessagesService.java` | Wire event pusher |
| `core/lc/.../InboxV2APIModule.java` | Bind pusher + Clock |
| `core/lp/.../app_inbox_v2/AppInboxV2EventsConsumer.java` | **New** — consumer |
| `core/lp/.../app_inbox_v2/AppInboxV2EventHandler.java` | **New** — handler interface |
| `core/lp/.../app_inbox_v2/AppInboxV2DeliveredEventHandler.java` | **New** — delivered handler |
| `core/lp/.../app_inbox_v2/InboxV2FStoreDeliveredBitUpdateBatchTask.java` | **New** — batch task + FileQueue overflow |
| `core/lp/.../app_inbox_v2/InboxV2DeliveredBitDTO.java` | **New** — batch task DTO |
| `core/lp/.../dto/appinboxv2/AppInboxV2EventPayloadPojo.java` | **New** — LP payload DTO |
| `core/lp/.../prerecordentries/AppInboxV2EventEntry.java` | **New** — routing entry |
| `core/lp/AppInboxV2Module.java` | **New** — Guice module |
| `core/lp/LpGuiceModule.java` | Register consumer + entry + module |
| `core/lp/events/MainRunner.java` | Shutdown hook |
| `core/lp/pom.xml` | Add `inboxv2-service` dependency |

## Implementation Order

1. `AppConstants` — add enum + constant
2. LP payload DTO (`AppInboxV2EventPayloadPojo`)
3. LP batch task (`InboxV2FStoreDeliveredBitUpdateBatchTask` + DTO)
4. LP handler + interface (`AppInboxV2DeliveredEventHandler`)
5. LP consumer (`AppInboxV2EventsConsumer`)
6. LP routing (`AppInboxV2EventEntry`)
7. LP wiring (`AppInboxV2Module` + `LpGuiceModule` + `MainRunner` + `pom.xml`)
8. LC renderer changes (`InboxV2RenderResult` + `InboxV2Renderer`)
9. LC event pusher (`InboxV2DeliveredEventPusher` + `InboxV2APIModule`)
10. LC orchestration (`InboxV2GetMessagesService`)

## Verification

1. Build: `./mvnw test-compile -pl core/utils,core/lp,core/lc,core/inboxv2/service`
2. Unit test `InboxV2FStoreDeliveredBitUpdateBatchTask` — mock FSClientSync, verify batchGet→batchPut flow, verify FileQueue overflow
3. Unit test `AppInboxV2DeliveredEventHandler` — verify it calls batch task correctly
4. Unit test `InboxV2DeliveredEventPusher` — verify Redis payload has `APP_INBOX_V2` ordinal and correct structure
5. Integration: serve messages, verify `isDeliveredOnce` flips on second fetch
