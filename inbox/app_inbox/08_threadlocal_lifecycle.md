# 8. The ThreadLocal Lifecycle Pattern

## Why ThreadLocal?

The Encoder allocates a **10KB scratch buffer**:

```java
private byte[] buffer = new byte[MAX_BYTE_ARRAY];  // 10,000 bytes
```

NB processes millions of inbox dispatches per second. Creating a new 10KB buffer per dispatch would cause massive GC pressure:

```
1,000,000 dispatches/sec x 10,000 bytes = 10 GB/sec of allocations
```

ThreadLocal solves this — each thread gets exactly **one** Encoder instance, reused across all dispatches.

## How `init()` Works

**File**: `core/inboxstore/src/com/wizrocket/inboxstore/Protocol/Notification.java:74-85`

```java
private static final ThreadLocal<Encoder> instances = new ThreadLocal<>();

public static Encoder init(byte[] b) {
    Encoder enc = instances.get();
    if (enc == null) {
        enc = new Encoder();       // first call on this thread -- allocate
        instances.set(enc);        // store for reuse
    }
    enc.b = b;                     // swap in new input byte array
    enc.idx = 0;                   // reset write cursor
    return enc;
}
```

**First call**: Creates new Encoder (allocating 10KB buffer), stores in ThreadLocal.
**Subsequent calls**: Retrieves existing Encoder, swaps `b`, resets `idx`. Buffer reused.

## The Lifecycle in NB's Dispatch Flow

```
Thread "inbox-store-bt-es-1":

  User 1: InboxClientSync.appendNotification(existingBytes1, ...)
    -> Notification.Encoder.init(existingBytes1)
       -> instances.get() -> null (first call)
       -> new Encoder() -> allocates 10KB buffer
       -> enc.b = existingBytes1, enc.idx = 0
    -> enc.addNotification(...)
    -> returns new byte[]

  User 2: InboxClientSync.appendNotification(existingBytes2, ...)
    -> Notification.Encoder.init(existingBytes2)
       -> instances.get() -> SAME encoder from User 1
       -> enc.b = existingBytes2, enc.idx = 0
    -> enc.addNotification(...)
    -> overwrites buffer from position 0
    -> returns new byte[]
```

10KB buffer allocated **once** for the lifetime of the thread.

## Entry Point Always Goes Through `init()`

**File**: `core/inboxstore/src/com/wizrocket/inboxstore/Redis/InboxClientSync.java:148-154`

```java
public static byte[] appendNotification(byte[] b, int targetId, int epoch, ...) {
    Notification.Encoder enc = Notification.Encoder.init(b);  // ALWAYS init first
    return enc.addNotification(targetId, epoch, ...);
}
```

There's no way to get an Encoder without initializing it — prevents stale data.

## Stale Data Risk

After each operation, `enc.b` points to the result array and `buffer` contains the operation's data. Neither is cleared between calls. This means:

- Between dispatches, the Encoder holds a reference to the last user's notification data
- This prevents GC from collecting it
- For a thread pool with 10 threads = 10 byte arrays kept alive unnecessarily
- Not significant given 10KB max, but a leak of principle

## Thread Pools — The Never-Cleaned Problem

NB's batch task uses a thread pool (`InboxStoreManager.java:78-80`):

```java
inboxStoreBatchTask = new BlockingBatchTask<>("INBOX_BT", Duration.ofSeconds(5), CAPACITY,
        utils.getNewTaskExecutorService(1, 10, TimeUnit.SECONDS, "inbox-store-bt-es"),
        this::task);
```

There's **no `instances.remove()` call anywhere**. The ThreadLocal Encoder lives for the entire thread lifetime. This is fine for fixed-size pools (bounded count), but would leak if threads were short-lived.

## Thread Safety

The Encoder is **NOT thread-safe**:

```java
private byte[] b = null;                // mutable, unsynchronized
private byte[] buffer = new byte[10000]; // mutable, unsynchronized
private int idx = 0;                    // mutable, unsynchronized
```

ThreadLocal ensures each thread gets its own instance. Two threads never share an Encoder.

**What could break this:**
- Passing an Encoder across threads (nobody does this)
- Virtual threads (Project Loom) — thousands of virtual threads would create thousands of Encoders. Future concern.

## Comparison: ThreadLocal (V1) vs Guice Singleton (V2)

**V2 approach** (`core/inboxv2/service/src/.../InboxV2MessageCodec.java:19-21`):

```java
@Singleton
@RequiredArgsConstructor(onConstructor = @__(@Inject))
public class InboxV2MessageCodec {
    private final InboxV2ProtocolCodecResolver factory;  // immutable

    public byte[] serialize(InboxV2Message message) {
        ByteArrayOutputStream out = new ByteArrayOutputStream(256);  // new per call
        // ... all state is local to the method
    }
}
```

| Aspect | V1 (ThreadLocal) | V2 (Guice Singleton) |
|--------|------------------|---------------------|
| **Instance per** | Thread | Entire JVM (one instance) |
| **State** | Mutable (`b`, `buffer`, `idx`) | Stateless |
| **Buffer** | 10KB reused per thread | 256B per call, grows as needed |
| **Thread safety** | Not thread-safe, ThreadLocal isolates | Fully thread-safe |
| **Memory** | 10KB x thread count, held forever | Allocated and GC'd per call |
| **GC pressure** | Very low | Higher, but modern GCs handle it |
| **Stale data risk** | Yes, if `init()` forgotten | None |
| **Virtual thread friendly** | No | Yes |

V1 optimized for 2018-era GC. V2 uses the modern stateless approach, trusting generational GC.
