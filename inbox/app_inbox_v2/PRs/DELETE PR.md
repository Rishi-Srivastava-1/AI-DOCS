# PBS — Delete PR (App Inbox V2)

**PR:** #43681 — Delete inbox message API changes  
**Branch:** `ONC-352-delete-handling`  
**Base branch:** `ONC-352-event-tracking`  
**Jira:** ONC-352  
**Modules touched:** `core/lc/`, `core/lp/`, `core/utils/`

---

## Overview

Adds the delete pipeline for App Inbox V2 — two distinct delete scenarios share the same `LC → Redis → LP → FStore` path:

1. **SDK-triggered delete** — user explicitly deletes a message via `DELETE /inbox/v2/deleteMessages`. LP increments `deleted` in `targeting_stats` and hard-deletes the key from FStore.
2. **System delete** — cap-excess and non-persistent keys identified by the read path (`InboxV2FetchService` / `InboxV2Renderer`) flow through the same pipeline with `isSystemDeleted=true`. LP skips stats and does only the FStore hard-delete.

---

## PR Structure

### LC Changes: `core/lc/`

| File | Role |
|------|------|
| `InboxV2DeleteMessagesService` *(new)* | Handles `DELETE /inbox/v2/deleteMessages`. Parses SDK payload (BSON array with `meta` + `deleteMessages` objects), extracts `guid`, maps `?os=` URL param → `deviceOrdinal` via switch, calls `InboxV2DeletedEventPusher.createAndPushToRedis` with `isSystemDeleted=false` |
| `InboxV2DeletedEventPusher` *(new)* | Synchronous pusher — no background executor. Builds `InboxV2RedisEventPojo` with `eventName`, `messages`, `deviceOrdinal`, `isSystemDeleted`, wraps in `RedisPayloadDto` with GUID-based meta (`identities=[guid]`), pushes to Redis inline |
| `InboxV2RedisEventPojo` *(extended)* | Added `deviceOrdinal` and `isSystemDeleted` fields to the shared pojo (previously only `eventName` + `messages`) |
| `InboxV2GetMessagesService` | Wired to call `InboxV2DeletedEventPusher` for `excessKeys` (cap overflow) and `nonPersistentKeys` — both with `isSystemDeleted=true` |
| `InboxV2Servlet` | Routes `/inbox/v2/deleteMessages` to `InboxV2DeleteMessagesService` |
| `InboxV2APIModule` | Binds `InboxV2DeletedEventPusher` |

### LP Changes: `core/lp/`

| File | Role |
|------|------|
| `AppInboxV2DeletedEventHandler` *(new)* | Handles `InboxV2Deleted`. If `!isSystemDeleted`: calls `TargetingCountManager.mark(accountId, targetId, batchId, device, pivot, DELETED)` per message. Resolves `userId` from `output.profile` (same pattern as delivered handler). Enqueues `InboxV2DeleteDTO` into `InboxV2FStoreDeleteBatchTask` for all messages regardless of `isSystemDeleted` |
| `InboxV2FStoreDeleteBatchTask` *(new)* | Standalone batch task (does NOT extend `AbstractInboxV2FStoreBitUpdateBatchTask`). Groups by `userId`, calls `fsClient.batchDelete()` — no read-modify-write needed. Same file-queue overflow + drain pattern as the bit-update tasks |
| `InboxV2DeleteDTO` *(new)* | `accountId`, `userId`, `identifier`. Binary serialization: `int(4) + long(8) + short(2) + identifier_bytes` |
| `AppInboxV2Module` | Adds `INBOX_V2_DELETED_EVENT_NAME` → `AppInboxV2DeletedEventHandler` binding in map binder |

### Shared: `core/utils/`

| Constant | Value | Used for |
|----------|-------|----------|
| `AppConstants.MockEvents.INBOX_V2_DELETED_EVENT_NAME` | `"InboxV2Deleted"` | Event name key in LP handler map |
| `AppConstants.KnownKeys.INBOX_V2_MESSAGE_ID` | `"wzrk_mid"` | Message identifier field in SDK delete payload |
| `TargetingStatsDao.DELETED` | new constant | `TargetingCountManager.mark()` stat type |

---

## LLD

### Flow 1 — SDK-triggered delete

