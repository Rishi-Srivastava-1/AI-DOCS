# App Inbox — Campaign Dispatch Flow

Standalone App Inbox campaign (not a Journey step). Includes the campaign-only knob **Device reach** (Last active vs All active) — which Journeys don't expose.

## Campaign UI — "Who" section

Fields exposed:
- **Target Segment** — segment / user filter rules
- **Operating Systems** — iOS / Android / Both → `target.device[]`
- **Devices** — Mobile / Tablet / TV → `target.deviceTypes[]`
- **Device reach** (unique to campaigns):
  - **Last active device** → `target.sendToAllDevices = false`
  - **All active devices** → `target.sendToAllDevices = true`

## Dispatch model — PBS/scheduled, not live-triggered

A Campaign is a batch job. Two sub-flavours:

| Flavour | Producer | Path |
|---|---|---|
| **PBS (Pre-Built Segment) / scheduled** | `PBSCampaignSpawningService` at NB | ES download file → NB spawns messages internally, **no `/enqueue`** |
| **Live segment refresh (query-driven campaign)** | `TargetWatcher` at NB | ES query → NB spawns messages internally |

Either way, **NB is the producer** — the message never enters via HTTP `/enqueue`, so:
- `nbRequestReceiveTime` is `0` on the produced messages
- The message goes directly to `TimedQueue` (or a dedicated batch queue)
- Downstream at `SlowProfileLookupFileQueueWorker` → `addMessageToProfileLookupBatchTask` (`TimedQueueWorker.java:255-267`), the routing goes to `profileLookupBatchTask` (default), not `liveProfileLookupBatchTask`

## Full pipeline — LC → dispatch

Campaign flow does not involve LC at all for the trigger. The event pipeline (LC → LP → ES) is used only to populate the ES download file / satisfy segment queries that the campaign is based on. The actual dispatch is initiated by NB on a schedule.

