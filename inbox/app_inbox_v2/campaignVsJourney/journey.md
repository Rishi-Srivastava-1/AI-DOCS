# App Inbox — Journey Dispatch Flow

Journey step delivering an App Inbox message (live-triggered via SDK event or API `/1/upload`). Journey UI does **not** expose the "Device reach" knob — the target defaults to `sendToAllDevices=false`.

## Journey UI — "Deliver to" node

Fields exposed at the App Inbox node in the Journey builder:
- **OS** — iOS / Android / Both → `target.device[]`
- **Devices** — Mobile / Tablet / TV → `target.deviceTypes[]`

Fields NOT exposed:
- **Device reach (Last active vs All active)** — no UI control. `target.sendToAllDevices` is forced to `false`.

## Dispatch model — live-triggered, LP-produced

Journey App Inbox messages are produced by LP when an event triggers a Journey segment. The full pipeline is a real-time event flow (LC → LP → NB), with an optional delay if the Journey step has a sleep.

## Full pipeline — LC → dispatch

```
Client SDK / API caller
  │
  ▼
LC (Log Collector)
  ├─ POST /1/upload or SDK payload
  ├─ Auth via account id / passcode
  └─ Serialize into Redis payload:
       meta = { a, lcTs, es, k?, i?, j?, g? }
       events = [{ t, ts, eName, eb, ... }]
  └─ RPUSH to account-keyed Redis queue

LP (Log Processor) — GenericEventProcessor.consume()
  ├─ preConsume (line 2038) — identity resolution
  │    ├─ if meta has guid → guid-shard branch → returns real (i, j)
  │    ├─ else if only k → K-shard branch (line 287+)
  │    │    ├─ ShardIdentityUtil.getJ(k) → returns existing j
  │    │    ├─ line 315-316: if j > 0 && requestK.size() == 1
  │    │    │     return Info(i=0, j=<existing>, ...)  ← i stays 0
  │    │    └─ else generate new (i, j) via deriveGuidFromK
  │    └─ profile.IID = obtainedI, profile.IDENTITY = obtainedJ (line 2350-2351)
  │
  ├─ event loop → consumer chain
  │    (line 606-607: ew.event.iid = profile.IID, ew.event.idn = profile.IDENTITY)
  │
  ├─ SegmentConsumer.doConsume → handlePathSplit → JourneyHelper.updateJourney
  │    (evaluates conditional splits, updates LAST_STEP_ID on profile)
  │
  ├─ SegmentConsumer.buildSMessage (line 1105+)
  │    ├─ delay = si.tgt.getInt(SegmentsDao.DELAY, 0)   ← step delay, e.g. 120 min
  │    ├─ if delay==0: minutes = TRIGGER_EVENT_MINUTE (9999)   ← immediate release
  │    │  else: minutes = currentMinute + delay + MINUTE_BUCKET_SIZE
  │    ├─ K-shard swap (line 1123-1127):
  │    │       if evt.iid == 0 && evt.idn != 0:
  │    │           evt.iid = evt.idn
  │    └─ TriggerSegmentStreamMessageSerializer.serialize(..., evt.iid, ...)
  │       (only evt.iid — evt.idn dropped after swap)
  │
  └─ pushNBUpdate (line 1907-1919)
       └─ meta = { pn, pm } from profile
       └─ Gateway.updateNB
             └─ POST http://<nbHost>:9090/enqueue?a=<accountId>
                   body: { meta, qms: [SMessage], lc_request_timestamp }

NB /enqueue — RequestProcessor.handle (case "/enqueue")
  ├─ Deserialize SMessage → SegmentStreamMessage
  ├─ m.setNbRequestReceiveTime(currentEpoch)   ← for the initial receive
  └─ TimedQueue.getInstance().enqueue(m)

NB TimedQueue.add (line 70)
  ├─ line 80-83: SegmentStreamMessage → enqueueToQueue(m) (no journey/DND validation)
  └─ minutes=9999 → scheduledMinuteToEpoch(9999)==now → immediate release

NB TimedQueueWorker.run (line 73)
  └─ dequeue
  └─ line 94-100: if m instanceof SegmentStreamMessage
       → SegmentStreamManager.getInstance().handle((SegmentStreamMessage) m)
       → continue

NB SegmentStreamManager.handle (line 681)
  ├─ if no exit query and journeyId > 0 (line 738-773):
  │    ├─ currentStepId = profile.LAST_STEP_ID (set by LP)
  │    └─ Utils.addToTimedQueueIfQualifiedForPostActionTarget(
  │            account, journeyId, currentStepId, profile, si, batchId)
  │
  └─ Utils.addToTimedQueueIfQualifiedForPostActionTarget (line 1429+)
       ├─ iterates si.journeyImmediatePostActionTargets()
       ├─ matches by jsid == nextStep (line 1458-1459)
       └─ PostActionTargetMessageEnqueueUtility.addToTimedQueue(
              account, journeyId, targetId, profile)

NB PostActionTargetMessageEnqueueUtility.addToTimedQueue (line 32)
  ├─ line 60: int delay = ti.getDelay()      ← reads Mongo target.delay
  ├─ line 62: prn = encode(NotificationInbox, Web)   ← Web placeholder
  ├─ line 64-65: iid = profile.I, idn = profile.J
  ├─ line 83: new Message(..., prn, idn > 0 ? 0 : iid, idn, ...)
  │           ← iid ZEROED when idn > 0
  │           ← nbRequestReceiveTime NOT copied — stays 0
  ├─ line 86-89: if delay > 0: m.setScheduledForMinute(minuteOfDay + delay + 5)
  └─ TimedQueue.enqueue(m)

[wait for scheduledForMinute to elapse — up to `delay` minutes]

NB TimedQueueWorker.run — dequeues (regular Message, not SegmentStreamMessage)
  └─ routeMessageAfterTimedQueueProcessing (line 186-213)
       ├─ if info.isSendToAllDevices() → addToSlowProfileLookupQueue(m)
       │        (Journey has sendToAllDevices=false — this branch skipped)
       ├─ else if MemoryQueues.isPreferredMode(mode) → false for NotificationInbox
       └─ else routeNonPreferredModeMessage(m)
              → MainExecutor.getSlowQueue().enqueue(m)

NB SlowProfileLookupFileQueueWorker.run
  └─ fileQueue.dequeue() → processMessage
  └─ addMessageToProfileLookupBatchTask (inherited from TimedQueueWorker line 255-267)
       ├─ if m.getNbRequestReceiveTime() > 0 → liveProfileLookupBatchTask.collect
       │  (Journey path: nbRequestReceiveTime==0 — this branch skipped)
       └─ else → profileLookupBatchTask.collect

NB ProfileLookupBatchTask.task (queueworkers/ProfileLookupBatchTask.java)
  ├─ iid/idn accumulation line 148-152
  │    if message.getIid() != 0: iidList.add(iid)
  │    else if message.getIdn() != 0: idnList.add(idn)
  │    (Journey delayed: iid==0, idn set → idnList only)
  └─ Utils.getProfileFields(info, mode, profileDTO, m)
       ├─ PROFILE_TYPE from target.device[]                (Utils.java:627-632)
       ├─ if isSendToAllDevices: DEVICE_TYPE_LIST + il=1   (Utils.java:271-275)
       │  (Journey: sendToAllDevices=false — this block skipped)
       ├─ if journeyId != -1 AND nbDynamicConfig.getEnableJourneyDeviceTypeFilter():
       │       DEVICE_TYPE_LIST + ENABLE_DEVICE_TYPE_FILTERING=true
       │       (Utils.java:648-657 — Journey-specific device-type filter path)
       │       "latest": true
       └─ modeOrdinal, pm, pn, JOURNEYS, personalisation
  └─ POST http://<esHost>:8080/p/ds?a=<account>

ES ProfileHandler (line 2810+)
  ├─ ptList (from PROFILE_TYPE), dts (from DEVICE_TYPE_LIST)
  ├─ useDeviceTypeFilter = latest && dts.notEmpty
  │                     && enableDeviceTypeFiltering
  │                     && linkedListReady
  ├─ getMultiDevices = getAllDevices || isMultiApp   ← false for Journey
  │
  └─ single-install branch (line 2906+):
       returns latest-active install for the uid, matching ptList & useDeviceTypeFilter
       no `il` field on response

NB TimedQueueProfileLookupBatchTask.processMessage (line 258)
  ├─ if message.isProfileNotFoundInLookup():
  │      processProfileNotFoundInLookUp → handleError(ProfileNotReachable) → return
  ├─ line 311-312: m.setIid, m.setIdn from returned profile
  ├─ line 462-464: setSendToDevices(il) — skipped (sendToAllDevices=false)
  └─ line 483-494: re-encode PRN using pn/pm decoder
       → PRN = NotificationInbox + Device.<Android/iOS/Web from decoder>

NB dispatch → NotificationInboxDispatcher → InboxStoreManager
  └─ handleStoreMessage (line 259-274)
       └─ sendToAllDevices=false → sendStoreMessage(message)
             → single inbox record on the resolved install
```

