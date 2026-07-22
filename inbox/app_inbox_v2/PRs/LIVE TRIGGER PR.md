# PBS — Live Trigger PR (App Inbox V2)

**PR:** #43058 — Live Trigger changes  
**Branch:** `ONC-449-live-trigger-app-inbox`  
**Base branch:** `ONC-350-Dispatch-Write-Path` (NB dispatch → FStore write path lives here)  
**Jira:** ONC-9  
**Modules touched:** `core/lc/`, `core/lp/`, `core/dao/`

> **NB changes are NOT in this PR.** The full NB-side dispatch stack (`InboxV2DispatchHandler`, `InboxV2Dispatcher`, `InboxV2BatchProcessor`, `InboxV2ContentHandler`, `FStoreInboxResultTracker`, etc.) was merged via the base branch `ONC-350-Dispatch-Write-Path`. This PR only adds the LC trigger and LP serialization that feed into that existing NB pipeline.

---

## Overview

App Inbox V2 live-trigger messages have two modes:

- **Non-persistent** — LC serves the message directly from the timed queue, no FStore write. Already handled by the existing `InboxManager` path.
- **Persistent (this PR)** — when `TargetingInfo.isPersistent()` is true, the message must be written to FStore so users see it on subsequent app opens. Requires a LC → LP → NB handoff.

This PR implements the persistent flow: LC detects a persistent V2 target at serve time, pushes a synthetic `InboxV2Persist` event to LP Redis, LP fetches `TargetingInfo` and serializes the trigger message into `BatchOutput.qMessages`, and the existing `GenericEventProcessor.pushNBUpdate()` path POSTs accumulated messages to NB's HTTP API.

---

## PR Structure

### LC Changes: `core/lc/`

| File | Role |
|------|------|
| `InboxManager` *(modified)* | Bifurcates V1 vs V2 inbox content at serve time. V2 messages get `isRead`/`isLive` flags. For persistent V2 targets: builds `InboxV2PersistEventPojo` and pushes via `InboxV2PersistEventPusher`. V1 messages are queued for deletion via `queueDeletionIfV1()` (extracted helper to de-dupe three previously identical blocks). V2 response list is always written even when empty for symmetric client contract |
| `InboxV2PersistEventPusher` *(new)* | Synchronous pusher — builds `RedisPayloadDto` from the pre-built pojo (using `iid`/`idn` from pojo fields in meta), pushes to Redis. Propagates exceptions to `InboxManager` so it can call `markError(DispatchFailed)` on failure — parity with V1 error handling |
| `InboxV2PersistEventPojo` *(new)* | `@Builder` DTO for the LC → LP wire payload: `eventName`, `targetId`, `batchId`, `iid`, `idn`, `ts`, `profile` (`BasicDBObject`), `deviceOrdinal` |

### LP Changes: `core/lp/`

