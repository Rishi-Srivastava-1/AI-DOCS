# InboxV2 Read Path Implementation Plan (ONC-351)

## Context

The InboxV2 system stores App Inbox messages in FStore (RocksDB) instead of Redis, one message per key. The protocol (`core/inboxv2/`) and dispatch/write path (`core/nb/.../inboxv2/`) are already merged. This PR implements the **read path** — serving messages to SDK via a new servlet and post-serve deletion.

**Three coexisting paths after this change:**
1. INBOX (Unified Inbox) → Redis per-user blob → UNTOUCHED
2. NotificationInbox (isInboxV2=false) → Legacy byte-protocol Redis → UNTOUCHED
3. NotificationInbox (isInboxV2=true) → FStore one-key-per-message → **THIS WORK**

## Scope

**In scope:**
- Fetch messages from FStore (`InboxV2FetchService`) via `scanByPrefix`
- Serve via new servlet `/inbox/v2/getMessages` (MobileAppServlet-style JSON array with guid "g")
- Post-serve delete of non-persistent messages (`InboxV2BatchDeleteTask`)
- Cap enforcement: 100 messages max, lazy delete excess on read
- Feature flag: `isFStoreInboxAPIEnabledForAccount`

**Out of scope (future PRs):**
- Delivered flag tracking (no LP event push after serve)
- Mark as read endpoint (`/inbox/v2/markAsRead`)
- All LP event handling (no consumer, handlers, flag update batch task)
- Clicked tracking

## Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| User resolution | `EventStoreHelper.getJForK()` | Same async callback pattern as Unified Inbox |
| LP events | Not needed | Separate endpoint, no manual events like Unified Inbox |
| Flag updates | Out of scope | Future PR |
| ID mapping | Sequential counter (0, 1, 2...) | Per-response uniqueness only, simplest, no collision risk |
| **Portability** | **All business logic in `core/inboxv2/service/`** | **Future: may move to separate deployable LC. Service layer must be untouched — only write a new thin servlet wrapper.** |

---

## Architecture: Portability Principle

```
┌─────────────────────────────────────────────────────────┐
│  core/lc  (THIN WRAPPER — HTTP concerns only)           │
│                                                         │
│  InboxV2Servlet          → parse HTTP, auth, route      │
│  InboxV2ServletAdapter   → parse guid, resolve i/j,     │
│                            call service, write response  │
└────────────────────────┬────────────────────────────────┘
                         │ calls
┌────────────────────────▼────────────────────────────────┐
│  core/inboxv2/service  (ALL BUSINESS LOGIC — portable)  │
│                                                         │
│  InboxV2ReadService     → orchestrates fetch + render   │
│                           + cap + post-serve delete     │
│  InboxV2FetchService    → scanByPrefix → decode → sort  │
│  InboxV2MessageConverter→ InboxV2Message → InboxMessage  │
│  InboxV2BatchDeleteTask → shard-based async batchDelete │
│  InboxV2ReadResult      → response DTO (messages, count)│
└─────────────────────────────────────────────────────────┘
```

**If this API moves to a separate LC in the future:**
- Write a new thin servlet in the new LC
- Inject the same `InboxV2ReadService` from `core/inboxv2/service/`
- Zero changes to the service layer

---

## Milestone 1: Service Layer — `core/inboxv2/service/`

All business logic lives here. No LC/HTTP dependencies.

### File 1: `core/inboxv2/service/src/com/wizrocket/inbox/service/fstore/InboxV2MessageConverter.java`
- Stateless converter: `InboxV2Message + FStoreInboxKey → InboxMessage`
- Field mapping: targetId→targetingId, sentTime, expiryTime, variant, personalisations, previewContent
- `id` field: sequential counter assigned by caller (0, 1, 2...)
- isRead=false, isDeliveredOnce=false (flags out of scope)

