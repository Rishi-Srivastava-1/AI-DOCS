# Error Tracking & Observability in LP

Reference notes on the three telemetry mechanisms in LP (`logger`, `Profiler.report`, `LPMetrics`) and how they're used across the Unified Inbox and App Inbox V2 flows. Focused on error/failure tracking — retry semantics, drop points, and where each signal lands.

---

## 1. The three mechanisms

LP emits telemetry through three completely separate systems. They serve different purposes and land in different observability tools. Do not conflate them.

| | `logger.info/warn/error` | `Profiler.report(...)` | `LPMetrics.record*(...)` |
|---|---|---|---|
| **Purpose** | Free-text event log | Sampled aggregate telemetry with context strings | Counters / Timers / Gauges |
| **Backend** | SLF4J → logback → local file → Splunk forwarder | CleverTap in-house profiler → Splunk / Last9 | Micrometer → Prometheus → Grafana |
| **Where it lands** | Splunk `lp.log` index | Splunk Profiler index / Last9 | Prometheus TSDB, visible in Grafana |
| **Data shape** | One line per call, arbitrary text | Sampled bucket with name + count + duration + priority | Numeric time-series with tag labels |
| **Real-time?** | Near-real-time (forwarder lag) | Near-real-time (sampled) | ~15s scrape interval, aggregated |
| **Alertable?** | Yes (Splunk saved searches) | Yes (Splunk saved searches) | Yes (Alertmanager on Prometheus, better SLA) |
| **Best for** | Exact per-event context, debugging one incident | Aggregate error rates with a message string and priority | Rate/percentile queries, dashboards, low-latency alerting |
| **Worst for** | Rate math, per-tenant graphs | Real-time alerting, per-tenant graphs at scale | Attaching arbitrary context (each new tag value is a new time-series) |

### `logger.*`

Standard SLF4J. Free-form, one line per call. Used for context-heavy debug/error messages tied to a specific event. Rotation is handled by logback; a Splunk forwarder ships the files to the log index.

Search example (Splunk):
```
index=lp "InboxV2 pushBack failed"
```

### `Profiler.report(...)`

CleverTap's in-house telemetry. **Not** the same as JVM profiling — it's a sampled aggregate reporter.

Typical shape:
```java
Profiler.report(
    "UNIFIED_INBOX_FLAG_UPDATE_BT partially successful/failed for accountId: 123",
    durationMs,
    1,                              // sample rate
    CTLogger.Feature.UNIFIED_INBOX,
    CTLogger.Priority.P2);
```

Emits a Splunk-side aggregate: name, count, duration, feature, priority. Cheap on the JVM side, but the message string is baked in — so cardinality of unique strings can be high (accountId in the message → one series per account in Splunk). Search on the fixed prefix, not the full string.

Search example (Splunk):
```
index=profiler_lp "InboxV2 client-side FStore drop"
```

### `LPMetrics.record*(...)`

Wrapper over Micrometer. One method per named metric. Every call is a fixed-shape `Counter`/`Timer`/`Gauge` write on the global registry. Prometheus scrapes `/metrics` on port 8082 every ~15s.

Typical shape:
```java
LPMetrics.getInstance().recordInboxV2BatchTaskFStoreFailure(accountIdStr, batchTaskName);
```

