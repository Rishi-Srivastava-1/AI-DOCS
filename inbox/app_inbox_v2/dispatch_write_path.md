# InboxV2 Dispatch Write Path — Thread & Code Flow

## Overview

The InboxV2 dispatch write path delivers Notification Inbox V2 messages from NB (Notification Backend) to FStore. The dispatch thread never directly calls FStore. Instead, messages flow through a persistent file queue and two batching layers before reaching FStore via gRPC.

There is **only ONE flow** — every message always goes through: memory batch → file queue → fstore-inbox-pq-worker → FStore batch → gRPC. There is no "skip the file queue" path.

---

## Thread Inventory

There are **10 threads** involved (5 shared, 5 dedicated to InboxV2):

| # | Thread Name | Count | Type | What It Does |
|---|------------|-------|------|------|
| 1 | `dispatch-manager` | 5 | ThreadPoolExecutor | Shared pool for ALL NB channels (push, SMS, email, inbox). For InboxV2: puts message into in-memory map and returns immediately |
| 2a | `FSTORE_INBOX_QUEUE_BATCH_PERSIST-task` | 1 | SingleThreadExecutor | The worker — picks up tasks and writes serialized protobuf batches to the file queue |
| 2b | `FSTORE_INBOX_QUEUE_BATCH_PERSIST-flush` | 1 | ScheduledExecutor | The timer — fires every 2s, submits flush tasks to Thread 2a |
| 3 | `fstore-inbox-pq-worker` | 1 | Daemon Thread | FStore Inbox Persistent Queue Worker — reads from file queue, deserializes, decrypts, enriches from cache, puts into FStore batch map |
| 4a | `fstore-inbox-bt-es` | 1 | SingleThreadExecutor | The worker — picks up batched messages grouped by shard and makes gRPC `batchPut()` calls to FStore |
| 4b | `FSTORE_INBOX_BT-flush` | 1 | ScheduledExecutor | The timer — fires every 2s, submits flush tasks to Thread 4a |

### Thread Roles Simplified

- **Thread 2b and 4b** are just alarm clocks. They wake up every 2s, tell their worker "flush now", and go back to sleep.
- **Thread 2a and 4a** are the actual workers. They never decide on their own when to run — they execute tasks submitted by either the calling thread (capacity full) or the timer thread (2s flush).
- **Thread 3 (`fstore-inbox-pq-worker`)** is the bridge between the file queue and FStore. It does the heavy lifting: file reads, decryption, cache lookups.

---

## When Are Threads Created?

All 5 InboxV2-dedicated threads are created at **NB startup**, not when the first message arrives:

```
NB MainExecutor starts
    → Guice creates InboxV2PersistentQueue (@Singleton)
        → creates BatchPersistQueueUtility
            → creates BlockingBatchTask → Thread 2a + 2b alive
    → Guice creates InboxV2BatchProcessor (@Singleton)
        → creates BlockingBatchTask → Thread 4a + 4b alive
    → Guice creates InboxV2DispatchHandler (@Singleton)
    → calls InboxV2DispatchHandler.init()
        → starts Thread 3 (fstore-inbox-pq-worker)
```

They sit idle waiting for work until messages arrive.

---

## Thread Boundaries

A **thread boundary** is where one thread hands off work to another via a shared data structure (queue, buffer, executor). The calling thread drops the work and moves on without waiting.

There are **3 thread boundaries**:

```
[dispatch-manager pool: 5 threads]           <-- shared across ALL NB channels
        |
        |  puts message into ConcurrentHashMap (cheap, fast)
        v
=== BOUNDARY 1: in-memory BlockingBatchTask buffer ===
        |
        |  capacity full (calling thread submits task to 2a)
        |  OR 2s timer (Thread 2b submits task to 2a)
        v
[Thread 2a: FSTORE_INBOX_QUEUE_BATCH_PERSIST-task]   <-- serializes protobuf & writes to disk
        |
        |  queue.enqueue(bytes)
        v
=== BOUNDARY 2: file on disk (fstore-inbox-store) ===
        |
        |  queue.dequeue() — polls every 1s
        v
[Thread 3: fstore-inbox-pq-worker]           <-- reads file, deserializes, decrypts, enriches
        |
        |  puts message into ConcurrentHashMap grouped by shard
        v
=== BOUNDARY 3: in-memory BlockingBatchTask buffer ===
        |
        |  capacity=5 full (Thread 3 submits task to 4a)
        |  OR 2s timer (Thread 4b submits task to 4a)
        v
[Thread 4a: fstore-inbox-bt-es]              <-- gRPC batchPut to FStore (100ms timeout)
```

