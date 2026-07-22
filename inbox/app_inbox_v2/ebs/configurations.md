# App Inbox V2 — File Queue & EBS Configuration

## 1. File Queue Configuration

### Local (host mode)

Configured via `host_service_functions.sh`. The filequeue directory is created before LP starts and passed through the generated JSON config.

- **Path**: `$HOME/local-setup/lp/filequeue`
- **How set**: `host_service_functions.sh` generates `conf.local.json` with `"filequeue_dir": "${filequeue_dir}"` and calls `mkdir -p` before LP starts
- **Override**: Pass `-DFILE_QUEUE_DIR=<path>` as a JVM arg to use a different path
- **Directory structure on disk**:
  ```
  ~/local-setup/lp/filequeue/
  └── inboxv2/
      ├── inbox_v2_delete_bt8MB/
      ├── inbox_v2_delivered_bt8MB/
      └── inbox_v2_read_bt8MB/
  ```

### Local (manual LP start — IDE or command line)

When running LP directly without `start_local.sh`, the setup scripts don't run so the filequeue directory is not pre-created and no config is generated. Two options:

**Option 1 — JVM arg (quickest)**

Pass `-DFILE_QUEUE_DIR=<path>` in the JVM args (e.g., in your IDE run configuration):

```
-DFILE_QUEUE_DIR=/tmp/lp/filequeue
```

`MainRunner` picks this up via `System.getProperty("FILE_QUEUE_DIR")` and `createPath()` will create the directory automatically. No manual `mkdir` needed.

**Option 2 — JSON config**

Add `filequeue_dir` to your local config JSON (the file passed via `-conf <path>`):

```json
{
  "filequeue_dir": "/tmp/lp/filequeue",
  ...
}
```

`MainRunner` reads this and `createPath()` creates the directory on startup.

---

### Staging / Prod (container mode)

Configured via `docker-compose.yml` and `entrypoint.sh`.

- **Env var**: `FILE_QUEUE_DIR=/home/logprocessor/data/filequeue` (set in `docker-compose.yml`)
- **How set**: `entrypoint.sh` reads `${FILE_QUEUE_DIR}`, creates the directory if absent, bakes it into the generated config JSON as `"filequeue_dir"`, and passes it as `-DFILE_QUEUE_DIR` JVM arg
- **Override**: Set a different value for `FILE_QUEUE_DIR` in the deployment environment (CloudFormation values)
- **Resolution order in Java** (`MainRunner.java`):
  1. `-DFILE_QUEUE_DIR` JVM system property (highest priority)
  2. `filequeue_dir` key in generated JSON config
  3. Hardcoded default: `/home/logprocessor/data/filequeue`

---

## 2. EBS Volume Configuration

### Local (host mode)

No EBS involved. The filequeue directory lives on the developer's local disk under `$HOME/local-setup/lp/filequeue`, which is a standard host directory. Data persists across LP restarts naturally.

### Local (container mode)

`docker-compose.nb.yaml` (local setup) bind-mounts the host directory into the container:

```yaml
volumes:
  - ${ROOT_DATA_DIR}/lp/filequeue:/home/logprocessor/data/filequeue
```

Data persists on the host across container restarts.

### Staging / Prod

LP's `docker-compose.yml` bind-mounts a persistent EBS volume into the container:

```yaml
volumes:
  - /media/xvdl/lp_data:/home/logprocessor/data
```

#### Infra team actions required

**a) Provision a new EBS volume**

| Property | Value |
|---|---|
| Type | gp3 (recommended) |
| Size | 20–50 GB to start (queue files are LZ4-compressed 8 MB pages) |
| AZ | Must match LP's EC2 instance AZ |
| Deletion policy | **Retain** — must survive instance termination |

**b) Attach and mount on the EC2 host**

Add to CloudFormation UserData / EC2 launch script:

```bash
# Format on first use only
if [ "$(file -s /dev/xvdl)" = "/dev/xvdl: data" ]; then
    mkfs -t ext4 /dev/xvdl
fi

mkdir -p /media/xvdl/lp_data
mount /dev/xvdl /media/xvdl
chown logprocessor:logprocessor /media/xvdl/lp_data

# Persist mount across reboots
echo "/dev/xvdl /media/xvdl ext4 defaults,nofail 0 2" >> /etc/fstab
```

`nofail` ensures the instance boots even if the volume temporarily fails to mount.

**c) Configure reattachment on instance replacement**

Use `AWS::EC2::VolumeAttachment` with `DeletionPolicy: Retain` in CloudFormation so the EBS is detached from the old LP instance and reattached to the new one at the same device path (`/dev/xvdl`). This is the same pattern already used for NB's `xvdn` volume.

**d) Set `FILE_QUEUE_DIR` in deployment values**

In `values.json` (or equivalent CloudFormation config):

```json
"FILE_QUEUE_DIR": "/home/logprocessor/data/filequeue"
```

#### How instance replacement works with EBS

```
Old LP instance goes down
    → CloudFormation detaches EBS (/dev/xvdl) — data intact
    → New EC2 instance starts
    → CloudFormation reattaches EBS at /dev/xvdl
    → Host mounts /dev/xvdl → /media/xvdl/
    → Container starts, bind-mounts /media/xvdl/lp_data → /home/logprocessor/data
    → LP boots, PersistentQueue reads existing meta/qfidx files
    → File queues resume from where they left off
```

#### Reference: NB equivalent

NB uses the same pattern with device `xvdn`:

```yaml
# NB docker-compose.yml
volumes:
  - /media/xvdn/nb_data:/home/nb/data
```

LP's setup mirrors this exactly, using `xvdl` for LP data.
