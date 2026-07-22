# EBS Size Calculation — App Inbox V2 (New Protocol)

> Rates are sourced from [traffic.md](./traffic.md).
> DTO sizes reflect the V2 protocol (version byte + campaign-stat fields on ops DTO).
> See [calculationV2.md](./calculationV2.md) for the protocol change rationale.

---

## File Queues in Scope

| Queue name | Batch task | DTO |
|---|---|---|
| `inbox_v2_ops_bt` | `InboxV2FStoreOperationsBatchTask` | `InboxV2FStoreEntryDTO` |
| `inbox_v2_merge_bt` | `InboxV2FStoreProfileMergeBatchTask` | `InboxV2ProfileMergeDTO` *(planned)* |

---

## DTO Serialized Byte Sizes (V2 Protocol)

### `InboxV2FStoreEntryDTO`

```
[1: version][4: accountId][8: userId][1: mutationType][4: targetId][4: batchId]
[4: deviceOrdinal][2: pivotLen][M: pivot UTF-8][K: identifier UTF-8]
```

| Field | Bytes |
|---|---|
| `version` (byte) | 1 |
| `accountId` (int) | 4 |
| `userId` (long) | 8 |
| `mutationType` (byte ordinal) | 1 |
| `targetId` (int) | 4 |
| `batchId` (int) | 4 |
| `deviceOrdinal` (int) | 4 |
| `pivotLen` (short) | 2 |
| `pivot` (UTF-8) — typical `"wzrk_default"` = 12B; worst case = 32B | 12–32 |
| `identifier` (UTF-8) — `targetId_yyyyMMdd` = 19B | 19 |
| **Worst case payload** | **79 B → rounded to 80 B** |

### `InboxV2ProfileMergeDTO` *(planned)*

```
[1: version][4: accountId][8: iid][8: idn]
```

| Field | Bytes |
|---|---|
| `version` (byte) | 1 |
| `accountId` (int) | 4 |
| `iid` (long) | 8 |
| `idn` (long) | 8 |
| **Total payload** | **21 bytes (fixed)** |

---

## Queue Storage Overhead (`SmallerPageSizePersistentQueue`)

| Parameter | Value |
|---|---|
| Data page size | 8 MB (8,388,608 bytes) |
| Per-item data overhead | +4 bytes CRC32 |
| Index entry size | 32 bytes (fixed per item) |
| Items per index page | 262,144 |
| **Ops items per data page** | floor(8,388,608 / 84) = **99,864** |
| **Merge items per data page** | floor(8,388,608 / 25) = **335,544** |

**Effective on-disk cost per item:**

| DTO | Payload | + CRC → data page cost | Index entry | Total on-disk |
|---|---|---|---|---|
| `InboxV2FStoreEntryDTO` | 80 B | 84 B | 32 B | **116 B** |
| `InboxV2ProfileMergeDTO` | 21 B | 25 B | 32 B | **57 B** |

Pages are allocated in 8 MB chunks — even 1 item allocates a full 8 MB page.

---

## EBS Calculations

### Standard Sizing — 1 Hour FStore Outage

---

#### Scenario A — 7 LP Instances (3,500 ops/s · 240 merge/s)

**Queue 1 — `inbox_v2_ops_bt`**
```
Total items = 3,500 × 3,600 = 12,600,000
```
| Layer | Calculation | Size |
|---|---|---|
| Data pages | ceil(12,600,000 / 99,864) = 127 pages | **1,016 MB** |
| Index pages | ceil(12,600,000 / 262,144) = 49 pages | **392 MB** |
| **Subtotal** | | **1,408 MB** |

**Queue 2 — `inbox_v2_merge_bt`**
```
Total items = 240 × 3,600 = 864,000
```
| Layer | Calculation | Size |
|---|---|---|
| Data pages | ceil(864,000 / 335,544) = 3 pages | **24 MB** |
| Index pages | ceil(864,000 / 262,144) = 4 pages | **32 MB** |
| **Subtotal** | | **56 MB** |

| | Raw | With 50% buffer | **Recommended** |
|---|---|---|---|
| Both queues | 1,464 MB | ~2,196 MB | **3 GB** |

---

#### Scenario B — 13 LP Instances (2,000 ops/s · 130 merge/s)

**Queue 1 — `inbox_v2_ops_bt`**
```
Total items = 2,000 × 3,600 = 7,200,000
```
| Layer | Calculation | Size |
|---|---|---|
| Data pages | ceil(7,200,000 / 99,864) = 73 pages | **584 MB** |
| Index pages | ceil(7,200,000 / 262,144) = 28 pages | **224 MB** |
| **Subtotal** | | **808 MB** |

**Queue 2 — `inbox_v2_merge_bt`**
```
Total items = 130 × 3,600 = 468,000
```
| Layer | Calculation | Size |
|---|---|---|
| Data pages | ceil(468,000 / 335,544) = 2 pages | **16 MB** |
| Index pages | ceil(468,000 / 262,144) = 2 pages | **16 MB** |
| **Subtotal** | | **32 MB** |

