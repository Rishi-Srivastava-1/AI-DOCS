# PBS — Inbox PR (App Inbox V2 Read Path)

**PR:** #42861 — Inbox V2 API Changes  
**Branch:** `ONC-351-read-inbox-v2`  
**Jira:** ONC-351  
**Modules touched:** `core/inboxv2/service/`, `core/lc/`

---

## Overview

Implements the SDK-facing read path for App Inbox V2. The SDK calls `/inbox/v2/getMessages`, receives its messages rendered with personalisation, and the system tracks first-delivery asynchronously. Cap-excess and non-persistent message cleanup are decoupled from the read path and handed off to the delete pipeline (wired in delete PR).

---

## PR Structure

### New Module: `core/inboxv2/service/`

| File | Role |
|------|------|
| `InboxV2FetchService` | Fetches messages from FStore via prefix scan, sorts by `sentTime` DESC, caps at 100, returns excess keys for pipeline deletion |
| `InboxV2FetchResponse` | Return type of `get()` — carries `messages` (top 100) + `excessKeys` (beyond cap) |
| `InboxV2FetchResult` | Single fetched entry: decoded `FStoreInboxKey` + decoded `InboxV2Message` |
| `FStoreInboxKeyUtils` | Encodes/decodes FStore key bytes ↔ `FStoreInboxKey` model |
| `InboxV2ServiceModule` | Guice module — installs `FStoreInboxCommonModule`, binds `FStoreInboxKeyUtils` + `InboxV2FetchService` |
| Serialization (`InboxV2MessageCodec`, etc.) | Versioned protobuf serialize/deserialize for `InboxV2Message` |

### LC Changes: `core/lc/`

| File | Role |
|------|------|
| `InboxV2Servlet` | SDK-facing async servlet at `/inbox/v2/*` — dispatches to `Map<URI, InboxV2Service>` factory |
| `InboxV2GetMessagesService` | Thin LC adapter — resolves identity, calls `InboxV2FetchService`, renders, formats HTTP response |
| `InboxV2Renderer` | Resolves personalisations via `SdkTargetingInfoProvider`, builds content via `InboxContentBuilder`, identifies non-persistent + first-delivered messages |
| `InboxV2RenderResult` | Renderer output: `renderedMessages`, `nonPersistentKeys`, `deliveredFirstTimeMessages` |
| `InboxV2RenderedMessage` | Single rendered message with `id`, `isRead`, `wzrkId`, `createdTs`, `wzrkTtl`, `content` |
| `InboxV2Service` (interface) | `handleRequest(accountId, body, os)` — implemented by `InboxV2GetMessagesService` |
| `InboxV2Result` | HTTP result wrapper: `success`, `httpStatus`, optional `BasicDBObject` payload |
| `InboxV2ErrorType` | Error enum → `toResult()` |
| `InboxV2APIModule` | Guice module — installs `InboxV2ServiceModule`, binds servlet factory |

### Removed from PR (moved to delete PR)

| Removed | Reason |
|---------|--------|
| `InboxV2BatchDeleteTask` | Was doing direct LC → FStore deletes; now deletion goes through LC → Redis → LP → FStore pipeline |
| `fetchService.deleteKeys()` | Non-persistent key cleanup moved to delete PR |
| `core-utils` dep in `inboxv2-service/pom.xml` | Only needed for `InboxV2BatchDeleteTask` |
| `InboxV2BatchDeleteTask.shutdown()` in `LcInitializer` | Removed with the class |

---

## LLD

### Old Flow (before PR changes)

```
SDK
 │
 ▼ POST /inbox/v2/getMessages
InboxV2Servlet (LC)
 │
 ▼
InboxV2GetMessagesService (LC)
 │ esHelper.getIJForGuid() → iid, idn
 │
 ▼
InboxV2FetchService.get(accountId, iid, idn)
 │ scanByPrefix (up to 200)
 │ filter expired
 │ sort sentTime DESC
 │
 ├─── if > 100 ──► deleteExcess()
 │                     │
 │                     ▼
 │              InboxV2BatchDeleteTask     ← DIRECT LC → FStore
 │                     │
 │                     ▼
 │                  FStore.batchDelete()
 │
 ▼ top 100 results
InboxV2Renderer.render()
 │ → renderedMessages
 │ → nonPersistentKeys
 │
 ├─── fetchService.deleteKeys(nonPersistentKeys)
 │                     │
 │                     ▼
 │              InboxV2BatchDeleteTask     ← DIRECT LC → FStore
 │                     │
 │                     ▼
 │                  FStore.batchDelete()
 │
 ▼
HTTP Response → SDK
```

