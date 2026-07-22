# EBS Size Calculation for App Inbox V2 File Queue

## What is EBS?

EBS (Elastic Block Store) is AWS persistent disk storage. Unlike instance storage which is lost when an EC2 instance stops, EBS data survives restarts.

## Why Do We Need EBS?

The file queues under `/home/logprocessor/data/filequeue` are always in the pipeline — every mutation flows through the file queue before being applied to FStore, regardless of FStore availability. When FStore is unavailable, the drain pauses and the queue accumulates until FStore recovers. Without EBS, this data would be lost on pod restart, breaking our "at-least-once delivery" guarantee.

## File Queues in Scope

Two queues, one per batch task:

| Queue name | Batch task | DTO |
|---|---|---|
| `inbox_v2_ops_bt` | `InboxV2FStoreOperationsBatchTask` | `InboxV2FStoreEntryDTO` |
| `inbox_v2_merge_bt` | `InboxV2FStoreProfileMergeBatchTask` | `InboxV2ProfileMergeDTO` *(planned)* |

---

## DTO Serialized Byte Sizes

Derived from actual `serialize()` implementations (or the approved plan for merge).

### `InboxV2FStoreEntryDTO`

Serialization format (`InboxV2FStoreEntryDTO.java:32`):
```
[4: int accountId][8: long userId][1: byte mutationType][N: identifier bytes (UTF-8)]
```

| Field | Bytes |
|---|---|
| `accountId` (int) | 4 |
| `userId` (long) | 8 |
| `mutationType` (byte ordinal) | 1 |
| `identifier` — `String.valueOf(randomPushId)` (long → decimal string) | 10–19 |
| `identifier` — `getPushId()` → `targetId_yyyyMMdd` (e.g. `1777549260_20260429`) | 19 |
| **Total payload** | **23–32 bytes** |
| **Worst case (design max)** | **32 bytes** |

### `InboxV2ProfileMergeDTO` *(planned — not yet implemented)*

Serialization format (approved plan):
```
[4: int accountId][8: long iid][8: long idn]
```

LP consumers receive `accountId` as `int` — same convention as `InboxV2FStoreEntryDTO`. No String/length-prefix needed.

| Field | Bytes |
|---|---|
| `accountId` (int) | 4 |
| `iid` (long) | 8 |
| `idn` (long) | 8 |
| **Total payload** | **20 bytes (fixed)** |

---

## Queue Storage Overhead (`SmallerPageSizePersistentQueue`)

| Parameter | Value |
|---|---|
| Data page size | 8 MB (8,388,608 bytes) |
| Per-item data overhead | +4 bytes CRC32 prefix |
| Index page size | 8 MB |
| Index entry size | 32 bytes (fixed per item) |
| Items per index page | 262,144 |

**Effective on-disk cost per item:**

| DTO | Payload | + CRC → data page cost | Index entry | Total on-disk |
|---|---|---|---|---|
| `InboxV2FStoreEntryDTO` | 32 bytes | 36 bytes | 32 bytes | **68 bytes** |
| `InboxV2ProfileMergeDTO` | 20 bytes | 24 bytes | 32 bytes | **56 bytes** |

Pages are allocated in 8 MB chunks — even 1 item allocates a full 8 MB page.

---

## Sizing Assumptions

| Parameter | Value | Source |
|---|---|---|
| Ops rate (READ + DELIVERED + DELETE) | 5,000 events/s per LP pod | Production estimate |
| Profile merge rate | 500 merges/s per LP pod | Production estimate |
| Worst-case FStore outage | 1 hour (3,600 s) | Sizing target |
| EBS is provisioned | Per LP pod | Architecture decision |

During a FStore outage, drains fail and re-enqueue — the queue fills at the **full ingest rate** (no drain deduction).

---

## EBS Calculation

### Queue 1 — `inbox_v2_ops_bt`

```
Total items = 5,000 events/s × 3,600 s = 18,000,000 items
```

| Layer | Calculation | Size |
|---|---|---|
| Items per data page | floor(8,388,608 / 36) = 233,016 | — |
| Data pages needed | ceil(18,000,000 / 233,016) = 78 pages | **624 MB** |
| Items per index page | 262,144 (fixed) | — |
| Index pages needed | ceil(18,000,000 / 262,144) = 69 pages | **552 MB** |
| **Subtotal** | | **1,176 MB** |

### Queue 2 — `inbox_v2_merge_bt`

```
Total items = 500 merges/s × 3,600 s = 1,800,000 items
```

| Layer | Calculation | Size |
|---|---|---|
| Items per data page | floor(8,388,608 / 24) = 349,525 | — |
| Data pages needed | ceil(1,800,000 / 349,525) = 6 pages | **48 MB** |
| Items per index page | 262,144 (fixed) | — |
| Index pages needed | ceil(1,800,000 / 262,144) = 7 pages | **56 MB** |
| **Subtotal** | | **104 MB** |

### Total per LP Pod

| | Raw | With 50% buffer (fragmentation + FS metadata) |
|---|---|---|
| Both queues combined | **1,280 MB (~1.25 GB)** | **~1.87 GB** |
| **Recommended provisioning** | | **2 GB** |

---

## Notes

- Under **normal operation** (FStore healthy), queues are near-empty — drained every 1–5 s. Steady-state EBS usage is a few pages (tens of MB).
- The **2 GB** figure covers only the file queue directories. OS, JVM heap, and other LP disk usage are additive.
- Old queue names (`inbox_v2_read_bt`, `inbox_v2_delivered_bt`, `inbox_v2_delete_bt`) are orphaned on deploy — drain-before-deploy strategy, they will be near-empty at deploy time.
- `inbox_v2_merge_bt` does not exist until the profile merge PR is implemented.

---

## Worst-Case / Max Configuration

| Parameter | Value |
|---|---|
| Ops rate (READ + DELIVERED + DELETE) | 10,000 events/s per LP pod |
| Profile merge rate | 2,000 merges/s per LP pod |
| Worst-case FStore outage | 2 hours (7,200 s) |

### Queue 1 — `inbox_v2_ops_bt`

```
Total items = 10,000 events/s × 7,200 s = 72,000,000 items
```

| Layer | Calculation | Size |
|---|---|---|
| Items per data page | floor(8,388,608 / 36) = 233,016 | — |
| Data pages needed | ceil(72,000,000 / 233,016) = 309 pages | **2,472 MB** |
| Items per index page | 262,144 (fixed) | — |
| Index pages needed | ceil(72,000,000 / 262,144) = 275 pages | **2,200 MB** |
| **Subtotal** | | **4,672 MB** |

### Queue 2 — `inbox_v2_merge_bt`

```
Total items = 2,000 merges/s × 7,200 s = 14,400,000 items
```

| Layer | Calculation | Size |
|---|---|---|
| Items per data page | floor(8,388,608 / 24) = 349,525 | — |
| Data pages needed | ceil(14,400,000 / 349,525) = 42 pages | **336 MB** |
| Items per index page | 262,144 (fixed) | — |
| Index pages needed | ceil(14,400,000 / 262,144) = 55 pages | **440 MB** |
| **Subtotal** | | **776 MB** |

### Total per LP Pod (Worst Case)

| | Raw | With 50% buffer (fragmentation + FS metadata) |
|---|---|---|
| Both queues combined | **5,448 MB (~5.32 GB)** | **~7.98 GB** |
| **Recommended provisioning** | | **8 GB** |