## Batch task routing — always default, never live

**For Journey App Inbox, `liveProfileLookupBatchTask` is never used.**

Why:
- `nbRequestReceiveTime` is set on the initial `SegmentStreamMessage` received at `/enqueue`.
- `SegmentStreamManager.handle` consumes that SegmentStreamMessage — the field is gone.
- `PostActionTargetMessageEnqueueUtility.addToTimedQueue` line 83 builds a **new `Message`** for the delayed dispatch and does **not copy `nbRequestReceiveTime`** — the new message has `nbRequestReceiveTime = 0`.
- At `TimedQueueWorker.java:255-267`, routing depends on `nbRequestReceiveTime > 0` — false → default `profileLookupBatchTask`.

Both `profileLookupBatchTask` and `liveProfileLookupBatchTask` are instances of `TimedQueueProfileLookupBatchTask` — same class, same query behavior. Only difference is batch-queue isolation, which is irrelevant for the Journey path since it always lands in the default queue.

## The two filters in Journey — how they're enforced

### `device[]` (OS)

- **Journey builder** writes `target.device[]` to Mongo (e.g. `[1,2]` for Both).
- **NB `Utils.java:627-632`** — maps `info.getDevices()` → `PROFILE_TYPE` list of ordinals.
- **ES `ProfileHandler.java:2855-2862`** — reads `PROFILE_TYPE` into `ptList`, filters installs.