### Boundary 1: dispatch-manager → in-memory batch → file queue

- **Mechanism:** `BlockingBatchTask` with `ConcurrentHashMap<String, BatchTaskQueue>`
- **Trigger:** Batch capacity reached OR 2-second scheduled flush
- **Why:** Dispatch threads must not block on file I/O. Messages are buffered in memory first, then written to disk in batches. This reduces the number of file I/O operations (100 messages → 1 file write instead of 100).

### Boundary 2: file on disk → fstore-inbox-pq-worker thread

- **Mechanism:** File-based `CompressedPersistentQueue` named `fstore-inbox-store`
- **Trigger:** `fstore-inbox-pq-worker` thread polls every 1 second
- **Why:** Provides durability. If NB crashes, messages survive on disk and are processed on restart.

### Boundary 3: fstore-inbox-pq-worker → in-memory batch → FStore gRPC

- **Mechanism:** `BlockingBatchTask` grouped by `accountId|targetId|shard`
- **Trigger:** 5 messages per group OR 2-second scheduled flush
- **Why:** Batching multiple user writes into a single gRPC call reduces network round trips. Messages arriving in random order get grouped by shard so one gRPC call handles many users.

---

## Code Flow — Step by Step

### Step 1: DispatchManager routes to InboxV2

**File:** `core/nb/src/com/wizrocket/nb/dispatchers/DispatchManager.java`
**Runs on:** `dispatch-manager` thread (pool of 5, queue capacity 10)

```java
// DispatchManager.java:374-376
case NotificationInbox:
    handleNotificationInboxDispatch((InboxStoreMessage) message);
    break;

// DispatchManager.java:560-567
private void handleNotificationInboxDispatch(InboxStoreMessage message) {
    TargetingInfo ti = message.getTargetingInfo();
    if (ti != null && ti.isInboxV2()) {
        inboxV2StoreManager.handleStoreMessage(message);   // → InboxV2 path
    } else {
        appInboxStoreManager.handleStoreMessage(message);   // → App Inbox V1 path
    }
}
```

The `dispatch-manager` pool is shared across ALL channels. It handles push, SMS, email, webhooks, inbox — everything. Thread pool definition:

```java
// DispatchManager.java:101-103
private static final ExecutorService es =
    new ThreadPoolExecutor(5, 5, 0L, TimeUnit.MILLISECONDS, workQueue,
        CTThreadFactory.getThreadFactory("dispatch-manager"));
```

**Note:** Unlike InboxV2 (non-blocking), push notifications dispatch synchronously on this thread — the dispatch-manager thread directly makes the HTTP call to FCM/APNs. InboxV2 was designed to be non-blocking so FStore latency doesn't stall other channels.

### Step 2: InboxV2StoreManager handles device explosion

**File:** `core/nb/src/com/wizrocket/nb/dispatchers/notificationinbox/inboxv2/InboxV2StoreManager.java`
**Runs on:** `dispatch-manager` thread

If `sendToAllDevices=true` AND multi-app with multi-selection enabled, the message is exploded into N copies (one per device). Each copy is sent separately. Otherwise, a single message is sent as-is.

### Step 3: InboxV2DispatchHandler enqueues to non-blocking path

**File:** `core/nb/src/com/wizrocket/nb/dispatchers/notificationinbox/inboxv2/InboxV2DispatchHandler.java`
**Runs on:** `dispatch-manager` thread

```java
public void sendStoreMessage(InboxStoreMessage message) {
    persistentQueue.addToNonBlockingPath(message);  // returns immediately
}
```

This is the facade that orchestrates the three components: persistent queue, queue worker, and batch processor.

### Step 4: Message goes into the in-memory batch

**File:** `core/nb/src/com/wizrocket/nb/dispatchers/notificationinbox/inboxv2/InboxV2PersistentQueue.java`
**Runs on:** `dispatch-manager` thread

```java
public void addToNonBlockingPath(InboxStoreMessage message) {
    batchPersistQueueUtility.addToBatchTaskForPersist(
        "FSTORE_INBOX_QUEUE_BATCH_PERSIST", message);
}
```

**File:** `core/nb/src/com/wizrocket/nb/utils/BatchPersistQueueUtility.java`

