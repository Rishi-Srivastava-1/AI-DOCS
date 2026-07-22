# How Deployment Works at CleverTap

## Overview — Two Parts

Every deployment has two separate phases:

```
Part 1: BUILD (generic — happens once)
        ↓
Part 2: DEPLOY (to a specific staging environment)
```

---

## Part 1 — Build

Triggered automatically when you push code to GitHub.

```
Developer pushes branch to GitHub
        ↓
Semaphore CI picks it up
        ↓
Builds Docker image for each service:
  - Notification Backend (~15 min)
  - Log Processor (~15 min)
  - Log Collector (~19 min)
  - Dashboard (~16 min)
        ↓
Pushes images to AWS ECR (image storage)
        ↓
Build complete — image tagged with branch + build number
e.g. logprocessor:semaphore-my-branch-ONC-352-123
```

**Key point:** Build is generic — not tied to any staging env yet.

---

## Part 2 — Deploy

Manually triggered — you choose which staging environment to deploy to.

```
You trigger deployment to staging-30
        ↓
Semaphore CI runs deploy_staging.yml
        ↓
Step 1: Wait for build image to be ready in ECR
        ↓
Step 2: Check if staging-30 is free (not used by someone else)
        ↓
Step 3: Check DND (Do Not Deploy) freeze
        ↓
Step 4: Clone CloudFormation repo (infra team's repo)
        ↓
Step 5: Update values.json with new ContainerTag (your build ID)
        → git commit + push to master of Cloudformation-Configuration repo
        ↓
Step 6: Run CloudFormation deploy (CF + ECS work together)
        → CF updates ECS Task Definition with new image tag
        → ECS stops old container, pulls new image from ECR, starts new container
        → ECS runs health check
        ↓
Health check passes → cf_deploy_success ✅
Health check fails  → cf_deploy_failed ❌
```

**Important:** CloudFormation and ECS both play a role in Step 6:
- **CloudFormation** = orchestrator — updates task definition, tells ECS what to do, monitors result
- **ECS** = executor — actually stops/starts containers, pulls images, runs health checks

---

## 30 Staging Environments

CleverTap has 30 staging environments. All created from **one CloudFormation template** with different parameters:

```
Same CF template
        ↓
STACK_NUMBER=1  → sk1-Redis-v2-Internal-1
STACK_NUMBER=2  → sk1-Redis-v2-Internal-2
...
STACK_NUMBER=30 → sk1-Redis-v2-Internal-30
```

Each staging has its own:
- EC2 machine
- ECS cluster
- LP container
- Redis container
- MongoDB connection (shared with production MongoDB cluster)

---

## Rolling Deployment — No Downtime (Multiple Instances)

When multiple LP instances are running:

```
10 LP instances running (old image)

Step 1: ECS stops container on EC2 #1
        Starts new container (new image) on EC2 #1
        Waits for health check ✅
        LB routes traffic to other 9 instances during update

Step 2: ECS stops container on EC2 #2
        Starts new container on EC2 #2
        Waits for health check ✅

... repeats until all 10 updated
```

**ECS settings that control this:**

| Setting | Meaning |
|---------|---------|
| Min healthy % | e.g. 90% → at least 9/10 must always be running |
| Max % | e.g. 110% → can temporarily go to 11 instances |

---

## Single Instance — Downtime on Deploy

Staging environments (like staging-30) typically have **1 EC2**:

```
1 LP container running (old image)
        ↓
ECS stops old container
        ↓
[DOWNTIME — no container running]
        ↓
ECS pulls new image from ECR
        ↓
New container starts
        ↓
Health check passes → back online
```

Downtime is acceptable on staging since it has no real user traffic.

---

## EC2 IP Stays Same Across Deployments

**Q: If we deploy multiple times, does the EC2 IP change?**

No! The EC2 machine stays the same — only the container is replaced:

```
EC2 (10.14.66.80) — launched April 23, running since then
        ↓
Deploy #1: old container stopped → new container started (same EC2)
Deploy #2: old container stopped → new container started (same EC2)
Deploy #3: old container stopped → new container started (same EC2)
```

IP only changes if the EC2 itself is replaced (hardware failure, scale down/up).

---

## CloudFormation Config Files — Where Things Are Defined

### File 1 — Cluster definition (EC2 setup)
**Path:** `Cloudformation/redis/sk1/clusters.json`

Defines the EC2 machine for each stack:
```json
{
  "Name": "sk1-Redis-v2-Internal-30",
  "StartIp": "10.14.66.80",       ← fixed IP (why Splunk always shows same IP)
  "InstanceCount": 1,              ← number of EC2s
  "InstanceType": "r6g.large",    ← machine size
  "EnableLogProcessorEcsService": true,  ← LP enabled/disabled
  "EnabledEc2Instance": true       ← EC2 on/off
}
```

