# App Inbox V2 — Dynamic Config Flags

All LD / dynamic config flags used by App Inbox V2 across every component. The same flag key may be read independently by multiple modules from their own `DynamicConfig` instance.

---

## Quick Reference

| Flag Key | Module | Type | Default | Scope | Controls |
|---|---|---|---|---|---|
| `rollout-inbox-v2` | All modules | BooleanConfig | `false` | Account | Master feature gate for all V2 components |
| `rollout-dashboard-inbox-v2` | Dashboard | BooleanConfig | `false` | Account | Dashboard V2 UI visibility (campaign + mgmt) |
| `rollout-inbox-v2-max-persistence` | Dashboard | IntegerConfig | `30` | Account | Max message TTL in days (targeting write) |
| `inbox-v2-scan-limit` | LC, LP | IntegerConfig | `500` | Account | Max messages per InboxV2GetMessages request (LC); FStore scanByPrefix cap in profile merge (LP) |
| `lp-inbox-v2-ops-config` | LP | JSONDynamicConfig | `{"batchSize":100,"flushIntervalSeconds":5,"executorQueueCapacity":1000}` | Stack | InboxV2FStoreOperationsBatchTask tuning |
| `lp-inbox-v2-profile-merge-config` | LP | JSONDynamicConfig | `{"batchSize":50,"flushIntervalSeconds":5,"executorQueueCapacity":500}` | Stack | InboxV2FStoreProfileMergeBatchTask tuning |

---

## `rollout-inbox-v2`

**Type:** BooleanConfig  
**Default:** `false`  
**Scope:** Account  
**Modules:** LC, LP, Server-Side Inbox, Dashboard

Master feature gate for App Inbox V2. Must be enabled consistently across all four modules — partial enablement causes silent mismatches (e.g. LP processes events but dashboard never writes them).

| Module | Accessor | Effect when `true` |
|--------|----------|--------------------|
| LC | `InboxDynamicConfig.isInboxV2EnabledForAccount()` | Gates `InboxV2Servlet` — rejects V2 API calls when off |
| LP | `LPDynamicConfig.rolloutInboxV2()` | Enables `APP_INBOX_V2` event processing in `GenericEventProcessor` |
| Server-Side Inbox | `InboxDynamicConfig.isInboxV2EnabledForAccount()` | Enables V2 logic in server-side inbox service |
| Dashboard | `DashboardDynamicConfig.getRolloutInboxV2()` | Gates V2 write path in `TargetingBaseAction`; switches TTL format from flat int to structured object |

---

## `rollout-dashboard-inbox-v2`

**Type:** BooleanConfig  
**Default:** `false`  
**Scope:** Account  
**Module:** Dashboard

Controls visibility of App Inbox V2 UI features inside the dashboard application (campaign creation UI, inbox message management views). Separate from `rollout-inbox-v2` so the backend write path and the UI can be rolled out independently.

---

## `rollout-inbox-v2-max-persistence`

**Type:** IntegerConfig  
**Default:** `7`  
**Scope:** Account  
**Module:** Dashboard  
**Accessor:** `DashboardDynamicConfig.getRolloutMaxPersistenceValue()`

Maximum TTL in days that a V2 inbox message can be persisted. Enforced at campaign creation / targeting write time in the dashboard. Value is per-account overridable.

---

## `inbox-v2-scan-limit`

**Type:** IntegerConfig  
**Default:** `500`  
**Scope:** Account  
**Modules:** LC, LP

Same flag key, read independently by each module via its own `DynamicConfig` instance.

| Module | Accessor | Effect |
|---|---|---|
| LC | `LcDynamicConfig.inboxV2ScanLimit()` | Max messages fetched per `InboxV2GetMessages` request in `InboxV2GetMessagesService` |
| LP | `LPDynamicConfig.inboxV2ScanLimit()` | Max FStore entries scanned per `scanByPrefix` call during profile merge in `InboxV2FStoreProfileMergeBatchTask` |

If a user's inbox hits the LP cap during merge, lex-tail entries under `+iid` are silently dropped (logged at P2). Raise per account if merge truncation is observed.

---

## `lp-inbox-v2-ops-config`

**Type:** JSONDynamicConfig  
**Default:** `{ batchSize: 100, flushIntervalSeconds: 5, executorQueueCapacity: 1000 }`  
**Module:** LP  
**Scope:** Stack  
**Accessor:** `LPDynamicConfig.getInboxV2OpsConfig()`  
**Consumer:** `InboxV2FStoreOperationsBatchTask` (via `buildConfig()` at construction time)

Tuning config for the DELIVERED / READ / DELETE FStore batch task. Read once at LP startup — changes require LP restart to take effect.

| JSON field | Default | Controls |
|------------|---------|----------|
| `batchSize` | `100` | Max DTOs drained per flush |
| `flushIntervalSeconds` | `5` | Max time between flushes |
| `executorQueueCapacity` | `1000` | In-memory queue depth before back-pressure |

---

## `lp-inbox-v2-profile-merge-config`

**Type:** JSONDynamicConfig  
**Default:** `{ batchSize: 50, flushIntervalSeconds: 5, executorQueueCapacity: 500 }`  
**Module:** LP  
**Scope:** Stack  
**Accessor:** `LPDynamicConfig.getInboxV2ProfileMergeConfig()`  
**Consumer:** `InboxV2FStoreProfileMergeBatchTask` (via `buildConfig()` at construction time)

Tuning config for the profile-merge (j-takes-precedence) FStore migration batch task. Also read once at LP startup.

| JSON field | Default | Controls |
|------------|---------|----------|
| `batchSize` | `50` | Max DTOs drained per flush |
| `flushIntervalSeconds` | `5` | Max time between flushes |
| `executorQueueCapacity` | `500` | In-memory queue depth before back-pressure |

---

## Summary

| Flag | LC | LP | Server-Side Inbox | Dashboard |
|------|----|----|-------------------|-----------|
| `rollout-inbox-v2` | ✓ | ✓ | ✓ | ✓ |
| `rollout-dashboard-inbox-v2` | | | | ✓ |
| `rollout-inbox-v2-max-persistence` | | | | ✓ |
| `inbox-v2-scan-limit` | ✓ | ✓ | | |
| `lp-inbox-v2-ops-config` | | ✓ | | |
| `lp-inbox-v2-profile-merge-config` | | ✓ | | |