See [Metrics anatomy](#4-metrics-anatomy) below.

---

## 2. When to use which

Rule of thumb: **use all three for a serious failure path; use only some for lighter signals.**

| Situation | logger | Profiler | LPMetrics |
|---|---|---|---|
| Unexpected exception you want a full stack trace for | ✓ (with `error(msg, t)`) | ✓ (P1) | ✓ (counter for rate) |
| Business-logic drop ("skipping this event because X") | | ✓ (P2) | ✓ (counter) |
| Retry enqueued | | ✓ (P2, low frequency) | ✓ (counter) |
| Latency measurement | | | ✓ (Timer) |
| One-line "processing account N" | ✓ | | |
| Health / queue length snapshot | | | ✓ (Gauge) |
| Debug trace during dev | ✓ | | |
| Per-user event trail | ✓ | | ✗ (never — cardinality) |

**Guiding principle:** if operators need to *alert* on a signal in real-time or *chart* it per tenant on Grafana, you need `LPMetrics`. If they need to *investigate* a specific failure with context, you need `logger` and/or `Profiler`. Most important failure paths deserve all three.

---

## 3. Retry vs drop — Unified Inbox vs App Inbox V2

Both flows are batch-task-based: dequeue LP events → apply mutation on external store → on failure, push back onto LP's delayed queue for retry. But the failure semantics differ meaningfully.

### 3.1 Pipeline shape

```
Unified Inbox                                    App Inbox V2 (PR #45337)
─────────────────                                ─────────────────────────
InboxRedisFlagUpdateBatchTask.task()             InboxV2FStoreOperationsBatchTask
  drain 5s / 100 DTOs                              drain 5s / 100 DTOs
     │                                                 │
     │ updateService.updateFlags(...)                  │ fsClient.batchGet/batchPut/batchDelete
     │  (Resilience4j retry + circuit breaker)         │  (raw gRPC — no local retry)
     ▼                                                 ▼
[Inbox Redis]  ← Lettuce driver                    [FStore]  ← gRPC
                                                       │
   on failure ↓                                        ↓ on failure
                                                       │
UnifiedInboxLPRedisUtils.pushBack                  GrpcErrorClassifier.classify()
   → LP delayed queue                                 ├─ PERMANENT → drop + counter
   → ~2-3 min later re-published                      └─ TRANSIENT → InboxV2RetryRedisUtils.pushBack
   → re-enters GEP as UNIFIED_INBOX                       → LP delayed queue
                                                          → ~2-3 min later re-published
                                                          → re-enters GEP as APP_INBOX_V2
```

### 3.2 Feature comparison

| Dimension | Unified Inbox | App Inbox V2 |
|---|---|---|
| Backend | Redis (Lettuce) | FStore (gRPC) |
| Error classifier | ✗ (any exception → retry) | ✓ `GrpcErrorClassifier` (TRANSIENT/PERMANENT) |
| In-call retry (Resilience4j) | ✓ | ✗ |
| Circuit breaker per shard | ✓ | ✗ |
| Delayed-queue retry | ✓ (no attempt cap) | ✓ (no attempt cap) |
| Stats coupled to storage success | ✗ (decoupled) | ✓ (inline `TargetingCountManager.markAsSent`/`mark(DELETED)`) |
| Prometheus counters | ✗ (none) | ✓ (3 counters) |
| Splunk visibility (Profiler) | ✓ | ✓ |
| Retry vs first-time observable in logs | ✓ (`isRetry` flag → separate Profiler line) | ✗ (identical handling) |

### 3.3 Where drops actually happen

Both flows have the same shape of drop points — the retry loop is robust to backend-down, but not to LP's own delayed-queue Redis being down.

**Unified Inbox drop paths:**

| Scenario | Handling | Outcome |
|---|---|---|
| Inbox Redis one shard down | `guardedUpdate` catches per shard → users diff'd → `pushBack` | **Retry** |
| Inbox Redis fully down | Same as above, all users → `pushBack` | **Retry** |
| Circuit breaker open on shard | Same catch path | **Retry** |
| LP delayed queue Redis down when `pushBack` runs | Outer catch in `task()` line 83 → Profiler P1 → return | **Drop** (no counter) |
| `AbortUpdateException` bubbles from `guardedUpdate` (line 224-225) | Outer catch → Profiler P1 → return | **Drop** (no counter) |
| Any other unhandled throwable in `task()` | Outer catch → Profiler P1 → return | **Drop** (no counter) |

**App Inbox V2 drop paths:**

| Scenario | Handling | Outcome |
|---|---|---|
| FStore shard transient failure (UNAVAILABLE, DEADLINE_EXCEEDED, RESOURCE_EXHAUSTED, ABORTED, UNKNOWN) | `handleShardFailure` → `pushBack` → delayed queue | **Retry** + `ct_lp_inbox_v2_retry_enqueued` |
| FStore permanent failure (INVALID_ARGUMENT, PERMISSION_DENIED, non-gRPC exception) | `handleShardFailure` → PERMANENT branch → Profiler P2 → return | **Drop** + `ct_lp_inbox_v2_permanent_drop` |
| LP delayed queue Redis down when `pushBack` runs | Inner catch in `handleShardFailure` → Profiler P2 → return | **Drop** (only `_batch_task_fstore_failure` fires, not `_retry_enqueued`) |

**Key invariant on the V2 side:**
```
ct_lp_inbox_v2_batch_task_fstore_failure  ≥
    ct_lp_inbox_v2_retry_enqueued (in DTOs) + ct_lp_inbox_v2_permanent_drop (in DTOs)
```
The gap is the "double failure" case: FStore failed *and* LP delayed-queue Redis also failed. Alerting on that gap (batch-task-failure counter moving but retry+drop counters flat) catches silent losses.

### 3.4 The three LPMetrics counters (V2)

| Method | Metric name | Tags | Unit | Fires on |
|---|---|---|---|---|
| `recordInboxV2BatchTaskFStoreFailure` | `ct_lp_inbox_v2_batch_task_fstore_failure` | account, `task_name` | **shards** | *any* shard failure |
| `recordInboxV2PermanentDrop` | `ct_lp_inbox_v2_permanent_drop` | account | **DTOs** | PERMANENT branch |
| `recordInboxV2RetryEnqueued` | `ct_lp_inbox_v2_retry_enqueued` | account | **DTOs** | After successful `pushBack` |

Note the unit split: shard-failure counts shards (one flaky shard is `+1` regardless of DTO count), the other two count DTOs (messages). Don't sum them on a dashboard.

Useful PromQL patterns:

```promql
# Overall failure rate per account
sum by (accountId) (rate(ct_lp_inbox_v2_batch_task_fstore_failure[5m]))

# Drop:retry ratio — how bad are the failures?
rate(ct_lp_inbox_v2_permanent_drop[5m])
  / (rate(ct_lp_inbox_v2_permanent_drop[5m]) + rate(ct_lp_inbox_v2_retry_enqueued[5m]))

# Silent-drop alarm — the double-failure case
  rate(ct_lp_inbox_v2_batch_task_fstore_failure[5m]) > 0
    and
  rate(ct_lp_inbox_v2_retry_enqueued[5m] + ct_lp_inbox_v2_permanent_drop[5m]) == 0
```

---

## 4. Metrics anatomy

The bits that are common to every `LPMetrics.record*` call.

### 4.1 Meter types

| Meter | Semantics | Example |
|---|---|---|
| `Counter` | Monotonically increasing count | `ct_lp_events_consumed`, all inbox v2 counters, `ct_lp_errors` |
| `Timer` | Duration → percentiles + SLO histogram buckets | `ct_lp_event_consume_latency`, all `*_update_latency` |
| `Gauge` | Instantaneous value that can go up/down | `ct_lp_redis_queue_length` |

Counters use a `DoubleAdder` (striped atomic add) — fixed-size regardless of value, cheap under concurrency. Timers use pre-computed SLO bucket arrays (`LP_TURN_AROUND_TIME_BUCKETS`, etc.) so histograms are comparable across metrics.

### 4.2 Tag structure

Every `LPMetrics` method starts with:
```java
.tags(DaoTagProvider.getAccountTags(accountId))
```

`DaoTagProvider.getAccountTags` (`core/dao/.../DaoTagProvider.java:56`) is the **cardinality guard**:

| Account condition | Tags returned |
|---|---|
| Parse-failed / unknown account | `accountId=<n>` only |
| Small account (<50k users, dynamic-config flag off) | `isSelfServe="true\|false"` **only** — no accountId, no name |
| Large account (≥50k users **or** flag on) | `accountId=<n>, accountName=<name>, isSelfServe=<bool>` |

Small accounts get aggregated into shared `isSelfServe` buckets to prevent per-tenant cardinality explosion. This means:
- **Small accounts do not appear in Grafana `accountId` dropdowns** — the label doesn't exist on their series.
- To debug a specific small account in Grafana, SRE must flip `enable-metric-tagging-with-account-info` on for that account. Historical metrics before the flip stay aggregated.
- Small-account debugging via logs (Splunk) is unaffected — logs aren't cardinality-guarded.

Common global labels are added by the observability framework at boot: `deployment`, `region`, `service`, `stack`, `host`.

### 4.3 Meter identity & registration

`Counter.builder(name).tags(...).register(registry)` is a **find-or-create** operation. Micrometer keys meters by `(name, sorted-tag-list, type)` and returns the same instance for equal keys. This makes `builder → register → increment` inline safe to call on hot paths — first call constructs, subsequent calls are HashMap lookups.

Once created, a meter lives for the JVM's lifetime. No TTL, no eviction. Heap grows with **unique (name, tag-combo) pairs**, not with time or traffic.

**Cardinality rule:** never tag with `userId`, `messageId`, request IDs, or free-form strings. Every new value = a new meter object + a new Prometheus time-series.

### 4.4 Pattern for a new metric

To add a new counter (concrete example, mirrors `recordInboxV2BatchTaskFStoreFailure`):

**Step 1 — constant in `LPMetricsConstants.java`:**
```java
public static final String CT_LP_INBOX_V2_BATCH_TASK_FSTORE_FAILURE =
        "ct_lp_inbox_v2_batch_task_fstore_failure";
```

**Step 2 — method in `LPMetrics.java`:**
```java
public void recordInboxV2BatchTaskFStoreFailure(String accountId, String taskName) {
    Tags tags = DaoTagProvider.getAccountTags(accountId).and(TASK_NAME, taskName);
    Counter.builder(CT_LP_INBOX_V2_BATCH_TASK_FSTORE_FAILURE)
            .tags(tags)
            .register(Metrics.getGlobalRegistry())
            .increment();
}
```

**Step 3 — call at the interesting site:**
```java
LPMetrics.getInstance().recordInboxV2BatchTaskFStoreFailure(accountIdStr, batchTaskName);
```

**Step 4 — test:** unit-test with a `SimpleMeterRegistry` set via the `@VisibleForTesting` constructor. Assert the counter exists and has the expected tags/value after invocation.

Style note: use the `Tags tags = ... .and(...)` form (matches every other multi-tag method in `LPMetrics`). The chained `.tag(k, v)` on the builder works identically but is inconsistent with the rest of the file.

---

## 5. Where the data goes / how to read it

### 5.1 The scrape endpoint

Each LP JVM exposes `/metrics` on port **8082** via `JettyServer.handle` (`core/lp/src/com/wizrocket/lp/JettyServer.java:99-127`). No auth in code. The port is firewalled off from the public internet in prod — reachable from in-VPC hosts (jump boxes, other services) and from the in-cluster Prometheus scraper. Same port also serves `/health` and `/ibsV2ForGoal` (unauthenticated admin mutation) — so the port is fundamentally trusted-network-only.

Binding: `NBUtils.getInstance().getMyIP()` — usually the host's private IP, not loopback. Confirm with:
```bash
ss -tlnp | grep 8082
```
The listener line shows exactly which address is bound.

### 5.2 Hitting `/metrics` locally

Local dev (host mode, LP running on Mac):
```bash
curl -s http://localhost:8082/metrics
```

If it hangs, LP is bound to a non-loopback IP; check `grep 'Binding to host' ${ROOT_DATA_DIR}/lp/logs/app/lp.log`.

Local dev (containerized mode, LP inside Podman): port 8082 is not published to host by default. `docker compose exec lp curl -s http://localhost:8082/metrics` from inside.

Local prometheus (`local-setup/configs/prometheus/prometheus.yml`) does **not** scrape LP — it only scrapes ES, cAdvisor, docker. So Grafana panels for LP will be empty locally even though the endpoint is live.

### 5.3 Hitting `/metrics` from an EC2/ECS jump box

If you're on an EC2 that's on the same VPC as an LP instance (typical for internal debugging on staging/QA/prod stacks):

```bash
# find the LP listener from LP host itself
ss -tlnp | grep 8082
#   → LISTEN 0 50 [::ffff:10.14.66.80]:8082 ... users:(("java",pid=...))

# use the private IP, not 127.0.0.1
curl -sf http://10.14.66.80:8082/health && echo
curl -s http://10.14.66.80:8082/metrics | head -20
```

For an ECS host running LP as a container with host networking (`network_mode: host`-equivalent), LP binds directly on the host's IP — `ss -tlnp` on the host still shows it.

### 5.4 Useful filter recipes

```bash
# InboxV2 counters
curl -s http://<host>:8082/metrics | grep '^ct_lp_inbox_v2'

# Unified Inbox pipeline metrics (framework-emitted with eventType/task_name tags)
curl -s http://<host>:8082/metrics | grep -E 'UNIFIED_INBOX|UnifiedInboxEventEntry'

# All LP-owned metric names
curl -s http://<host>:8082/metrics | grep '^ct_lp_' | awk -F'{' '{print $1}' | sort -u

# Total time-series count (heap-cost proxy)
curl -s http://<host>:8082/metrics | grep -cv '^#'

# Top 20 metric names by number of series (cardinality hotspots)
curl -s http://<host>:8082/metrics \
  | grep -v '^#' \
  | awk -F'{' '{print $1}' \
  | sort | uniq -c | sort -rn | head -20
```

### 5.5 "Tail -f" style watch

`/metrics` is a snapshot per request — no streaming. To watch counters tick:

```bash
# with watch installed
watch -n 1 -d 'curl -s http://<host>:8082/metrics | grep "^ct_lp_inbox_v2"'

# without watch
while true; do
  clear
  curl -s http://<host>:8082/metrics | grep '^ct_lp_inbox_v2'
  sleep 2
done
```

For event-level ordering (not aggregate rates), use logs instead:
```bash
tail -f ${ROOT_DATA_DIR}/lp/logs/app/lp.log | grep -i 'InboxV2\|FStore'
```

---

## 6. Heap, persistence & lifecycle

### 6.1 Heap footprint

Every registered meter lives in JVM heap on the LP process. Rough per unique time-series (name + tag combo):
- Counter: ~a few hundred bytes to ~1 KB (Meter.Id + DoubleAdder + Prometheus-side mirror)
- Timer: ~5–10 KB (22 SLO buckets + count + sum, plus mirror)

Heap is O(unique tag combinations), **not** O(time) or O(traffic). Counter *values* are fixed-size — a counter at 5 billion takes the same space as one at 5.

The scary case is unbounded tag values (userId, messageId, etc.) — meter count grows monotonically with traffic. This is what `DaoTagProvider`'s small-account bucketing exists to prevent.

Quick health check on any pod:
```bash
# is the series count stable, or climbing every scrape?
curl -s http://<host>:8082/metrics | grep -cv '^#'
sleep 15
curl -s http://<host>:8082/metrics | grep -cv '^#'
```

### 6.2 What happens on JVM restart / crash

Everything in `LPMetrics` is in-memory only. On restart:
- All counters reset to 0.
- All meters must be re-registered on first call (happens naturally as the code path runs).
- Any un-scraped increments since the last Prometheus scrape are lost — bounded by scrape interval (~15s).

Prometheus is designed for this. `rate()` and `increase()` detect counter resets (a drop in raw value) and adjust automatically. Restart shows as a small gap in graphs, not a corruption.

**Nothing about metrics survives a crash.** If a signal needs to survive JVM death (billing, event accounting), it must live in a different pipeline — Redis queues, MongoDB, EventStore counters. Metrics are for aggregate operational signals, not accounting.

### 6.3 Profiler and logs on restart

- Logs: file-based, survive restart (until log rotation).
- Profiler: buffered in-process, then shipped. Buffered-but-unshipped entries at crash time are lost. Sampling means loss is usually invisible at aggregate level.

---

## 7. Practical checklist for a new failure path

When adding a new code path where failures/drops/retries can happen (e.g., a new inbox mutation type, a new backend integration), install all three signals:

- [ ] `logger.error(msg, throwable)` at the failure site with enough context (accountId, key IDs) to grep in Splunk
- [ ] `Profiler.report(name, count, throwable, Feature.<F>, Priority.P1|P2)` — P1 for unexpected/severe, P2 for classified failures with retry
- [ ] `LPMetrics.record*` counter — separate counters for "failure occurred", "drop", "retry enqueued" (see V2 pattern)
- [ ] Never tag `LPMetrics` calls with per-user/per-message identifiers
- [ ] If the retry path itself can fail, that's a separate drop-and-count case
- [ ] If failure has a taxonomy (transient vs permanent, retriable vs poison), classify and drop poisonous ones explicitly rather than retrying forever

The Unified Inbox flow followed only the first two. The App Inbox V2 flow (PR #45337) followed all of them and added the taxonomy — that's the intended bar for new work.
