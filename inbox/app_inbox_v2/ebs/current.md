# EBS Usage in Ultron — Events Backup Feature

## The EBS Volume

On the LP EC2 instance, a dedicated 29G EBS volume is attached and mounted at:

```
/dev/nvme2n1  →  mounted at  →  /var/lib/logprocessor
```

This is a network-attached SSD (EBS over NVMe) separate from the root OS disk (`/dev/nvme0n1p1`, 30G).
Isolating it means LP disk I/O cannot fill up the root disk and crash the OS.

---

## What Is the Events-Backup Feature?

A parallel, async pipeline in LP that captures a copy of every event processed through
REALTIME and RENDERED queues and ships it to a durable store (S3 or local disk).

**Purpose:** Event backup / replay — if LP loses events due to a crash or Redis queue loss,
the backup copy can be replayed.

**Default state:** Disabled. Enabled via LaunchDarkly key `events-backup-config`.

---

## How the Feature Works

### Entry Point

LP starts and stops the feature via `MainRunner`:

```
MainRunner.run()             → eventsBackupTasksManager.startEventBackupTasks()
MainRunner.addShutDownHook() → eventsBackupTasksManager.shutdownEventBackupTasks()
```

### Which Events Are Backed Up?

Inside `GenericEventProcessor.consume()`, every LP event is checked:

- Queue type must be `REALTIME` or `RENDERED` (other queues are excluded)
- Must not be a pushback/retry event (to avoid duplication)
- `shouldBackupEvents` must be `true` in LaunchDarkly config

### Data Flow

```
LP Worker Thread
    ↓
GenericEventProcessor.consume()
    ↓
EventDataBufferWriter.recordEvent(redisEntry)
    ↓  [Non-blocking push — LP thread does not wait]
EventsBuffer — LinkedBlockingQueue (capacity: 100,000 events)
    ↓  [If full → event is DROPPED, reported via Profiler]
    ↓
[Async Worker Threads — default 2]
EventDataBufferPoller.pollBatch()
    → batches up to 5,000 events, waits max 2,000ms to fill a batch
    ↓
EventWriter.write(batch)
    → S3 (current) or Disk (configurable)
```

---

## How Is the EBS Disk Used Here?

### Current Setup — S3 Writer (EBS NOT actively used)

LP is hardcoded to use the S3 writer in `LpGuiceModule.java`:

```java
install(new EventsBackupModule(EventsBackupWriterType.S3));
```

With S3 as the writer, events are:
1. Buffered in-memory (ByteBuffer, 100MB)
2. Gzip compressed
3. Uploaded to S3 via multipart upload (up to 10 parallel threads)
4. Session rotates every 1GB of data

**The EBS disk at `/var/lib/logprocessor` is NOT written to in this mode.**
This explains why the disk shows only 24K used despite LP being active.

### Alternate Setup — Disk Writer (EBS actively used)

If the writer type were switched to `EventsBackupWriterType.DISK`, events would be written
to the EBS volume via a SLF4J rolling file appender (`CustomLogger`):

```
/var/lib/logprocessor/events/<machine-ip>/events.log
/var/lib/logprocessor/events/<machine-ip>/events-2024-01-15-14-0.log.lz4  ← rolled files
```

---

## LogConfig — The Disk Write Settings

Defined in `core/events-backup/src/main/java/com/wizrocket/eventsbackup/config/LogConfig.java`:

```java
private String logFileBasePath = "/var/lib/logprocessor/events";
private String maxFileSize     = "500MB";   // roll file after this size
private int    maxHistory      = 30;        // max number of rolled files to retain
private String totalSizeCap    = "25GB";    // soft cap on total disk usage
private String writeBufferSize = "50MB";    // in-memory SLF4J buffer before flush
```

**Important:** These are application-level limits enforced by Logback — not OS or EBS limits.
The EBS disk hard limit is 29G. The `totalSizeCap` of 25GB leaves only ~4GB headroom.
If Logback cleanup fails for any reason, the disk will fill up and LP can crash.

These settings are only active when `EventsBackupWriterType.DISK` is used.
In the current S3 setup, they are configured but have no effect.

---

## Local Environment Behavior

The feature is completely inert locally — no EBS volume, no S3 credentials, no `entrypoint.sh`
or `conf.local.json` changes are needed.

### Why It Doesn't Break

The kill switch is checked at the very first line of `EventDataBufferWriter.recordEvent()`:

```java
if (!eventsBackupDynamicConfigProvider.getConfig().isShouldBackupEvents() || rawEvent == null) {
    return;  // exits immediately — nothing written anywhere
}
```

`shouldBackupEvents` defaults to `false` in `EventsBackupConfig`:

```java
private boolean shouldBackupEvents = false;
```

### What Happens Locally Step by Step

```
LP starts locally
    ↓
EventsBackupDynamicConfigProvider.fetchConfig()
    ↓
LPDynamicConfig has no "events-backup-config" key set in local LD
    ↓
configStr is null/empty → falls back to EventsBackupConfig.DEFAULT
    ↓
DEFAULT has shouldBackupEvents = false
    ↓
Every recordEvent() call hits the if-check and returns immediately
    ↓
Nothing written to disk, nothing sent to S3
```

The feature only activates when LaunchDarkly explicitly pushes a config with
`shouldBackupEvents: true`. Since local LD either isn't connected or doesn't have
this key configured, the entire pipeline is a no-op — the `/var/lib/logprocessor`
directory doesn't even need to exist.

---

## Summary

| Aspect | Detail |
|---|---|
| EBS mount | `/dev/nvme2n1` → `/var/lib/logprocessor` (29G) |
| Current writer | S3 (EBS not used) |
| EBS used when | Writer type switched to `DISK` |
| Disk write path | `/var/lib/logprocessor/events/<ip>/events.log` |
| Soft disk cap | 25GB (via Logback `totalSizeCap`) |
| Hard disk cap | 29G (EBS volume size) |
| Feature kill switch | `shouldBackupEvents` in LaunchDarkly `events-backup-config` |
| Queues backed up | `REALTIME`, `RENDERED` only |
| Currently on disk | 24K (near-empty, S3 mode active) |
