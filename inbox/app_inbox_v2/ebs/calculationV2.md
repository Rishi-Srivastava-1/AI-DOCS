# EBS Size Calculation for App Inbox V2 File Queue — V2 Protocol

> **What changed since [calculation.md](./calculation.md):** The serialized formats for `InboxV2FStoreEntryDTO` and `InboxV2ProfileMergeDTO` now carry a 1-byte format version (forward-compatibility), and `InboxV2FStoreEntryDTO` additionally carries campaign-stat fields (`targetId`, `batchId`, `deviceOrdinal`, `pivot`) so that `markAsSent` can fire idempotently inside `InboxV2FStoreOperationsBatchTask` when the DELIVERED bit actually flips false→true. Per-item bytes grew, so EBS provisioning must grow with it.

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

## DTO Serialized Byte Sizes (V2 Protocol)

Derived from current `serialize()` implementations.

### `InboxV2FStoreEntryDTO`

Serialization format (`InboxV2FStoreEntryDTO.java`):
```
[1: version][4: accountId][8: userId][1: mutationType][4: targetId][4: batchId]
[4: deviceOrdinal][2: pivotLen][M: pivot UTF-8 bytes][K: identifier UTF-8 bytes]
```

| Field | Bytes |
|---|---|
| `version` (byte, hardcoded `0x01`) | 1 |
| `accountId` (int) | 4 |
| `userId` (long) | 8 |
| `mutationType` (byte ordinal) | 1 |
| `targetId` (int) | 4 |
| `batchId` (int) | 4 |
| `deviceOrdinal` (int) | 4 |
| `pivotLen` (short, length prefix for pivot) | 2 |
| `pivot` (UTF-8) — typical `"wzrk_default"` (12 B); user-defined variants up to ~32 B | 12–32 |
| `identifier` (UTF-8) — `targetId_yyyyMMdd` (e.g. `1777549260_20260429`) | 19 |
| **Fixed header** | **28 bytes** |
| **Typical payload** (pivot=12, identifier=19) | **59 bytes** |
| **Worst case (design max)** (pivot=32, identifier=19) | **79 bytes → round to 80 bytes** |

### `InboxV2ProfileMergeDTO`

Serialization format:
```
[1: version][4: accountId][8: iid][8: idn]
```

| Field | Bytes |
|---|---|
| `version` (byte, hardcoded `0x01`) | 1 |
| `accountId` (int) | 4 |
| `iid` (long) | 8 |
| `idn` (long) | 8 |
| **Total payload** | **21 bytes (fixed)** |

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

| DTO | Payload (worst case) | + CRC → data page cost | Index entry | Total on-disk |
|---|---|---|---|---|
| `InboxV2FStoreEntryDTO` | 80 bytes | 84 bytes | 32 bytes | **116 bytes** |
| `InboxV2ProfileMergeDTO` | 21 bytes | 25 bytes | 32 bytes | **57 bytes** |

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
| Items per data page | floor(8,388,608 / 84) = 99,864 | — |
| Data pages needed | ceil(18,000,000 / 99,864) = 181 pages | **1,448 MB** |
| Items per index page | 262,144 (fixed) | — |
| Index pages needed | ceil(18,000,000 / 262,144) = 69 pages | **552 MB** |
| **Subtotal** | | **2,000 MB** |

### Queue 2 — `inbox_v2_merge_bt`

```
Total items = 500 merges/s × 3,600 s = 1,800,000 items
```

| Layer | Calculation | Size |
|---|---|---|
| Items per data page | floor(8,388,608 / 25) = 335,544 | — |
| Data pages needed | ceil(1,800,000 / 335,544) = 6 pages | **48 MB** |
| Items per index page | 262,144 (fixed) | — |
| Index pages needed | ceil(1,800,000 / 262,144) = 7 pages | **56 MB** |
| **Subtotal** | | **104 MB** |

### Total per LP Pod

| | Raw | With 50% buffer (fragmentation + FS metadata) |
|---|---|---|
| Both queues combined | **2,104 MB (~2.05 GB)** | **~3.08 GB** |
| **Recommended provisioning** | | **4 GB** |

---

## Notes

- Under **normal operation** (FStore healthy), queues are near-empty — drained every 1–5 s. Steady-state EBS usage is a few pages (tens of MB).
- The **4 GB** figure covers only the file queue directories. OS, JVM heap, and other LP disk usage are additive.
- Per-item disk cost grew from **68 bytes → 116 bytes** for `inbox_v2_ops_bt` because the V2 protocol carries the campaign-stat fields (`targetId`, `batchId`, `deviceOrdinal`, `pivot`) needed to make `markAsSent` idempotent against the DELIVERED bit flip.
- Per-item disk cost grew from **56 bytes → 57 bytes** for `inbox_v2_merge_bt` (the +1 is the version byte).
- The `pivot` field is variable-length — these calculations assume a 32-byte worst-case variant name. Accounts that consistently use longer pivot strings should re-derive the data-page cost.

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
| Items per data page | floor(8,388,608 / 84) = 99,864 | — |
| Data pages needed | ceil(72,000,000 / 99,864) = 721 pages | **5,768 MB** |
| Items per index page | 262,144 (fixed) | — |
| Index pages needed | ceil(72,000,000 / 262,144) = 275 pages | **2,200 MB** |
| **Subtotal** | | **7,968 MB** |

### Queue 2 — `inbox_v2_merge_bt`

```
Total items = 2,000 merges/s × 7,200 s = 14,400,000 items
```

| Layer | Calculation | Size |
|---|---|---|
| Items per data page | floor(8,388,608 / 25) = 335,544 | — |
| Data pages needed | ceil(14,400,000 / 335,544) = 43 pages | **344 MB** |
| Items per index page | 262,144 (fixed) | — |
| Index pages needed | ceil(14,400,000 / 262,144) = 55 pages | **440 MB** |
| **Subtotal** | | **784 MB** |

### Total per LP Pod (Worst Case)

| | Raw | With 50% buffer (fragmentation + FS metadata) |
|---|---|---|
| Both queues combined | **8,752 MB (~8.55 GB)** | **~12.83 GB** |
| **Recommended provisioning** | | **14 GB** |

---

## Comparison vs V1 Protocol

| Metric | V1 ([calculation.md](./calculation.md)) | V2 (this doc) | Delta |
|---|---|---|---|
| `InboxV2FStoreEntryDTO` payload (worst case) | 32 B | 80 B | +150% |
| `InboxV2FStoreEntryDTO` per-item on-disk | 68 B | 116 B | +71% |
| `InboxV2ProfileMergeDTO` payload | 20 B | 21 B | +5% |
| Standard provisioning (5k ev/s × 1h) | 2 GB | **4 GB** | +100% |
| Worst-case provisioning (10k ev/s × 2h) | 8 GB | **14 GB** | +75% |