```
SDK
 │
 ▼ DELETE /inbox/v2/deleteMessages?os=Android
InboxV2Servlet (LC)
 │
 ▼
InboxV2DeleteMessagesService (LC)
 │  parse BSON → extract guid (meta object) + messages (deleteMessages object)
 │  mapOsToDeviceOrdinal("Android") → deviceOrdinal=1
 │
 ▼ InboxV2DeletedEventPusher (inline — no background executor)
 │  payload: { eventName=InboxV2Deleted, messages, deviceOrdinal, isSystemDeleted=false }
 │  meta:    { a, ts, ids=[guid], es=BATCH }
 │
 ▼ Redis  (InboxV2Deleted event)
 │
 ▼                 async, LP picks up from Redis
 │
AppInboxV2EventsConsumer (LP)
 │  routes by eventName → AppInboxV2DeletedEventHandler
 │
 ▼ AppInboxV2DeletedEventHandler
 │  isSystemDeleted=false → incrementStats():
 │    parse wzrkId → targetId, batchId
 │    map deviceOrdinal → ProfileReach.Device
 │    TargetingCountManager.mark(accountId, targetId, batchId, device, pivot, DELETED)
 │
 │  userId from output.profile (iid/idn resolved by LP pipeline)
 │  enqueue InboxV2DeleteDTO(accountId, userId, identifier) per message
 │
 ▼ InboxV2FStoreDeleteBatchTask
 │  group by userId
 │  fsClient.batchDelete(shard, INBOX_V2, contextualKeys)
 │
 ▼ FStore  (key removed entirely)
```

---

### Flow 2 — System delete (cap-excess / non-persistent)

```
InboxV2GetMessagesService (LC)
 │  InboxV2FetchService returns excessKeys (beyond cap of 100)
 │  InboxV2Renderer returns nonPersistentKeys
 │
 ▼ InboxV2DeletedEventPusher(excessKeys, isSystemDeleted=true)
 ▼ InboxV2DeletedEventPusher(nonPersistentKeys, isSystemDeleted=true)
 │
 ▼ Redis → LP → AppInboxV2DeletedEventHandler
 │  isSystemDeleted=true → skip TargetingCountManager.mark()
 │  enqueue InboxV2DeleteDTO per message (FStore delete still happens)
 │
 ▼ InboxV2FStoreDeleteBatchTask → FStore.batchDelete()
```

---

### Event Wire Format (Redis payload)

```json
{
  "meta": {
    "a": 1699358338,
    "ts": 1748677200,
    "ids": ["abc123guid"],
    "es": 3
  },
  "events": [{
    "t": 42,
    "payload": {
      "eventName": "InboxV2Deleted",
      "messages": [
        {
          "identifier": "1777549260",
          "wzrk_id": "1777549260_20260429",
          "wzrk_pivot": "wzrk_default"
        }
      ],
      "deviceOrdinal": 1,
      "isSystemDeleted": false
    }
  }]
}
```

> **Contrast with delivered event:** delivered uses `"i"` + `"j"` (numeric `iid`/`idn`) in meta. Delete uses `"ids"` (GUID string array). LP resolves identity from whichever field is present — the LP pipeline handles both patterns.

---

## Key Design Decisions

### 1. `isSystemDeleted` gates targeting stats
`AppInboxV2DeletedEventHandler` calls `TargetingCountManager.mark()` only when `!isSystemDeleted`. Cap-excess and non-persistent deletes are housekeeping — inflating the user-visible `deleted` stat with them would misrepresent campaign performance. FStore hard-delete always runs regardless.

### 2. GUID in meta (not `iid`/`idn`)
`InboxV2DeleteMessagesService` gets identity from the SDK request body (`g` field in the meta BSON object), not from cookie/auth identity resolution. GUID is what the SDK provides at delete time. LP resolves `userId` from `output.profile` (same as the delivered handler) once the LP pipeline resolves identity from GUID.

### 3. `deviceOrdinal` from URL param, not user profile
The `?os=` URL query param is the source for `deviceOrdinal`. LC does not read the user profile — it reads the OS from the request. Unknown/null values default to `Web` with `Profiler.report()` for telemetry.

### 4. `InboxV2DeletedEventPusher` is synchronous (no background executor)
Unlike `InboxV2DeliveredEventPusher` (which offloads to a dedicated thread pool), the deleted pusher runs inline. The delete endpoint is a low-frequency user action — not on the hot `getMessages` path — so the latency tradeoff of inline Jackson serialization is acceptable.

### 5. `InboxV2FStoreDeleteBatchTask` does not extend `AbstractInboxV2FStoreBitUpdateBatchTask`
Hard delete needs only `fsClient.batchDelete()` — no read-modify-write cycle, no idempotency check. The abstract base is structured around `isAlreadyApplied()` + `applyMutation()` hooks that make no sense for deletion. The task duplicates the batch infrastructure (thread pool, file queue, drain) instead of forcing a misfit abstraction.

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
