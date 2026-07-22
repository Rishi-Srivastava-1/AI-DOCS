# App Inbox V2 — Traffic Analysis

> Rates derived from real 30-day production analytics across eu1 and in1 regions.
> These numbers feed into [ebsCalculationNewProtocol.md](./ebsCalculationNewProtocol.md).

---

## Analytics Events Tracked

| Event | Campaign type | Drives |
|---|---|---|
| `notification sent` | app inbox | **DELIVERED** ops |
| `notification viewed` | app inbox | **READ** ops |
| `identity set` (type: appended) | — | **profile merge** |

Per-second formula: `count / (30 × 24 × 60 × 60)` = `count / 2,592,000`

---

## Raw Data — Notification Sent / Viewed (30-day counts)

### eu1

| Account | Sent (30d) | Viewed (30d) |
|---|---|---|
| MX Player | 72,577,515 | 0 |
| Rapido Rider | 144,239,455 | 0 |
| Business Lane | 1,500,330 | 0 |

### in1

| Account | Sent (30d) | Viewed (30d) |
|---|---|---|
| Bajaj | 2,390,864,938 | 38,802,000 |
| Milk Basket | 6,618,800 | 7,912 |
| Kotak811 | 161,162,002 | 11,861,976 |
| Jagran | 41,069,232 | 137,050 |
| HDFC | 8,086,763 | 145,494 |

## Raw Data — Identity Set (30-day counts, profile merge)

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

> Rates are **low relative to general LP traffic** because these events are generated
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

LP is sharded by userId — total regional traffic splits evenly across LP instances.

| LP instances | Ops / sec / LP | Merge / sec / LP |
|---|---|---|
| 7 | 25,000 / 7 ≈ **3,500** | 1,700 / 7 ≈ **240** |
| 13 | 25,000 / 13 ≈ **2,000** | 1,700 / 13 ≈ **130** |
| 50 | 25,000 / 50 = **500** | 1,700 / 50 ≈ **34** |
| 100 | 25,000 / 100 = **250** | 1,700 / 100 ≈ **17** |