### `deviceTypes[]` (Mobile / Tablet / TV)

- **Journey builder** writes `target.deviceTypes[]` to Mongo.
- **NB `Utils.java:648-657`** — Journey-specific path. When `journeyId != -1` AND account has LD flag `enableJourneyDeviceTypeFilter=true`:
  ```java
  profileFields.append(DEVICE_TYPE_LIST, info.getTgtObject().get(TargetingDao.DEVICE_TYPES));
  profileFields.append(ENABLE_DEVICE_TYPE_FILTERING, true);
  ```
- **ES `ProfileHandler.java:2864-2876`** — reads `DEVICE_TYPE_LIST` into `int[] dts`. `useDeviceTypeFilter = latest && dts.notEmpty && enableDeviceTypeFiltering && linkedListReady`. Filters install list.

**Note:** the `deviceTypes` filter for Journey is gated on a **dynamic config flag** (`enableJourneyDeviceTypeFilter`). If the flag is off for the account, the filter is not sent, and ES will not filter by device-type — the message goes to whichever install matches `PROFILE_TYPE` alone.

## `sendToAllDevices` is hard-coded to `false`

- Journey UI does not expose Device reach.
- `target.sendToAllDevices = false` by default in Mongo.
- All three enforcement sites therefore go through the "single-install" code paths:
  - `Utils.java:271-275` — `DEVICE_TYPE_LIST` + `il=1` block does not fire (this block is gated on `sendToAllDevices==true`, separate from the Journey-specific path at line 648-657).
  - `ProfileHandler.java:2853` — `getMultiDevices=false` → single-install branch at line 2906+.
  - `InboxStoreManager.java:262-273` — no fan-out; single `sendStoreMessage`.

