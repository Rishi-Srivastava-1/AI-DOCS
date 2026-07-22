# AWS Basics — EC2 & Related Concepts

## What is EC2?
EC2 (Elastic Compute Cloud) is AWS's virtual server service. Think of it as a **PC in the cloud**.

| Your PC | EC2 Instance |
|---------|-------------|
| Physical machine on your desk | Virtual machine in AWS data center |
| Windows / Mac OS | Usually Linux |
| You access via keyboard/monitor | You access via SSH or Session Manager |
| You pay electricity bill | You pay AWS per hour |

---

## Instance Types
The instance type defines the hardware spec (CPU, RAM).

- `r6g` — memory-optimized, uses AWS Graviton2 (ARM) processors
- `m5` — general purpose
- `c5` — compute optimized
- Sizes: `medium < large < 2xlarge < 4xlarge` (increasing CPU/RAM)

---

## How to Connect to EC2
**AWS Systems Manager Session Manager** — browser-based terminal, no SSH keys needed.
- Access controlled via AWS IAM permissions
- Sessions are logged and auditable (session ID tied to your email)
- Prompt shows `sh-5.2$` (normal user) or `[root@...]#` (root user)

---

## Users & Permissions

### Normal User vs Root
| | Normal User | Root User |
|--|-------------|-----------|
| Prompt | `$` | `#` |
| Access | Limited | Full (admin) |
| Analogy | Employee with ID card | Security admin with master key |

### Switch to Root
```bash
sudo su -
```
- `sudo` — Super User Do (run with admin privileges)
- `su` — Switch User
- `-` — load root's full environment

### Go back to normal user
```bash
exit
```

### Who controls sudo access?
Two layers of protection:
1. **AWS IAM** — controls who can even connect via Session Manager
2. **`/etc/sudoers`** — controls who can run `sudo` once inside

---

## Checking System Resources

### RAM
```bash
free -h
```
- `-h` = human readable (shows GB/MB)

Output columns: `total`, `used`, `free`, `available`

### CPU
```bash
nproc        # number of cores
lscpu        # detailed CPU info
```

### Disk
```bash
df -h
```
- `df` = Disk Free
- `-h` = human readable
- Shows disk space used vs free for each mounted disk

---

## Filesystem Structure
```bash
ls /    # show top level of filesystem (like C:\ on Windows)
```

| Folder | Purpose |
|--------|---------|
| `/etc` | Config files |
| `/var` | Logs, data, caches |
| `/usr` | Programs and commands |
| `/bin` | Essential commands (ls, cp, grep etc.) |
| `/home` | Regular users' home directories |
| `/root` | Root user's home directory |
| `/tmp` | Temporary files (cleared on reboot) |
| `/proc` | Running processes info (virtual folder) |

---

## Network Commands

### Check MongoDB connections
```bash
ss -tnp | grep 27017
```
- `ss` — Socket Statistics (show network connections)
- `-t` — TCP connections only
- `-n` — show IP addresses (not hostnames)
- `-p` — show which process is making the connection
- `grep 27017` — filter only MongoDB connections (27017 is MongoDB's default port)
- `|` — pipe: pass output of one command to the next

Output columns:
| Column | Meaning |
|--------|---------|
| `ESTAB` | Connection is active |
| `Recv-Q` | Data waiting to be received (0 = healthy) |
| `Send-Q` | Data waiting to be sent (0 = healthy) |
| Local Address | This machine's IP and port |
| Remote Address | The MongoDB server's IP and port |

---

## MongoDB on EC2 (CleverTap Setup)
- MongoDB runs inside a **Docker container** on EC2
- Managed by **AWS ECS** (Elastic Container Service)
- Running **MongoDB 6.0.27**
- Part of a **replica set** (`rs0`) — multiple MongoDB nodes working together

### Replica Set
- Multiple MongoDB nodes (we saw 6: `10.14.7.22`, `10.14.7.7`, `10.14.7.38`, `10.14.6.6`, `10.14.6.20`, `10.14.6.5`)
- App servers connect to all nodes simultaneously
- Provides redundancy — if one node fails, others take over
- Always odd number of nodes (3, 5, or 7) for voting

### App Server → MongoDB Connection
All 100 app servers connect to the **same MongoDB replica set** via a shared connection string stored in AWS Secrets Manager or environment variables.

---

## Redis on EC2
- Redis runs alongside the app on the same EC2 instance
- Has a **dedicated 62 GB disk** at `/var/lib/redis` for persistence files
- No memory (`maxmemory`) limit set — can use as much RAM as available
- `df -h` measures **disk usage**, not RAM usage
- To check actual Redis RAM usage:
```bash
redis-cli info memory | grep used_memory_human
```

---

## EC2 to EC2 Communication (VPC)

EC2 machines can talk to each other freely within the same **VPC (Virtual Private Cloud)**.

- VPC is like a **private office network** in AWS
- All EC2s inside it communicate using **private IPs** (e.g. `10.14.x.x`)
- `10.x.x.x` addresses are private — not reachable from the internet
- **Security Groups** act as firewalls — control which machines can talk to which
  - e.g. "Only LP machines can connect to MongoDB on port 27017"

**How LP finds MongoDB:**
- LP doesn't use MongoDB's IP directly — it uses a **DNS name**:
  ```
  MONGO_META_CLUSTER=sk1-mongo-metadata-1.mongodb.clevertap.net
  ```
- DNS acts like a phonebook — resolves the name to the actual IP
- If MongoDB moves to a new IP, only DNS is updated — LP config stays the same

---

## Auto Scaling — When EC2s are Added or Removed

**Scale Up (add EC2s):**
```
Normal traffic → 10 LP instances
        ↓
Sudden spike (festival sale, IPL match)
        ↓
CPU/RAM goes high
        ↓
AWS Auto Scaling adds more EC2s automatically
```

**Scale Down (remove EC2s):**
- Traffic drops → extra EC2s removed automatically
- Cost optimization — no point paying for idle machines

**EC2 can also go down:**

| Reason | Example |
|--------|---------|
| Hardware failure | AWS datacenter issue |
| Out of memory | App consumes all RAM |
| Out of disk | Logs fill up the disk |
| App crash | Bug causes JVM crash |
| AWS maintenance | AWS retires old hardware |

When EC2 goes down → ECS detects it → starts container on another EC2 → Auto Scaling replaces the EC2.

---

## Useful Commands Cheatsheet

| Command | Meaning |
|---------|---------|
| `ls` | List files in current directory |
| `ls /` | List top-level filesystem |
| `pwd` | Print current directory path |
| `cd ~` | Go to home directory |
| `cat <file>` | Print contents of a text file |
| `free -h` | Show RAM usage |
| `df -h` | Show disk usage |
| `nproc` | Show number of CPU cores |
| `ss -tnp` | Show network connections |
| `docker ps` | Show running Docker containers |
| `sudo su -` | Switch to root user |
| `exit` | Go back to previous user |
| `Ctrl + C` | Cancel/stop a running command |
| `grep <word> <file>` | Search for a word in a file |
