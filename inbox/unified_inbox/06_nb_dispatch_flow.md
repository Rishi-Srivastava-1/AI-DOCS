# 6. NB Dispatch Flow

## Entry Point

**File**: `core/nb/src/com/wizrocket/nb/dispatchers/notificationinbox/ServerSideInboxDispatchHandler.java`

```java
void handleInboxMessage(InboxStoreMessage message) {
    serverSideInboxDispatcherFactory.create(message).run();
}
```

Routed from `DispatchManager.processMessage()`:
```java
case INBOX:
    unifiedInboxDispatchHandler.handleInboxMessage((InboxStoreMessage) message);
    break;
```

## The Dispatch Flow

**File**: `core/nb/src/com/wizrocket/nb/dispatchers/notificationinbox/ServerSideInboxDispatcher.java`

### Step 1: Route to single or broadcast

```java
// ServerSideInboxDispatcher.java:91-99
public boolean process() throws Exception {
    if (this.message instanceof BroadcastableInboxStoreMessage broadcastMessage) {
        return processBroadcastMessage(broadcastMessage);
    } else {
        return processSingleMessage();
    }
}
```

### Step 2: Convert NB message to InboxMessage

```java
// ServerSideInboxDispatcher.java:179-205
InboxMessage toStoreInboxMessage(InboxStoreMessage nbMessage) {
    TargetingInfo ti = nbMessage.getTargetingInfo();
    String messageKey = nbMessage.getPivotFieldName();

    // 1. Trace Liquid template access — capture which profile/event values were used
    String traceKey = addTraceKey(nbMessage.getProfileDTO());
    ProfileDTO decryptedProfileClone = DecryptedValuesReplacer
            .profileCloneWithDecryptedValuesInProfileObject(nbMessage.getProfileDTO());
    Map<String, Object> accessedValues =
            traceLiquidPersonalisations(decryptedProfileClone, ti, messageKey, traceKey);

    // 2. Add @-style (non-liquid) replacement personalisations
    addReplacementPersonalisations(accessedValues, decryptedProfileClone, ti);

    // 3. Build preview content if this is a preview message
    String previewContentStr = null;
    if (nbMessage.isPreview()) {
        previewContentStr = getPreviewContentStr(nbMessage);
    }

    // 4. Build the InboxMessage
    return InboxMessage.builder()
            .isPreviewMessage(nbMessage.isPreview())
            .targetingId(nbMessage.getTargetID())
            .targetingVersion(nbMessage.getTargetingVersion())
            .sentTime(nbMessage.getTs())
            .expiryTime((long) nbMessage.getTs() + getTTLSeconds(nbMessage.getTargetingInfo()))
            .variant(encodeMessageKey(messageKey, ti.getPivotKeyToIDMap()))
            .personalisations(personalisationsEncoder.encode(accessedValues, ti))
            .previewContent(previewContentStr)
            .build();
}
```

### Step 3: Write to Redis via InboxUpdateService

```java
// ServerSideInboxDispatcher.java:140-168
private boolean processSingleMessage() {
    boolean response = false;
    try {
        response = this.inboxUpdateService.add(
                toUserKey(this.message),
                toStoreInboxMessage(this.message));
    } catch (Exception e) {
        Profiler.report("Unified Inbox Message Retrying...");
    }

    // Decide verdict based on response
    UnifiedInboxResponseActionDecider.ResponseAction action =
            this.inboxResponseActionDecider.readResponseAndDecideVerdict(this.message, response);
    Verdict verdict = action.verdict();

    if (verdict == Verdict.RETRY) {
        pushMessageBackToQueue(this.message);    // retry via TimedQueue
    } else if (verdict == Verdict.FAILURE) {
        this.message.setSendingError(action.errorType());
    }

    return verdict == Verdict.SUCCESS;
}
```

### Step 4: Handle broadcast (multi-user batch)

```java
// ServerSideInboxDispatcher.java:109-138
private boolean processBroadcastMessage(BroadcastableInboxStoreMessage broadcastMessage) {
    boolean result = false;
    for (Member member : broadcastMessage.getMembers()) {
        InboxStoreMessage inboxStoreMessage = getInboxStoreMessageFromMember(member);
        boolean success = this.inboxUpdateService.add(
                toUserKey(inboxStoreMessage),
                toStoreInboxMessage(inboxStoreMessage));
        if (success) result = true;

        // Per-member verdict
        ResponseAction action = this.inboxResponseActionDecider
                .readResponseAndDecideVerdict(inboxStoreMessage, success);
        if (action.verdict() == Verdict.RETRY) {
            member.setFailure(action.errorType(), action.verdict());
            pushMessageBackToQueue(inboxStoreMessage);
        } else if (action.verdict() == Verdict.FAILURE) {
            member.setFailure(action.errorType(), action.verdict());
        }
    }
    return result;
}
```

## Retry Mechanism

**File**: `core/nb/src/com/wizrocket/nb/dispatchers/notificationinbox/ServerSideInboxDispatcher.java:315-345`

```java
protected void pushMessageBackToQueue(InboxStoreMessage message) {
    int scheduledTimeToDispatchMessage =
            getScheduledTimeToDispatchMessage(message.getRetryCount());
    incrementRetryCountAndSetScheduledTime(message, scheduledTimeToDispatchMessage);
    timedQueue.enqueue(message);
    this.setEnqueuedForRetry(true);
}
```

Retry uses a `TimedQueue` with 5-minute intervals:
- Retry 1: immediately (if under 5 min, retried when TimedQueue has resources)
- Retry 2: at 5-minute mark
- Retry 3: at 10-minute mark
- Max retries: configurable via `unifiedInboxRetryCount` (default 4)

## Key Differences from App Inbox NB Flow

| Aspect | App Inbox | Unified Inbox |
|--------|-----------|---------------|
| **Personalisation capture** | Explicit from `replacements` config (event props only) | Traced from Liquid template rendering (profile + event + linked) |
| **Profile decryption** | Not needed (profile props not stored) | `DecryptedValuesReplacer` decrypts PII before tracing |
| **Write method** | `InboxClientSync.appendNotification()` (raw byte manipulation) | `inboxUpdateService.add()` (WATCH/MULTI/EXEC) |
| **Retry** | Infinite loop with sleep(2000) for connection errors | `TimedQueue` with configurable backoff for all failures |
| **Verdict system** | None — success/failure only | `Verdict.SUCCESS / RETRY / FAILURE` with per-member tracking |
| **Preview support** | None | `isPreview()` → stores previewContent JSON |
| **Batch handling** | `BatchTask` merges per region, then pipeline SET | Per-member `add()` call within broadcast loop |