Result: **one inbox record per matching user on their latest-active install**, filtered by OS (and device-type if the LD flag is on).

## Special case: identity-only trigger (SUC-127057)

If the trigger event was pushed via API with only `identity` (no guid, no install context):

1. LP `preConsume` K-shard branch returns `Info(i=0, j=<existing>)` — no reverse `j → i` mapping.
2. `SegmentConsumer` K-shard swap makes SMessage carry `iid=idn=<identity>`.
3. `PostActionTargetMessageEnqueueUtility` line 83 zeroes iid (`idn > 0 ? 0 : iid`) — new Message has `iid=0, idn=<identity>`.
4. After delay, `ProfileLookupBatchTask` queries ES with `idnList=[<identity>]`, `iidList=[]`.
5. ES returns no matching install profile (no guid-shard entry ever bound).
6. `profileNotFoundInLookup=true` → bounce path with `Device.Web`.
7. `TargetingEventStoreHelper.mapDeviceToDownload(Web, NotificationInbox)` throws — no `Web` case for NotificationInbox in the mapping.

The three UI filters (OS, device-type, and — if it existed — device-reach) are all downstream of the missing install profile. None of them can save this case.

## Payload sent to ES `/p/ds`

`ProfileClientFields` at `ProfileLookupBatchTask.java:241-243`:

```java
new ESProfileFetcher.ProfileClientFields(
    esIP, account,
    iidList,           // per-message iid (may be empty if only idn present)
    idnList,           // per-message idn
    null,              // iidIdnMap
    profileFields);    // BasicDBObject
```

Contents of `profileFields`:

| Field | Value | Set when |
|---|---|---|
| `PROFILE_TYPE` | `[Android.ord, iOS.ord]` from `target.device[]` | always |
| `DEVICE_TYPE_LIST` | `[1,2,3]` from `target.deviceTypes[]` | Journey + LD flag on |
| `ENABLE_DEVICE_TYPE_FILTERING` | `true` | Journey + LD flag on |
| `I_LIST` ("il") | — | never for Journey (sendToAllDevices=false) |
| `latest` | `true` | always (Journey path) |
| `JOURNEYS` | required journey ids for lookup | always for Journey |
| `modeOrdinal` | `NotificationInbox.ord` | always |
| `pn`, `pm` | `1` | always |
| `SEGMENTS_ALIASES`, personalisation | mode-specific | always |

Identifier per message: **either iid or idn, not both** (`ProfileLookupBatchTask.java:148-152`, if-else, prefers iid).

## Key differences from Campaign flow

- **Live-triggered**, not batch-scheduled. LP is the producer, driven by user events.
- **Goes through `SegmentStreamMessage`** → `SegmentStreamManager` → `PostActionTargetMessageEnqueueUtility` — an extra hop compared to campaigns.
- **`nbRequestReceiveTime` is lost** on the SegmentStream→PostAction rebuild; delayed messages always route to default batch task.
- **No "Device reach" knob** — `sendToAllDevices=false` hard-coded. No fan-out.
- **Device-type filter is LD-flag gated** — differs from campaigns which apply it whenever `sendToAllDevices=true`.
- **Journey-specific fields** are added to the ES request: `JOURNEYS`, `latest=true`, etc. (Utils.java:648-657).
- **Vulnerable to the K-shard identity-only issue** because Journey entry events can be API-triggered with only an identity — see SUC-127057 for the failure mode.

## Slow-queue distinction — `slowQueue` vs `slowProfileLookupFileQueue`

Both are disk-backed `FileQueue` instances but with different worker pools, back-pressure behavior, and routing conditions. Journey App Inbox always uses `slowQueue` because `sendToAllDevices` is hard-coded to `false`.

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

