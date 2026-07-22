# EBS Size Calculation for App Inbox V2 File Queue — V3 (Real Analytics)

> **What changed since [calculationV2.md](./calculationV2.md):** Rates are now derived from real
> production analytics (30-day event counts) across eu1 and in1 regions, not synthetic assumptions.
> Multiple LP instance-count scenarios are provided.
> DTO sizes and queue overhead are identical to V2 protocol.

---

## Raw Analytics Data

Analytics events used:
- `notification sent` — campaign type: `app inbox` → drives **DELIVERED** ops
- `notification viewed` — campaign type: `app inbox` → drives **READ** ops
- `identity set` (type: appended) → drives **profile merge**

Per-second formula: `count / (30 × 24 × 60 × 60)` = `count / 2,592,000`

### Notification Sent / Viewed (30-day counts)

#### eu1

| Account | Sent (30d) | Viewed (30d) |
|---|---|---|
| MX Player | 72,577,515 | 0 |
| Rapido Rider | 144,239,455 | 0 |
| Business Lane | 1,500,330 | 0 |

#### in1

| Account | Sent (30d) | Viewed (30d) |
|---|---|---|
| Bajaj | 2,390,864,938 | 38,802,000 |
| Milk Basket | 6,618,800 | 7,912 |
| Kotak811 | 161,162,002 | 11,861,976 |
| Jagran | 41,069,232 | 137,050 |
| HDFC | 8,086,763 | 145,494 |

### Identity Set (30-day counts, profile merge)

| Account | Identity Set (30d) |
|---|---|
| Bajaj | 22,000,000 |
| Kotak811 | 5,500,000 |
| MX Player | 1,600,000 |

---

## Rate Derivation

### Ops (DELIVERED + READ + DELETE)

**DELIVERED (notification sent):**
```
Max single account : 2,390,864,938 (~2.3B) — Bajaj
Assume 20 accounts of this scale per region
Total 30d          : 2.3B × 20 = 46B
Per sec            : 46B / 2,592,000 ≈ 18,000 / sec
```

**READ (notification viewed):**
```
Max single account : 38,802,000 (~39M) — Bajaj
Assume 20 accounts of this scale per region
Total 30d          : 39M × 20 = 780M (~800M)
Per sec            : 800M / 2,592,000 ≈ 310 / sec
```

**DELETE:**
```
Manual deletes     : negligible
Non-persistent PBS : ~1/3 of sent events (live non-persistent messages are not
                     saved to FStore, but delete events are still processed)
                   : 18,000 / 3 ≈ 6,000 / sec
```

**Total ops per sec (across all LPs in region):**
```
DELIVERED + READ + DELETE = 18,000 + 310 + 6,000 ≈ 25,000 / sec
```

> Note: rates are **low relative to general LP traffic** because these events are generated
> only for accounts actively running App Inbox V2 campaigns.

### Profile Merge

```
Max single account : 22,000,000 — Bajaj
Assume 200 accounts across region
Total 30d          : 22M × 200 = 4.4B
Per sec            : 4.4B / 2,592,000 ≈ 1,700 / sec
```

---

## Per-LP Instance Rates

LP is sharded by userId — total regional traffic is split evenly across LP instances.

| LP instances | Ops / sec / LP | Merge / sec / LP |
|---|---|---|
| 7 | 25,000 / 7 ≈ **3,500** | 1,700 / 7 ≈ **240** |
| 13 | 25,000 / 13 ≈ **2,000** | 1,700 / 13 ≈ **130** |
| 50 | 25,000 / 50 = **500** | 1,700 / 50 ≈ **34** |
| 100 | 25,000 / 100 = **250** | 1,700 / 100 ≈ **17** |

---

## DTO Serialized Byte Sizes (V2 Protocol)

*(Unchanged from [calculationV2.md](./calculationV2.md))*

| DTO | Payload (worst case) | + CRC → data page cost | Index entry | Total on-disk |
|---|---|---|---|---|
| `InboxV2FStoreEntryDTO` | 80 bytes | 84 bytes | 32 bytes | **116 bytes** |
| `InboxV2ProfileMergeDTO` | 21 bytes | 25 bytes | 32 bytes | **57 bytes** |

---

## Queue Storage Overhead (`SmallerPageSizePersistentQueue`)

| Parameter | Value |
|---|---|
| Data page size | 8 MB (8,388,608 bytes) |
| Per-item data overhead | +4 bytes CRC32 |
| Index entry size | 32 bytes (fixed) |
| Items per index page | 262,144 |
| **Ops items per data page** | floor(8,388,608 / 84) = **99,864** |
| **Merge items per data page** | floor(8,388,608 / 25) = **335,544** |

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
Ops total  = 3,500 × 7,200 = 25,200,000
Merge total = 240 × 7,200  =  1,728,000
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
Ops total  = 2,000 × 7,200 = 14,400,000
Merge total = 130 × 7,200  =    936,000
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
Ops total  = 500 × 7,200 = 3,600,000
Merge total = 34 × 7,200 =   244,800
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
Ops total  = 250 × 7,200 = 1,800,000
Merge total = 17 × 7,200 =   122,400
```
| Queue | Data pages | Index pages | Subtotal |
|---|---|---|---|
| `inbox_v2_ops_bt` | ceil(1,800,000 / 99,864) = 19 × 8 MB = 152 MB | 7 × 8 MB = 56 MB | **208 MB** |
| `inbox_v2_merge_bt` | ceil(122,400 / 335,544) = 1 × 8 MB = 8 MB | 1 × 8 MB = 8 MB | **16 MB** |

| | Raw | With 50% buffer | **Recommended** |
|---|---|---|---|
| Both queues | 224 MB | ~336 MB | **512 MB** |

---

## Summary Table

| LP instances | Ops /s/LP | Merge /s/LP | 1h outage (recommended) | 2h outage (recommended) | 6h outage (recommended) |
|---|---|---|---|---|---|
| 7 | 3,500 | 240 | **3 GB** | **5 GB** | **14 GB** |
| 13 | 2,000 | 130 | **2 GB** | **3 GB** | **8 GB** |
| 50 | 500 | 34 | **1 GB** | **1 GB** | **2 GB** |
| 100 | 250 | 17 | **512 MB** | **512 MB** | **1 GB** |

---

## Notes

- Rates are derived from 30-day production analytics. Bajaj is the single-account ceiling — 20-account and 200-account multipliers are conservative worst-case assumptions.
- `inbox_v2_merge_bt` does not exist until the profile merge PR is implemented. Until then, only `inbox_v2_ops_bt` storage applies.
- Non-persistent PBS messages (1/3 of sent) are not stored in FStore but still generate DELETE events processed by LP — counted in the ops total.
- This document uses V2 protocol DTO sizes. See [calculationV2.md](./calculationV2.md) for the protocol change rationale.
- The **2 GB** figure and above cover only the file queue directories. OS, JVM heap, and other LP disk usage are additive.
