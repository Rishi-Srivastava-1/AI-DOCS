# Live-Triggered Push Campaign Flow

End-to-end flow of a **live-triggered push campaign** in Ultron — a campaign configured with an event trigger (e.g. "when user does Charged, send them a push") that fires in real time when the triggering SDK event arrives.

> Do not confuse with:
> - `ManualTriggerConsumer` (`/1/send/externaltrigger.json`) — server-side operator-triggered, users pre-supplied, no event→campaign lookup.
> - `LiveActivityConsumer` (`/v2/liveActivity/start`) — iOS Live Activities (Dynamic Island widgets), a different feature entirely.

## Flow Diagram

```
┌─────────────┐
│  SDK / API  │  User does event (e.g. "Charged")
└──────┬──────┘
       │ HTTP
       ▼
┌─────────────────────────────────────────────────────────┐
│  LC  (Log Collector)                                    │
│  • Event ingestion endpoint (e.g. /a1)                  │
│  • Validates + writes raw event to Redis event queue    │
└──────┬──────────────────────────────────────────────────┘
       │  Redis (event queue, EventType.EVENT)
       ▼
┌─────────────────────────────────────────────────────────┐
│  LP  (Log Processor) — MainRunner → GenericEventProcessor│
│                                                         │
│   PreRecordTaskList (per EventEntry, ordered):          │
│    …                                                    │
│    ┌───────────────────────────────────────────────┐    │
│    │  TargetConsumer.doConsume()  ← QUALIFIER      │    │
│    │  1. loadTargets(event):                       │    │
│    │     targetingDao.getTriggerTargetsDto(true)   │    │
│    │       .getTargetingInfoForEvent(evtName)      │    │
│    │     → cached event-name → List<TargetingInfo> │    │
│    │  2. Evaluate where-clause / segment on profile│    │
│    │  3. Build TMessage per matched campaign       │    │
│    │  4. output.getqMessages().add(tm)             │    │
│    └───────────────────────────────────────────────┘    │
│    OnlineApiTargetConsumer  (online-API targets)        │
│    JourneyEventEntry consumers (journey triggers)       │
│    …                                                    │
│                                                         │
│  GenericEventProcessor.pushMessagesToNB() → HTTP /enqueue│
└──────┬──────────────────────────────────────────────────┘
       │  HTTP POST /enqueue  (or /notify)
       ▼
┌─────────────────────────────────────────────────────────┐
│  NB  (Notification Backend)                             │
│                                                         │
│  Queue worker (TimedQueueWorker) drains TimedQueue      │
│    → ProfileLookupBatchTask (batched ES profile fetch)  │
│    → MessagePreDispatchValidatorFacade                  │
│         · FrequencyCapValidationStrategy   (fcaps)      │
│         · ControlGroupValidationStrategy                │
│         · UnreachableProfile / DND / Bloom filter       │
│         · Rate limiter (ThrottledCampaignRateLimiter)   │
│    → MessageMaker → channel dispatcher                  │
└──────┬──────────────────────────────────────────────────┘
       │
       ▼
┌─────────────────────────────────────────────────────────┐
│  Delivery — FCM (Android push) / APNs (iOS push)        │
└─────────────────────────────────────────────────────────┘
```

## Where qualification actually happens

**LP does the event→campaign match and where-clause evaluation. NB does the final dispatch-eligibility gate.**

| Stage | Class | File |
|---|---|---|
| Event ingress | `LC` servlets (e.g. `A1Servlet`) | `core/lc/src/java/com/wizrocket/lc/apis/...` |
| LP hot path | `GenericEventProcessor.consume()` | `core/lp/src/com/wizrocket/lp/events/GenericEventProcessor.java` |
| **Event → live campaigns** | `TargetConsumer.loadTargets()` | `core/lp/src/com/wizrocket/lp/events/consumers/targets/TargetConsumer.java:192` |
| Trigger-target cache | `TargetingDao.getTriggerTargetsDto()` → `TargetingDto.getTargetingInfoForEvent(evtName)` | `core/dao/.../TargetingDao.java`, `.../TargetingDto.java` |
| Where-clause eval + NB msg build | `TargetConsumer.getMessages()` / `buildTMessage()` | same file |
| Journey triggers | `OnlineApiTargetConsumer`, `JourneyEventEntry` | `core/lp/.../consumers/targets/` |
| Handoff to NB | `GenericEventProcessor.pushMessagesToNB()` → `/enqueue` | `core/lp/.../GenericEventProcessor.java` |
| Pre-dispatch gate | `MessagePreDispatchValidatorFacade.validateAndAcceptAction()` | `core/nb/src/com/wizrocket/nb/validator/MessagePreDispatchValidatorFacade.java` |
| Frequency caps | `FrequencyCapValidationStrategy` + `AdvancedFcapsRuleCache` | `core/nb/.../utils/frequencycap/` |
| Push dispatch | FCM / APNs dispatchers | `core/nb/src/com/wizrocket/nb/dispatchers/` |

## Key points

- **Event→campaign index is a per-event-name lookup**: `TargetingDto.getTargetingInfoForEvent(eventName)` — cached, refreshed by `TargetWatcher`. Sourced from MongoDB `targets` collection where each live-campaign document has an event trigger name.
- **Segment/where-clause is evaluated in LP** inside `TargetConsumer` (this is the primary "qualification"). If the event matches AND the profile passes the where-clause, an NB message is built.
- **NB re-validates at dispatch** — frequency caps, control group, DND/unsubscribe, rate limits. These aren't "qualification" in the segmentation sense; they're delivery-eligibility gates that can drop an already-qualified message.

Short version: **LP `TargetConsumer` is the "who qualifies?" class for live push campaigns**, driven by a cached event-name → `List<TargetingInfo>` index in `TargetingDao`. NB only enforces delivery gates.