```
[Event ingest side — populates ES]
Client SDKs → LC → Redis → LP → ES  (routine ingest, ongoing)
                                 └─ segment membership computed by ES
                                    (KShardData, PBS files, segment aliases)

[Campaign dispatch side — NB-driven]
NB PBSCampaignSpawningService / TargetWatcher (scheduled tick)
  │
  ├─ CampaignEventStoreQueryCreator.buildQuery(targetingInfo)
  │      writes target.device[], target.deviceTypes[], sendToAllDevices
  │      into ES query
  │
  └─ POST http://<esHost>:8080/download?a=<account> (or /q depending on path)
        └─ ES QueryParser.prepareQueries
             ├─ QueryParser.java:3359: query.setSendToAllDevices(qd.getBoolean(...))
             ├─ Query.deviceTypes (== OS filter)
             └─ Query.deviceTypeList (== Mobile/Tablet/TV filter)
        └─ ES writes filtered install list into the download file

NB reads download file → spawns Messages per matching install
  │
  ├─ each Message: iid=<install>, idn=<identity>, prn=encoded, mode=NotificationInbox
  ├─ nbRequestReceiveTime = 0 (NB internal spawn, no HTTP)
  └─ TimedQueue.enqueue(m)

NB TimedQueueWorker.run
  └─ dequeues Message
  └─ routeMessageAfterTimedQueueProcessing (TimedQueueWorker.java:186-213)
       ├─ if info.isSendToAllDevices() → addToSlowProfileLookupQueue(m)
       │        → SlowProfileLookupFileQueueWorker
       │        → nbRequestReceiveTime==0 → profileLookupBatchTask.collect
       │
       └─ else if MemoryQueues.isPreferredMode(mode) → false for NotificationInbox
             else routeNonPreferredModeMessage(m)
                  → MainExecutor.getSlowQueue().enqueue(m)
                  → SlowProfileLookupFileQueueWorker
                  → nbRequestReceiveTime==0 → profileLookupBatchTask.collect

NB ProfileLookupBatchTask.task (queueworkers/ProfileLookupBatchTask.java)
  ├─ iid/idn accumulation line 148-152 (if iid > 0: iidList else idnList)
  └─ Utils.getProfileFields(info, mode, profileDTO, m)
       ├─ PROFILE_TYPE ← from target.device[]           (OS filter)
       ├─ if sendToAllDevices: DEVICE_TYPE_LIST + il=1  (device-type filter + install list)
       │  else: neither
       └─ if journeyId != -1: additional DEVICE_TYPE_LIST + ENABLE_DEVICE_TYPE_FILTERING
          (does NOT apply — campaigns have journeyId == -1)
  └─ POST http://<esHost>:8080/p/ds?a=<account>
        payload: { iids, idns, fields: { PROFILE_TYPE, DEVICE_TYPE_LIST?, il?, latest, ... } }

ES ProfileHandler (line 2810+)
  ├─ readListOrInt(fields, PROFILE_TYPE) → ptList         (OS filter)
  ├─ readListOrInt(fields, DEVICE_TYPE_LIST) → dts        (device-type filter)
  ├─ getAllDevices = fields.getBoolean(I_LIST, false)
  ├─ getMultiDevices = getAllDevices || isMultiApp
  │
  ├─ if getMultiDevices (Case A: sendToAllDevices=true):
  │     aggregateAllIidsForUidMapOptimized(account, uid, ptList, dts, ...)
  │     → returns profile with `il` populated (filtered install list)
  │
  └─ else (Case B: sendToAllDevices=false):
        single-profile branch (line 2906+)
        → returns latest-active install matching ptList
        → no `il` field

NB TimedQueueProfileLookupBatchTask.processMessage (line 258)
  ├─ m.setIid/setIdn from returned profile (line 311-312)
  ├─ if isSendToAllDevices && il != null → m.setSendToDevices(il)    (line 462-464)
  └─ re-encode PRN using pn/pm decoder (line 483-494)

NB dispatch → NotificationInboxDispatcher → InboxStoreManager
  └─ handleStoreMessage (line 259-274)
       ├─ if sendToAllDevices && deviceInfoList != null:
       │      for each install in deviceInfoList:
       │          utils.modifyMessage(msg, deviceInfo) → per-install iid/prn
       │          sendStoreMessage(msg)                → one inbox record per install
       │
       └─ else:
              sendStoreMessage(message)              → single inbox record on current install
```

## Batch task routing

**Always `profileLookupBatchTask` (default)** for campaign dispatches. `liveProfileLookupBatchTask` requires `nbRequestReceiveTime > 0`, and campaigns are NB-spawned — no HTTP receive.

## `sendToAllDevices` — the campaign-only knob

The two UI options map to two behaviorally distinct flows:

### `Last active device` — `sendToAllDevices = false`

- NB **does not request** `il` or `DEVICE_TYPE_LIST` from ES (`Utils.java:271-275` — the append block is gated on `isSendToAllDevices()`).
- ES's `getMultiDevices` is false → single-install branch at `ProfileHandler.java:2906+` → returns the profile's latest-active install matching `PROFILE_TYPE`.
- NB gets a single-profile response, `il` is null.
- `TimedQueueProfileLookupBatchTask.java:462-464` — the `setSendToDevices(il)` copy is gated on both `isSendToAllDevices()` AND `il != null`; both false → not attached.
- `InboxStoreManager.handleStoreMessage` — the fan-out loop at line 262-273 requires `sendToAllDevices==true` (or multi-app-multi-select). Falls to `sendStoreMessage(message)` — a single inbox record for the single install ES resolved.
- **One inbox record per matching user, on their latest-active install only.**

### `All active devices` — `sendToAllDevices = true`

