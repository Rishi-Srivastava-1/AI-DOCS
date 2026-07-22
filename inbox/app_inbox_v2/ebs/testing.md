# Testing File Queue Changes

## Prerequisites for Staging

Staging testing requires AWS EKS access. Request IAM role with:

- `eks:DescribeCluster`
- `eks:ListClusters`
- Kubernetes RBAC: `pods: get, list, exec, delete` on LP namespace

### Setup after getting IAM access

```bash
# Configure AWS credentials
aws configure

# Verify access
aws sts get-caller-identity

# Configure kubectl for EKS
aws eks update-kubeconfig --name <cluster-name> --region <region>

# Test
kubectl get pods -n <lp-namespace>
```

---

## 1. Unit Tests

Run existing tests:

```bash
./mvnw test -pl core/lp -Dtest=AbstractInboxV2FStoreBitUpdateBatchTaskTest
./mvnw test -pl core/lp -Dtest=InboxV2FStoreEntryDTOTest
```

Tests use `@TempDir` to create isolated file queues automatically.

## 2. Local Environment

### File queue directory

The file queue directory is configured via `filequeue_dir` in LP's JSON config — no JVM args or manual setup needed.

- **`start_local.sh` users**: `launch_lp()` in `host_service_functions.sh` automatically creates `~/local-setup/lp/filequeue/` and writes its path into the generated `conf.local.json` on every start.
- **IntelliJ users**: `core/lp/conf/conf.local.json` (checked in) sets `filequeue_dir` to `/tmp/lp-filequeue`. The directory is created automatically by `PageManager.mkdirs()` on first LP startup.

### Start LP locally (host mode — recommended)

```bash
./local-setup/start_local.sh --profile extended
```

### Start LP locally (Docker mode)

```bash
cd local-setup
docker compose --profile lp up
```

### Test steps

1. Start LP
2. Send inbox events
3. Stop FStore (simulate failure)
4. Send more events (they go to file queue)
5. Restart LP
6. Start FStore
7. Verify queued messages are processed

## 3. Staging Environment

### File queue directory

In staging/prod, LP runs in a Docker container as the `logprocessor` user. The file queue path is `/home/logprocessor/data/filequeue` (set in `local-setup/configs/lp/conf.default.json`). This parent directory is pre-created by the container setup. Subdirectories for each queue (e.g. `inbox_v2_read_bt8MB/`) are created automatically by `PageManager.mkdirs()` on first LP startup — no deployment changes needed.

### Check EBS mount

```bash
kubectl exec -it <lp-pod> -- df -h /home/logprocessor/data
```

### Check file queue

```bash
kubectl exec -it <lp-pod> -- ls -la /home/logprocessor/data/filequeue/
```

### Watch queue size

```bash
kubectl exec -it <lp-pod> -- du -sh /home/logprocessor/data/filequeue/
```

### Test pod restart recovery

1. Note current file queue size
2. Kill LP pod: `kubectl delete pod <lp-pod>`
3. Wait for new pod to start
4. Verify it drains the old queue