### File 2: `core/inboxv2/service/src/com/wizrocket/inbox/service/fstore/InboxV2BatchDeleteTask.java`
- `@Singleton`, uses `BlockingBatchTask<byte[]>`
- Region key = shard (string) — all keys in batch target same shard → single `batchDelete` gRPC call
- Pattern follows: `InboxRedisFlagUpdateBatchTask` at `core/lp/.../InboxRedisFlagUpdateBatchTask.java`
- Constructor: `BlockingBatchTask<>("INBOXV2_BATCH_DELETE_BT", Duration.ofSeconds(5), 100, executor, this::task)`
- `addToBatchTask(int shard, List<byte[]> keys)` — public entry point
- `task(String shardStr, List<byte[]> keys)` → `fsClient.batchDelete(shard, keys)`
- `shutdown()` for clean shutdown registration

### File 3: `core/inboxv2/service/src/com/wizrocket/inbox/service/fstore/InboxV2FetchService.java`
- `@Singleton`
- Dependencies: `FSClientInboxSyncManager`, `FStoreInboxKeyUtils`, `InboxV2MessageCodec`, `InboxV2MessageConverter`, `InboxV2BatchDeleteTask`
- Method: `Optional<UserInbox> get(int accountId, long iid, long idn)`
- Logic:
  1. `userId = idn != 0 ? -idn : iid` (same convention as write path)
  2. `FSClientSync fsClient = syncManager.getClientForAccount()`
  3. `shard = fsClient.pick(userId)`
  4. `scanByPrefix(shard, accountId, userId, Context.INBOX_V2, emptyPrefix)`
  5. Decode: `FStoreInboxKeyUtils.decodeKey(pair.key)` + `InboxV2MessageCodec.deserialize(pair.value)`
  6. Convert `InboxV2Message` → `InboxMessage` via `InboxV2MessageConverter`
  7. Sort by sentTime desc, cap at 100
  8. If excess: queue oldest keys for async delete via `InboxV2BatchDeleteTask`
  9. Return `UserInbox`

**Reuse:** `FSClientSync.scanByPrefix()`, `FStoreInboxKeyUtils.decodeKey()`, `InboxV2MessageCodec.deserialize()` — all existing

### File 4: `core/inboxv2/service/src/com/wizrocket/inbox/service/fstore/InboxV2ReadResult.java`
- Response DTO: `List<RenderedInboxMessage> messages`, `int totalAvailableMessages`, `boolean truncated`
- Decouples service return from HTTP response format

### File 5: `core/inboxv2/service/src/com/wizrocket/inbox/service/fstore/InboxV2ReadService.java`
- `@Singleton` — the main orchestrator, contains ALL business logic
- Dependencies: `InboxV2FetchService`, `InboxRenderer`, `InboxV2BatchDeleteTask`
- Method: `InboxV2ReadResult getMessages(int accountId, long iid, long idn)`
- Logic:
  1. Build `UserKey(accountId, idn)`
  2. `InboxV2FetchService.get(accountId, iid, idn)` → `Optional<UserInbox>`
  3. If empty: return empty `InboxV2ReadResult`
  4. `InboxRenderer.personalise(userKey, inbox, 0)` → `List<RenderedInboxMessage>`
  5. Cap: 100 messages, 1MB response size limit
  6. Post-serve: collect non-persistent message keys, queue delete via `InboxV2BatchDeleteTask`
  7. Return `InboxV2ReadResult`

### File 6: `core/inboxv2/service/src/com/wizrocket/inbox/service/fstore/InboxV2ServiceModule.java`
- Guice module for service layer bindings
- Binds: `InboxV2ReadService`, `InboxV2FetchService`, `InboxV2MessageConverter`, `InboxV2BatchDeleteTask`
- `install(new FStoreInboxCommonModule())` — FStore client config

