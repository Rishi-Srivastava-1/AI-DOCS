# Docker Basics

## What Problem Does Docker Solve?

When setting up a service locally you need to:
1. Install Java
2. Install Redis
3. Set environment variables
4. Hope it works on your machine

**The problem:** "It works on my machine but not on production"

**Docker solves this** — it packages the app + everything it needs into one unit called a **container**. Same container runs identically on your laptop, staging, and production.

---

## Key Concepts

| Concept | Analogy | Meaning |
|---------|---------|---------|
| **Image** | Recipe / blueprint | Template that defines what's in the container |
| **Container** | Cooked dish | A running instance of an image |
| **Dockerfile** | Cooking instructions | File that defines how to build an image |
| **Registry** | Recipe book store | Place where images are stored (e.g. AWS ECR) |

**Simple analogy:**
- **Image** = a WhatsApp installer (.apk file)
- **Container** = WhatsApp actually running on your phone
- You can run multiple containers from the same image (like installing WhatsApp on multiple phones)

---

## Common Docker Commands

### See running containers
```bash
docker ps
```
- Shows all **currently running** containers
- Like Task Manager on Windows

```bash
docker ps -a
```
- `-a` = all — shows stopped/crashed containers too

### See container details & config
```bash
docker inspect <container-id>
```
- Shows everything about a container — env variables, volumes, network, config

```bash
docker inspect <container-id> | grep -i mongo
```
- Filter inspect output for specific keywords (e.g. mongo, redis)

### Connect to a running container (like SSH into it)
```bash
docker exec -it <container-id> bash
```
- `exec` — run a command inside a running container
- `-it` — interactive terminal
- `bash` — open a bash shell inside the container

### See container logs
```bash
docker logs <container-id>
```
- Shows stdout/stderr output from the container

```bash
docker logs -f <container-id>
```
- `-f` = follow — streams logs in real time (like `tail -f`)

### See resource usage (live)
```bash
docker stats
```
- Shows real-time CPU, RAM, network usage for each container
- Like Task Manager but for Docker
- Press `Ctrl + C` to exit

### Pretty format for docker ps
```bash
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
```

---

## Docker in CleverTap's EC2 Setup

CleverTap runs services as Docker containers on EC2, managed by **AWS ECS**.

### What we observed:

**MongoDB EC2 (`10.14.7.22`):**
| Container | Purpose |
|-----------|---------|
| `mongo:6.0.27` | MongoDB database |
| `cadvisor` | Container resource monitoring |
| `splunk-forwarder` | Log shipping to Splunk |
| `fluent-bit` | Log aggregation |
| `mafiree-mongo-agent` | MongoDB monitoring agent |
| `amazon-ecs-agent` | AWS ECS orchestration |

**LP + Redis EC2 (`10.14.66.0`):**
| Container | Purpose |
|-----------|---------|
| `logprocessor` | LP (Log Processor) — processes events |
| `redis:8.6.4` | Redis — message queue between LC and LP |
| `cadvisor` | Container resource monitoring |
| `splunk-forwarder` | Log shipping to Splunk |
| `amazon-ecs-agent` | AWS ECS orchestration |

---

## Why Redis and LP are on the Same EC2

LP constantly reads from Redis:
```
Redis (queue) → LP reads from it
```
- Same machine = no network hop = faster communication
- Saves cost (one EC2 instead of two)

---

## How Docker Knows Where MongoDB Is

MongoDB connection is passed as an **environment variable** when the container starts:
```
MONGO_META_CLUSTER=sk1-mongo-metadata-1.mongodb.clevertap.net
```

Flow:
```
Infra team
    ↓
AWS Secrets Manager (stores credentials + hostnames)
    ↓
ECS Task Definition (injects them as env variables)
    ↓
Docker container starts with config already set
    ↓
App reads env variable and connects to MongoDB
```

---

## Docker vs Running Locally (IntelliJ)

| | IntelliJ (local) | Docker (EC2) |
|--|-----------------|--------------|
| Visible in `docker ps` | No | Yes |
| Visible in `ps aux` | Yes | Yes |
| Isolated environment | No | Yes |
| Same everywhere | No | Yes |

`docker ps` only shows Docker containers — to see ALL processes use:
```bash
ps aux
```

---

## AWS ECR (Elastic Container Registry)

CleverTap stores Docker images in **AWS ECR** — their private Docker image store.

Image name format:
```
939866731096.dkr.ecr.ap-northeast-2.amazonaws.com/sk1/clevertap/logprocessor:semaphore-nested-fup-change-daily-2026-06-10
```