### File 2 — Image versions (live state of all environments)
**Path:** `Cloudformation-Configuration/values.json`

Three entries per stack — example for staging-30:
```json
"sk1-Redis-v2-Internal-30": {
  "SplunkForwarderImageTag": "github-master-184"   ← Splunk version
}

"sk1-Redis-v2-Internal-30-Logprocessor-Service": {
  "ContainerTag": "semaphore-app-inbox-qa-4-2"     ← LP image version (YOUR build goes here)
}

"sk1-Redis-v2-Internal-30-Redis-Service": {
  "ContainerTag": "8.6.4"                           ← Redis version
}
```

**This is the answer to "where is Redis version defined?"** — `values.json` under `ContainerTag`.

### values.json is a live registry

Every deployment automatically updates `values.json` and commits to master:
```bash
# From deploy_staging.yml
git commit -am "LogProcessor: deploying semaphore-my-branch-123 to sk1-Redis-v2-Internal-30"
git push
```

So master of Cloudformation-Configuration always reflects:
- Which image version is currently deployed on each stack
- Who deployed it (commit message has branch name)
- When it was deployed (git commit timestamp)

### File 3 — Network config
**Path:** `Cloudformation-Configuration/stacks/clevertap/core/sk1/sk1-redis.cfg`
```json
{
  "Parameters": {
    "NetworkStack": "sk1"   ← which VPC/network to use
  }
}
```

---

## CloudFormation (CF)

CloudFormation is AWS's **Infrastructure as Code** tool.

Instead of manually clicking in AWS Console to create resources — write it all in a YAML file:

```yaml
Resources:
  MyEC2:
    Type: AWS::EC2::Instance
    Properties:
      InstanceType: r6g.large

  MyECSCluster:
    Type: AWS::ECS::Cluster

  MyLPService:
    Type: AWS::ECS::Service
    Properties:
      TaskDefinition: sk1-Redis-v2-Internal-lp-30
      DesiredCount: 1
```

CloudFormation creates everything automatically in the correct order.

### CloudFormation Stack

A **stack** = one complete deployment (all resources for one environment).

`sk1-Redis-v2-Internal-30` is a CF stack containing:
- EC2 instance
- ECS cluster
- LP service + task definition
- Redis service
- Splunk service
- Cadvisor service
- All env variables
- Security groups

### CF Rollback — Safety Net

```
CF detects container failed health check
        ↓
Automatically rolls back to previous working version
        ↓
Old container starts again
        ↓
Staging back to previous state
```

### Where CF Templates Live

CF templates are in a **separate private repo** managed by infra team:
- `CleverTap-SNE/Cloudformation` — templates
- `CleverTap-SNE/Cloudformation-Configuration` — config values

Ultron only contains `deploy_staging.yml` — the script that triggers CF deployment.

---

## cf_deploy_failed — What It Means

`cf_deploy_failed` is set in `deploy_staging.yml` line 221:
```bash
if ! python3 scripts/deploy.py ...; then
  echo "CloudFormation deploy failed..."
  sem-context put deploymentFailureReason="cf_deploy_failed"
  exit 1
fi
```

**Most common causes:**

| Cause | What happens |
|-------|-------------|
| Container fails health check | App crashed or not responding on port |
| Out of memory | Container needs more RAM than EC2 has |
| Missing env variable | App couldn't find required config |
| ECR pull failed | EC2 couldn't download image |
| Health check timeout | Container took too long to start |

---

## DND (Do Not Deploy) Check

Before deploying, Semaphore checks if a **deployment freeze** is active:

```bash
./scripts/check-dnd-from-postgraphile-and-replace-parameter-value
```

If freeze is on → deployment fails with `deploymentFailureReason="dnd"` instead of deploying.

Used during:
- Production release freeze
- Critical incidents
- Maintenance windows

---

## Full Picture — Build to Production

```
Developer branch
        ↓
Push to GitHub
        ↓
Semaphore: Run tests
        ↓
Semaphore: Build Docker image → push to ECR
        ↓
Deploy to staging (manual trigger)
        ↓
Test on staging
        ↓
PR reviewed + approved
        ↓
Merge to master
        ↓
Semaphore: Build production image → push to ECR
        ↓
Deploy to production (manual trigger with approvals)
        ↓
Rolling deployment across all production EC2s
```

---

## Three AWS Services — Summary

| Service | Full Name | Role |
|---------|-----------|------|
| **ECR** | Elastic Container Registry | Stores Docker images (warehouse) |
| **ECS** | Elastic Container Service | Manages and runs containers (manager) |
| **EC2** | Elastic Compute Cloud | The actual machine containers run on (worker) |

```
Semaphore builds image → ECR stores it → ECS pulls it → EC2 runs it
     (factory)            (warehouse)      (manager)      (worker)
```