---

### New Flow — Inbox PR

```
SDK
 │
 ▼ POST /inbox/v2/getMessages
InboxV2Servlet (LC)
 │
 ▼
InboxV2GetMessagesService (LC)
 │ esHelper.getIJForGuid() → iid, idn
 │
 ▼
InboxV2FetchService.get(accountId, iid, idn)
 │ scanByPrefix (up to 200)
 │ filter expired
 │ sort sentTime DESC
 │
 ▼ InboxV2FetchResponse
 ├── messages      → top 100 InboxV2FetchResult
 └── excessKeys    → List<FStoreInboxKey> beyond cap
                        (collected, NOT deleted here)
 │
 ▼
InboxV2Renderer.render()
 │ → renderedMessages
 │ → nonPersistentKeys   (collected, NOT deleted here)
 │
 ▼
HTTP Response → SDK


  excessKeys ──────────┐
                       │  ← wired in delete PR
  nonPersistentKeys ───┘    (InboxV2DeletedEventPusher, isSystemDeleted=true)
```

---

### Full Flow — After Delete PR Merges

```
SDK
 │
 ▼ POST /inbox/v2/getMessages
InboxV2Servlet (LC)
 │
 ▼
InboxV2GetMessagesService (LC)
 │
 ├──► InboxV2FetchService.get()
 │         │
 │         ▼ InboxV2FetchResponse
 │    ┌────┴──────────────┐
 │  messages           excessKeys
 │    │
 │    ▼
 │  InboxV2Renderer.render()
 │    ├── renderedMessages
 │    └── nonPersistentKeys
 │
 │  [system deletes — LC → Redis → LP → FStore]
 ├──► InboxV2DeletedEventPusher(excessKeys,       isSystemDeleted=true)
 └──► InboxV2DeletedEventPusher(nonPersistentKeys, isSystemDeleted=true)
           │
           ▼ Redis  (InboxV2Deleted event)
           │
           ▼ LP: AppInboxV2EventsConsumer
           │
           ▼ AppInboxV2DeletedEventHandler
           │  isSystemDeleted=true → skip stats
           │
           ▼ InboxV2FStoreDeleteBatchTask
           │
           ▼ FStore.batchDelete()

 [SDK-triggered deletes — separate endpoint]
SDK ──► DELETE /inbox/v2/deleteMessages
           │
           ▼ InboxV2DeleteMessagesService (LC)
           │
           ▼ InboxV2DeletedEventPusher(messages, isSystemDeleted=false)
           │
           ▼ Redis → LP → AppInboxV2DeletedEventHandler
               isSystemDeleted=false → mark targeting_stats + FStore delete
```

---

## Key Design Decisions

### 1. LC is read-only for FStore
All FStore writes (deletes) go through `LC → Redis → LP → FStore`. LC only reads from FStore during `getMessages`. This keeps LP as the sole owner of FStore mutations.

### 2. `InboxV2FetchResponse` decouples scan from deletion
`InboxV2FetchService` returns excess keys to the caller instead of deleting them internally. This respects the module boundary — `inboxv2-service` has no dependency on LC's Redis infrastructure.

### 3. `isSystemDeleted` flag separates housekeeping from user-intent deletes
Cap-excess and non-persistent deletes are system-initiated — they should not inflate the `deleted` targeting stat. The `isSystemDeleted=true` flag gates `TargetingCountManager.mark()` in `AppInboxV2DeletedEventHandler`.

### 4. Inbox PR vs Delete PR responsibility split

| Concern | Inbox PR | Delete PR |
|---------|----------|-----------|
| Read path (`getMessages`) | ✓ | — |
| Cap enforcement (collect excess keys) | ✓ | — |
| Non-persistent key identification | ✓ | — |
| System delete via pipeline | — | ✓ |
| SDK-triggered delete endpoint | — | ✓ |
| Targeting stats for user deletes | — | ✓ |