- NB appends `DEVICE_TYPE_LIST` (`target.deviceTypes[]`) and `il=1` to profileFields (`Utils.java:271-275`).
- ES `getMultiDevices=true` → multi-install branch at `ProfileHandler.java:2882+` → `aggregateAllIidsForUidMapOptimized(account, uid, ptList, dts, ...)` filters installs by both `ptList` (OS) and `dts` (device types) and returns them as `il` on the profile.
- NB reads `il` at `TimedQueueProfileLookupBatchTask.java:461-464`, attaches to message via `m.setSendToDevices(il)`.
- `InboxStoreManager.handleStoreMessage` — line 262-273 iterates `deviceInfoList` (== `il`), creates one `InboxStoreMessage` per install, calls `utils.modifyMessage(msg, deviceInfo)` to set per-install `iid` and re-encode PRN from that install's `pn`.
- **One inbox record per install per user, filtered by OS + device-type.**

## Where each filter is enforced

| Filter | LP | NB (request build) | ES (fetch) | NB (post-lookup) | NB (fan-out) |
|---|---|---|---|---|---|
| `device[]` (OS) | — | `PROFILE_TYPE` mapped from `info.getDevices()` (Utils.java:627-632) | filters installs by `ptList` (ProfileHandler.java:2855-2862) | — | — |
| `deviceTypes[]` | — | `DEVICE_TYPE_LIST` — only when `sendToAllDevices=true` (Utils.java:271-275) | filters by `dts` (ProfileHandler.java:2864-2876) | — | — |
| `sendToAllDevices` | — | `il=1` + `DEVICE_TYPE_LIST` when true | multi-install branch when true | copies `il` onto message | fans out per install when true |

**LP is not involved in campaign filtering** — LP only participates in event ingest that feeds ES with segment membership. Campaign dispatch is initiated by NB directly against ES.

## Key differences from Journey flow

- **No `SegmentStreamMessage` path** — campaigns don't go through `SegmentStreamManager` or `PostActionTargetMessageEnqueueUtility`.
- **No delay handling on NB side** — campaign `startTime` is honored by the spawning scheduler, not by TimedQueue delay logic.
- **Batch task is always default** (`profileLookupBatchTask`) — never `liveProfileLookupBatchTask`.
- **`sendToAllDevices` is user-facing** — appears explicitly in the "Device reach" UI knob.
- **`enableJourneyDeviceTypeFilter` LD flag path (Utils.java:648-657) does NOT fire** — `journeyId == -1` for campaigns. Device-type filtering only kicks in via the `sendToAllDevices=true` code path (Utils.java:271-275).

## Payload sent to ES `/p/ds`

`ProfileClientFields` at `ProfileLookupBatchTask.java:241-243`:

```java
new ESProfileFetcher.ProfileClientFields(
    esIP, account,
    iidList,           // per-message iid (from download file) or empty
    idnList,           // per-message idn or empty
    null,              // iidIdnMap
    profileFields);    // BasicDBObject
```

Contents of `profileFields`:

| Field | Value | Set when |
|---|---|---|
| `PROFILE_TYPE` | `[Android.ord, iOS.ord]` from `target.device[]` | always |
| `DEVICE_TYPE_LIST` | `[1,2,3]` from `target.deviceTypes[]` | `sendToAllDevices=true` |
| `I_LIST` ("il") | `1` | `sendToAllDevices=true` |
| `latest` | `true` | always (Journey/live path) — for campaigns depends on scenario |
| `modeOrdinal` | `NotificationInbox.ord` | always |
| `pn`, `pm` | `1` | always |
| `PROFILE_BY_FIELD_INDEX` | required | always |
| `SEGMENTS_ALIASES`, `EMAIL/PHONE`, etc. | mode-specific additions | always |

Identifier per message: **either `iid` or `idn`, not both** (`ProfileLookupBatchTask.java:148-152`, if-else, prefers iid).

## Slow-queue distinction — `slowQueue` vs `slowProfileLookupFileQueue`

Both are disk-backed `FileQueue` instances but with different worker pools, back-pressure behavior, and routing conditions. Campaign App Inbox uses one or the other based on `sendToAllDevices`.