### Tests (all in `core/inboxv2/service/tests/`):
- `InboxV2MessageConverterTest.java` — field mapping, null personalisations
- `InboxV2FetchServiceTest.java` — empty result, normal fetch, >100 cap triggers delete, sort order
- `InboxV2ReadServiceTest.java` — happy path, empty inbox, 1MB cap, post-serve delete
- `InboxV2BatchDeleteTaskTest.java` — batch accumulation, task execution calls batchDelete

---

## Milestone 2: LC Thin Wrapper (`core/lc/`)

LC only handles: HTTP parsing, authentication, guid resolution, calling service, writing HTTP response. **Zero business logic.**

### File 1: `core/lc/src/java/com/wizrocket/lc/apis/targeting/send/InboxV2Servlet.java`
- `@WebServlet(urlPatterns = "/inbox/v2/*", asyncSupported = true)` extends `AbstractAPIDataPushServlet`
- Pattern: mirrors `UnifiedInboxServlet` (93 lines)
- `@Inject InboxV2ServletAdapter adapter`
- `@Inject InboxDynamicConfig dynamicConfig`
- `processRequest()`: auth → `preValidate()` (feature flag) → `adapter.handleGetMessages(accountId, body, response, callback)`
- `preValidate()`: check URI + `dynamicConfig.isFStoreInboxAPIEnabledForAccount().getValue(accountId)`
- `shouldThrottle()` returns false

### File 2: `core/lc/src/java/com/wizrocket/lc/service/inboxv2/InboxV2ServletAdapter.java`
- Thin adapter — bridges HTTP layer to service layer
- Dependencies: `InboxV2ReadService`, `EventStoreHelper`, `AccountsProvider`
- `handleGetMessages(int accountId, String body, BasicDBObject response, RequestCompletedCallback callback)`:
  1. Parse body as JSON array, extract meta with `"g"` (guid)
  2. Resolve i (iid) and j (idn) via `EventStoreHelper.getJForK()` — async callback
  3. If j==0: return error via callback
  4. Call `readService.getMessages(accountId, iid, idn)` — **this is the only line that touches the service**
  5. Format `InboxV2ReadResult` → `BasicDBObject` response
  6. `callback.done(result, accountId)`
- **No business logic** — just HTTP concerns (parsing, error mapping, response formatting)

### File 3: `core/lc/src/java/com/wizrocket/lc/service/inboxv2/InboxV2ErrorType.java` (enum)
- LC-specific HTTP error codes (maps service errors to HTTP status codes)
- USER_UNAVAILABLE, INTERNAL_ERROR, API_NOT_ENABLED_FOR_ACCOUNT, INCORRECT_REQUEST_JSON, INCORRECT_URI
- Same `createAPIResult()` pattern as `UnifiedInboxErrorType`

### File 4: `core/lc/src/java/com/wizrocket/lc/guice/InboxV2APIModule.java`
- `extends AbstractModule`
- Binds: `InboxV2ServletAdapter`
- `install(new InboxV2ServiceModule())` — pulls in entire service layer
- Pattern: `UnifiedInboxAPIModule` at `core/lc/.../UnifiedInboxAPIModule.java`

### Modify: LC module wiring
- Install `InboxV2APIModule` alongside `UnifiedInboxAPIModule`

### Tests (in `core/lc/tests/`):
- `InboxV2ServletTest.java` — feature flag disabled → 403, unknown URI → error, delegates to adapter
- `InboxV2ServletAdapterTest.java` — guid parsing, user resolution, calls readService, error handling

---

## Milestone 3: Feature Flag

### Modify: `core/server-side-inbox/service/src/com/wizrocket/inbox/service/InboxDynamicConfig.java`
- Add: `BooleanConfig isFStoreInboxAPIEnabledForAccount` with key `"rollout-fstore-inbox-api"`, default `false`
- One-line addition following existing `isUnifiedInboxAPIEnabledForAccount` pattern

---

## Milestone 4: Maven pom.xml Dependencies

