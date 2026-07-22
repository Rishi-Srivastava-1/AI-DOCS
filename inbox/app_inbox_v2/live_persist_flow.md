# Live App Inbox V2 — End-to-End Flow

End-to-end flow of a **live-triggered persistent App Inbox V2 message** — a persistent InboxV2 campaign that qualifies live on an SDK event (e.g. "Charged"), is served immediately by LC to the SDK, and is persisted to FStore asynchronously via LP → NB.

This document traces the trigger from the SDK request all the way to FStore.

> Do not confuse with:
> - **App Inbox V1** — legacy inbox-store-based path with a different NB routing (`InboxV1StoreManager`).
> - **Push + Inbox combined campaigns** — a push campaign that also creates an inbox side-effect; qualification happens via the SDK event → LP `TargetConsumer` → NB `PushDispatcher` → `InboxStoreDispatchManager`. Same NB tail from L5 onward, but the LC-side origination is different.
> - **InboxV2 lifecycle bookkeeping events** — Delivered, Deleted, Read, BitUpdate, ProfileMerge. These are FStore-only synthetic events; see `dispatch_write_path.md` for their write flow. They do **not** go through NB.

---

## High-Level Flow Diagram

```
┌─────────────┐
│  SDK / API  │  User does event (e.g. "Charged")
└──────┬──────┘
       │ HTTP  (SDK request with events[])
       ▼
┌─────────────────────────────────────────────────────────────┐
│  LC  (Log Collector)                                        │
│  • MobileAppServlet / IngestApiDataPushServlet              │
│  • InboxManager.serveInboxNotifications()                   │
│     ├─ Qualifies inbox targets against event + profile      │
│     ├─ Serves inbox live in the SDK response                │
│     └─ For each isInboxV2() && isPersistent() target:       │
│         container.addAllSupplementaryRequests(              │
│                 InboxV2PersistRequestDto(...))              │
│  • Container serialized to single JSON, RPUSH to Redis      │
│    (SDK events + Notification Sent + InboxV2Persist in ONE) │
└──────┬──────────────────────────────────────────────────────┘
       │  Redis (LP ingestion queue)
       ▼
┌─────────────────────────────────────────────────────────────┐
│  LP  (Log Processor) — GenericEventProcessor.consume()      │
│                                                             │
│  For each event in the batch, routed by "t":                │
│  ├─ t=2  (Charged)          → EventConsumer, TargetConsumer │
│  ├─ t=2  (Notification Sent)→ NotifEventConsumer            │
│  └─ t=37 (InboxV2Persist)   → AppInboxV2EventsConsumer      │
│                                → AppInboxV2PersistEventHandler│
│                                → output.getqMessages()      │
│                                    .add(triggerMessage)     │
│                                                             │
│  Framework: eventsProcessed >= 1 (from real t=2 events),    │
│  past the L793 early-return.                                │
│                                                             │
│  ES update: pushEventStoreUpdate(events=[Charged, NotifSent])│
│  NB update: pushNBUpdate(qMessages=[trigger])               │
└──────┬──────────────────────────────────────────────────────┘
       │  HTTP POST /enqueue  (Gateway.updateNB → ClientSync.recordQMessages)
       ▼
┌─────────────────────────────────────────────────────────────┐
│  NB  (Notification Backend)                                 │
│                                                             │
│  RequestProcessor.handle() case "/enqueue"                  │
│    ├─ TriggerMessageDeserializer.deSerialize(msg)           │
│    ├─ TimedQueue.enqueue(message)                           │
│    ▼                                                        │
│  NotificationQueueWorker.run() (background, NQWThreadPool)  │
│    ├─ ProfileDTODecrypter (at-rest decrypt)                 │
│    ▼                                                        │
│  MessageDispatcher.processAndDispatch(message)              │
│    ├─ MessagePreDispatchValidatorFacade                     │
│    │    · fcap, control group, unreachable, rate limit      │
│    ▼                                                        │
│  PushDispatcher (mode=NotificationInbox, isInboxV2=true)    │
│    ▼                                                        │
│  InboxStoreDispatchManager.handlePushInboxMessage(...)      │
│    ▼ (isInboxV2 branch)                                     │
│  InboxV2StoreManager.handleStoreMessage(message)            │
│    ▼                                                        │
│  InboxV2DispatchHandler.sendStoreMessage(iMessage)          │
│    ▼                                                        │
│  InboxV2PersistentQueue.addToNonBlockingPath(message)       │
└──────┬──────────────────────────────────────────────────────┘
       │  Async, background workers (see dispatch_write_path.md)
       ▼
┌─────────────────────────────────────────────────────────────┐
│  FStore (sharded gRPC message store)                        │
│  • Per-user inbox message row                               │
│  • isDeliveredOnce flag, TTL, targetId, batchId, wzrkId     │
└─────────────────────────────────────────────────────────────┘
```

