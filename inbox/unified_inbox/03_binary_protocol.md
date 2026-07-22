# 3. The Binary Protocol

## Overview

Unlike App Inbox (sign-bit tricks, fixed-size fields, no envelope), Unified Inbox uses a **modern binary format** with:
- VLQ (Variable-Length Quantity) for all integers
- Length-prefixed blocks for forward compatibility
- An envelope with version, message count, and ID counter
- Status bitmask for boolean flags

## The Full Format Specification

**File**: `core/server-side-inbox/service/src/com/wizrocket/inbox/service/serialization/UserInboxBinarySerializer.java:14-80`

```
USER_INBOX :=
    VLQ (protocol version, currently 0)
    VLQ (lastMessageId)
    VLQ (message count)
    MESSAGE*

MESSAGE :=
    META_BLOCK
    PERSONALISATION_BLOCK
    PREVIEW_CONTENT_BLOCK (optional, only if isPreviewMessage)

META_BLOCK :=
    VLQ (size of this block in bytes)
    VLQ (message id)
    INT32 (sent time, epoch seconds)
    VLQ (ttl period in minutes = (expiryTime - sentTime) / 60)
    INT32 (targeting id)
    VLQ (targeting version)
    VLQ (variant + 1)
    BYTE (status flags)

STATUS := (1 byte)
    bit 0: is read
    bit 1: is delivered once
    bit 7: is preview message
    bits 2-6: reserved

PERSONALISATION_BLOCK :=
    VLQ (size of this block in bytes)
    KV_PAIR* (key-value pairs)
    0x00 (zero byte terminates keys)

PREVIEW_CONTENT_BLOCK :=
    VLQ (size of this block in bytes)
    VLQ (preview content version = 1)
    STRING_OR_NUMBER (content string)
```

## Constants

**File**: `core/server-side-inbox/service/src/com/wizrocket/inbox/service/serialization/UserInboxBinarySerializer.java:85-93`

```java
public static final int PROTOCOL_VERSION = 0;
public static final Duration TTL_GRANULARITY = Duration.ofMinutes(1);
public static final int READ_STATUS_MASK = 0b0000_0001;
public static final int DELIVERED_ONCE_STATUS_MASK = 0b0000_0010;
public static final int PREVIEW_MESSAGE_MASK = 0b1000_0000;
public static final int PREVIEW_CONTENT_VERSION_1 = 1;
```

## Serialization Code

### Envelope

```java
// UserInboxBinarySerializer.java:95-103
public byte[] serialize(UserInbox inbox) {
    ByteArrayOutputStream out = new ByteArrayOutputStream(1024);
    ExtendedDataOutputStream data = new ExtendedDataOutputStream(out);
    data.writeVLQ(PROTOCOL_VERSION);                    // always 0
    data.writeVLQ(inbox.getLastMessageId());             // ID counter
    data.writeVLQ(inbox.getMessages().size());           // message count
    inbox.getMessages().forEach(m -> writeMessage(m, data));
    return out.toByteArray();
}
```

### Per-Message

```java
// UserInboxBinarySerializer.java:106-119
private void writeMessage(InboxMessage message, ExtendedDataOutputStream data) {
    data.writeBlockWithLen(out -> writeMeta(message, out));      // meta block
    if (message.getPersonalisations() instanceof PersonalisationsContainer container) {
        data.writeBlockWithLen(out -> {
            container.writer().accept(out);
            return true;
        });
    } else {
        data.writeBlockWithLen(out -> true);   // zero-sized block
    }
    if (message.isPreviewMessage()) {
        data.writeBlockWithLen(out -> writePreviewContent(message, out));
    }
}
```

### Meta Block

```java
// UserInboxBinarySerializer.java:122-142
private boolean writeMeta(InboxMessage message, ExtendedDataOutputStream data) {
    data.writeVLQ(message.getId());
    data.writeInt((int) message.getSentTime());
    data.writeVLQ((message.getExpiryTime() - message.getSentTime()) / TTL_GRANULARITY.getSeconds());
    data.writeInt(message.getTargetingId());
    data.writeVLQ(message.getTargetingVersion());
    data.writeVLQ(message.getVariant() + 1L);    // +1 because default is -1

    int status = 0;
    if (message.isRead())          status |= READ_STATUS_MASK;          // bit 0
    if (message.isDeliveredOnce()) status |= DELIVERED_ONCE_STATUS_MASK; // bit 1
    if (message.isPreviewMessage()) status |= PREVIEW_MESSAGE_MASK;     // bit 7
    data.writeByte(status);
    return true;
}
```