| Aspect | `slowQueue` | `slowProfileLookupFileQueue` |
|---|---|---|
| Constant | `"slow_queue"` | `"slow_profile_lookup_queue"` (`Constants.java:50`) |
| Accessor | `MainExecutor.getSlowQueue()` | `MainExecutor.getSlowProfileLookupFileQueue()` |
| Worker | `SlowFileQueueWorker` (extends `AbstractFileQueueMessageWorker`) | `SlowProfileLookupFileQueueWorker` (extends `TimedQueueWorker` directly) |
| Worker pool size | `noOfSlowFileQueueWorker` | `noOfSlowProfileQueueWorker` |
| Disk path | `<persistentQueuesPath>/slow_queue` | `<persistentQueuesPath>/slow_profile_lookup_queue` |
| Batch task name | `"slow-file-queue-profile-lookup"` | `"slow-queue-profile-lookup"` |
| Back-pressure check | `queue.reEnqueueCountExceeded()` → sleep 1s (AbstractFileQueueMessageWorker line 56-61) | none — tight dequeue loop |

### Routing decision — `TimedQueueWorker.java:186-213`

```java
if (info.isSendToAllDevices()) {
    addToSlowProfileLookupQueue(m);              // → slowProfileLookupFileQueue
} else if (MemoryQueues.isPreferredMode(m.getMode())) {
    addMessageToProfileLookupBatchTask(m, info); // immediate, in-memory
} else {
    routeNonPreferredModeMessage(m);             // → slowQueue (or triggerMessageSlowQueue)
}
```

The `sendToAllDevices` check comes first and short-circuits the preferred-mode branch — even a Push campaign (a preferred mode) routes to `slowProfileLookupFileQueue` when `sendToAllDevices=true`.

### Which queue Campaign App Inbox uses

| UI setting | Path | Queue |
|---|---|---|
| `Last active device` (`sendToAllDevices=false`) | routeNonPreferredModeMessage → `addToSlowFileQueue` | **`slowQueue`** |
| `All active devices` (`sendToAllDevices=true`) | `addToSlowProfileLookupQueue` | **`slowProfileLookupFileQueue`** |

App Inbox is a non-preferred mode (`MemoryQueues.isPreferredMode = mode == Push || mode == Browser`), so `Last active` always falls through to `slowQueue`. `All active` bypasses the mode check entirely.

### Why they're separate — real behavioral differences

1. **Back-pressure isolation.** `slowQueue` throttles itself when re-enqueues pile up (1s sleep gate). `slowProfileLookupFileQueue` does not — it drains at full speed. This matters because `sendToAllDevices=true` traffic has heavier per-message ES lookups (multi-install fan-out) and needs its own throughput budget.
2. **Worker pool sizing.** Ops scale `noOfSlowFileQueueWorker` and `noOfSlowProfileQueueWorker` independently. A `sendToAllDevices=true` campaign burst shouldn't starve general non-preferred traffic.
3. **On-disk isolation.** Failures/corruption in one directory don't contaminate the other. Independent replay.
4. **Metric attribution.** Different worker classes, different metric prefixes — dashboards can attribute latency/error rates to the right subsystem.

### After dequeue — identical processing

Both workers extend `TimedQueueWorker` and inherit the same `addMessageToProfileLookupBatchTask` method. Once a message is off either queue, the routing to `profileLookupBatchTask` / `liveProfileLookupBatchTask` and the ES `/p/ds` call are behaviorally identical. Only the pre-dequeue queueing + throttling logic differs.

There's also a third file queue in the same neighborhood — `triggerMessageSlowQueue` (worker: `TriggerMessageSlowQueueWorker`) — used by `routeNonPreferredModeMessage` when the LD flag `rolloutTriggerMessageSlowQueue` is on AND `m.isFirstLiveAttempt()`. It's a specialization of `slowQueue` to isolate first-time live triggers from retry/re-enqueue traffic. Campaigns (NB-spawned, not HTTP-received) do not set `isFirstLiveAttempt` — so they never route to this queue.

## Summary

Campaign App Inbox = **NB-driven batch dispatch** that reads a pre-computed download file, spawns Messages, and queries ES to enrich each message with install info. The three filters (OS, device-types, device-reach) are enforced entirely on the ES side, driven by the request that NB assembles from the target metadata. LP is not involved in the dispatch path.