### Modify: `core/inboxv2/service/pom.xml`
- Add: `core/core-utils` (for `BlockingBatchTask`) — if not already present
- Verify: `data-science/fstore-client`, `core/server-side-inbox/models` already present

### Modify: `core/lc/pom.xml`
- Add: `core/inboxv2/service` (pulls in FetchService, ReadService, etc.)
- Check if `data-science/fstore-client` is already transitively included

---

## Implementation Order

1. **Milestone 4** — Maven dependencies (unblocks everything)
2. **Milestone 3** — Feature flag (1 line, needed by servlet)
3. **Milestone 1** — Service layer (all business logic, testable in isolation)
4. **Milestone 2** — LC thin wrapper (depends on 1, 3)

---

## Key Reused Classes (No Changes Needed)

| Class | Location | Role |
|-------|----------|------|
| `InboxRenderer` | `core/lc/.../targeting/inbox/InboxRenderer.java` | Personalise InboxMessage → RenderedInboxMessage |
| `RenderedInboxMessage` | `core/lc/.../targeting/inbox/RenderedInboxMessage.java` | Response shape |
| `PersonalisationsDecoder` | `core/server-side-inbox/` | Decode stored personalisations |
| `InboxContentBuilder` | `core/server-side-inbox/` | Build message content |
| `TargetingInfoProvider` | `core/server-side-inbox/` | Fetch TargetingInfo |
| `InboxMessage` / `UserInbox` | `core/server-side-inbox/models/` | Domain models |
| `FStoreInboxKeyUtils` | `core/inboxv2/service/.../fstore/` | Key encode/decode |
| `InboxV2MessageCodec` | `core/inboxv2/service/.../serialization/` | Value serialize/deserialize |
| `FSClientInboxSyncManager` | `data-science/fstore-client/.../inboxv2/` | FStore client management |
| `BlockingBatchTask` | `core/core-utils/.../batchtask/` | Batch aggregation |

---

## New Files Summary

| # | File | Module | Lines (est) |
|---|------|--------|-------------|
| **Service layer (`core/inboxv2/service/`)** | | | |
| 1 | `InboxV2MessageConverter.java` | core/inboxv2/service | ~40 |
| 2 | `InboxV2BatchDeleteTask.java` | core/inboxv2/service | ~60 |
| 3 | `InboxV2FetchService.java` | core/inboxv2/service | ~80 |
| 4 | `InboxV2ReadResult.java` | core/inboxv2/service | ~15 |
| 5 | `InboxV2ReadService.java` | core/inboxv2/service | ~90 |
| 6 | `InboxV2ServiceModule.java` | core/inboxv2/service | ~20 |
| **LC thin wrapper (`core/lc/`)** | | | |
| 7 | `InboxV2Servlet.java` | core/lc | ~50 |
| 8 | `InboxV2ServletAdapter.java` | core/lc | ~70 |
| 9 | `InboxV2ErrorType.java` | core/lc | ~40 |
| 10 | `InboxV2APIModule.java` | core/lc | ~15 |

**Total: ~10 new files, ~480 lines + tests**

**Portability: If moving to separate LC, only files 7–10 need to be rewritten. Files 1–6 are reused as-is.**

---

## Verification

1. Build: `./mvnw test-compile -pl core/inboxv2/service,core/lc`
2. Run service tests: `./mvnw test -pl core/inboxv2/service -Dtest=InboxV2FetchServiceTest,InboxV2MessageConverterTest,InboxV2ReadServiceTest,InboxV2BatchDeleteTaskTest`
3. Run LC tests: `./mvnw test -pl core/lc -Dtest=InboxV2ServletTest,InboxV2ServletAdapterTest`
4. Spotless: `./mvnw spotless:apply -pl core/inboxv2/service,core/lc`
5. Local E2E: start local env, create InboxV2 campaign (isInboxV2=true), dispatch message, call `/inbox/v2/getMessages` with guid, verify response
