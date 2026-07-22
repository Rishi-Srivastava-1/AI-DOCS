#!/usr/bin/env python3
"""
Decode a CleverTap Profile `pm` (P_MERGEABLE) value.

pm packs profile-wide (person-level) fields into a 64-bit long (55 bits used).
Source of truth: core/es/src/com/wizrocket/eventsstore/protocol/Profile.java

Usage:
    python3 decode_pm.py <pm_value>      # decode the passed value
    python3 decode_pm.py                 # falls back to hardcoded PM below
"""

import sys

# ------------------------------------------------------------
# Fallback pm value (used when no CLI arg is provided)
PM = 574208952491860992
# ------------------------------------------------------------

GENDER = {0: "Unset", 1: "Male", 2: "Female", 3: "Other"}


def to_unsigned_64(signed: int) -> int:
    """Convert a Java signed long to its unsigned 64-bit representation."""
    return signed & 0xFFFFFFFFFFFFFFFF


def bits(unsigned: int, offset: int, width: int) -> int:
    """Extract `width` bits starting at `offset` from an unsigned 64-bit value."""
    return (unsigned >> offset) & ((1 << width) - 1)


def decode_pm(pm_signed: int) -> dict:
    u = to_unsigned_64(pm_signed)

    allow_system_events         = bits(u,  4, 1)
    duplication_over_email      = bits(u,  5, 1)
    duplication_over_phone      = bits(u,  7, 1)
    dnd_whatsapp                = bits(u,  8, 1)
    daylight_saving             = bits(u,  9, 1)
    timezone_offset             = bits(u, 10, 6)
    dnd_email                   = bits(u, 17, 1)
    opted_out                   = bits(u, 18, 1)
    dnd_push_all                = bits(u, 19, 1)
    dnd_phone                   = bits(u, 20, 1)
    has_email                   = bits(u, 21, 1)
    has_photo                   = bits(u, 22, 1)
    city                        = bits(u, 23, 20)
    region                      = bits(u, 43, 8)
    country                     = bits(u, 51, 8)
    age_deprecated              = bits(u, 59, 3)
    gender_ord                  = bits(u, 62, 2)

    return {
        "input_signed":            pm_signed,
        "unsigned_u64":            u,
        "hex":                     f"0x{u:016x}",
        "binary":                  f"{u:064b}",
        "gender_ordinal":          gender_ord,
        "gender":                  GENDER.get(gender_ord, f"Unknown({gender_ord})"),
        "age_deprecated":          age_deprecated,
        "country":                 country,
        "region":                  region,
        "city":                    city,
        "hasPhoto":                has_photo,
        "hasEmail":                has_email,
        "DNDPhone":                dnd_phone,
        "DNDPushAll":              dnd_push_all,
        "optedOut":                opted_out,
        "DNDEmail":                dnd_email,
        "timezoneOffset":          timezone_offset,
        "daylightSaving":          daylight_saving,
        "DNDWhatsapp":             dnd_whatsapp,
        "duplicationOverPhone":    duplication_over_phone,
        "duplicationOverEmail":    duplication_over_email,
        "allowSystemEventsAndCommunication": allow_system_events,
    }


def print_decoded(decoded: dict) -> None:
    print()
    print(f"  Input (signed)        : {decoded['input_signed']}")
    print(f"  Unsigned (u64)        : {decoded['unsigned_u64']}")
    print(f"  Hex                   : {decoded['hex']}")
    print(f"  Binary                : {decoded['binary']}")
    print()
    print("  === Demographics ===")
    print(f"  gender (bits 62-63)   : {decoded['gender_ordinal']} ({decoded['gender']})")
    print(f"  age [dep] (bits 59-61): {decoded['age_deprecated']}")
    print()
    print("  === Geo ===")
    print(f"  country (bits 51-58)  : {decoded['country']}")
    print(f"  region (bits 43-50)   : {decoded['region']}")
    print(f"  city (bits 23-42)     : {decoded['city']}")
    print()
    print("  === Contact / Profile ===")
    print(f"  hasPhoto (bit 22)     : {decoded['hasPhoto']}")
    print(f"  hasEmail (bit 21)     : {decoded['hasEmail']}")
    print()
    print("  === DND / Opt-out flags (profile-wide) ===")
    print(f"  DNDPhone (bit 20)     : {decoded['DNDPhone']}")
    print(f"  DNDPushAll (bit 19)   : {decoded['DNDPushAll']}")
    print(f"  optedOut (bit 18)     : {decoded['optedOut']}")
    print(f"  DNDEmail (bit 17)     : {decoded['DNDEmail']}")
    print(f"  DNDWhatsapp (bit 8)   : {decoded['DNDWhatsapp']}")
    print()
    print("  === Timezone ===")
    print(f"  tzOffset (bits 10-15) : {decoded['timezoneOffset']}")
    print(f"  daylightSaving (bit 9): {decoded['daylightSaving']}")
    print()
    print("  === Dedup / Misc ===")
    print(f"  duplicationOverPhone (bit 7)   : {decoded['duplicationOverPhone']}")
    print(f"  duplicationOverEmail (bit 5)   : {decoded['duplicationOverEmail']}")
    print(f"  allowSystemEventsAndComm (bit 4): {decoded['allowSystemEventsAndCommunication']}")
    print()


def resolve_pm() -> int:
    """Return the pm value from CLI arg if provided, else the hardcoded fallback."""
    if len(sys.argv) > 1:
        raw = sys.argv[1]
        try:
            return int(raw)
        except ValueError:
            print(f"Error: '{raw}' is not a valid integer.")
            sys.exit(1)
    return PM


if __name__ == "__main__":
    print_decoded(decode_pm(resolve_pm()))
