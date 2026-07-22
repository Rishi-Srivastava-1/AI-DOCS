# AWS Storage — EBS and EFS

## The Problem Before EBS

EC2 originally had only **local disk** (Instance Store) — physically tied to the server:

```
EC2 on Server A → data on Server A's local disk
        ↓
Server A fails → EC2 moves to Server B
        ↓
Server B has no data → DATA GONE ❌
```

Same problem with containers:
```
Redis writing data inside container
        ↓
Container restarts → container filesystem resets
        ↓
ALL DATA GONE ❌
```

Same problem when upgrading EC2:
```
r6g.large EC2 → stop it → data gone
Start new r6g.xlarge → empty disk ❌
```

**Root cause:** Local disk is TIED to the physical server. Server dies → disk dies → data dies.

---

## EBS (Elastic Block Store)

### What is EBS?

EBS separates the disk from the server — it lives **independently** in AWS:

```
BEFORE EBS:
Physical Server = CPU + RAM + Disk (all tied together)

AFTER EBS:
Physical Server = CPU + RAM only
EBS = separate disk living in separate storage system
        ↓
EC2 connects to EBS over network
```

### How EBS Solves the Problem

**EC2 crashes:**
```
EC2 on Server A connected to EBS
        ↓
Server A fails
        ↓
EC2 moves to Server B
        ↓
Server B connects to SAME EBS Volume
        ↓
All data still there ✅
```

**Container restarts:**
```
EBS mounted at /var/lib/redis on EC2
Docker maps /var/lib/redis into Redis container
        ↓
Container crashes and restarts
        ↓
New container mounts same /var/lib/redis
        ↓
All Redis data still there ✅
```

**EC2 upgrade:**
```
Stop old r6g.large EC2
EBS sits independently — untouched
        ↓
Start new r6g.xlarge EC2
Attach same EBS Volume
        ↓
All data available ✅
```

---

### EBS is Mounted on EC2

EBS doesn't have its own path — it's accessed through EC2:

```
EBS Volume (living separately in AWS)
        ↓ mounted at
/var/lib/redis (path on EC2)
        ↓
When you write to /var/lib/redis on EC2
you are actually writing to EBS
```

**Analogy:**
> EBS is like a USB drive plugged into your laptop (EC2)
> - USB has its own storage
> - You access it as `D:/` on your laptop
> - Even if laptop breaks, USB data is safe
> - Plug USB into another laptop → data is there

---

### What We Saw in CleverTap

**lsblk output on LP+Redis EC2 (`ip-10-14-66-80`):**
```
NAME            SIZE   MOUNTPOINT
nvme1n1          64G   /var/lib/redis           ← EBS (Redis data)
nvme0n1          30G                            ← EBS (OS disk)
├─nvme0n1p1      30G   /                        ← OS partition
└─nvme0n1p128    10M   /boot/efi                ← boot partition
nvme2n1          30G   /var/lib/logprocessor    ← EBS (LP file queue)
```

Three EBS volumes on this EC2:

| Volume | Size | Mount Point | Purpose |
|--------|------|-------------|---------|
| `nvme0n1` | 30GB | `/` | OS + Docker containers |
| `nvme1n1` | 64GB | `/var/lib/redis` | Redis persistence data |
| `nvme2n1` | 30GB | `/var/lib/logprocessor` | LP file queue |

**AWS Console (Storage tab) showed:**

| Volume ID | Device | Size | Encrypted |
|-----------|--------|------|-----------|
| `vol-0299aa9ef1184058e` | `/dev/xvda` | 30GB | Yes |
| `vol-0350d5cfa58011f99` | `/dev/sdn` | 30GB | Yes |
| `vol-005e85046b03a3e78` | `/dev/sdp` | 64GB | Yes |

All volumes are **encrypted** using AWS KMS keys.

---

### Device Names Explained

Linux refers to disks using device names under `/dev/`:

| Name | Breakdown | Meaning |
|------|-----------|---------|
| `/dev/xvda` | `xv` = Xen Virtual, `d` = disk, `a` = first | First virtual disk |
| `/dev/sdn` | `sd` = SCSI disk, `n` = 14th letter | 14th disk slot |
| `/dev/sdp` | `sd` = SCSI disk, `p` = 16th letter | 16th disk slot |

**Analogy:**
> Like Windows drive letters: `C:` = `/dev/xvda`, `D:` = `/dev/sdn`, `E:` = `/dev/sdp`

---

### NVMe Naming (`nvme0n1`, `nvme1n1`, `nvme2n1`)

Modern EC2 instances use **NVMe** (NVM Express) — a faster disk interface designed for SSDs.

**Breaking down `/dev/nvme2n1`:**

| Part | Meaning |
|------|---------|
| `/dev/` | Device directory — where Linux keeps all hardware |
| `nvme` | NVM Express — type of fast disk interface |
| `2` | 3rd disk (0 = first, 1 = second, 2 = third) |
| `n1` | Namespace 1 — first partition group on this disk |