```java
public void addToBatchTaskForPersist(String region, Message message) {
    try {
        batchQueuePersistTask.addData(region, MessageSerialization.toGenericMessage(message));
        // ↑ converts Java object to protobuf, puts into ConcurrentHashMap
        // This is just a map put — cheap and fast
    } catch (InterruptedException | RejectedExecutionException e) {
        addToQueue(message);  // ← FALLBACK: dispatch-manager thread writes directly to file
        if (e instanceof InterruptedException) {
            Thread.currentThread().interrupt();
        }
    }
}
```

**Important:** The message data is already encrypted (ProfileDTO, PlatformID) when it arrives from LP → NB. Thread 2a does not encrypt anything — it just serializes the Java object (with its already-encrypted fields) into protobuf bytes for file storage.

**On exception:** If the batch task rejects (executor shut down or overloaded), the dispatch-manager thread itself writes directly to the file queue. This is rare but means the dispatch-manager thread briefly blocks on file I/O as a safety net to avoid losing the message.

**Queue configuration:**
- Queue name: `fstore-inbox-store`
- Queue type: `SmallerPageSizePersistentQueue` (compressed)
- Batch window: 2 seconds
- Batch header: `{F, I, B, P}` bytes

--- **dispatch-manager thread is now FREE to handle other channels** ---

### Step 5: Thread 2a writes batch to file queue

**File:** `core/nb/src/com/wizrocket/nb/utils/BatchPersistQueueUtility.java`
**Runs on:** `FSTORE_INBOX_QUEUE_BATCH_PERSIST-task` thread (Thread 2a)

**Triggered by:** either the dispatch-manager thread (map queue full) or Thread 2b (2s timer), both of which submit a task to Thread 2a's executor.

Inside `BlockingBatchTask`, when a task is submitted (`BlockingBatchTask.java:170-183`):

```java
private void executeTaskForElements(String key, BatchTaskQueue<T> elements) {
    List<T> elementsList = elements.clearAndReturnAllElements();  // drain the queue
    if (elementsList.isEmpty()) return;
    batchedDataConsumer.accept(key, elementsList);  // → calls batchPersistToQueue()
}
```

The callback:

```java
void batchPersistToQueue(String region, List<GenericMessage> genericMessages) {
    // Serialize ALL accumulated messages into ONE protobuf batch
    NBBatchMessageProtobuf.BatchGenericMessage batch = getGenericMessageBatch(genericMessages);
    // ONE file write for the entire batch
    queue.enqueue(NBMessageProtobufsUtil.toByteArray(batch, header));
}
```

**Why batch before writing to disk?** Without batching, 100 messages = 100 file writes. With batching, 100 messages = 1 file write. Each `enqueue()` involves serialization, disk seek, and write syscall — doing it once per batch is significantly cheaper.

### Step 6: Thread 3 (fstore-inbox-pq-worker) reads from file queue

**File:** `core/nb/src/com/wizrocket/nb/dispatchers/notificationinbox/inboxv2/InboxV2QueueWorker.java`
**Runs on:** `fstore-inbox-pq-worker` daemon thread (Thread 3)

```java
public void start(BatchPersistQueueUtility batchPersistQueueUtility) {
    BasicThreadFactory factory = new BasicThreadFactory.Builder()
            .namingPattern("fstore-inbox-pq-worker")
            .daemon(true)
            .uncaughtExceptionHandler((t, e) -> log.error(CTLogger.Priority.P1,
                    "Uncaught exception in FStore inbox queue worker", e))
            .build();
    workerThread = factory.newThread(() -> runWorkerLoop(batchPersistQueueUtility));
    workerThread.start();
}

private void runWorkerLoop(BatchPersistQueueUtility batchPersistQueueUtility) {
    while (!shutdown) {
        try {
            Optional<ArrayList<Message>> fromQueue = batchPersistQueueUtility
                .getFromQueue(Constants.FSTORE_INBOX_BATCH_PERSIST_HEADER);

            if (fromQueue.isEmpty()) {
                EDUtils.sleep(1000L);   // nothing on disk, sleep 1 second, try again
            } else {
                processMessages(fromQueue.get());
            }
        } catch (Exception e) {
            log.error(CTLogger.Priority.P1, "FStore inbox queue worker error", e);
            EDUtils.sleep(1000L);
        } finally {
            AppConstants.ACCOUNT_ID.remove();  // clean up ThreadLocal
        }
    }
}
```