The `sendToAllDevices` check comes first and short-circuits the preferred-mode branch — but Journey targets always have `sendToAllDevices=false` (UI hides the knob), so this branch never fires for Journey App Inbox.

### Which queue Journey App Inbox uses

**Always `slowQueue`** — sometimes `triggerMessageSlowQueue` when the LD flag is on and this is a first-live-attempt.

Why:
- Journey App Inbox target has `sendToAllDevices=false` (hard-coded).
- App Inbox is a non-preferred mode (`MemoryQueues.isPreferredMode = mode == Push || mode == Browser` — NotificationInbox is neither).
- So `routeMessageAfterTimedQueueProcessing` falls through to `routeNonPreferredModeMessage`.

`routeNonPreferredModeMessage` (`TimedQueueWorker.java:227-237`):
```java
boolean rolloutFlag = nbDynamicConfig.getRolloutTriggerMessageSlowQueue()
        .getValue(Integer.parseInt(m.getAccountID()));
boolean firstLiveAttempt = m.isFirstLiveAttempt();

if (rolloutFlag && firstLiveAttempt) {
    addToTriggerMessageSlowQueue(m);    // → triggerMessageSlowQueue
} else {
    addToSlowFileQueue(m);              // → slowQueue
}
```

`isFirstLiveAttempt` is set once at `TimedQueue.add` when `scheduledForMinute` is still `TRIGGER_EVENT_MINUTE (9999)`. But for the Journey delayed path, the message going through `PostActionTargetMessageEnqueueUtility.addToTimedQueue` sets `scheduledForMinute = minuteOfDay + delay + 5` at line 86-89, so the marker is not 9999 when it enters `TimedQueue.add` the second time — `isFirstLiveAttempt=false`. So Journey App Inbox delayed dispatch normally routes to **`slowQueue`**, not `triggerMessageSlowQueue`.

### Why the queues are separate — real behavioral differences

1. **Back-pressure isolation.** `slowQueue` throttles itself when re-enqueues pile up (1s sleep gate at `AbstractFileQueueMessageWorker.java:56-61`). `slowProfileLookupFileQueue` does not — it drains at full speed. This matters because `sendToAllDevices=true` traffic (which routes to `slowProfileLookupFileQueue`) has heavier per-message ES lookups (multi-install fan-out) and needs its own throughput budget.
2. **Worker pool sizing.** Ops scale `noOfSlowFileQueueWorker` and `noOfSlowProfileQueueWorker` independently. A `sendToAllDevices=true` campaign burst shouldn't starve general non-preferred traffic (like Journey App Inbox).
3. **On-disk isolation.** Failures/corruption in one directory don't contaminate the other. Independent replay.
4. **Metric attribution.** Different worker classes, different metric prefixes — dashboards can attribute latency/error rates to the right subsystem.

### The third neighbor — `triggerMessageSlowQueue`

Not used by Journey App Inbox for the delayed path (SUC-127057). Would be used only for a Journey App Inbox message at its very first live attempt with the LD flag on — but the delay logic strips `isFirstLiveAttempt` before the second TimedQueue add, so in practice Journey delayed App Inbox uses `slowQueue` alone.

### After dequeue — identical processing

Both `SlowFileQueueWorker` and `SlowProfileLookupFileQueueWorker` extend `TimedQueueWorker` and inherit the same `addMessageToProfileLookupBatchTask` method. Once a message is off either queue, the routing to `profileLookupBatchTask` / `liveProfileLookupBatchTask` and the ES `/p/ds` call are behaviorally identical. Only the pre-dequeue queueing + throttling logic differs.

## Summary

Journey App Inbox = **LP-triggered live dispatch** that goes through `SegmentStreamMessage` → `SegmentStreamManager` → `PostActionTargetMessageEnqueueUtility` (with optional delay) → TimedQueue → **`slowQueue`** → default `ProfileLookupBatchTask` → ES per-user profile fetch → `InboxStoreManager` single-install storage. OS filter enforced always; device-type filter enforced only when LD flag is on; Device-reach knob does not exist (always `sendToAllDevices=false`, no fan-out).