---

## Stage-by-Stage Detail

### Stage 1 — LC: qualify + serve + queue the persist trigger

**Entry point:** SDK POSTs an event batch to LC (e.g. `A1Servlet` / `MobileAppServlet`). Somewhere in servlet processing, `InboxManager.serveInboxNotifications(...)` is invoked to compute and return the inbox content for the requesting user.

**Key file:** `core/lc/src/java/com/wizrocket/lc/targeting/InboxManager.java`

The relevant loop iterates over qualified inbox targets. For each target:

```java
if (ti.isInboxV2() && ti.isPersistent()) {
    container.addAllSupplementaryRequests(List.of(
            new InboxV2PersistRequestDto(
                    targetId, batchId,
                    container.getI(), container.getJ(),
                    insertionTimestamp,
                    device != null ? device.ordinal() : ProfileReach.Device.Web.ordinal())));
}
```

**Design choice — supplementary vs. separate RPUSH:**
Earlier design used a **separate** Redis push for InboxV2Persist via `InboxV2EventPusher.pushPersist(...)`. That was dropped because a standalone `APP_INBOX_V2` batch on LP has no real events → `eventsProcessed.size() == 0` → LP's framework hits the L793 early-return in `GenericEventProcessor` and skips NB update entirely. Riding the SDK event's own batch as a supplementary request keeps `eventsProcessed >= 1` and lets the framework fire NB update naturally.

**What the container serializes to Redis** (`MobileAppRequestContainer.toRedisString()`):

```json
{
  "meta": {"a": 1200000000, "g": "...", "i": 6927, "j": 6927, "ts": 1783701997, ...},
  "events": [
    {"t": 2, "eName": "Charged", "eb": {...}, ...},
    {"t": 2, "eName": "Notification Sent", "eb": {"targetIdToPivot": {...}}, ...},
    {"t": 37, "ts": 1783701997, "payload": {                                          // ← our supplementary
        "eventName": "InboxV2Persist",
        "targetId": 1783699389,
        "batchId": 20260710,
        "i": 6927,
        "j": 6927,
        "ts": 1783701997,
        "deviceOrdinal": 1
    }}
  ]
}
```

**Key files:**

| File | Role |
|------|------|
| `core/lc/.../targeting/InboxManager.java` | `serveInboxNotifications(...)` — qualifies + serves + queues the persist trigger |
| `core/lc/.../dtos/appinboxv2/InboxV2PersistRequestDto.java` | `BaseRequestDto` subclass; `getType() → APP_INBOX_V2`; `toBasicDBObject(...)` returns `{payload: {...}}` |
| `core/lc/.../request_containers/MobileAppRequestContainer.java` | `addAllSupplementaryRequests(...)` + `toRedisString()` — merges supplementary requests into the same `events[]` array as the SDK's real events |

---

### Stage 2 — LP: dispatch the InboxV2Persist event to the handler

**Entry:** `GenericEventProcessor.consume(RedisEntry)` at `core/lp/src/com/wizrocket/lp/events/GenericEventProcessor.java`.

Per-event routing at `L515–521`:
```java
final int type = rawEvent.getInt(LPConstants.EVENT_TYPE);
final EventType eventType = EventType.val(type);
final List<BaseConsumer<? extends BaseEvent>> tasks =
        taskManager.getPreRecordTasks(eventType);
```

For `t=37 (APP_INBOX_V2)`, the pre-record task list is `AppInboxV2EventEntry` (registered in `LpGuiceModule`), which runs `AppInboxV2EventsConsumer`.

