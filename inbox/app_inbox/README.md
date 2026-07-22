# App Inbox (NotificationInbox) — Legacy Binary Protocol Deep Dive

This folder contains a detailed point-by-point breakdown of the legacy App Inbox binary protocol used in CleverTap's Ultron codebase.

## Documents

| # | File | Topic |
|---|------|-------|
| 1 | [01_what_lives_in_redis.md](./01_what_lives_in_redis.md) | The Big Picture — what Redis stores, the blob model, 10KB cap, who writes vs reads, lifecycle |
| 2 | [02_redis_key_encoding.md](./02_redis_key_encoding.md) | Redis Key Encoding — namespace (0-3 bytes) + IID (4 or 8 bytes), `encodeInt`/`encodeShortUnsigned`, variable-length key format |
| 3 | [03_per_notification_binary_layout.md](./03_per_notification_binary_layout.md) | Per-Notification Binary Layout — the sign-bit trick, four shapes (A/B/C/D), field sizes, Year 2038 problem |
| 4 | [04_how_personalisations_are_stored.md](./04_how_personalisations_are_stored.md) | How Personalisations Are Stored — NB extraction of event properties, binary encoding, LC rendering, stale vs fresh values |
| 5 | [05_endidx_navigation.md](./05_endidx_navigation.md) | How `endIdxForNotification()` Navigates — walking variable-length notifications, tracing all four shapes, corruption behavior |
| 6 | [06_encoder_operations.md](./06_encoder_operations.md) | The Three Encoder Operations — `addNotification` (dedup + overflow), `removeTargets` (LC deletion), `deleteExpired` (TTL cron) |
| 7 | [07_decoder.md](./07_decoder.md) | The Decoder — byte-to-object translation, LC consumption, what decoder does NOT do, corruption fragility |
| 8 | [08_threadlocal_lifecycle.md](./08_threadlocal_lifecycle.md) | ThreadLocal Lifecycle — why ThreadLocal, reuse pattern, stale data risk, V1 vs V2 comparison |

## Key Source Files

| Component | File Path |
|-----------|-----------|
| Protocol (Encoder/Decoder) | `core/inboxstore/src/com/wizrocket/inboxstore/Protocol/Notification.java` |
| Utility functions | `core/inboxstore/src/com/wizrocket/inboxstore/Utils.java` |
| Redis sync client (NB) | `core/inboxstore/src/com/wizrocket/inboxstore/Redis/InboxClientSync.java` |
| Redis async client (LC) | `core/inboxstore/src/com/wizrocket/inboxstore/Redis/InboxClientAsync.java` |
| NB dispatcher | `core/nb/src/com/wizrocket/nb/dispatchers/notificationinbox/NotificationInboxDispatcher.java` |
| NB store manager | `core/nb/src/com/wizrocket/nb/providers/InboxStoreManager.java` |
| LC inbox manager | `core/lc/src/java/com/wizrocket/lc/targeting/InboxManager.java` |
| TTL cleanup cron | `core/inboxstore/src/com/wizrocket/inboxstore/DeleteExpiryCron.java` |
| RedisTargetAndEpoch | `core/inboxstore/src/com/wizrocket/inboxstore/RedisTargetAndEpoch.java` |

## Binary Protocol Summary

```
Redis Key: [namespace: 0-3 bytes][IID: 4 or 8 bytes]

Redis Value (per user, concatenated notifications):
  [Notification_1][Notification_2]...[Notification_N]

Each Notification (variable length, 8-10000+ bytes):
  [targetId: 4B, sign=has_pers] [epoch: 4B, sign=has_variant]
  [variant: 1B]?  [persCount: 1B]?
  [propId: 2B, valueLen: 1B, value: NB]*
```
