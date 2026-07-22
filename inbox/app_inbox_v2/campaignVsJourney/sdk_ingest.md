# SDK Ingest — App Inbox Happy Path

How OS / device-type / send-to-last-active-vs-all-devices filters actually work when the trigger event comes from an SDK (guid present). This is the counter-case to SUC-127057's identity-only failure — the flow where every filter has real install data to enforce against.

Companion to [campaign.md](./campaign.md) and [journey.md](./journey.md).

## Key difference vs identity-only path

For SDK events, `requestGuid` is non-blank. That steers LP → ES identity resolution into the **guid-shard branch** at `ShardIdentityUtil.getInfo` line 187, which deterministically returns `i > 0` (the real install id). The K-shard swap at `SegmentConsumer.java:1123-1127` never fires. Every downstream stage operates on real install-shard data.

Contrast with SUC-127057 (identity-only `/1/upload`) where ES returns `i=0` and the K-shard swap forces `evt.iid = evt.idn = <identity>`, propagating an identity value as if it were an install id all the way to NB dispatch.

## Full pipeline

### 1. SDK → LC

CleverTap SDK on the device fires an event. Payload includes:
```json
{
  "d": [{
    "g": "abcd1234-guid-of-this-device",   // required — guid = CleverTap ID
    "type": "event",
    "evtName": "TestEvent2",
    "evtData": {"data": 2},
    "ts": ...,
    "s": ...,
    "af": { ... }                          // app fields (OS, version, model, ...)
  }]
}
```

LC packages into Redis:
```
meta = { a: <account>, lcTs: ..., es: 1, g: "abcd1234...", k: [<identity aliases if any>] }
events = [{ t: 2, ts: ..., eName: "TestEvent2", eb: {data: 2} }]
```

### 2. LP `GenericEventProcessor.preConsume` — guid-shard resolution

At line 2109-2110:
```java
final String requestGuid = meta.getString("g");     // "abcd1234..."
final BasicDBList requestK = meta.getOrDefault("k", new BasicDBList());
```

At line 2251-2266, `ShardIdentityUtil.getInfo(0, 0, requestK, "abcd1234...", ...)` enters the **guid-shard branch** at line 187 because `requestGuid` is non-blank:

```java
if (StringUtils.isNotBlank(requestGuid)) {
    if (i == 0 || j == 0) {
        final long[] out = getOrGenIJ(requestGuid, j, hosts, false);
        i = out[0];   // ← real install id, e.g. 3335493
        j = out[1];   // ← identity (may be 0 for anonymous user)
    }
    if (i == 0) throw new EventStoreClientException("Unexpected condition - i == 0", 0);
    ...
    return new Info(i > 0, j >= 0, ...);
}
```

**Result: `i > 0` deterministically.** The install shard stores `guid → i`; for a known device it returns the existing i, for a first-ever event it generates a fresh i.

LP writes at line 2350-2351: `profile.IID = i`, `profile.IDENTITY = j`. Later at line 606-607: `ew.event.iid = i > 0`, `ew.event.idn = j`.

At `SegmentConsumer.buildSMessage` line 1123-1127, the K-shard swap **does not fire** (`evt.iid > 0`). SMessage carries the real install iid.

### 3. NB `/enqueue` → SegmentStreamManager → PostActionTargetMessageEnqueueUtility

For a Journey path, the message enters as `SegmentStreamMessage`. `SegmentStreamManager.handle` consumes it, then `PostActionTargetMessageEnqueueUtility.addToTimedQueue` line 83 builds a new Message:

```java
new Message(..., prn, idn > 0 ? 0 : iid, idn, ...)
```

**iid is zeroed when idn > 0** — this happens regardless of SDK vs identity origin. The delayed Message has `iid=0, idn=<real j>`. Unlike the SUC-127057 case, `idn` here maps to a real user who has real installs at the ES side. NB will look up the profile by `idn` at ProfileLookupBatchTask and ES will find the install-shard entries.

