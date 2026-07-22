# InboxV2 FStore Operations Batch Task — Deep Dive

Deep dive into `InboxV2FStoreOperationsBatchTask` and its parent `AbstractInboxV2FStoreBatchTask` — the LP-side batching layer that funnels App Inbox V2 mutations (READ / DELIVERED / DELETE) into FStore.

Companion doc to the read/write path implementation plans. This file focuses on the **batch-and-flush machinery**, the timing model, failure handling, and the correctness lessons we learned while reviewing it.

---

## Table of contents

1. [Where this sits in the pipeline](#where-this-sits-in-the-pipeline)
2. [The two concrete implementations](#the-two-concrete-implementations)
3. [Config knobs: `batchSize`, `executorQueueCapacity`, `flushInterval`](#config-knobs)
4. [Data structures under the hood](#data-structures-under-the-hood)
5. [Why it's called `BlockingBatchTask` — the backpressure contract](#why-its-called-blockingbatchtask)
6. [FStore failure handling — the three-layer ladder](#fstore-failure-handling)
7. [The multi-mutation retry bug (and the fix we shipped)](#the-multi-mutation-retry-bug)
8. [Concrete timing scenarios](#concrete-timing-scenarios)
9. [Reference — key file locations](#reference)

---

## Where this sits in the pipeline

```
Client SDK → LC → LP → ES → NB → Delivery
                 │
                 └── NotifEventConsumer.handleInboxV2Notification*(...)
                          │
                          └── addToBatchTask(InboxV2FStoreEntryDTO)
                                    │
                                    ▼
                          InboxV2FStoreOperationsBatchTask
                          InboxV2FStoreProfileMergeBatchTask
                                    │
                                    ▼
                              FStore (gRPC)
```

Every inbox-v2 mutation event that arrives at LP is turned into a small `InboxV2FStoreEntryDTO` and handed to a batch task via `addToBatchTask`. The batch task accumulates DTOs, flushes them in groups, and writes to FStore in one shot.

Why batching? FStore is a network-attached gRPC service. Writing one row per event would kill throughput. Batching amortises the per-call overhead and turns N event writes into ⌈N/batchSize⌉ FStore RPCs.

---

## The two concrete implementations

`AbstractInboxV2FStoreBatchTask<T>` is the reusable batching skeleton. Exactly **two** concrete subclasses exist:

| Class | File | Purpose |
|---|---|---|
| `InboxV2FStoreOperationsBatchTask` | `core/lp/src/com/wizrocket/lp/service/inboxv2/InboxV2FStoreOperationsBatchTask.java` | READ bit, DELIVERED bit, hard DELETE |
| `InboxV2FStoreProfileMergeBatchTask` | `core/lp/src/com/wizrocket/lp/service/inboxv2/InboxV2FStoreProfileMergeBatchTask.java` | Migrate FStore entries from `+iid` to `-idn` on profile merge (j-wins) |

Each subclass is a `@Singleton` in LP with its **own** copy of the entire plumbing:

```
LP process
├── InboxV2FStoreOperationsBatchTask (Singleton)
│     ├── BlockingBatchTask<OpsDto>
│     │     └── keyToQueueMap: {acct-A → dequeA, acct-B → dequeB, ...}   ← per-account (Queue A)
│     ├── flushExecutor (1 worker thread named "inbox-v2-ops-bt")
│     │     └── ArrayBlockingQueue<Runnable>                             ← shared (Queue B)
│     └── scheduled flush timer (own thread)
│
└── InboxV2FStoreProfileMergeBatchTask (Singleton)
      ├── BlockingBatchTask<MergeDto>
      │     └── keyToQueueMap: {acct-A → dequeA', acct-B → dequeB', ...} ← per-account (Queue A)
      ├── flushExecutor (1 worker thread with its own name)
      │     └── ArrayBlockingQueue<Runnable>                             ← shared (Queue B)
      └── scheduled flush timer (own thread)
```

**Implications:**
- **No shared state between the two tasks.** Ops FStore writes and profile-merge FStore writes run on separate worker threads, backed by separate queues, timed by separate schedulers.
- A slow FStore path for merges cannot stall ops writes, and vice versa.
- Backpressure is scoped to one task — an ops burst that fills its `ArrayBlockingQueue` doesn't affect merges.
- **Memory ceiling doubles** — the worst-case pending-DTO ceiling for the LP process is the sum of both towers.

---

## Config knobs

Each concrete task builds a `BatchTaskConfig`:

```java
BatchTaskConfig.builder()
        .batchTaskName("INBOX_V2_OPERATIONS_BT")
        .threadName("inbox-v2-ops-bt")
        .batchSize(cfg.getInteger("batchSize",
                LPDynamicConfigConstants.DEFAULT_INBOX_V2_OPS_BATCH_SIZE))
        .executorQueueCapacity(cfg.getInteger("executorQueueCapacity",
                LPDynamicConfigConstants.DEFAULT_INBOX_V2_OPS_EXECUTOR_QUEUE_CAPACITY))
        .flushInterval(Duration.ofSeconds(cfg.getInteger("flushIntervalSeconds",
                LPDynamicConfigConstants.DEFAULT_INBOX_V2_OPS_FLUSH_INTERVAL_SECONDS)))
        .build();
```

Three knobs, three orthogonal concerns. Each controls a different layer of the pipeline.

### `batchSize` — per-account queue capacity

- **What it controls:** how many DTOs for a single `accountId` can pile up before that account's queue is drained.
- **Runtime data structure:** capacity of a `LinkedBlockingDeque<T>` inside a `LinkedBlockingBatchTaskQueue<T>`, held under `ConcurrentHashMap<String accountId, ...>` inside `BlockingBatchTask`.
- **Trigger type:** size-driven. When an account's queue hits `batchSize`, that queue's flush job is submitted immediately.
- **Tradeoff:** bigger = fewer FStore RPCs, better throughput, higher memory + latency for slow-moving accounts. Smaller = lower latency + memory, more RPCs.

### `flushInterval` — the wall-clock timer

- **What it controls:** the periodic wall-clock trigger for `flush()`, which walks every non-empty account queue and submits a job.
- **Runtime data structure:** period of a `ScheduledFutureTask` in a `ScheduledThreadPoolExecutor`'s `DelayedWorkQueue`.
- **Trigger type:** time-driven. Every `flushInterval`, low-traffic accounts get drained even if they never reached `batchSize`.
- **Why it exists:** without it, a low-traffic account's DTOs would sit in memory forever waiting for `batchSize` to be reached.
- **Tradeoff:** shorter = lower worst-case latency + more small batches; longer = better batching efficiency + higher tail latency.

### `executorQueueCapacity` — flush executor's work queue

- **What it controls:** how many pending flush jobs can queue up when the single worker thread is busy.
- **Runtime data structure:** capacity of an `ArrayBlockingQueue<Runnable>` inside a `ThreadPoolExecutor(corePool=1, maxPool=1)`.
- **Trigger type:** backpressure. When the queue is full, `ReEnqueueTaskRejectedExecutionHandler` blocks the producer via `queue.put(r)`.
- **Tradeoff:** bigger = absorbs bigger bursts; smaller = tighter memory ceiling under sustained pressure.

**Memory ceiling arithmetic** (upper bound):
```
peak pending DTOs ≤ executorQueueCapacity × batchSize
peak memory      ≈ executorQueueCapacity × batchSize × sizeof(DTO)
```

**Summary table:**

| Variable | Declared type | Runtime data structure | Trigger | Tradeoff |
|---|---|---|---|---|
| `batchSize` | `int` | `LinkedBlockingDeque<T>` capacity, one per account | Size | Throughput vs. latency + memory |
| `flushInterval` | `Duration` | Period of `ScheduledFutureTask` | Time | Batching efficiency vs. tail latency |
| `executorQueueCapacity` | `int` | `ArrayBlockingQueue<Runnable>` capacity | Backpressure | Burst absorption vs. worst-case heap |

---

## Data structures under the hood

The batch task pipeline has **two independent queues** at runtime.

```
producer thread (LP hot path)
        │
        │ addToBatchTask(dto)
        ▼
┌──────────────────────────────┐
│ Queue A: per-account queue   │  ← inside BlockingBatchTask
│ (one queue per accountId)    │     capacity = batchSize
└──────────────────────────────┘
        │
        │ when full OR flushInterval elapses
        │      → submit(runnable) to flushExecutor
        ▼
┌──────────────────────────────┐
│ Queue B: executor work queue │  ← ArrayBlockingQueue in ThreadPoolExecutor
│ (holds Runnables to run)     │     capacity = executorQueueCapacity
└──────────────────────────────┘
        │
        │ single worker thread pulls & runs
        ▼
        task(accountId, List<T>)     ← the actual FStore write
```

### Queue A — per-account, holds DTOs

- **Scope:** one instance **per `accountId`**, created lazily via `computeIfAbsent` inside `BlockingBatchTask.addData`.
- **Concrete type:** `LinkedBlockingBatchTaskQueue<T>` wrapping a `java.util.concurrent.LinkedBlockingDeque<T>`.
- **Bounded by:** `batchSize`.
- **Element type:** `T` — the DTO of the concrete subclass (e.g. `InboxV2FStoreEntryDTO`).
- **Producer behaviour when full:** `put()` on the deque **blocks** the caller until space frees up.

### Queue B — single, shared, holds Runnables

- **Scope:** exactly **one instance per batch task** (not per account).
- **Concrete type:** `java.util.concurrent.ArrayBlockingQueue<Runnable>` inside a `ThreadPoolExecutor(1, 1, 0, SECONDS, ...)`.
- **Bounded by:** `executorQueueCapacity`.
- **Element type:** `Runnable` — each element is a `() -> executeTaskForElements(key, queue)` lambda pinning a specific accountId's Queue A.
- **Producer behaviour when full:** `offer()` returns false → `ReEnqueueTaskRejectedExecutionHandler` kicks in → `queue.put(r)` blocks the caller until space frees up.

### Side-by-side

| | Queue A (`LinkedBlockingBatchTaskQueue<T>`) | Queue B (`ArrayBlockingQueue<Runnable>`) |
|---|---|---|
| Instances | N (one per accountId) | 1 (per batch task) |
| Package | `com.wizrocket.coreutils.batchtask` | `java.util.concurrent` |
| Underlying | Linked-node bounded deque | Fixed-length ring buffer |
| Element type | `T` (DTO) | `Runnable` (flush job) |
| Capacity | `batchSize` | `executorQueueCapacity` |
| Locking | Per-queue `ReentrantLock` inside deque; map is `ConcurrentHashMap` | Single `ReentrantLock` shared with the worker |
| On full | Producer blocks in `put()` | Rejected → re-enqueued (producer blocks in handler's `put()`) |

### Total DTOs pending — the correct formula

When Queue B has `N` Runnables pending, they reference `N` different account queues, each with its own current size.

```
Total DTOs pending = Σ size(queue_i)  for every Runnable_i currently in Queue B
```

Upper bound (each account queue capped at `batchSize`):
```
Max total DTOs pending ≤ executorQueueCapacity × batchSize
```

Multiplication gives you the **memory sizing ceiling**; the sum gives you the **actual count** at any instant.

### How each config knob maps to a structure

| Config | Structure it configures | Element |
|---|---|---|
| `batchSize` | Capacity of each `LinkedBlockingDeque<T>` (Queue A, one per accountId) | DTO |
| `executorQueueCapacity` | Capacity of the shared `ArrayBlockingQueue<Runnable>` (Queue B) | Runnable |
| `flushInterval` | Period of the `ScheduledFutureTask` in `ScheduledThreadPoolExecutor`'s `DelayedWorkQueue` | n/a |

### About `TimeUnit.SECONDS` in the flushExecutor

```java
new ThreadPoolExecutor(1, 1, 0, TimeUnit.SECONDS, ...)
```

`TimeUnit.SECONDS` here is the unit for `keepAliveTime = 0`. Because `corePoolSize == maxPoolSize == 1` and `allowCoreThreadTimeOut(true)` is never called, no thread ever times out — this parameter is inert. It's just filling a required argument slot in the constructor.

**The only time-driven component in the pipeline is `flushInterval`.** Queue B itself is purely event-driven — it reacts only to `submit()` calls and `take()` returns, no internal clock.

### `!` in jar paths

The `!` character in paths like `.../async-profiler-4.4.jar!/one/profiler/Events.class` is Java's **jar-URL separator**. Anything before `!` is the archive; anything after is the entry inside it. Part of the `jar:file:` URL scheme handled by `java.net.JarURLConnection`.

---

## Why it's called `BlockingBatchTask`

The "Blocking" prefix advertises a **backpressure contract**: under sustained overload, `addData()` will block the calling thread rather than silently drop data or grow memory unboundedly.

There are **two independent blocking points** on the producer's `addData` path:

### Block point #1 — Queue A (per-account deque) is full

Inside `LinkedBlockingBatchTaskQueue.addAndIsQueueFull`:

```java
public synchronized boolean addAndIsQueueFull(T item) throws InterruptedException {
    if (isShutdown) throw new RejectedExecutionException(...);
    this.queue.put(item);         // BLOCKS if size == batchSize
    return isFull();
}
```

Producers writing to a specific account whose deque is full will stall on `deque.put`. Producers writing to other accounts are unaffected.

### Block point #2 — Queue B (executor work queue) is full

When a size-trigger submit happens and `ArrayBlockingQueue` is at capacity, the `ReEnqueueTaskRejectedExecutionHandler` runs on the producer thread and calls `queue.put(r)` — which blocks until a Runnable is drained.

### How `put()` actually works when the queue is full

`ArrayBlockingQueue.put(e)` does **not** insert into a full queue. Under the hood:

1. Acquires the queue's `ReentrantLock`.
2. Checks `count == items.length`. If true, calls `notFull.await()` — atomically releases the lock and parks the thread.
3. Stays asleep until a worker calls `take()`, which after dequeuing calls `notFull.signal()`.
4. Producer wakes, re-acquires lock, re-checks condition, then enqueues. Signals `notEmpty`. Releases lock.

Net effect: **the producer stalls exactly as long as the worker takes to finish its current task**. The Runnable is never lost. This converts a hard rejection into producer-side backpressure — the release valve that keeps the pipeline correct under overload.

### `ReEnqueueTaskRejectedExecutionHandler`

Full implementation (`core/core-utils/src/main/java/com/wizrocket/coreutils/executors/ReEnqueueTaskRejectedExecutionHandler.java`):

```java
public void rejectedExecution(Runnable r, ThreadPoolExecutor executor) {
    try {
        long now = System.currentTimeMillis();
        executor.getQueue().put(r);                       // blocking put
        Profiler.report("Rejected task re-added successfully. Thread: "
                + Thread.currentThread().getName(), System.currentTimeMillis() - now);
    } catch (InterruptedException e) {
        CTLogger.e(..., "Thread interrupted while adding to queue", e);
        Thread.currentThread().interrupt();
    }
}
```

Instead of `AbortPolicy` (throw), `DiscardPolicy` (drop), or `CallerRunsPolicy` (run inline — unacceptable on the LP hot path), this handler blocks until the queue has room, then enqueues. Every stall is profiled — that's the observability signal that backpressure is active.

### Observability

`BlockingBatchTask.addData` measures its own wall-clock and reports if it spent more than `PROFILING_THRESHOLD` (5 seconds):

```
"BlockingBatchTask INBOX_V2_OPERATIONS_BT time taken to addData"
```

And the re-enqueue handler:

```
"Rejected task re-added successfully. Thread: inbox-v2-ops-bt"
```

Two orthogonal signals — one for block point #1, one for #2.

---

## FStore failure handling

Three layers of defense inside `InboxV2FStoreOperationsBatchTask.task()`.

### Layer 1 — Per-shard try/catch

The core loop:

```java
for (Map.Entry<Integer, List<...>> entry : bucketsByShard.entrySet()) {
    int shard = entry.getKey();
    List<InboxV2FStoreEntryDTO> shardDtos = entry.getValue();
    try {
        processShard(accountId, shard, shardDtos, fsClient);
    } catch (Exception e) {
        handleShardFailure(accountIdStr, shard, shardDtos, e);
    }
}
```

- Failures are shard-scoped. If shard 3 fails, shards 1/2/4 still commit.
- `try/catch` is inside the loop, so one exception doesn't abort the rest of the batch.

### Layer 2 — `GrpcErrorClassifier` splits transient vs. permanent

```java
public static Type classify(Throwable t) {
    if (t instanceof StatusRuntimeException sre) {
        return switch (sre.getStatus().getCode()) {
            case DEADLINE_EXCEEDED, UNAVAILABLE, RESOURCE_EXHAUSTED, ABORTED, UNKNOWN -> Type.TRANSIENT;
            default -> Type.PERMANENT;
        };
    }
    return Type.PERMANENT;
}
```

| gRPC code | Meaning | Verdict |
|---|---|---|
| `UNAVAILABLE` | FStore process down / network unreachable | TRANSIENT — retry |
| `DEADLINE_EXCEEDED` | Timeout | TRANSIENT — retry |
| `RESOURCE_EXHAUSTED` | Backpressure signal | TRANSIENT — retry |
| `ABORTED` | Concurrency conflict | TRANSIENT — retry |
| `UNKNOWN` | Server-side error | TRANSIENT — retry |
| `INVALID_ARGUMENT`, `NOT_FOUND`, `PERMISSION_DENIED`, etc. | Bad request / config | PERMANENT — drop |
| Any non-gRPC exception | Client-side bug | PERMANENT — drop |

**Permanent path:** metric + P2 profiler event, then drop. Loud, quantified loss — not silent.

### Layer 3 — Retry via LP's delayed Redis queue

Transient failures push back to LP's delayed queue via `InboxV2RetryRedisUtils.pushBack`. The pushBack:

1. Groups failed DTOs by `(userId, mutationType, deviceOrdinal, updateStats)`.
2. Wraps each group in `AppInboxV2RetryEventPojo` with `updateFstore=true`.
3. Chooses the Redis queue name as `accountId + "_" + esShardIdx` (**ES shard, not FStore shard** — the Javadoc calls this out explicitly).
4. Calls `lp.pushToDelayedQueue(...)`.

The delayed queue holds events for a fixed delay, then republishes them onto the regular LP queue. From there they flow through the normal handler and re-enter the batch task.

**No retry cap. No attempt tracking.** Per the pattern comment in `InboxV2RetryRedisUtils.pushBack`:

> "Matches Unified Inbox: pushes indefinitely until FStore recovers, no cap, no attempt tracking."

### Layer 4 (safety net) — Retry-path failure

If `pushBack` itself throws (Redis down, serialization bug):

```java
} catch (Exception retryEx) {
    Profiler.report(
            String.format("InboxV2 pushBack failed, dropping shard: shard=%d accountId=%s",
                    shard, accountIdStr),
            1, retryEx, CTLogger.Feature.INBOX_V2, CTLogger.Priority.P2);
}
```

Drop with visible logging. Prevents infinite loops that would deadlock the worker.

### Summary of the ladder

| Level | Trigger | Action | Data outcome |
|---|---|---|---|
| 1. Per-shard try/catch | Any exception from `processShard` | Route to `handleShardFailure` | Other shards still processed |
| 2. Classifier: PERMANENT | Client-side error | Drop + metric + P2 profiler event | Lost, but quantified |
| 3. Classifier: TRANSIENT | Server-side error | `pushBack` to LP delayed queue | Retried indefinitely until success |
| 4. Retry itself fails | Redis down / serialization bug | Drop + metric + P2 profiler event | Lost, but visible; prevents worker deadlock |

---

## The multi-mutation retry bug

### The bug

Inside `task()`, `deduplicateKeys` folded multiple DTOs for the same `(accountId, userId, identifier)` key into:
- `keyToMutations`: an `EnumSet<InboxV2MutationType>` of **all** mutation types seen.
- `keyToDto`: a single canonical DTO per key (DELETE wins; else first-seen).

`processShard` used **both** — applied every mutation bit-flag to the message, then persisted once.

But `handleShardFailure` used **only `keyToDto`** — pushed back the canonical DTO with its *one* `mutationType`. The mutation set was thrown away.

### Concrete failure

Batch: `[DTO_1 (DELIVERED, msg-A), DTO_2 (READ, msg-A)]` for the same user.

After dedup:
- `keyToMutations["...msg-A"] = {DELIVERED, READ}`
- `keyToDto["...msg-A"] = DTO_1 (DELIVERED)`

`processShard` fails with `UNAVAILABLE`. Retry pushBack list: `[DTO_1 (DELIVERED)]`. **READ is lost.** The read bit never flips on FStore.

### Truth table of what got lost

| Batch mutations for one key | Retry envelope carried | Lost on retry |
|---|---|---|
| `DELIVERED` × N | DELIVERED | nothing |
| `DELIVERED + READ` | DELIVERED only | **READ** |
| `READ + DELIVERED` | READ only | **DELIVERED (bit + stat)** |
| `DELIVERED + READ + DELETE` | DELETE only | DELIVERED stat (bit doesn't matter — row deleted) |

### The fix — shard-first refactor

Instead of "dedup globally → group deduped keys by shard → process each shard", the fix flips it to "bucket raw DTOs by shard → dedup **inside** each shard's process step". Since `userId → shard` is deterministic, all DTOs for one key land in the same bucket, so dedup semantics are identical. But now the raw shard bucket is what `handleShardFailure` receives — every original DTO survives retry.

**Before:**
```java
protected void task(...) {
    ...
    Map<String, Set<...>> keyToMutations = new LinkedHashMap<>();
    Map<String, DTO> keyToDto = new LinkedHashMap<>();
    deduplicateKeys(list, keyToMutations, keyToDto);

    Map<Integer, List<String>> byShard = keyToMutations.keySet().stream()
            .collect(Collectors.groupingBy(
                    key -> fsClient.pick(keyToDto.get(key).getUserId())));

    for (var shardEntry : byShard.entrySet()) {
        int shard = shardEntry.getKey();
        List<String> shardKeys = shardEntry.getValue();
        try {
            processShard(accountId, shard, shardKeys, keyToDto, keyToMutations, fsClient);
        } catch (Exception e) {
            handleShardFailure(accountIdStr, shard, shardKeys, keyToDto, e);  // ← loses mutations
        }
    }
}
```

**After:**
```java
protected void task(...) {
    ...
    Map<Integer, List<InboxV2FStoreEntryDTO>> bucketsByShard = list.stream()
            .collect(Collectors.groupingBy(dto -> fsClient.pick(dto.getUserId())));

    for (var entry : bucketsByShard.entrySet()) {
        int shard = entry.getKey();
        List<InboxV2FStoreEntryDTO> shardDtos = entry.getValue();
        try {
            processShard(accountId, shard, shardDtos, fsClient);
        } catch (Exception e) {
            handleShardFailure(accountIdStr, shard, shardDtos, e);  // ← every original DTO
        }
    }
}

private void processShard(int accountId, int shard,
        List<InboxV2FStoreEntryDTO> shardDtos, FSClientSync fsClient) {
    Map<String, Set<...>> keyToMutations = new LinkedHashMap<>();
    Map<String, DTO> keyToDto = new LinkedHashMap<>();
    deduplicateKeys(shardDtos, keyToMutations, keyToDto);  // scoped to this shard

    List<String> shardKeys = new ArrayList<>(keyToMutations.keySet());
    applyBitUpdates(accountId, shard, shardKeys, keyToDto, keyToMutations, fsClient);
    applyDeletes(accountId, shard, shardKeys, keyToDto, keyToMutations, fsClient);
}

void handleShardFailure(String accountIdStr, int shard,
        List<InboxV2FStoreEntryDTO> shardDtos, Exception e) {
    int count = shardDtos.size();
    ...
    retryRedisUtils.pushBack(shardDtos, accountIdStr);
}
```

### Why the retry envelope is correct after the fix

`InboxV2RetryRedisUtils.pushBack` already groups by `(userId, mutationType, deviceOrdinal, updateStats)`. Given `[DELIVERED_dto, READ_dto]`:
- Two groups (different `mutationType`) → two `AppInboxV2RetryEventPojo` envelopes.
- Each envelope's `eventNameFor(mutationType)` picks the correct downstream handler (`INBOX_V2_DELIVERED_EVENT_NAME` / `INBOX_V2_READ_EVENT_NAME`).
- Both land in Redis, republish, re-enter LP, re-enter batch task, dedup again on retry pass, apply both bits in one `batchPut`.

### Why not deep-copy DTOs?

Considered and rejected. The DTOs are treated as read-only throughout `task()`:
- `deduplicateKeys` stores **references**, not copies.
- `processShard` and its helpers only *read* DTO fields; they mutate the `InboxV2Message` deserialized from FStore, never the DTO.
- `pushBack` reads DTO fields to construct new envelope objects.

Deep copy would allocate on the hot path for no correctness benefit. Instead, `InboxV2FStoreEntryDTO` was migrated to `@Value @Builder` so the read-only contract is enforced structurally by the type system rather than by convention.

**DTO before:**
```java
@Builder
@Getter
@NoArgsConstructor
@AllArgsConstructor
public class InboxV2FStoreEntryDTO {
    private int accountId;
    ...  // non-final
}
```

**DTO after:**
```java
@Value
@Builder
public class InboxV2FStoreEntryDTO {
    int accountId;
    ...  // implicitly private final; class implicitly final
}
```

Value-based `equals`/`hashCode`/`toString` are a bonus — tests that use `verify(...pushBack(List.of(dto), ...))` become more robust to future refactors.

### Metric semantics change

The two counters below now count **raw DTOs** instead of **unique keys**:
- `LPMetrics.recordInboxV2RetryEnqueued(accountId, count)`
- `LPMetrics.recordInboxV2PermanentDrop(accountId, count)`

Under a same-key duplicate storm, the new count can be 2–3× the old count. Flag this in the PR description so dashboards don't chase a phantom regression at deploy time.

### Cases the fix handles

| Scenario | Result |
|---|---|
| Success path (no failure) | Identical to old behaviour — success case is functionally unchanged |
| Same-mutation duplicates (`[DELIVERED, DELIVERED]`) | Idempotent retry — no correctness issue |
| Different mutations, same key (`[DELIVERED, READ]`) | **Bug fixed** — both preserved on retry |
| All three mutations, same key | **Bug fixed** — all preserved (DELETE ultimately wins on retry) |
| Multiple keys, same shard | All DTOs pushed back on failure |
| Different keys, different shards | Per-shard isolation — only failed bucket retried |
| Permanent failure | Dropped with metric (unchanged) |
| pushBack itself fails | Dropped with P2 profiler event (unchanged) |

---

## Concrete timing scenarios

Assume config: `batchSize=100`, `flushInterval=5s`, `executorQueueCapacity=1000`. Task duration ~200 ms per FStore write.

### Scenario 1 — `batchSize` triggers (high-traffic account)

`acct-A` at 200 evt/sec:

| Wall clock | Event | Queue A | Queue B |
|---|---|---|---|
| `10:00:00.000` | dto1 arrives | 1 | 0 |
| `10:00:00.500` | dto100 arrives → isQueueFull=true | 100 → 0 (drained) | 1 pending |
| `10:00:00.500` | Worker picks up, runs `task()` | 0 | 0 (worker took it) |
| `10:00:00.700` | Worker done | ~40 (backfilled) | 0 |

Flushes every ~500 ms via `batchSize`. `flushInterval` never fires for this account.

### Scenario 2 — `flushInterval` triggers (low-traffic account)

`acct-B` at 5 evt/sec:

| Wall clock | Event | Queue A |
|---|---|---|
| `10:00:00.100` | dto1 | 1 |
| `10:00:03.000` | ~15 DTOs | 15 |
| `10:00:05.000` | **Timer fires**, drains | 15 → 0 |
| `10:00:10.000` | Timer fires again | ~25 → 0 |

First DTO's latency ≈ 5 s. Last DTO's latency ≈ 200 ms.

### Scenario 3 — total DTOs pending is a sum, not a product

If Queue B has 3 Runnables pointing at three account deques with sizes 80, 12, 45:

```
Total DTOs pending = 80 + 12 + 45 = 137        ← actual
Upper bound        = 3 × 100      = 300        ← ceiling
```

Multiplication gives you the ceiling; sum gives you the actual count.

### Scenario 4 — burst absorption via `executorQueueCapacity`

Timer fires at `10:00:05.000` and finds 52 non-empty accounts → submits 52 Runnables. Queue B capacity is 50 — 2 hit `ReEnqueueTaskRejectedExecutionHandler` and get re-enqueued after a short delay. Under a single-worker pool (5 writes/sec), this backlog takes ~10 s to drain.

### Scenario 5 — FStore fully down

FStore returns `UNAVAILABLE` for every call.

| Time | State |
|---|---|
| `t=5s` | Flush fires → worker calls `fsClient.batchGet` → `StatusRuntimeException(UNAVAILABLE)` |
| `t=5s` | `handleShardFailure` → classifier says TRANSIENT → `pushBack(shardDtos)` → DTOs land on LP delayed Redis queue |
| `t=~5s + delayedQueue.delay` | Delayed queue republishes → LP consumer receives → back to batch task |
| `t=~10s+` | Another flush → same failure → loop |
| `t=Nmin` | FStore recovers → `batchPut` succeeds → stats marked |

DTOs are always durable somewhere (in-memory deque, in-transit to Redis, in delayed Redis queue, or in FStore). No moment where they exist only in a failed call's stack frame.

---

## Reference

### Key files

| File | Role |
|---|---|
| `core/lp/src/com/wizrocket/lp/service/inboxv2/AbstractInboxV2FStoreBatchTask.java` | Reusable batching skeleton — wires `BlockingBatchTask` + `ThreadPoolExecutor` |
| `core/lp/src/com/wizrocket/lp/service/inboxv2/InboxV2FStoreOperationsBatchTask.java` | READ / DELIVERED / DELETE ops task |
| `core/lp/src/com/wizrocket/lp/service/inboxv2/InboxV2FStoreProfileMergeBatchTask.java` | Profile-merge migration task |
| `core/lp/src/com/wizrocket/lp/service/inboxv2/InboxV2FStoreEntryDTO.java` | DTO carried through the ops batch task |
| `core/lp/src/com/wizrocket/lp/service/inboxv2/BatchTaskConfig.java` | Config holder (`batchSize`, `executorQueueCapacity`, `flushInterval`) |
| `core/lp/src/com/wizrocket/lp/service/inboxv2/GrpcErrorClassifier.java` | Transient vs. permanent gRPC classification |
| `core/lp/src/com/wizrocket/lp/redis/inboxv2/InboxV2RetryRedisUtils.java` | Push failed DTOs to LP delayed Redis queue |
| `core/core-utils/src/main/java/com/wizrocket/coreutils/batchtask/BlockingBatchTask.java` | Generic key-sharded batcher — the `ConcurrentHashMap<String, LinkedBlockingDeque<T>>` core |
| `core/core-utils/src/main/java/com/wizrocket/coreutils/batchtask/LinkedBlockingBatchTaskQueue.java` | The per-account deque wrapper |
| `core/core-utils/src/main/java/com/wizrocket/coreutils/executors/ReEnqueueTaskRejectedExecutionHandler.java` | Blocking re-enqueue handler for `ThreadPoolExecutor` rejections |

### Test file

`core/lp/tests/com/wizrocket/lp/service/inboxv2/InboxV2FStoreOperationsBatchTaskTest.java` — includes tests for multi-mutation retry preservation, per-shard isolation, dedup semantics, permanent/transient classification, and all `task()` integration cases.

### Related docs in this folder

- `dispatch_write_path.md` — end-to-end write path
- `read_path_implementation_plan.md` — read path plan
- `tracking.md` — general tracking