**`AppInboxV2EventsConsumer.doConsume`** (`core/lp/.../consumers/app_inbox_v2/AppInboxV2EventsConsumer.java`):

```java
payload = Jackson.fromJsonString(event.payload.toString(),
        AppInboxV2EventPayloadPojo.class);
handler.handle(event.accountId, payload, output);
```

The `event.payload` field is populated from the JSON `"payload"` sub-object via `SinglePushEvent.@From(field = "payload")`. Then the consumer looks up the per-event-name handler:

| eventName | Handler | Purpose |
|-----------|---------|---------|
| `InboxV2Delivered` | `AppInboxV2BitUpdateEventHandler` | FStore-only: flip `isDeliveredOnce` |
| `InboxV2Deleted` | `AppInboxV2DeletedEventHandler` | FStore-only: delete inbox entry |
| `InboxV2Read` | `AppInboxV2BitUpdateEventHandler` | FStore-only: flip `isRead` |
| `InboxV2BitUpdate` | `AppInboxV2BitUpdateEventHandler` | FStore-only: generic bit update |
| `InboxV2ProfileMerge` | `AppInboxV2ProfileMergeEventHandler` | FStore-only: merge users |
| **`InboxV2Persist`** | **`AppInboxV2PersistEventHandler`** | **NB-dispatch: build trigger message for NB** |

**`AppInboxV2PersistEventHandler.handle`** (`core/lp/.../consumers/app_inbox_v2/AppInboxV2PersistEventHandler.java`):

```java
BasicDBObject profile = output.profile != null ? output.profile : new BasicDBObject();
ProfileDTO profileDTO = ProfileDTOCreator.getProfileDTOFromBasicDBObject(profile);

ProfileReach.Device device = ProfileReach.Device.val(payload.getDeviceOrdinal());
int modeDevice = ProfileReach.encode(ProfileReach.Mode.NotificationInbox, device);

String serialized;
if (LPDynamicConfig.getInstance().getRolloutLPPB3Serialization().getValue(accountId)) {
    serialized = convertToPB3String(TriggerTargetMessageSerializer.serialize(
            QMessage.TRIGGER_EVENT_MINUTE, DateComputer.computeDate(),
            accountIdStr, payload.getTargetId(), payload.getBatchId(),
            modeDevice, payload.getIid(), payload.getIdn(),
            profileDTO, payload.getTs(), null));
} else {
    serialized = TMessageStringSerializer.serializeToString(...);
}
output.getqMessages().add(serialized);
```