| File | Role |
|------|------|
| `AppInboxV2PersistEventHandler` *(new)* | Handles `InboxV2Persist`. Re-deserializes `rawPayload.getRawJson()` into `AppInboxV2PersistEventPayloadPojo` (double-deserialization — base consumer can't know which sub-type to use). Fetches `TargetingInfo` via `targetingDao.getTargetForId()`. Serializes message via PB3 (`TriggerTargetMessageSerializer`) or string (`TMessageStringSerializer`) based on `LPDynamicConfig.getRolloutLPPB3Serialization()` rollout flag. Writes serialized string into `output.getqMessages()` |
| `AppInboxV2PersistEventPayloadPojo` *(new)* | LP-side mirror of `InboxV2PersistEventPojo`: `eventName`, `targetId`, `batchId`, `iid`, `idn`, `ts`, `profile`, `deviceOrdinal` |
| `AppInboxV2Module` *(new — replaces version from event tracking PR on this branch)* | Adds `INBOX_V2_PERSIST_EVENT_NAME` → `AppInboxV2PersistEventHandler` binding. Provides `LPDynamicConfig` via `@Provides @Singleton` (deferred singleton init to avoid circular dependency with the holder-pattern singleton) |

### DAO Changes: `core/dao/`

| File | Role |
|------|------|
| `TargetingInfo` *(extended)* | Added `isInboxV2()`, `isPersistent()`, `isAbsoluteTtl()` utility methods for V2 inbox feature flags |
| `TargetingInfoUtils` *(extended)* | `isV2TriggerInboxTarget()` — checks both `inboxVersion == 2` and `isInboxV2` boolean flag |

---

## LLD

### Persistent Live Trigger Flow

```
SDK requests inbox (standard InboxManager path)
 │
 ▼ InboxManager (LC) evaluates live target
 │  TargetingInfo.isInboxV2() && TargetingInfo.isPersistent() → persistent V2
 │
 │  buildInboxV2PersistEventPojo(accountId, targetId, batchId, iid, idn, ts, profile, deviceOrdinal)
 │
 ▼ InboxV2PersistEventPusher.push(accountId, pojo)  [synchronous, throws on failure]
 │  meta: { a, ts, i=iid, j=idn, es=BATCH }
 │  payload: InboxV2PersistEventPojo (full object serialized as event payload)
 │
 ▼ Redis  (InboxV2Persist event, eventType=APP_INBOX_V2)
 │
 ▼                 async, LP picks up from Redis
 │
AppInboxV2EventsConsumer (LP)
 │  routes by eventName → AppInboxV2PersistEventHandler
 │
 ▼ AppInboxV2PersistEventHandler
 │  Jackson.fromJsonString(rawPayload.getRawJson(), AppInboxV2PersistEventPayloadPojo)
 │  targetingDao.getTargetForId(targetId)  → TargetingInfo (drop if null/archived)
 │  encode modeDevice = ProfileReach.encode(NotificationInbox, device)
 │
 │  if rolloutLPPB3Serialization:
 │    TriggerTargetMessageSerializer.serialize(...)  → PB3 bytes → convertToPB3String()
 │  else:
 │    TMessageStringSerializer.serializeToString(...)
 │
 │  output.getqMessages().add(serialized)
 │
 ▼ GenericEventProcessor.pushNBUpdate()  [existing LP pipeline]
 │  HTTP POST accumulated qMessages → NB API
 │
 ▼ NB DispatchManager  [from ONC-350 base branch]
 │  dequeues from timed queue
 │  InboxV2MessageRouter routes based on TargetingInfo.isInboxV2()
 │
 ▼ InboxV2DispatchHandler → InboxV2Dispatcher → FStore.batchPut()
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
      "eventName": "InboxV2Persist",
      "targetId": 1777549260,
      "batchId": 20260429,
      "iid": 5647,
      "idn": 0,
      "ts": 1748677200,
      "profile": { ... },
      "deviceOrdinal": 1
    }
  }]
}
```

> **Contrast with delivered/deleted events:** persist uses `iid`/`idn` in meta (same as delivered). The payload is a flat object, not a `messages` list — persist carries one message per event (one target per SDK serve).

---

## Key Design Decisions

### 1. LC pushes persist event synchronously; exceptions propagate up
`InboxV2PersistEventPusher.push()` throws `Exception` to the caller. `InboxManager` catches it and calls `markError(DispatchFailed)` — consistent with how V1 inbox Redis failures are handled. There is no background executor here (unlike the delivered pusher) because the persistent live trigger path can tolerate the inline Redis push latency.

### 2. Double-deserialization in `AppInboxV2PersistEventHandler`
`AppInboxV2EventsConsumer` deserializes the outer event into `AppInboxV2EventPayloadPojo` (the base pojo with `messages` list, shared across all InboxV2 event handlers). The persist handler then calls `Jackson.fromJsonString(rawPayload.getRawJson(), AppInboxV2PersistEventPayloadPojo.class)` to get the correct fields (`targetId`, `batchId`, etc.). This is intentional — the consumer can't know which sub-type to use at parse time.

### 3. Serialization format gated by rollout flag
LP serializes the trigger message to NB in either PB3 (`TriggerTargetMessageSerializer`) or legacy string (`TMessageStringSerializer`) format, controlled by `LPDynamicConfig.getRolloutLPPB3Serialization()`. This allows gradual rollout of PB3 serialization per account without a code change.

### 4. `TargetingInfo` null → drop silently
If the target was archived or deleted between the time LC pushed the event and LP processed it, `targetingDao.getTargetForId()` returns null. The handler logs a `[DROPPING]` profiler report and returns — no retry. This is intentional: a deleted campaign should not write to FStore.

### 5. NB dispatch stack is in base branch, not this PR
All NB-side components (`InboxV2DispatchHandler`, `InboxV2Dispatcher`, `InboxV2BatchProcessor`, `InboxV2ContentHandler`, `FStoreInboxResultTracker`, etc.) were merged via `ONC-350-Dispatch-Write-Path`. This PR only wires the LC → LP side. The NB side processes whatever ends up in the timed queue via the existing `DispatchManager` thread — no NB changes required here.

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
