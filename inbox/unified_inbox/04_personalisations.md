# 4. How Personalisations Are Stored

## Fundamental Difference from App Inbox

App Inbox stores personalisations as flat `{numericPropertyId: stringValue}` pairs at the binary level.

Unified Inbox has a **multi-layer encoding system** that:
1. Traces which template values are actually accessed during Liquid rendering
2. Encodes keys hierarchically (top-level → property-level → nested)
3. Supports nested maps, not just flat key-value pairs
4. Stores BOTH profile AND event properties (not just event like App Inbox)

## The Personalisation Encoding Architecture

```
NB Dispatch:
  ServerSideInboxDispatcher.toStoreInboxMessage()
    |
    +-> traceLiquidPersonalisations()     ← run template, capture accessed values
    +-> addReplacementPersonalisations()  ← add @-style (non-liquid) replacements
    +-> personalisationsEncoder.encode()  ← encode to UnpackedMap
         |
         +-> TopLevelPersonalisationKeyEncoder  ← "Profile" -> 0, "Event" -> 1, etc.
         +-> ProfilePropertyEncoder             ← "name" -> numeric ID
         +-> EventPropertyEncoder               ← "productId" -> numeric ID
         +-> NoopKeyEncoder                     ← nested string keys kept as-is

Binary Serialization:
  UnpackedMap → MapBinarySerializer.writeMap() → bytes
    |
    +-> writeStringOrNumber(key)    ← encode key as int (numeric) or string
    +-> writeValue(value)           ← encode value as string or nested map
    +-> writeVLQ(0)                 ← zero byte terminates map

Binary Deserialization:
  bytes → PackedMap → MapBinaryDeserializer.readMap() → Map<StringOrNumber, Object>
```

## Phase 1: NB Traces Liquid Template Access

**File**: `core/nb/src/com/wizrocket/nb/dispatchers/notificationinbox/ServerSideInboxDispatcher.java:279-289`

Unlike App Inbox (which explicitly lists needed properties from `replacements`), Unified Inbox uses **tracing** — it actually renders the Liquid template and captures which values were accessed:

```java
private Map<String, Object> traceLiquidPersonalisations(
        ProfileDTO decryptedProfileClone, TargetingInfo ti, String messageKey, String traceKey) {
    Map<String, Object> accessedValues;
    try {
        // Actually render the template — triggers Liquid to access profile/event values
        contentBuilder.buildContentForAllDevices(ti, messageKey,
                ProfileDTO.getProfileObject(decryptedProfileClone));
    } finally {
        // Retrieve the trace — which keys were actually accessed
        accessedValues = ti.getLiquidScriptingCache().getAndRemoveTrace(traceKey);
    }
    return accessedValues;
}
```

This is fundamentally different from App Inbox:
- **App Inbox**: NB reads `replacements` config to know which keys → extracts them from profile
- **Unified Inbox**: NB renders the template → captures what Liquid actually touched

The result is a `Map<String, Object>` like:
```java
{
    "Profile": {
        "name": "Rishi",
        "city": "Mumbai"
    },
    "Event": {
        "productId": "iPhone",
        "price": 999
    }
}
```

**Key insight**: Unified Inbox stores **both profile AND event** personalisations, unlike App Inbox which only stores event properties.

## Phase 2: Add @-Style Replacements

**File**: `core/nb/src/com/wizrocket/nb/dispatchers/notificationinbox/ServerSideInboxDispatcher.java:240-261`