## Length-Prefixed Blocks: `writeBlockWithLen()`

**File**: `core/server-side-inbox/service/src/com/wizrocket/inbox/service/serialization/ExtendedDataOutputStream.java:66-75`

```java
public void writeBlockWithLen(Predicate<ExtendedDataOutputStream> writer) {
    if (scratch == null) {
        scratch = new ByteArrayOutputStream(1024);
    }
    scratch.reset();
    if (writer.test(new ExtendedDataOutputStream(scratch))) {
        writeVLQ(scratch.size());    // write block length first
        scratch.writeTo(this);        // then write block content
    }
}
```

This is a two-pass approach:
1. Write the block content to a scratch buffer
2. Write the length (VLQ) of the scratch buffer
3. Copy the scratch buffer to the main output

This allows the reader to **skip unknown blocks** — if a new field is added in a future protocol version, old readers can skip it by reading the length and advancing.

## Deserialization Code

**File**: `core/server-side-inbox/service/src/com/wizrocket/inbox/service/serialization/UserInboxBinaryDeserializer.java`

```java
public UserInbox deserialize(ByteBuffer bytes) {
    ExtendedDataInputStream in = new ExtendedDataInputStream(new ByteBufferInputStream(bytes));
    long protocolVersion = in.readVLQ();
    if (protocolVersion != PROTOCOL_VERSION) {
        throw new IOException(String.valueOf(protocolVersion));
    }
    int lastMessageId = (int) in.readVLQ();
    long messageCount = in.readVLQ();
    List<InboxMessage> messages = LongStream.range(0, messageCount)
            .mapToObj(i -> readMessage(in))
            .collect(Collectors.toList());
    return UserInbox.builder()
            .lastMessageId(lastMessageId)
            .messages(messages)
            .build();
}

private InboxMessage readMessage(ExtendedDataInputStream in) {
    InboxMessageBuilder builder = InboxMessage.builder();
    int metaLen = in.readBlock(block -> readMeta(block, builder));
    if (metaLen == 0) {
        throw new IOException(String.valueOf(metaLen));
    }
    in.readBlock(block -> builder.personalisations(new PackedMap(block.readAllBytes())));
    if (builder.isPreviewMessage()) {
        in.readBlock(block -> readPreviewContent(block, builder));
    }
    return builder.build();
}

private void readMeta(ExtendedDataInputStream in, InboxMessageBuilder builder) {
    builder.id((int) in.readVLQ());
    long sentTime = in.readInt();
    builder.sentTime(sentTime);
    long ttl = in.readVLQ();
    builder.expiryTime(sentTime + ttl * TTL_GRANULARITY.getSeconds());
    builder.targetingId(in.readInt());
    builder.targetingVersion((int) in.readVLQ());
    builder.variant((int) in.readVLQ() - 1);     // -1 to undo the +1 offset
    byte status = in.readByte();
    builder.isRead((status & READ_STATUS_MASK) != 0);
    builder.isDeliveredOnce((status & DELIVERED_ONCE_STATUS_MASK) != 0);
    builder.isPreviewMessage((status & PREVIEW_MESSAGE_MASK) != 0);
}
```

## Key Differences from App Inbox Protocol

| Aspect | App Inbox | Unified Inbox |
|--------|-----------|---------------|
| **Envelope** | None | Version + lastMessageId + count |
| **Message ID** | None (identified by targetId+epoch) | Unique auto-incremented `id` per user |
| **Integer encoding** | Fixed 4-byte int32 | VLQ (1-9 bytes, adaptive) |
| **Optional field flags** | Sign bit of targetId/epoch | Explicit status byte bitmask |
| **TTL storage** | Not stored (epoch only, TTL from MongoDB) | Stored as VLQ(minutes since sentTime) |
| **Block structure** | None — must parse sequentially | Length-prefixed — can skip unknown blocks |
| **Status flags** | None | isRead, isDeliveredOnce, isPreviewMessage |
| **Personalisations** | Flat {numericId: string}, max 255 | Nested maps with StringOrNumber keys, max 50 keys |
| **Preview support** | None | Optional preview content block |
| **targetingVersion** | Not stored | Stored for future content versioning |