`getFromQueue` handles both batch and single entries (`BatchPersistQueueUtility.java:103-125`):

```java
public Optional<ArrayList<Message>> getFromQueue(byte[] skipHeader) throws Exception {
    byte[] data = queue.dequeue();          // read bytes from file
    if (data == null) return Optional.empty();

    if (NBMessageProtobufsUtil.containsHeader(data, skipHeader)) {
        // BATCH entry (written by Thread 2a) — header {F,I,B,P} present
        // deserialize batch protobuf → multiple messages
    } else {
        // SINGLE entry (written by dispatch-manager fallback on exception)
        // deserialize single message
    }
    return Optional.of(messages);
}
```

### Step 7: Thread 3 decrypts, enriches, and feeds to BatchProcessor

**File:** `core/nb/src/com/wizrocket/nb/dispatchers/notificationinbox/inboxv2/InboxV2QueueWorker.java`
**Runs on:** `fstore-inbox-pq-worker` thread (Thread 3)

```java
void processMessages(List<Message> messages) {
    for (Message message : messages) {
        // 1. Set account context (ThreadLocal)
        AppConstants.ACCOUNT_ID.set(message.getAccountID());

        // 2. Decrypt ProfileDTO (first time decryption happens in the entire flow)
        if (!profileDTODecrypter.decryptProfileDTOReadFromQueueAndMarkError(
                this.getClass(), message)) {
            continue;   // decryption failed → skip
        }

        // 3. Load account metadata and set timezone (from cache, not DB)
        AccountMeta meta = accountMetaDao.getNonNullAccountMeta(
                message.getAccountID(), true);  // true = fromCache
        DateTime.setTZ(meta.getTz());

        // 4. Load targeting info if not present (from cache, not DB)
        TargetingInfo targetingInfo = message.getTargetingInfo();
        if (targetingInfo == null) {
            targetingInfo = targetingDao.getTargetForId(true, message.getTargetID());
        }

        // 5. Build InboxStoreMessage and put into FStore batch map
        if (targetingInfo != null) {
            InboxStoreMessage iMessage = new InboxStoreMessage(message);
            iMessage.setTargetingInfo(targetingInfo);
            iMessage.setProfileDTO(message.getProfileDTO());
            batchProcessor.addMessage(iMessage);  // → puts into Thread 4's map
        }
    }
}
```

**Note:** `accountMetaDao.getNonNullAccountMeta(accountId, true)` and `targetingDao.getTargetForId(true, ...)` are cache lookups (the `true` flag means `fromCache`). They only hit the database on cache miss. Under normal operation these are cheap in-memory reads.

Thread 3 has **no knowledge of shards**. It just reads, decrypts, enriches, and passes the message forward.

### Step 8: Message enters the FStore batch map (shard grouping happens here)

**File:** `core/nb/src/com/wizrocket/nb/dispatchers/notificationinbox/inboxv2/InboxV2BatchProcessor.java`
**Runs on:** `fstore-inbox-pq-worker` thread (Thread 3) — but this is logically Thread 4's batching concern

```java
public void addMessage(InboxStoreMessage message) {
    long userId = ProfileUtil.getUid(message.getIid(), message.getIdn());
    int shard = fsClientSyncManager.getClientForAccount().pick(userId);
    String region = message.getAccountID() + "|" + message.getTargetID() + "|" + shard;

    try {
        batchTask.addData(region, message);
        // ↑ puts into ConcurrentHashMap grouped by accountId|targetId|shard
        // Thread 3 just does a map put and moves on to the next message
    } catch (InterruptedException | RejectedExecutionException e) {
        persistentQueue.addToQueue(message);  // fallback: write back to file queue
        if (e instanceof InterruptedException) {
            Thread.currentThread().interrupt();
        }
    }
}
```

**Why group by shard?** Messages arrive in random order. Without grouping:
```
msg1 (shard=0) → 1 gRPC call
msg2 (shard=2) → 1 gRPC call
msg3 (shard=0) → 1 gRPC call
= 3 gRPC calls
```

With grouping:
```
shard=0 → [msg1, msg3] → 1 gRPC call
shard=2 → [msg2]       → 1 gRPC call
= 2 gRPC calls
```

