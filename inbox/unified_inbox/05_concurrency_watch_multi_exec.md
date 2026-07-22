# 5. Concurrency: WATCH/MULTI/EXEC Pattern

## The Problem

Like App Inbox, Unified Inbox stores all messages for a user in a single Redis key. Any mutation (add, mark as read, delete) requires the read-modify-write pattern:

```
1. READ current blob from Redis
2. MODIFY in memory (add message, flip read flag, etc.)
3. WRITE updated blob back to Redis
```

If two NB workers do this simultaneously for the same user, one write can overwrite the other. App Inbox has no protection against this. Unified Inbox solves it with Redis's **optimistic locking**.

## The WATCH/MULTI/EXEC Pattern

**File**: `core/server-side-inbox/service/src/com/wizrocket/inbox/service/redis/InboxUpdateServiceRedis.java:160-190`

```java
private boolean update(RedisConnection connection, RedisInboxKey inboxKey,
        Consumer<UserInbox> mutator) {
    StatefulRedisConnection<RedisInboxKey, UserInbox> conn = connection.getConnection();
    if (!conn.isOpen()) {
        throw new InboxUpdateException(connection.getAccountId(), connection.getHost());
    }
    synchronized (conn) {
        RedisCommands<RedisInboxKey, UserInbox> commands = conn.sync();
        try {
            commands.watch(inboxKey);                                  // 1. WATCH

            UserInbox userInbox = Optional.ofNullable(commands.get(inboxKey))
                    .orElseGet(() -> UserInbox.builder().build());     // 2. GET
            mutator.accept(userInbox);                                 // 3. MUTATE
            limits.apply(userInbox);                                   // 4. ENFORCE LIMITS

            commands.multi();                                          // 5. MULTI
            commands.set(inboxKey, userInbox);                         // 6. SET (queued)
            return !commands.exec().wasDiscarded();                    // 7. EXEC
        } catch (Exception e) {
            if (conn.isMulti()) {
                commands.discard();
            } else {
                commands.unwatch();
            }
            throw new InboxUpdateException(e, connection.getAccountId(), connection.getHost());
        }
    }
}
```

### How It Works — Step by Step

```
Step 1: WATCH(key)
  Redis starts tracking this key for modifications.
  If ANY other client modifies it before EXEC, the transaction will abort.

Step 2: GET(key)
  Read current UserInbox. If null (first message), create empty one.

Step 3: MUTATE
  Apply the mutation in memory (add message, mark read, etc.)

Step 4: ENFORCE LIMITS
  InboxLimits.apply() — cap at 300 messages, trim personalisations, etc.

Step 5: MULTI
  Start a Redis transaction — all subsequent commands are queued, not executed.

Step 6: SET(key, value)
  Queue the write. Not executed yet.

Step 7: EXEC
  Attempt to execute the transaction.
  - If the key was NOT modified since WATCH → SUCCESS (returns results)
  - If the key WAS modified by another client → DISCARDED (wasDiscarded() = true)
```

### Race Condition Handling

```
Worker 1                              Worker 2
    |                                     |
    +- WATCH key                          |
    +- GET → {msg1}                       +- WATCH key
    |                                     +- GET → {msg1}
    +- mutate: add msg2 → {msg1, msg2}   |
    +- MULTI                              +- mutate: add msg3 → {msg1, msg3}
    +- SET {msg1, msg2}                   +- MULTI
    +- EXEC → SUCCESS                     +- SET {msg1, msg3}
    |  (key wasn't changed)               +- EXEC → DISCARDED!
    |                                     |  (key was changed by Worker 1)
    |                                     |
    |                                     +- RetryDecorator kicks in:
    |                                     +- WATCH key again
    |                                     +- GET → {msg1, msg2}  ← sees Worker 1's write
    |                                     +- mutate: add msg3 → {msg1, msg2, msg3}
    |                                     +- MULTI → SET → EXEC → SUCCESS
```

No data loss. Both messages are preserved.

## Three Layers of Protection

### Layer 1: `synchronized(conn)`

```java
synchronized (conn) {
    // Only one thread can use this Redis connection at a time
}
```

Prevents two Java threads from interleaving WATCH/GET/MULTI/SET/EXEC on the same connection. Without this, thread A's MULTI could execute between thread B's GET and SET.

### Layer 2: Redis WATCH

Detects if a **different connection** (possibly on a different NB instance) modified the key. The Java `synchronized` only protects within one JVM — WATCH protects across the cluster.

### Layer 3: RetryDecorator (Resilience4j)