**Key points:**
- **Profile source:** `output.profile` (populated by LP's own `preConsume` + record-handler responses). The wire does **not** carry the profile — LP is the authoritative source.
- **Identity encoding:** `i` (iid) and `j` (idn) on the wire — matches `AppInboxV2EventPayloadPojo.@JsonProperty("i")` / `("j")`.
- **QMessage type:** `TRIGGER_EVENT_MINUTE` — signals NB this is a live trigger.
- **Mode:** `NotificationInbox` + Device — NB uses this in `PushDispatcher.isInboxV2` branching.
- **Result:** the serialized trigger goes on `output.getqMessages()`, to be pushed to NB by the framework.

**Why NB update fires** (`GenericEventProcessor.java:1122`):
```java
if (!getQMessages(output).isEmpty()) {
    ...
    pushNBUpdate(profile, getQMessages(output), maxEventLcTs);
}
```
- `output.getqMessages()` has our trigger → gate passes → `Gateway.updateNB(...)` runs.

**Why the L793 early-return does NOT fire:**
- `addToEventProcessedList` excludes `APP_INBOX_V2` from `eventsProcessed`.
- **But** the batch contains real events (Charged, Notification Sent), so `eventsProcessed.size() >= 1`.
- Early return skipped → ES + NB updates both fire.

**Key files:**

| File | Role |
|------|------|
| `core/lp/.../events/GenericEventProcessor.java` | Hot-path orchestrator (`consume`, `preConsume`, event loop, ES + NB updates) |
| `core/lp/.../consumers/prerecordentries/AppInboxV2EventEntry.java` | Registers `AppInboxV2EventsConsumer` for `t=37` |
| `core/lp/.../consumers/app_inbox_v2/AppInboxV2EventsConsumer.java` | Deserializes `payload`, dispatches to eventName-specific handler |
| `core/lp/.../consumers/app_inbox_v2/AppInboxV2PersistEventHandler.java` | Builds the NB trigger message |
| `core/lp/.../events/dto/appinboxv2/AppInboxV2EventPayloadPojo.java` | Jackson deserialization target |
| `core/nb-common/.../triggermessageserialization/TriggerTargetMessageSerializer.java` | PB3 serialization of the trigger |
| `core/nb-common/.../messageserialization/TMessageStringSerializer.java` | String-format serialization (legacy) |

---

### Stage 3 — LP → NB: HTTP POST /enqueue

**Framework call at LP** (`GenericEventProcessor.java:1907`):
```java
private void pushNBUpdate(BasicDBObject profile, List<String> qMessages, int maxEventLcTs) {
    final int shard = getShardFromIJ(profile);
    if (qMessages.size() == 0) return;
    BasicDBObject meta = new BasicDBObject();
    meta.put("pn", profile.getLong("pn", 0));
    meta.put("pm", profile.getLong("pm", 0));
    gateway.updateNB(shard, qMessages, meta, maxEventLcTs);
}
```

**Gateway.updateNB** (`core/lp/.../events/Gateway.java:282`):
```java
BasicDBObject payload = new BasicDBObject();
payload.put("meta", meta);          // pn, pm
payload.put("qms", qMessages);      // list of serialized triggers
payload.put(LC_REQUEST_TIMESTAMP, maxEventLcTs);
ClientSync.recordQMessages(getHost(shard).replace("8080", "9090"),
        AppConstants.ACCOUNT_ID.get(), JsonUtils.toStrictJson(payload));
```

**ClientSync.recordQMessages** (`core/es-base/.../client/ClientSync.java:547`):
```java
recordInternal(HTTP_PREFIX + host + "/enqueue?a=" + account, entity, host, null);
```

HTTP POST to NB at port 9090.

---

### Stage 4 — NB /enqueue: deserialize + TimedQueue enqueue

**Entry:** `RequestProcessor.handle()` case `"/enqueue"` at `core/nb/src/com/wizrocket/nb/httpservices/RequestProcessor.java:338`.

Key steps (L340–406):
1. Parse body as `BasicDBObject`; read `meta` and `qms`.
2. For each `msg` in `qms`:
   - **`Message message = TriggerMessageDeserializer.deSerialize(s)`** — shared LP→NB deserializer, handles both PB3 and string formats. Do not open-code this branch anywhere else in NB (see `core/nb/CLAUDE.md`).
   - Fill `systemMessageId` if missing via `nbUtils.fillMessageId(...)`.
   - Set `AppConstants.ACCOUNT_ID` ThreadLocal (legacy — required by DAO reads).
   - Compute and set `prn` (profile-reach numeric) from mode + device.
   - `TargetingDebugManager.markForDebug(..., RECEIVED_AT_NB, ...)` — target-debug hook.
   - Attach `ProfileDTO`; if null, use `ProfileDTO.getEmptyProfileDTO()`.
   - **L405: `pushToFStore(message)`** — NOTE: this is a **different** FStore write for *inaction* offline qualification (only for `isInstantClosure()` targets). It is **not** the InboxV2 write path.
   - **L406: `TimedQueue.getInstance().enqueue(message)`** — enters the legacy dispatch queue.

**TimedQueue** (`core/nb/.../queues/TimedQueue.java`): Redis-backed queue keyed by scheduled minute. For live triggers, `scheduledForMinute == TRIGGER_EVENT_MINUTE (9999)` — the enqueue path rewrites it to the current minute so the worker picks it up immediately (`TimedQueue.java:74–77`, `firstLiveAttempt=true`).

**Key files:**

| File | Role |
|------|------|
| `core/nb/.../httpservices/RequestProcessor.java` | HTTP URL switch, `/enqueue` entry (L338) |
| `core/nb/.../entities/TriggerMessageDeserializer.java` | Wire → `Message` decode (PB3 + string branches, Profiler-instrumented) |
| `core/nb/.../queues/TimedQueue.java` | Legacy dispatch queue (Redis-backed) |

---

### Stage 5 — NB dequeue: NotificationQueueWorker + pre-dispatch validation

**Worker:** `NotificationQueueWorker.run()` (`core/nb/.../queueworkers/NotificationQueueWorker.java`), pulled by `NQWThreadPool`.

Per message:
1. **At-rest decryption** — `ProfileDTODecrypter` is called on the dequeued profile. Any encrypted PII fields (signalled by `Constants.CT_ENCRYPTION_ENABLED`) are decrypted before the message reaches the dispatcher. See `core/nb/CLAUDE.md` § Encryption for details.
2. Calls **`MessageDispatcher.processAndDispatch(message)`** (`core/nb/.../dispatchers/MessageDispatcher.java`) — channel routing entry.

**Pre-dispatch validation:** `MessagePreDispatchValidatorFacade.validateAndAcceptAction(Message)` at `core/nb/.../validator/MessagePreDispatchValidatorFacade.java`. Three phases:

| Phase | What runs | On failure |
|-------|-----------|-----------|
| Lock-free | `getListOfValidators(message)` — bloom filter, control group, unreachable profile, missing platform, ranking, message state, anonymous profile | Executes `Action` (decrement pending, journey step, etc.); short-circuits |
| Rate limiter | `ThrottledCampaignRateLimiter.tryAcquire` / `parkToReacquireLater` | Parks the message; retries |
| Locked | Per-target lock via `TargetAccountLocksCache`; runs frequency-cap + target-limits validators; on success `ti.markAsSent(shardId, 1)` inside the lock | Executes `Action`; short-circuits |

Only messages passing all three phases reach the channel dispatcher.

---

### Stage 6 — NB InboxV2 routing

**PushDispatcher branch** (`core/nb/.../dispatchers/PushDispatcher.java:130`):
```java
TargetingInfo inboxChildTI = ...;
boolean isInboxV2 = inboxChildTI != null && inboxChildTI.isInboxV2();
...
inboxStoreDispatchManager.handlePushInboxMessage(message, hasFailed, isInboxV2, inboxChildTI);
```

For a pure InboxV2 persistent trigger, `mode == NotificationInbox` — the message routes through `PushDispatcher`'s inbox side-effect handling (V2 targets don't have a "push" delivery of their own; the trigger IS the inbox write).