| | Raw | With 50% buffer | **Recommended** |
|---|---|---|---|
| Both queues | 840 MB | ~1,260 MB | **2 GB** |

---

#### Scenario C — 50 LP Instances (500 ops/s · 34 merge/s)

**Queue 1 — `inbox_v2_ops_bt`**
```
Total items = 500 × 3,600 = 1,800,000
```
| Layer | Calculation | Size |
|---|---|---|
| Data pages | ceil(1,800,000 / 99,864) = 19 pages | **152 MB** |
| Index pages | ceil(1,800,000 / 262,144) = 7 pages | **56 MB** |
| **Subtotal** | | **208 MB** |

**Queue 2 — `inbox_v2_merge_bt`**
```
Total items = 34 × 3,600 = 122,400
```
| Layer | Calculation | Size |
|---|---|---|
| Data pages | ceil(122,400 / 335,544) = 1 page | **8 MB** |
| Index pages | ceil(122,400 / 262,144) = 1 page | **8 MB** |
| **Subtotal** | | **16 MB** |

| | Raw | With 50% buffer | **Recommended** |
|---|---|---|---|
| Both queues | 224 MB | ~336 MB | **1 GB** |

---

#### Scenario D — 100 LP Instances (250 ops/s · 17 merge/s)

**Queue 1 — `inbox_v2_ops_bt`**
```
Total items = 250 × 3,600 = 900,000
```
| Layer | Calculation | Size |
|---|---|---|
| Data pages | ceil(900,000 / 99,864) = 10 pages | **80 MB** |
| Index pages | ceil(900,000 / 262,144) = 4 pages | **32 MB** |
| **Subtotal** | | **112 MB** |

**Queue 2 — `inbox_v2_merge_bt`**
```
Total items = 17 × 3,600 = 61,200
```
| Layer | Calculation | Size |
|---|---|---|
| Data pages | ceil(61,200 / 335,544) = 1 page | **8 MB** |
| Index pages | ceil(61,200 / 262,144) = 1 page | **8 MB** |
| **Subtotal** | | **16 MB** |

| | Raw | With 50% buffer | **Recommended** |
|---|---|---|---|
| Both queues | 128 MB | ~192 MB | **512 MB** |

---

### Worst-Case Sizing — 2 Hour FStore Outage

---

#### Scenario A — 7 LP Instances (3,500 ops/s · 240 merge/s)

```
Ops total   = 3,500 × 7,200 = 25,200,000
Merge total =   240 × 7,200 =  1,728,000
```
| Queue | Data pages | Index pages | Subtotal |
|---|---|---|---|
| `inbox_v2_ops_bt` | ceil(25,200,000 / 99,864) = 253 × 8 MB = 2,024 MB | 97 × 8 MB = 776 MB | **2,800 MB** |
| `inbox_v2_merge_bt` | ceil(1,728,000 / 335,544) = 6 × 8 MB = 48 MB | 7 × 8 MB = 56 MB | **104 MB** |

| | Raw | With 50% buffer | **Recommended** |
|---|---|---|---|
| Both queues | 2,904 MB | ~4,356 MB | **5 GB** |

---

#### Scenario B — 13 LP Instances (2,000 ops/s · 130 merge/s)

```
Ops total   = 2,000 × 7,200 = 14,400,000
Merge total =   130 × 7,200 =    936,000
```
| Queue | Data pages | Index pages | Subtotal |
|---|---|---|---|
| `inbox_v2_ops_bt` | ceil(14,400,000 / 99,864) = 145 × 8 MB = 1,160 MB | 55 × 8 MB = 440 MB | **1,600 MB** |
| `inbox_v2_merge_bt` | ceil(936,000 / 335,544) = 3 × 8 MB = 24 MB | 4 × 8 MB = 32 MB | **56 MB** |

| | Raw | With 50% buffer | **Recommended** |
|---|---|---|---|
| Both queues | 1,656 MB | ~2,484 MB | **3 GB** |

---

#### Scenario C — 50 LP Instances (500 ops/s · 34 merge/s)

```
Ops total   = 500 × 7,200 = 3,600,000
Merge total =  34 × 7,200 =   244,800
```
| Queue | Data pages | Index pages | Subtotal |
|---|---|---|---|
| `inbox_v2_ops_bt` | ceil(3,600,000 / 99,864) = 37 × 8 MB = 296 MB | 14 × 8 MB = 112 MB | **408 MB** |
| `inbox_v2_merge_bt` | ceil(244,800 / 335,544) = 1 × 8 MB = 8 MB | 1 × 8 MB = 8 MB | **16 MB** |

| | Raw | With 50% buffer | **Recommended** |
|---|---|---|---|
| Both queues | 424 MB | ~636 MB | **1 GB** |

---

#### Scenario D — 100 LP Instances (250 ops/s · 17 merge/s)