For non-Journey campaigns, the Message is not re-built through `PostActionTargetMessageEnqueueUtility`. It keeps its real iid straight through to `ProfileLookupBatchTask`.

### 4. TimedQueue → slow queue → ProfileLookupBatchTask → ES `/p/ds`

`Utils.getProfileFields(info, mode, profileDTO, m)` assembles the request. The three filter fields:

| Field | Populated when | Source |
|---|---|---|
| `PROFILE_TYPE` | always | `info.getDevices()` — target's OS list (device[]) |
| `DEVICE_TYPE_LIST` | `sendToAllDevices=true` OR (Journey + LD flag `enableJourneyDeviceTypeFilter`) | target's deviceTypes[] |
| `I_LIST` ("il"=1) | `sendToAllDevices=true` | boolean marker |
| `ENABLE_DEVICE_TYPE_FILTERING` | Journey + LD flag | boolean marker |
| `latest` | Journey path | boolean marker |
| `modeOrdinal` | always | mode.ordinal() |

NB sends `iidList=[<install>]` or `idnList=[<j>]` (whichever the Message carries) + `profileFields`.

### 5. ES `ProfileHandler` — filter enforcement

Read at line 2810+:
```java
boolean latest = fields.getBoolean("latest", false);
boolean enableDeviceTypeFiltering = fields.getBoolean(ENABLE_DEVICE_TYPE_FILTERING, false);
BasicDBList profileTypes = (BasicDBList) fields.get(PROFILE_TYPE);
ProfileType[] ptList = ...;   // e.g. [ProfileType.Android, ProfileType.iOS]
BasicDBList deviceTypes = (BasicDBList) fields.get(DEVICE_TYPE_LIST);
int[] dts = ...;              // e.g. [1, 2, 3] = Mobile, Tablet, TV
final boolean useDeviceTypeFilter =
    latest && ArrayUtils.isNotEmpty(dts) && enableDeviceTypeFiltering
        && account.getProfileData().isLinkedListReady();
boolean getAllDevices = fields.getBoolean(I_LIST, false);
boolean isMultiApp = ...;
boolean getMultiDevices = getAllDevices || isMultiApp;
```

Two branches based on `getMultiDevices`:

#### Branch A — `sendToAllDevices=true` (line 2882+)

```java
AccelLongMap bookUid = getUidsMap(account, iids, idns);
BasicDBObject fetchDevicesConfig = new BasicDBObject()
    .append(I_LIST, getAllDevices)
    .append(I_LIST_WITHOUT_LIMIT, ...)
    .append(IS_MULTI_APP_SELECTION, isMultiAppSelection);
AccelObjectMap<List<Long>> bookUidToIList =
    Utils.aggregateAllIidsForUidMapOptimized(
        account, bookUid, ptList, dts, isPush, appIdList, fetchDevicesConfig, ...);
sendAllProfilesForAllDevicesAndEncrypt(account, bookUidToIList, fields, out);
```

`aggregateAllIidsForUidMap*` walks the account's install linked-list for each user and:
- **OS filter** — checks each install's `ProfileType` against `ptList`. Only installs where `install.profileType ∈ ptList` pass.
- **Device-type filter** — if `useDeviceTypeFilter=true`, checks each install's device-type ordinal against `dts`. Only installs where `install.deviceType ∈ dts` pass.
- Returns `Map<uid, List<Long>>` — the surviving install ids per user.

`sendAllProfilesForAllDevicesAndEncrypt` serializes each install into the `il` field of the response profile.

#### Branch B — `sendToAllDevices=false` (line 2906+)