**BatchTask configuration:**
```java
this.batchTask = new BlockingBatchTask<>(
    "FSTORE_INBOX_BT",                                              // name
    Duration.ofSeconds(2),                                           // flush every 2s
    5,                                                               // capacity per group
    utils.getNewTaskExecutorService(1, 10, TimeUnit.SECONDS, "fstore-inbox-bt-es"),
    this::processBatch);                                             // callback
```

**Why a separate thread (4a) instead of Thread 3 writing to FStore directly?** Parallelism. With the current design, Thread 3 keeps reading and enriching messages while Thread 4a handles the slow gRPC calls. Without Thread 4a, Thread 3 would block on each gRPC call (100ms+) and couldn't read from the file queue during that time.

### Step 9: Thread 4a processes the batch

**File:** `core/nb/src/com/wizrocket/nb/dispatchers/notificationinbox/inboxv2/InboxV2BatchProcessor.java`
**Runs on:** `fstore-inbox-bt-es` thread (Thread 4a)

**Triggered by:** either Thread 3 (group reached 5 messages) or Thread 4b (2s timer), both submit a task to Thread 4a's executor.

```java
private void processBatch(String region, List<InboxStoreMessage> messages) {
    if (shutdown) {
        // during shutdown, drain back to file queue for safety
        for (InboxStoreMessage message : messages) {
            persistentQueue.addToQueue(message);
        }
        return;
    }
    processBatch(messages);
}

void processBatch(List<InboxStoreMessage> messages) {
    // All messages in this batch share the same accountId, targetId, shard
    InboxStoreMessage first = messages.get(0);

    // Build a BroadcastableInboxStoreMessage (one message with multiple members)
    BroadcastableInboxStoreMessage bMessage = new BroadcastableInboxStoreMessage(
        first.getAccountID(), first.getPlatformIDEntity(), first.getBatchID(),
        first.getTargetID(), first.getPrn(), first.getIid(), first.getIdn(),
        first.getTs(), null, first.getPivotFieldName());
    bMessage.setTargetingInfo(first.getTargetingInfo());

    for (InboxStoreMessage m : messages) {
        bMessage.addRegistrationId(m.getPlatformIDEntity(), m.getIid(), m.getIdn(),
            m.getProfileDTO(), m.getTs(), m.getRandomPushId());
    }

    // Create InboxV2Dispatcher via Guice AssistedInject and run it
    dispatcherFactory.create(bMessage).run();
}
```

### Step 10: InboxV2Dispatcher encodes and writes to FStore

**File:** `core/nb/src/com/wizrocket/nb/dispatchers/notificationinbox/inboxv2/InboxV2Dispatcher.java`
**Runs on:** `fstore-inbox-bt-es` thread (Thread 4a)

```java
public boolean process() {
    if (this.message instanceof BroadcastableInboxStoreMessage broadcastMessage) {
        return processBroadcastMessage(broadcastMessage);
    } else {
        return processSingleMessage();
    }
}
```

For each member in the broadcast:

```java
private boolean processBroadcastMessage(BroadcastableInboxStoreMessage broadcastMessage) {
    Member firstMember = broadcastMessage.getMembers().get(0);
    long firstUserId = ProfileUtil.getUid(firstMember.getIid(), firstMember.getIdn());
    FSClientSync client = fsClientSyncManager.getClientForAccount();
    int shard = client.pick(firstUserId);  // all members have same shard

    List<FSClientSync.ContextualEntry> entries = new ArrayList<>();

    for (Member member : broadcastMessage.getMembers()) {
        InboxStoreMessage m = getInboxStoreMessageFromMember(member);

        if (m.isDryRun()) continue;

        long uid = ProfileUtil.getUid(member.getIid(), member.getIdn());

        // 1. Build FStore key (13-byte prefix + variable identifier)
        byte[] userKey = contentHandler.getUserKey(m);

        // 2. Encode message value with targetId
        byte[] valueBytes = contentHandler.encodeValue(m);

        // 3. Compute TTL-based expiry
        int expirySeconds = contentHandler.computeExpiry(m);

        // 4. Build entry for FStore
        entries.add(new FSClientSync.ContextualEntry(
            m.getAccountIdInt(), uid, userKey, valueBytes, expirySeconds));

        validMessages.add(m);
    }

    return writeBatch(shard, entries, client);
}
```

### Step 11: The actual gRPC call to FStore

**File:** `core/nb/src/com/wizrocket/nb/dispatchers/notificationinbox/inboxv2/InboxV2Dispatcher.java`
**Runs on:** `fstore-inbox-bt-es` thread (Thread 4a)

