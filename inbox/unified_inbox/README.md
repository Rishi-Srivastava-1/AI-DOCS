# Unified Inbox (Server-Side Inbox) — Deep Dive

This folder contains a detailed point-by-point breakdown of the Unified Inbox (INBOX mode) system in CleverTap's Ultron codebase.

## Documents

| # | File | Topic |
|---|------|-------|
| 1 | [01_what_lives_in_redis.md](./01_what_lives_in_redis.md) | Storage model, UserInbox envelope, lifecycle, hard limits, comparison with App Inbox |
| 2 | [02_redis_key_encoding.md](./02_redis_key_encoding.md) | VLQ key encoding, IID/IDN with LSB tag, user-level identity, connection sharding |
| 3 | [03_binary_protocol.md](./03_binary_protocol.md) | Full binary format spec, length-prefixed blocks, VLQ encoding, serialization/deserialization code |
| 4 | [04_personalisations.md](./04_personalisations.md) | Liquid tracing, multi-layer key encoding, nested maps, PackedMap/UnpackedMap, StringOrNumber |
| 5 | [05_concurrency_watch_multi_exec.md](./05_concurrency_watch_multi_exec.md) | WATCH/MULTI/EXEC pattern, race condition handling, RetryDecorator, CircuitBreaker, synchronized |
| 6 | [06_nb_dispatch_flow.md](./06_nb_dispatch_flow.md) | ServerSideInboxDispatcher, toStoreInboxMessage, broadcast handling, retry via TimedQueue |
| 7 | [07_lc_delivery_flow.md](./07_lc_delivery_flow.md) | /inbox/fetch endpoint, InboxRenderer, lastKnownId optimisation, messages persist after delivery |
| 8 | [08_protocol_shapes_reference.txt](./08_protocol_shapes_reference.txt) | All message shapes (A/B/C/D), VLQ reference, StringOrNumber reference, size comparison |
| 9 | [09_flag_update_flows.md](./09_flag_update_flows.md) | Read flag, delivered flag, clicked tracking, delete — LC endpoints → LP event → batch task → Redis write |

## Key Source Files

### Models
| File | Description |
|------|-------------|
| `core/server-side-inbox/models/src/com/wizrocket/inbox/models/UserInbox.java` | Inbox envelope (lastMessageId + messages) |
| `core/server-side-inbox/models/src/com/wizrocket/inbox/models/InboxMessage.java` | Per-message model (id, status, personalisations) |
| `core/server-side-inbox/models/src/com/wizrocket/inbox/models/UserKey.java` | User identity (accountId, iid, idn) |
| `core/server-side-inbox/models/src/com/wizrocket/inbox/models/StringOrNumber.java` | Tagged union for personalisation keys |
| `core/server-side-inbox/models/src/com/wizrocket/inbox/models/Personalisations.java` | Marker interface for personalisation data |

### Serialization
| File | Description |
|------|-------------|
| `core/server-side-inbox/service/src/.../serialization/UserInboxBinarySerializer.java` | Serializer — UserInbox to bytes |
| `core/server-side-inbox/service/src/.../serialization/UserInboxBinaryDeserializer.java` | Deserializer — bytes to UserInbox |
| `core/server-side-inbox/service/src/.../serialization/MapBinarySerializer.java` | Nested map serializer for personalisations |
| `core/server-side-inbox/service/src/.../serialization/MapBinaryDeserializer.java` | Nested map deserializer |
| `core/server-side-inbox/service/src/.../serialization/ExtendedDataOutputStream.java` | VLQ, StringOrNumber, block writing |
| `core/server-side-inbox/service/src/.../serialization/ExtendedDataInputStream.java` | VLQ, StringOrNumber, block reading |

### Redis
| File | Description |
|------|-------------|
| `core/server-side-inbox/service/src/.../redis/InboxUpdateServiceRedis.java` | WATCH/MULTI/EXEC write operations |
| `core/server-side-inbox/service/src/.../redis/InboxFetchServiceRedis.java` | Read operations |
| `core/server-side-inbox/service/src/.../redis/BinaryInboxCodec.java` | Lettuce codec (key/value encode/decode) |
| `core/server-side-inbox/service/src/.../redis/RedisInboxKeyFactory.java` | Key construction (IID/IDN → inboxId) |
| `core/server-side-inbox/service/src/.../redis/RedisConnectionProvider.java` | Connection sharding and pooling |

### NB Dispatch
| File | Description |
|------|-------------|
| `core/nb/src/.../notificationinbox/ServerSideInboxDispatcher.java` | Dispatch logic, personalisation tracing |
| `core/nb/src/.../notificationinbox/ServerSideInboxDispatchHandler.java` | Entry point from DispatchManager |

### LC Delivery
| File | Description |
|------|-------------|
| `core/lc/src/.../targeting/inbox/FetchInboxServlet.java` | /inbox/fetch endpoint |
| `core/lc/src/.../targeting/inbox/InboxRenderer.java` | Message rendering with personalisation decode |

### Codec (Personalisation Key Encoding)
| File | Description |
|------|-------------|
| `core/server-side-inbox/service/src/.../codec/PersonalisationsEncoder.java` | String keys → numeric IDs |
| `core/server-side-inbox/service/src/.../codec/PersonalisationsDecoder.java` | Numeric IDs → string keys |
| `core/server-side-inbox/service/src/.../codec/ProfilePropertyEncoder.java` | Profile property name → ID |
| `core/server-side-inbox/service/src/.../codec/EventPropertyEncoder.java` | Event property name → ID |
| `core/server-side-inbox/service/src/.../codec/TopLevelPersonalisationKeyEncoder.java` | "Profile"→0, "Event"→1 |

## Binary Protocol Summary

```
Redis Key:   VLQ(namespace) + VLQ(inboxId)
             inboxId = idn != 0 ? (idn << 1) : (iid << 1) + 1

Redis Value:
  USER_INBOX :=
    VLQ(protocolVersion=0)
    VLQ(lastMessageId)
    VLQ(messageCount)
    MESSAGE*

  MESSAGE :=
    VLQ(metaBlockSize) + META_BLOCK
    VLQ(persBlockSize) + PERSONALISATION_BLOCK
    VLQ(previewBlockSize) + PREVIEW_BLOCK  (only if isPreviewMessage)

  META_BLOCK :=
    VLQ(id) INT32(sentTime) VLQ(ttlMinutes) INT32(targetingId)
    VLQ(targetingVersion) VLQ(variant+1) BYTE(status)

  PERSONALISATION_BLOCK :=
    (StringOrNumber key, StringOrNumber value)* + 0x00 terminator
    Values can be strings or nested maps (recursive)
```