**File**: `core/server-side-inbox/service/src/com/wizrocket/inbox/service/guice/InboxUpdateServiceModule.java:56-60`

```java
@Provides @Singleton
RetryDecorator updateRetry(RetryConfig config, LoggingRetryEventHandlers eventHandlers) {
    Retry retry = RetryRegistry.of(config, eventHandlers).retry(UPDATE_RES);
    return f -> Retry.decorateSupplier(retry, f);
}
```

Configuration:
- **Max attempts**: 10
- **Wait between retries**: 2 seconds
- **Retry condition**: Result is `false` (transaction was discarded)

When `EXEC` returns `wasDiscarded()`, the operation is retried with fresh data.

### Layer 4: CircuitBreakerDecorator (Resilience4j)

Stops hammering a Redis host that's unhealthy:
- **Sliding window**: 60 seconds, time-based
- **Failure threshold**: 80%
- **Minimum calls**: 3 (before circuit can open)
- **Open duration**: 60 seconds (then half-open to test)
- **Half-open max attempts**: 10

## Batch WATCH/MULTI/EXEC

For operations on multiple keys (e.g., `updateFlags` — mark multiple users' messages as delivered):

**File**: `core/server-side-inbox/service/src/com/wizrocket/inbox/service/redis/InboxUpdateServiceRedis.java:234-276`

```java
private boolean updateBatch(RedisConnection connection, List<KeyToUpdate> keys,
        BiFunction<UserKey, Optional<UserInbox>, Optional<UserInbox>> mutator) {
    synchronized (conn) {
        RedisCommands commands = conn.sync();
        try {
            commands.watch(keysArr);                         // WATCH all keys
            List<KeyValue> values = commands.mget(keysArr);  // MGET all at once
            // ... apply mutator to each ...
            commands.multi();
            commands.mset(updatedValues);                    // MSET all at once
            return !commands.exec().wasDiscarded();           // atomic commit
        } catch (Exception e) {
            if (conn.isMulti()) commands.discard();
            else commands.unwatch();
            throw new InboxUpdateException(e, ...);
        }
    }
}
```

Same pattern but with `MGET`/`MSET` for multiple keys. If ANY watched key was modified, the entire batch is retried.

## Error Handling

```java
} catch (Exception e) {
    if (conn.isMulti()) {
        commands.discard();      // inside MULTI → DISCARD to exit transaction mode
    } else {
        commands.unwatch();      // before MULTI → UNWATCH to release the watch
    }
    throw new InboxUpdateException(e, connection.getAccountId(), connection.getHost());
}
```

Two cleanup paths depending on where the error occurred:
- **Before MULTI**: Connection is in WATCH mode → `UNWATCH` to release
- **After MULTI**: Connection is in transaction mode → `DISCARD` to abort

## How Each Operation Uses the Pattern

### `add(UserKey, InboxMessage)`

```java
public boolean add(UserKey userKey, InboxMessage message) {
    limits.apply(message);
    return update(userKey, inbox -> {
        inbox.setLastMessageId(inbox.getLastMessageId() + 1);  // increment counter
        message.setId(inbox.getLastMessageId());                // assign unique ID
        inbox.getMessages().add(message);                       // append
    });
}
```

### `markAsRead(UserKey, int messageId)`

```java
public boolean markAsRead(UserKey userKey, int messageId) {
    return update(userKey, inbox -> inbox.getMessages().stream()
            .filter(message -> message.getId() == messageId)
            .forEach(message -> message.setRead(true)));
}
```

### `delete(UserKey, int messageId)`

```java
public boolean delete(UserKey userKey, int messageId) {
    return update(userKey, inbox -> inbox.getMessages()
            .removeIf(message -> message.getId() == messageId));
}
```

All three use the same `update()` method with different mutators — the WATCH/MULTI/EXEC wrapping is shared.

## Comparison: App Inbox vs Unified Inbox Concurrency

| Aspect | App Inbox | Unified Inbox |
|--------|-----------|---------------|
| **Concurrency model** | None. Last writer wins. | WATCH/MULTI/EXEC optimistic locking |
| **Race detection** | Not detected | `exec().wasDiscarded()` |
| **On conflict** | Silent data loss | Retry with fresh data (up to 10 times) |
| **Thread safety** | BatchTask serializes per region key | `synchronized(conn)` + WATCH |
| **Retry** | Infinite sleep loop for connection errors only | Resilience4j Retry for both connection AND concurrency |
| **Circuit breaker** | None | Resilience4j per Redis host |
| **Connection model** | Jedis (sync, single connection) | Lettuce (20 connections per host, load-balanced) |