```java
private boolean writeBatch(int shard, List<FSClientSync.ContextualEntry> entries,
        FSClientSync fsClient) {
    try {
        retry.executeRunnable(
            () -> fsClient.batchPut(shard, Context.INBOX_V2, entries,
                                    FSTORE_TIMEOUT_MS, TimeUnit.MILLISECONDS));
        // ↑ gRPC call to FStore
        //   - 100ms timeout per call
        //   - Resilience4j retry: 3 attempts, 100ms delay between retries
        return true;
    } catch (Exception e) {
        return false;
    }
}
```

**This is where the message finally leaves NB and reaches FStore.**

### Step 12: Success or Failure handling

**Runs on:** `fstore-inbox-bt-es` thread (Thread 4a)

**On success** (`InboxV2Dispatcher.java:200-217`):

```java
protected void onSuccess() {
    for (InboxStoreMessage m : validMessages) {
        int ttlDays = (int) (contentHandler.computeTtlSeconds(m) / (24 * 60 * 60));
        resultTracker.success(m, ttlDays);    // mark as sent, track stats
    }
}
```

**On failure** (`InboxV2Dispatcher.java:221-248`):

```java
protected void onFailure() {
    for (InboxStoreMessage m : validMessages) {
        FStoreInboxResponseActionHandler.ResponseAction action =
            responseActionHandler.handleFailure(m);
        // RETRY → re-enqueue for later (max 4 retries, 5-minute intervals)
        // FAILURE → mark as DispatchFailed, track error stats
    }
}
```

---

## BlockingBatchTask Internals

Both batching layers (Boundary 1 and Boundary 3) use the same generic `BlockingBatchTask` from `core/core-utils`.

**File:** `core/core-utils/src/main/java/com/wizrocket/coreutils/batchtask/BlockingBatchTask.java`

### Data Structure

```java
Map<String, BatchTaskQueue<T>> keyToQueueMap = new ConcurrentHashMap<>();
```

Each unique key (region) gets its own queue. Items are added to the queue for their key.

### Every BlockingBatchTask Creates 2 Threads

1. **taskExecutor** (the worker) — executes the `batchedDataConsumer` callback when a task is submitted
2. **flushExecutorService** (the timer) — fires every N seconds, submits flush tasks to the worker

The timer exists because: if only 3 messages arrive and capacity is 5, those 3 messages would sit in memory forever without the timer forcing a flush.

### How addData Works

```java
public void addData(String key, T item) throws InterruptedException {
    // 1. Get or create the queue for this key
    BatchTaskQueue<T> queue = keyToQueueMap.computeIfAbsent(key,
        k -> new LinkedBlockingBatchTaskQueue<>(capacity));

    // 2. Add item to the queue
    boolean isQueueFull = queue.addAndIsQueueFull(item);

    // 3. If queue is full, submit task to worker thread
    if (isQueueFull) {
        taskExecutor.submit(() -> executeTaskForElements(key, queue));
    }
    // If not full, item sits in memory until timer fires flush
}
```

### How Flush Works (timer triggers this)

```java
public List<Future> flush() {
    for (Map.Entry<String, BatchTaskQueue<T>> entry : keyToQueueMap.entrySet()) {
        if (queue.isEmpty()) continue;
        // Submit task to worker thread for each non-empty queue
        taskExecutor.submit(() -> executeTaskForElements(key, queue));
    }
}
```

### The Worker Runs This

```java
private void executeTaskForElements(String key, BatchTaskQueue<T> elements) {
    List<T> elementsList = elements.clearAndReturnAllElements();  // drain the queue
    if (elementsList.isEmpty()) return;
    batchedDataConsumer.accept(key, elementsList);  // the callback
}
```

Both paths (capacity trigger and timer trigger) converge at the same worker method.

---

## Failure Handling

### At every stage, the message is never dropped

| Failure Point | What Happens |
|---|---|
| In-memory batch #1 rejects (Thread 2a overloaded) | `dispatch-manager` thread writes directly to file queue (blocks briefly on I/O) |
| In-memory batch #2 rejects (Thread 4a overloaded) | `fstore-inbox-pq-worker` thread writes message back to file queue |
| NB shutting down | All pending messages in batch #2 are drained to file queue |
| FStore gRPC call fails | Resilience4j retries 3 times with 100ms delay |
| FStore retries exhausted | `FStoreInboxResponseActionHandler` re-enqueues (max 4 retries, 5-min intervals) |
| All retries exhausted | Message marked as `DispatchFailed`, error stats tracked |
| Encoding/serialization fails | Message marked as failed immediately, no retry (not a transient error) |