**InboxStoreDispatchManager** (`core/nb/.../dispatchers/notificationinbox/InboxStoreDispatchManager.java:54`):
```java
public void handlePushInboxMessage(PushMessage message, boolean hasFailed, boolean isInboxV2,
        TargetingInfo inboxChildTI) {
    if (isInboxV2) {
        inboxV2StoreManager.handleStoreMessage(message, inboxChildTI);
    } else {
        // V1 store path
    }
}
```

**InboxV2StoreManager.handleStoreMessage** (`core/nb/.../dispatchers/notificationinbox/inboxv2/InboxV2StoreManager.java:40`):
```java
public void handleStoreMessage(PushMessage pm, TargetingInfo inboxChildTI) {
    int inboxChildId = pm.getTargetingInfo().getInboxTargetIdCreatedWithPush();
    ProfileReach.Device device = pm.getDevice();
    int prn = ProfileReach.encode(ProfileReach.Mode.NotificationInbox, device);

    InboxStoreMessage iMessage = new InboxStoreMessage(pm);
    iMessage.setPrn(prn);
    iMessage.setTargetID(inboxChildId);
    iMessage.setTargetingInfo(inboxChildTI);
    iMessage.setSendToDevices(pm.getSendToDevices());
    iMessage.setProfileDTO(pm.getProfileDTO());

    handleStoreMessage(iMessage);       // → inboxV2DispatchHandler.sendStoreMessage
}
```

**InboxV2DispatchHandler.sendStoreMessage** (`core/nb/.../dispatchers/notificationinbox/inboxv2/InboxV2DispatchHandler.java:73`):
```java
public void sendStoreMessage(InboxStoreMessage message) {
    persistentQueue.addToNonBlockingPath(message);
}
```

**Key files:**