**All three disks on the LP+Redis EC2:**
```
nvme0n1  →  0 = first disk   (OS disk)
nvme1n1  →  1 = second disk  (Redis disk)
nvme2n1  →  2 = third disk   (LP disk)
```

Just counting up from 0 as EBS volumes are attached!

**Why NVMe and not `sda` or `xvda`?**
- NVMe is the fastest disk protocol available
- Designed specifically for SSDs
- AWS uses it on all modern EC2 instance types
- Much faster than older `sda`/`xvda` interfaces

**df -h Filesystem column explained:**
```
/dev/nvme2n1    30G    184K    28G    1%    /var/lib/logprocessor
```

| Column | Value | Meaning |
|--------|-------|---------|
| **Filesystem** | `/dev/nvme2n1` | The EBS pen drive (device name) |
| **Size** | 30G | Total size of the pen drive |
| **Used** | 184K | How much data is on it |
| **Avail** | 28G | How much space is left |
| **Use%** | 1% | Only 1% used |
| **Mounted on** | `/var/lib/logprocessor` | Where EC2 plugged it in |

> `Filesystem` = the actual EBS pen drive. `Mounted on` = the door on EC2 to access it. Two different things — one is the storage, one is the access point.

---

### Why LP Has Its Own EBS (`/var/lib/logprocessor`)

LP uses a **file queue** as a safety net. When MongoDB/ES is down or overloaded:

```
Events come in from Redis
        ↓
LP can't process (MongoDB down etc.)
        ↓
Events saved to /var/lib/logprocessor/filequeue
        ↓
LP retries when things recover
```

Contents of `/var/lib/logprocessor`:
```
filequeue/    ← LP's retry queue
lost+found/   ← Linux filesystem recovery folder (always present)
```

**Why dedicated EBS for this?**
```
Without dedicated EBS:
filequeue grows on OS disk (30GB)
        ↓
OS disk fills up → entire EC2 breaks ❌

With dedicated EBS:
filequeue grows on its own 30GB disk
        ↓
OS disk unaffected → EC2 keeps running ✅
```

This was a deliberate decision by the LP team — requested specifically so file queue growth never impacts the OS.

---

### EBS Key Properties

| Property | Detail |
|----------|--------|
| **Persistent** | Survives EC2 stop, restart, termination |
| **One-to-one** | One EBS volume → one EC2 at a time |
| **Network attached** | Connected over network (slightly slower than local) |
| **Fixed size** | You define the size upfront (30GB, 64GB etc.) |
| **Encrypted** | All CleverTap EBS volumes encrypted with KMS |
| **Enabled by** | `EnableXvdhEc2Volume: true` in `clusters.json` |

---

### How to Check EBS

**In terminal (on EC2):**
```bash
lsblk     # all disks + partitions + mount points (even unmounted)
df -h     # only mounted disks + space used/free
```

**In AWS Console:**
```
EC2 → Instances → click instance → Storage tab
```
Shows: Volume ID, device name, size, state, encryption, attachment time.

---

## EFS (Elastic File System)

### What is EFS?

EFS is shared storage — **multiple EC2s can access it simultaneously**:

```
EBS:                              EFS:
EC2 #1 → EBS Volume               EC2 #1 →
(only 1 EC2 at a time)            EC2 #2 → EFS (all simultaneously)
                                  EC2 #3 →
```

### EBS vs EFS

| | EBS | EFS |
|--|-----|-----|
| **EC2s** | Only 1 at a time | Multiple simultaneously |
| **Size** | Fixed (you define it) | Auto grows/shrinks |
| **Cost** | Pay for provisioned size | Pay for what you use |
| **Speed** | Faster | Slightly slower |
| **Analogy** | Personal locker | Shared Google Drive |
| **Use case** | Redis data, LP queue (single EC2) | Shared config, certificates (many EC2s) |

### What We Saw in CleverTap (df -h output)

```
127.0.0.1:/   8.0E   /var/lib/ecs/volumes/ecs-sk1-Redis-v2-Internal-lp-30-44-fs-...
```

- `8.0E` = 8 Exabytes — EFS is virtually unlimited size (auto grows)
- Mounted at `/var/lib/ecs/volumes/...`
- Used by ECS for shared storage across containers in the cluster

---

## Summary — Three Storage Types

| | Instance Store | EBS | EFS |
|--|---------------|-----|-----|
| **Lives on** | Physical server | Separate storage | Separate storage |
| **Survives restart** | ❌ No | ✅ Yes | ✅ Yes |
| **Multiple EC2s** | ❌ No | ❌ No | ✅ Yes |
| **Size** | Fixed | Fixed | Auto |
| **Speed** | Fastest | Fast | Slower |
| **CleverTap usage** | Not used | Redis, LP queue, OS | ECS shared volumes |