```java
private void addReplacementPersonalisations(
        Map<String, Object> accessedValues, ProfileDTO profile, TargetingInfo ti) {
    BasicDBObject replacements = ti.replacements();
    if (replacements == null) return;

    replacements.keySet().forEach(key -> {
        String[] keyParts = key.split(":");
        if (keyParts.length == 1) {
            // Profile system property: "Name" → lookup in REPLACEMENT_TO_LIQUID_KEYS
            copyKey(accessedValues, profile, LiquidConstants.PROFILE, prop);
        } else if (ProfileConstants.P_X.equals(keyParts[0])) {
            // Custom profile property: "p:city"
            copyKey(accessedValues, profile, LiquidConstants.PROFILE, keyParts[1]);
        } else if ("e".equals(keyParts[0])) {
            // Event property: "e:5" → decode property ID 5 to name
            eventPropertyDecoder.decode(Integer.parseInt(keyParts[1]), ti).ifPresent(
                    prop -> copyKey(accessedValues, profile, LiquidConstants.EVENT, prop));
        }
    });
}
```

## Phase 3: Encode Keys to Numeric IDs

**File**: `core/server-side-inbox/service/src/com/wizrocket/inbox/service/codec/PersonalisationsEncoder.java`

The raw `Map<String, Object>` is encoded to `Map<StringOrNumber, Object>` where string keys are converted to numeric IDs for space efficiency:

### Top-Level Keys

```java
// TopLevelPersonalisationKeyEncoder.java
// "Profile" → 0, "Event" → 1, "Linked" → 2, etc.
Optional<Integer> encode(String key, TargetingInfo ti) {
    int index = PersonalisationKeys.TOP_LEVEL_KEYS.indexOf(key);
    return index >= 0 ? Optional.of(index) : Optional.empty();
}
```

### Profile Property Keys

```java
// ProfilePropertyEncoder.java
Optional<Integer> encode(String key, TargetingInfo ti) {
    if (SYSTEM_PROFILE_PROPERTY_IDS.containsKey(key)) {
        // System property: "name" → (id << 1) | 1  (LSB = 1 indicates system)
        return Optional.of((SYSTEM_PROFILE_PROPERTY_IDS.get(key) << 1) | 1);
    } else {
        // Custom property: "city" → id << 1  (LSB = 0 indicates custom)
        int id = keyValueMapping.getKey(PROFILE_KEY, false, key)[0];
        return Optional.of(id << 1);
    }
}
```

### Event Property Keys

Similar to profile but for event properties via `KeyValueMappingDao`.

### Result after encoding

```java
// Before encoding:
{
    "Profile": { "name": "Rishi", "city": "Mumbai" },
    "Event": { "productId": "iPhone" }
}

// After encoding:
{
    0: { 3: "Rishi", 14: "Mumbai" },    // 0=Profile, 3=name(system), 14=city(custom)
    1: { 22: "iPhone" }                  // 1=Event, 22=productId
}
```

## Phase 4: Binary Serialization of the Map

**File**: `core/server-side-inbox/service/src/com/wizrocket/inbox/service/serialization/MapBinarySerializer.java`

### StringOrNumber Encoding

**File**: `core/server-side-inbox/service/src/com/wizrocket/inbox/service/serialization/ExtendedDataOutputStream.java:48-57`

```java
public void writeStringOrNumber(StringOrNumber value) {
    value.apply(
            str -> {
                byte[] bytes = str.getBytes(StandardCharsets.UTF_8);
                writeVLQ((long) bytes.length << 1);       // LSB = 0 → string
                write(bytes);
            },
            num -> writeVLQ((num << 1) | 1));              // LSB = 1 → number
}
```

The LSB of the VLQ header distinguishes strings from numbers:
```
Number 42:   VLQ((42 << 1) | 1) = VLQ(85) = [55]       (1 byte)
String "Hi": VLQ(2 << 1) = VLQ(4) = [04] then [48 69]  (3 bytes)
```

### Map Serialization