| File | Role |
|------|------|
| `core/nb/.../dispatchers/PushDispatcher.java` | Detects `isInboxV2` on the target, routes to inbox-store manager |
| `core/nb/.../dispatchers/notificationinbox/InboxStoreDispatchManager.java` | V1 vs V2 branch |
| `core/nb/.../dispatchers/notificationinbox/inboxv2/InboxV2StoreManager.java` | V2-specific entry — builds `InboxStoreMessage` from `PushMessage` |
| `core/nb/.../dispatchers/notificationinbox/inboxv2/InboxV2DispatchHandler.java` | Facade for the persistent queue + content Redis rails |

---

### Stage 7 — NB persistent queue → FStore

From `sendStoreMessage`, the message flows through two async layers before FStore. See **`dispatch_write_path.md`** for the full thread inventory and batching detail. Summary:

```
InboxV2PersistentQueue.addToNonBlockingPath(message)
    ↓ (on-thread: in-memory map)
InboxV2QueueWorker (single daemon thread)
    ↓ file queue (persistent, at-most-once)
fstore-inbox-pq-worker
    ↓ deserialize + enrich (cache lookups)
InboxV2BatchProcessor.addMessage(...)
    ↓ (batches by account + shard)
InboxV2Dispatcher (gRPC batch call to FStore)
    ↓
FStore row: {accountId, userId, targetId, batchId, wzrkId, isDeliveredOnce=false, ttl, ...}
```

**Key files:**

| File | Role |
|------|------|
| `core/nb/.../dispatchers/notificationinbox/inboxv2/InboxV2PersistentQueue.java` | File-backed persistent queue |
| `core/nb/.../dispatchers/notificationinbox/inboxv2/InboxV2QueueWorker.java` | Drains persistent queue → batch processor |
| `core/nb/.../dispatchers/notificationinbox/inboxv2/InboxV2BatchProcessor.java` | Batches messages by shard, hands off to dispatcher |
| `core/nb/.../dispatchers/notificationinbox/inboxv2/InboxV2Dispatcher.java` | gRPC client to FStore |
| `ai_docs/inbox/app_inbox_v2/dispatch_write_path.md` | Full thread inventory + queue layer detail |

Parallel rail: **MVI inbox content Redis write** — `InboxV2ContentPersistentQueue → InboxV2ContentQueueWorker → MviInboxContentWriter → Redis PSETEX`. Best-effort at-most-once for the inbox message body (LC read-time reference), separate from the FStore pointer. See `InboxV2DispatchHandler.sendContentToRedis(...)` and `dispatch_write_path.md` § Content Rail.

---

## Data Flowing on the Wire

### LC → LP (via Redis)

Single RPUSH per SDK request, one event array carrying [SDK events + supplementary Notification Sent + supplementary InboxV2Persist]:

```json
{
  "meta": {
    "a": 1200000000, "g": "guid-xyz", "i": 6927, "j": 6927,
    "ts": 1783701997, "es": 0, "lcTs": 1783701997,
    "af": {...}, "ip": "...", "dt": 1, "os": 1, ...
  },
  "events": [
    {"t": 2, "ts": 1783701997, "eName": "Charged", "eb": {...}, ...},
    {"t": 2, "ts": 1783701997, "eName": "Notification Sent",
        "eb": {"targetIdToPivot": {"1783699389": "wzrk_default"}, "CT Source": "Mobile"}, ...},
    {"t": 37, "ts": 1783701997, "payload": {
        "eventName": "InboxV2Persist",
        "targetId": 1783699389,
        "batchId": 20260710,
        "i": 6927,
        "j": 6927,
        "ts": 1783701997,
        "deviceOrdinal": 1
    }}
  ]
}
```

### LP → NB (HTTP POST /enqueue)

```json
{
  "meta": {"pn": <profile-numeric>, "pm": <profile-numeric>},
  "qms": ["<serialized trigger 1>", "<serialized trigger 2>", ...],
  "lcTs": 1783701997
}
```

Each `qms[i]` is either PB3-base64 (if `getRolloutLPPB3Serialization` is on for the account) or `TMessageStringSerializer` legacy format. Both decode to a `Message` object on NB side via `TriggerMessageDeserializer.deSerialize(...)`.