| Part | Meaning |
|------|---------|
| `939866731096` | AWS account ID |
| `ap-northeast-2` | AWS region (Seoul) |
| `sk1/clevertap/logprocessor` | Image name |
| `semaphore-nested-fup-change-daily-2026-06-10` | Image tag (version/build) |

---

## AWS ECS (Elastic Container Service)

ECS is AWS's service that **manages and runs Docker containers on EC2**.

- Decides which container runs on which EC2
- Restarts containers if they crash
- Manages scaling (how many instances to run)
- Container names follow the pattern: `ecs-<cluster>-<service>-<task>`

---

## ECS Key Concepts

| Concept | Meaning | Analogy |
|---------|---------|---------|
| **Task Definition** | Blueprint — which image, CPU/RAM, env variables | Job description |
| **Task** | One running container | One employee doing the job |
| **Service** | Ensures X tasks always running | HR — always maintains headcount |
| **Cluster** | Group of EC2s managed together | Office building with many workers |

### Scheduling Strategies
- `Daemon` — runs **exactly 1 container per EC2** (like a system service)
- `Replica` — runs a **specific number of copies** (1, 2, 3...)

### Launch Types
- `EC2` — container runs on your EC2 machine
- `Fargate` — container runs on AWS's machine, no EC2 needed

---

## Fargate — Containers Without EC2

**Normal ECS + EC2:**
```
You manage EC2 → ECS runs containers on YOUR EC2 → You pay always
```

**ECS + Fargate:**
```
You just say "run this container with 0.5 CPU, 1GB RAM"
AWS manages the machine → You pay only when container runs
```

| | EC2 | Fargate |
|--|-----|---------|
| Like | Buying a car | Taking an Uber |
| You manage | Everything | Nothing |
| Pay | Always (even idle) | Only when running |
| Best for | Heavy, always-on (LP, Redis) | Lightweight agents (VmagentEcs) |

---

## Dockerfile — LP Real Example

LP's Dockerfile is at `core/lp/semaphore/Dockerfile`. It uses **multi-stage build**:

```
Stage 1 (base)    → Java 17 base image from ECR
Stage 2 (builder) → Download GeoIP + S2Mapping data
Stage 3 (final)   → Copy data + LP jar + entrypoint → Production image
```

### Key Dockerfile Instructions

| Instruction | Meaning |
|-------------|---------|
| `FROM` | Start from a base image |
| `ARG` | Build-time variable (passed during `docker build`) |
| `ENV` | Runtime environment variable (available when container runs) |
| `RUN` | Execute a command during build |
| `COPY` | Copy files into the image |
| `WORKDIR` | Set working directory |
| `USER` | Switch to a specific Linux user |
| `ENTRYPOINT` | Script/command that runs when container starts |
| `EXPOSE` | Documents which port the app listens on |

### ARG vs ENV

| | `ARG` | `ENV` |
|--|-------|-------|
| Available during | `docker build` only | Build AND runtime |
| Visible in container | No | Yes |
| Use for | Build-time config (BUILD_ID) | Runtime config (MONGO_URI, TZ) |

### Multi-Stage Build — Why?

```
Stage 2 (builder) — downloads GeoIP files (needs wget, tar)
        ↓
Stage 3 (final) — copies only the extracted files
        ↓
wget, tar NOT included in final image → smaller image size
```

### entrypoint.sh — What Happens When Container Starts

`core/lp/semaphore/entrypoint.sh` runs when LP container starts:
1. Reads container memory limit from cgroup
2. Calculates JVM heap size (`-Xmx`) as 80% of container RAM
3. Creates required directories (logs, data, heap-dump-store)
4. Generates LP config file with MongoDB host
5. Starts Java process with all JVM flags

### Who Builds the Dockerfile?
- **LP Developer** — writes code + maintains Dockerfile
- **Semaphore CI** — automatically runs `docker build` on every merge
- **ECR** — stores the built image
- **ECS** — pulls image from ECR and runs it on EC2

---

## Monitoring Containers in CleverTap

| Tool | Made by | Purpose |
|------|---------|---------|
| **VmagentEcs** | VictoriaMetrics (open source) | Collect metrics → send to Grafana |
| **Splunk Forwarder** | Splunk Inc. (commercial) | Collect and ship logs |
| **Cadvisor** | Google (open source) | Container CPU/RAM monitoring |

**VmagentEcs runs on Fargate** — so even if EC2 goes down, it keeps reporting that EC2 is down.