```
Ops total   = 250 × 7,200 = 1,800,000
Merge total =  17 × 7,200 =   122,400
```
| Queue | Data pages | Index pages | Subtotal |
|---|---|---|---|
| `inbox_v2_ops_bt` | ceil(1,800,000 / 99,864) = 19 × 8 MB = 152 MB | 7 × 8 MB = 56 MB | **208 MB** |
| `inbox_v2_merge_bt` | ceil(122,400 / 335,544) = 1 × 8 MB = 8 MB | 1 × 8 MB = 8 MB | **16 MB** |

| | Raw | With 50% buffer | **Recommended** |
|---|---|---|---|
| Both queues | 224 MB | ~336 MB | **512 MB** |

---

### Worst-Case Sizing — 6 Hour FStore Outage

---

#### Scenario A — 7 LP Instances (3,500 ops/s · 240 merge/s)

```
Ops total   = 3,500 × 21,600 = 75,600,000
Merge total =   240 × 21,600 =  5,184,000
```
| Queue | Data pages | Index pages | Subtotal |
|---|---|---|---|
| `inbox_v2_ops_bt` | ceil(75,600,000 / 99,864) = 758 × 8 MB = 6,064 MB | 289 × 8 MB = 2,312 MB | **8,376 MB** |
| `inbox_v2_merge_bt` | ceil(5,184,000 / 335,544) = 16 × 8 MB = 128 MB | 20 × 8 MB = 160 MB | **288 MB** |

| | Raw | With 50% buffer | **Recommended** |
|---|---|---|---|
| Both queues | 8,664 MB | ~12,996 MB | **14 GB** |

---

#### Scenario B — 13 LP Instances (2,000 ops/s · 130 merge/s)

```
Ops total   = 2,000 × 21,600 = 43,200,000
Merge total =   130 × 21,600 =  2,808,000
```
| Queue | Data pages | Index pages | Subtotal |
|---|---|---|---|
| `inbox_v2_ops_bt` | ceil(43,200,000 / 99,864) = 433 × 8 MB = 3,464 MB | 165 × 8 MB = 1,320 MB | **4,784 MB** |
| `inbox_v2_merge_bt` | ceil(2,808,000 / 335,544) = 9 × 8 MB = 72 MB | 11 × 8 MB = 88 MB | **160 MB** |

| | Raw | With 50% buffer | **Recommended** |
|---|---|---|---|
| Both queues | 4,944 MB | ~7,416 MB | **8 GB** |

---

#### Scenario C — 50 LP Instances (500 ops/s · 34 merge/s)

```
Ops total   = 500 × 21,600 = 10,800,000
Merge total =  34 × 21,600 =    734,400
```
| Queue | Data pages | Index pages | Subtotal |
|---|---|---|---|
| `inbox_v2_ops_bt` | ceil(10,800,000 / 99,864) = 109 × 8 MB = 872 MB | 42 × 8 MB = 336 MB | **1,208 MB** |
| `inbox_v2_merge_bt` | ceil(734,400 / 335,544) = 3 × 8 MB = 24 MB | 3 × 8 MB = 24 MB | **48 MB** |

| | Raw | With 50% buffer | **Recommended** |
|---|---|---|---|
| Both queues | 1,256 MB | ~1,884 MB | **2 GB** |

---

#### Scenario D — 100 LP Instances (250 ops/s · 17 merge/s)

```
Ops total   = 250 × 21,600 = 5,400,000
Merge total =  17 × 21,600 =   367,200
```
| Queue | Data pages | Index pages | Subtotal |
|---|---|---|---|
| `inbox_v2_ops_bt` | ceil(5,400,000 / 99,864) = 55 × 8 MB = 440 MB | 21 × 8 MB = 168 MB | **608 MB** |
| `inbox_v2_merge_bt` | ceil(367,200 / 335,544) = 2 × 8 MB = 16 MB | 2 × 8 MB = 16 MB | **32 MB** |

| | Raw | With 50% buffer | **Recommended** |
|---|---|---|---|
| Both queues | 640 MB | ~960 MB | **1 GB** |

---

## Summary Table

| LP instances | Ops /s/LP | Merge /s/LP | 1h outage | 2h outage | 6h outage |
|---|---|---|---|---|---|
| 7 | 3,500 | 240 | **3 GB** | **5 GB** | **14 GB** |
| 13 | 2,000 | 130 | **2 GB** | **3 GB** | **8 GB** |
| 50 | 500 | 34 | **1 GB** | **1 GB** | **2 GB** |
| 100 | 250 | 17 | **512 MB** | **512 MB** | **1 GB** |

---

## Notes

- `inbox_v2_merge_bt` does not exist until the profile merge PR is implemented. Until then only `inbox_v2_ops_bt` applies.
- Non-persistent PBS messages (1/3 of sent) are not stored in FStore but still generate DELETE events processed by LP — counted in the ops total.
- All figures cover only the file queue directories. OS, JVM heap, and other LP disk usage are additive.
- For rate derivation and raw analytics data see [traffic.md](./traffic.md).