### Trigger message contents (post-deserialization at NB)

Key fields on the `Message` object relevant to InboxV2:

| Field | Source | Used by |
|-------|--------|---------|
| `mode = NotificationInbox` | LP `AppInboxV2PersistEventHandler` (`modeDevice` encoding) | NB routing to `InboxStoreDispatchManager` |
| `targetId` | LC `container.getI()`, propagated | NB target lookup, FStore row key |
| `batchId` | LC (from `DateTime.getYMD`) | Attribution, TTL computation |
| `iid` / `idn` | LC `container.getI/J()` | User identity, FStore userId |
| `profileDTO` | LP `output.profile` at handler time | NB dispatch, decryption |
| `ts` | LC insertion timestamp | Event time in FStore |
| `scheduledForMinute = TRIGGER_EVENT_MINUTE (9999)` | LP handler | NB TimedQueue rewrites to current minute |

---

## Failure Modes and Handling

| Stage | Failure | Handling |
|-------|---------|----------|
| LC container serialization | Exception building `InboxV2PersistRequestDto` | Caught by outer inbox-manager try; error logged via `Profiler.report`; SDK still gets the inbox response |
| LC Redis RPUSH | Redis down / write timeout | Handled by `MobileAppRequestContainer`'s existing Redis push path (same as regular SDK events); backup Redis fallback |
| LP JSON parsing | Malformed `payload` | `AppInboxV2EventsConsumer.doConsume` catches, `Profiler.report("[InboxV2] Dropping: malformed payload ...")`, returns Continue |
| LP handler exception | `TriggerTargetMessageSerializer` throws, etc. | `AppInboxV2PersistEventHandler` catches, `Profiler.report("[DROPPING] [InboxV2Persist] Failed ...")`. Trigger dropped, batch continues. |
| LP → NB HTTP | Socket timeout / NetworkEx | `GenericEventProcessor` at L1146–1160: on timeout, drops with `Profiler.report`; on connection failure, pushes back to Redis via `Retry.serialise(...)` for retry |
| NB deserialization | Bad PB3 / bad string | `RequestProcessor` L418–421 catches per-message, logs, other messages in batch continue |
| NB TimedQueue enqueue | Redis down | Same Redis client / fallback as legacy path |
| NB pre-dispatch validation | fcap block, unreachable, DND, control group | `Action` chain runs (decrement pending, mark control group, etc.); message dropped from dispatch |
| NB InboxV2 store queue | Persistent queue full / write error | Retried on next queue-worker cycle (queue is persistent → survives process restart) |
| FStore gRPC | gRPC error / timeout | `InboxV2BatchProcessor` handles retry / re-enqueue; see `dispatch_write_path.md` |

---

## Design Notes

**Why "supplementary request" instead of a separate RPUSH:**
LP's `GenericEventProcessor.consume()` has an early-return at L793:
```java
if (eventsProcessed.size() == 0) return;
```
where `eventsProcessed` is populated by `addToEventProcessedList`, which excludes `APP_INBOX_V2` (and `UNIFIED_INBOX`). If InboxV2Persist rode a standalone Redis push, its batch would have no other events → `eventsProcessed == 0` → early return → NB update skipped → trigger lost.

By clubbing the trigger into the SDK request's own batch, `eventsProcessed >= 1` from the real events (Charged, Notification Sent), so the L793 check passes and the framework fires both ES update (with real events) and NB update (with our trigger from `output.getqMessages()`).

**Why profile isn't on the wire (LC → LP):**
Earlier design carried `profile: BasicDBObject` inside `InboxV2PersistEventPojo`. That was dropped because:
1. LP already has the authoritative profile in `output.profile`, populated by `preConsume` + record-handler responses.
2. Jackson-deserialized nested `BasicDBObject` values arrive as `LinkedHashMap`/`ArrayList`, breaking `ProfileDTOCreator`'s unchecked casts to `BasicDBList` / `BasicDBObject` on PB3 path. This would drop every real persistent trigger silently (caught by outer try/catch).

Using `output.profile` (native BSON, never round-tripped through Jackson) avoids both issues.