### FStore gRPC failure detail

`writeBatch()` returns false → `IDispatcher.run()` calls `onFailure()`:

```java
protected void onFailure() {
    for (InboxStoreMessage m : validMessages) {
        FStoreInboxResponseActionHandler.ResponseAction action =
            responseActionHandler.handleFailure(m);
        // RETRY → re-enqueue for later attempt
        // FAILURE → mark as DispatchFailed
    }
}
```

### Shutdown behavior

During graceful shutdown, pending batches in Thread 4a are drained to the file queue rather than dropped:

```java
private void processBatch(String region, List<InboxStoreMessage> messages) {
    if (shutdown) {
        for (InboxStoreMessage message : messages) {
            persistentQueue.addToQueue(message);  // save to disk, process after restart
        }
        return;
    }
    processBatch(messages);
}
```

---

## Key Files

| File | Role |
|------|------|
| `core/nb/src/.../dispatchers/DispatchManager.java` | Entry point, `dispatch-manager` thread pool, routes to InboxV2 |
| `core/nb/src/.../notificationinbox/inboxv2/InboxV2StoreManager.java` | Device explosion, routes to dispatch handler |
| `core/nb/src/.../notificationinbox/inboxv2/InboxV2DispatchHandler.java` | Facade — orchestrates queue, worker, batch processor |
| `core/nb/src/.../notificationinbox/inboxv2/InboxV2PersistentQueue.java` | File-backed persistent queue (`fstore-inbox-store`) |
| `core/nb/src/.../notificationinbox/inboxv2/InboxV2QueueWorker.java` | `fstore-inbox-pq-worker` daemon thread — polls file, deserializes, decrypts, enriches |
| `core/nb/src/.../notificationinbox/inboxv2/InboxV2BatchProcessor.java` | Groups by `accountId\|targetId\|shard`, batches for FStore |
| `core/nb/src/.../notificationinbox/inboxv2/InboxV2Dispatcher.java` | Builds FStore entries, calls `batchPut()` via gRPC |
| `core/nb/src/.../notificationinbox/inboxv2/InboxV2ContentHandler.java` | Encodes user keys, values, computes TTL/expiry |
| `core/nb/src/.../notificationinbox/inboxv2/FStoreInboxResultTracker.java` | Tracks success/failure stats per message |
| `core/nb/src/.../notificationinbox/inboxv2/FStoreInboxResponseActionHandler.java` | Retry logic on FStore write failure (max 4 retries, 5-min intervals) |
| `core/nb/src/.../notificationinbox/inboxv2/FStoreInboxModule.java` | Guice module — binds FStore client, Resilience4j retry config |
| `core/nb/src/com/wizrocket/nb/utils/BatchPersistQueueUtility.java` | Batches protobuf serialization before writing to persistent queue |
| `core/core-utils/src/.../batchtask/BlockingBatchTask.java` | Generic batching utility — creates 2 threads (worker + timer) per instance |

---

## Design Rationale

**Why non-blocking?** The `dispatch-manager` threads are shared across ALL channels (push, SMS, email, webhooks, InboxV2). If InboxV2 blocked on FStore gRPC calls (network I/O), it would stall push/SMS/email delivery. Push, by contrast, dispatches synchronously on the dispatch-manager thread — it directly makes HTTP calls to FCM/APNs.

**Why a persistent file queue?** Durability. If NB crashes or restarts, messages survive on disk in the `fstore-inbox-store` file and are processed when the `fstore-inbox-pq-worker` thread starts back up.

**Why two batching layers?**
1. **First batch (BatchPersistQueueUtility):** Reduces file I/O. Instead of one file write per message, many messages are serialized into one protobuf batch and written in a single `enqueue()` call.
2. **Second batch (InboxV2BatchProcessor):** Reduces gRPC calls. Messages are grouped by `accountId|targetId|shard` so one gRPC `batchPut()` handles multiple users destined for the same FStore shard.

**Why does Thread 3 not write to FStore directly?** Parallelism. Thread 3 keeps reading and enriching messages from the file queue while Thread 4a handles the slow network I/O (gRPC 100ms+). Without this separation, Thread 3 would block on each gRPC call and couldn't process the file queue during that time.