```java
// MapBinarySerializer.java:10-32
public void writeMap(Map<StringOrNumber, Object> map, ExtendedDataOutputStream data) {
    map.forEach((k, v) -> {
        ValueType valueType = getType(v);
        if (valueType == ValueType.EMPTY) return;    // skip empty
        if (k.isString() && k.getString().isEmpty()) return;  // skip "" keys

        // negative numbers can't be VLQ'd — convert to string
        if (k.isNumber() && k.getNumber() < 0) {
            k = StringOrNumber.of(String.valueOf(k.getNumber()));
        }

        data.writeStringOrNumber(k);          // write key
        writeValue(data, v, valueType);        // write value
    });
    data.writeVLQ(0);                          // zero byte = end of map
}
```

Values can be:
- **String**: written as StringOrNumber string
- **Nested map**: write `MAP_VALUE_TYPE` (1 as number) then recursively write the sub-map

```java
private void writeValue(ExtendedDataOutputStream data, Object v, ValueType type) {
    if (type == ValueType.MAP) {
        data.writeStringOrNumber(StringOrNumber.of(MAP_VALUE_TYPE));  // marker: 1
        writeMap((Map<StringOrNumber, Object>) v, data);              // recursive
    } else if (type == ValueType.STRING) {
        data.writeStringOrNumber(StringOrNumber.of(String.valueOf(v)));
    }
}
```

### Map Deserialization

**File**: `core/server-side-inbox/service/src/com/wizrocket/inbox/service/serialization/MapBinaryDeserializer.java`

```java
public Map<StringOrNumber, Object> readMap(ExtendedDataInputStream in) {
    Map<StringOrNumber, Object> result = new HashMap<>();
    while (true) {
        StringOrNumber key = in.readStringOrNumber();
        if (key.isString() && key.getString().isEmpty()) {
            break;   // zero byte → end of map
        }
        in.readStringOrNumber().apply(
                str -> result.put(key, parseIfNumeric(str)),   // string value
                num -> {
                    if (num == MAP_VALUE_TYPE) {
                        result.put(key, readMap(in));           // nested map (recursive)
                    }
                });
    }
    return result;
}
```

## PackedMap vs UnpackedMap

**File**: `core/server-side-inbox/service/src/com/wizrocket/inbox/service/content/PackedMap.java`

```java
public class PackedMap implements PersonalisationsContainer {
    private final byte[] bytes;   // raw binary data

    public Consumer<ExtendedDataOutputStream> writer() {
        return out -> out.write(bytes);   // write raw bytes directly
    }

    public Map<StringOrNumber, Object> asMap() {
        return new MapBinaryDeserializer()
            .readMap(new ExtendedDataInputStream(new ByteArrayInputStream(bytes)));
    }
}
```

**File**: `core/server-side-inbox/service/src/com/wizrocket/inbox/service/content/UnpackedMap.java`

```java
public class UnpackedMap implements PersonalisationsContainer {
    private final Map<StringOrNumber, Object> map;   // structured data

    public Consumer<ExtendedDataOutputStream> writer() {
        return out -> new MapBinarySerializer().writeMap(map, out);
    }

    public Map<StringOrNumber, Object> asMap() {
        return map;   // return directly
    }
}
```

- **UnpackedMap**: Created by NB at dispatch time (has the raw Map). Serialized when writing to Redis.
- **PackedMap**: Created by deserializer at read time (has raw bytes). Deserialized lazily on `.asMap()` call.

## Comparison: App Inbox vs Unified Inbox Personalisations

| Aspect | App Inbox | Unified Inbox |
|--------|-----------|---------------|
| **What's stored** | Event properties only | Profile + Event + Linked + recommendations |
| **Key format** | Numeric property IDs only (2-byte short) | StringOrNumber — numbers or string keys |
| **Value format** | Flat string, max 255 bytes | String or nested map, max 500 chars |
| **Nesting** | None | Recursive nested maps |
| **How keys are identified** | Explicit from `replacements` config | Traced from actual Liquid template access |
| **Max entries** | 255 | 50 |
| **Encoding** | Fixed `[propId:2B][len:1B][value:NB]` | VLQ-based StringOrNumber with zero-terminated map |
| **Profile props stored?** | No | Yes |