Single-install branch. For each requested uid:
- Walks the install linked-list starting from the **most recently active install** (that's what `latest=true` selects).
- Applies the same filters: `install.profileType ∈ ptList` and (if enabled) `install.deviceType ∈ dts`.
- Returns the **first** install that passes both filters. No `il` field on the response.

**"Last active" means "most recently touched install that passes the OS + deviceType filters"** — not just the very latest install irrespective of filters. If the very latest install is a TV but the target excludes TVs, ES walks to the next-newest install that is Mobile/Tablet.

### 6. NB post-lookup — attach install list if fan-out

`TimedQueueProfileLookupBatchTask.java:462-464`:
```java
BasicDBList il = (BasicDBList) profileObjectFromES.get("il");
if (info.isSendToAllDevices() && il != null) {
    m.setSendToDevices(il);
}
```

Only attached in Branch A. In Branch B, `il` is null → nothing attached.

At line 483-494 the PRN is re-encoded using the returned profile's `pn`/`pm`. For real Android/iOS installs, the decoder returns `Device.Android`/`Device.iOS`, and PRN = `NotificationInbox + Android/iOS`.

### 7. NB dispatch — `InboxStoreManager.handleStoreMessage` fan-out

`InboxStoreManager.java:259-274`:
```java
if (deviceInfoList != null && (isSendToAllDevices() || multiAppMultiSelect)) {
    deviceInfoList.forEach(_deviceInfo -> {
        InboxStoreMessage msg = new InboxStoreMessage(message);
        utils.modifyMessage(msg, (BasicDBObject) _deviceInfo);   // sets per-install iid + PRN
        sendStoreMessage(msg);
    });
} else {
    sendStoreMessage(message);
}
```

- Branch A: iterates `deviceInfoList` (== `il`), one `InboxStoreMessage` per install. `utils.modifyMessage(msg, deviceInfo)` sets per-install iid and re-encodes the PRN from that install's `pn`.
- Branch B: single message on the resolved install.

## Concrete walk-through — same profile as SUC-127057

Same user, but this time the trigger event arrives via SDK from install `3335493` (guid present).

4 installs in `dls`:
- `2748861` — Android Pixel 6 (Mobile, `ap` present, older)
- `2753712` — identity-typed (no `ap`, `pn: "2753712"` — the outlier from SUC-127057)
- `2806596` — Samsung SM-S928B (Mobile, older)
- `3335493` — Samsung SM-S928B (Mobile, most recent, has FCM token — **latest active**)

Target: `device: [1,2]` (Android + iOS), `deviceTypes: [1,2,3]` (Mobile+Tablet+TV).

**ES install linked-list contents (after filter evaluation):**

| Install | profileType | deviceType | OS filter (Android/iOS) | Device-type filter (all) |
|---|---|---|---|---|
| `3335493` | Android | Mobile | ✓ | ✓ |
| `2806596` | Android | Mobile | ✓ | ✓ |
| `2748861` | Android | Mobile | ✓ | ✓ |
| `2753712` | Web | (unknown) | ✗ | — |

### Branch B — `sendToAllDevices=false` (Last active device)

1. ES walks the linked-list from most recent → oldest.
2. Sees `3335493` — Android ∈ `[Android, iOS]` ✓ → Mobile ∈ `[1,2,3]` ✓ → returns this install's profile.
3. NB dispatches a single inbox record to install `3335493`.

**Result:** 1 inbox record on the user's latest Samsung device.

### Branch A — `sendToAllDevices=true` (All active devices)

1. ES aggregates all installs passing both filters.
2. `3335493`, `2806596`, `2748861` all pass. `2753712` filtered out (OS=Web excluded).
3. ES returns `il = [ info_3335493, info_2806596, info_2748861 ]`.
4. NB fan-out at `InboxStoreManager.java:264-270` creates 3 InboxStoreMessages, one per install. Each gets its own iid and PRN.
5. 3 inbox records persisted — one per Android install.

**Result:** 3 inbox records, one per Android install; the Web-typed install is excluded.

### Crucial contrast with SUC-127057

Install `2753712` — the Web-typed outlier that crashed SUC-127057 — **never reaches NB dispatch** in either branch. The OS filter excludes it at ES side. This is the entire point of the filter chain: install-shard data is authoritative for OS/device-type, and ES prunes non-matching installs before NB ever sees them.

The SUC-127057 case is not a filter failure — it's a case where **the install-shard has nothing at all for the requested identity**, because that identity was never bound to a device. Both branches of ES's ProfileHandler return empty. The bounce path then defaults to `Device.Web` and hits the missing `mapDeviceToDownload` case.

## Filter enforcement summary (SDK path)

| Filter | Set at | Enforced at | Data source |
|---|---|---|---|
| OS (`target.device[]`) | Dashboard target config | ES `ProfileHandler` line 2882-2894 (Branch A) / 2906+ (Branch B) via `ptList` | each install's stored `ProfileType` |
| Device-type (`target.deviceTypes[]`) | Dashboard target config | ES `aggregateAllIidsForUidMap*` via `dts` array, gated on `useDeviceTypeFilter` (requires `latest`, LD flag, linked-list ready) | each install's stored device-type ordinal |
| Last Active vs All Active (`sendToAllDevices`) | Dashboard target config (Campaign only; Journey hard-codes false) | ES branches at line 2853 based on `getMultiDevices`; NB branches at `InboxStoreManager:262` for fan-out | linked-list traversal order (Branch B walks from latest, Branch A aggregates all) |

## Why the SUC-127057 identity-only case can't leverage any of these

The identity-only bug is that `evt.iid=0, evt.idn=<j>` when the user has no install-shard entry at all:
- Branch B: ES searches for an install profile keyed by `idn` — none exists → `profileNotFoundInLookup=true` → `Device.Web` bounce path.
- Branch A: ES aggregate walk finds no installs for this uid — empty `il` → same outcome.

Both filters (OS, deviceType) act at the install level. If there's no install linked-list entry to walk, there's nothing to filter, and the flow falls to the error path.

**For SDK-ingested events, the install always exists** (LP resolved a guid to a real i, ES stored it in the install shard). All three filters have real data to act on, and the flow works.

## Journey vs Campaign — SDK path differences

Both flavours use the same guid-shard resolution at LP, the same ES filter enforcement, and the same NB post-lookup + fan-out logic. Differences follow the same table as identity-only:

| Aspect | Journey (SDK) | Campaign (SDK) |
|---|---|---|
| Producer | LP `SegmentConsumer` → SMessage → NB `/enqueue` | NB `TargetWatcher` / `PBSCampaignSpawningService` (no `/enqueue`) |
| SegmentStreamMessage hop | yes | no |
| `PostActionTargetMessageEnqueueUtility` iid zeroing | yes — Message becomes `iid=0, idn=<j>` | no — Message keeps real iid straight through |
| `sendToAllDevices` | hard-coded `false` (UI hides it) | user-controlled (Last active / All active) |
| Slow queue used | `slowQueue` | `slowQueue` (Last active) or `slowProfileLookupFileQueue` (All active) |
| Batch task | always default `profileLookupBatchTask` | usually default (NB-spawned, `nbRequestReceiveTime=0`) |

Everything downstream of ProfileLookupBatchTask (ES query, filter enforcement, dispatch) is identical between the two.

## Summary

**SDK path**: guid → LP resolves to real `i` → SegmentConsumer swap skipped → Message carries real install context → ES install-shard is populated for this user → OS + device-type + `sendToAllDevices` filters all operate on real data → correct install(s) receive the inbox record.

**Identity-only path (SUC-127057)**: no guid → K-shard swap fires → Message carries identity as `iid` → ES install-shard has nothing for this identity → filters have no data to act on → dispatch falls to error path → `mapDeviceToDownload(Web, NotificationInbox)` throws.

Same target config, same three UI filters, but the outcome depends entirely on whether the trigger event carried a guid.