**Why NB re-uses the trigger message's `profileDTO` (no fresh ES fetch):**
For live triggers on the legacy `/enqueue` path, NB dispatches directly from the deserialized `Message.profileDTO`. There is no re-fetch from ES (unlike the new `/notify` path, which has `ProfileLookupBatchTask` for batched ES lookups). This is why LP embeds `profileDTO` in the trigger — it's the terminal profile snapshot NB will use for personalization + dispatch decisions.

---

## Cross-References

- `dispatch_write_path.md` — NB persistent queue + FStore write detail (Stage 7)
- `fstore_ops_batch_task.md` — LP FStore batch task for lifecycle events (Delivered/Deleted/etc — separate from this flow)
- `read_path_implementation_plan.md` — LC read-side (inbox fetch by SDK)
- `error-tracking-and-observability.md` — metrics + Profiler labels across the flow
- `dynamic-config-flags.md` — LC/LP/NB flags gating the flow (e.g. `getRolloutLPPB3Serialization`)
- `core/nb/CLAUDE.md` — NB module map (queues, validators, encryption, dispatchers)
- `ai_docs/Push/livePush.md` — analogous doc for live-triggered push campaigns

---

## Key Files Index

### LC
- `core/lc/src/java/com/wizrocket/lc/targeting/InboxManager.java`
- `core/lc/src/java/com/wizrocket/lc/dtos/appinboxv2/InboxV2PersistRequestDto.java`
- `core/lc/src/java/com/wizrocket/lc/request_containers/MobileAppRequestContainer.java`

### LP
- `core/lp/src/com/wizrocket/lp/events/GenericEventProcessor.java`
- `core/lp/src/com/wizrocket/lp/events/consumers/prerecordentries/AppInboxV2EventEntry.java`
- `core/lp/src/com/wizrocket/lp/events/consumers/app_inbox_v2/AppInboxV2EventsConsumer.java`
- `core/lp/src/com/wizrocket/lp/events/consumers/app_inbox_v2/AppInboxV2PersistEventHandler.java`
- `core/lp/src/com/wizrocket/lp/events/dto/appinboxv2/AppInboxV2EventPayloadPojo.java`
- `core/lp/src/com/wizrocket/lp/events/Gateway.java`

### Serialization (shared)
- `core/nb-common/src/com/wizrocket/nb/common/triggermessageserialization/TriggerTargetMessageSerializer.java`
- `core/nb-common/src/com/wizrocket/nb/common/messageserialization/TMessageStringSerializer.java`

### Client HTTP
- `core/es-base/src/com/wizrocket/eventsstore/client/ClientSync.java`

### NB
- `core/nb/src/com/wizrocket/nb/httpservices/RequestProcessor.java`
- `core/nb/src/com/wizrocket/nb/entities/TriggerMessageDeserializer.java`
- `core/nb/src/com/wizrocket/nb/queues/TimedQueue.java`
- `core/nb/src/com/wizrocket/nb/queueworkers/NotificationQueueWorker.java`
- `core/nb/src/com/wizrocket/nb/dispatchers/MessageDispatcher.java`
- `core/nb/src/com/wizrocket/nb/validator/MessagePreDispatchValidatorFacade.java`
- `core/nb/src/com/wizrocket/nb/dispatchers/PushDispatcher.java`
- `core/nb/src/com/wizrocket/nb/dispatchers/notificationinbox/InboxStoreDispatchManager.java`
- `core/nb/src/com/wizrocket/nb/dispatchers/notificationinbox/inboxv2/InboxV2StoreManager.java`
- `core/nb/src/com/wizrocket/nb/dispatchers/notificationinbox/inboxv2/InboxV2DispatchHandler.java`
- `core/nb/src/com/wizrocket/nb/dispatchers/notificationinbox/inboxv2/InboxV2PersistentQueue.java`
- `core/nb/src/com/wizrocket/nb/dispatchers/notificationinbox/inboxv2/InboxV2QueueWorker.java`
- `core/nb/src/com/wizrocket/nb/dispatchers/notificationinbox/inboxv2/InboxV2BatchProcessor.java`
- `core/nb/src/com/wizrocket/nb/dispatchers/notificationinbox/inboxv2/InboxV2Dispatcher.java`
