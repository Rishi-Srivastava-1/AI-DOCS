#!/usr/bin/env python3
"""
Decode a CleverTap Profile `pn` (P_NONMERGEABLE) value.

pn packs device-specific fields into a 64-bit long (45 bits used).
Source of truth: core/es/src/com/wizrocket/eventsstore/protocol/Profile.java

Usage:
    python3 decode_pn.py <pn_value>      # decode the passed value
    python3 decode_pn.py                 # falls back to hardcoded PN below
"""

import sys

# ------------------------------------------------------------
# Fallback pn value (used when no CLI arg is provided)
PN = -8898830289193007952
# ------------------------------------------------------------

BROWSER = {
    0: "Others", 1: "Firefox", 2: "Chrome", 3: "Seamonkey", 4: "Safari",
    5: "Opera", 6: "IE", 7: "UCBrowser", 8: "MobileApp", 9: "Kaios",
}

DEVICE_TYPE = {0: "Desktop", 1: "Mobile", 2: "Tablet", 3: "TV"}

OS = {
    0: "Others", 1: "Android", 2: "iOS", 3: "Windows",
    4: "Mac", 5: "BlackBerry", 6: "Linux",
}


def to_unsigned_64(signed: int) -> int:
    """Convert a Java signed long to its unsigned 64-bit representation."""
    return signed & 0xFFFFFFFFFFFFFFFF


def bits(unsigned: int, offset: int, width: int) -> int:
    """Extract `width` bits starting at `offset` from an unsigned 64-bit value."""
    return (unsigned >> offset) & ((1 << width) - 1)


def decode_pn(pn_signed: int) -> dict:
    u = to_unsigned_64(pn_signed)

    identity                    = bits(u,  0, 40)
    is_migrated                 = bits(u, 40, 1)
    profile_marked_for_deletion = bits(u, 46, 1)
    ddnd                        = bits(u, 47, 1)
    guid_present_on_iid_shard   = bits(u, 48, 1)
    dnd_whatsapp_deprecated     = bits(u, 49, 1)
    blacklisted_for_shard       = bits(u, 50, 1)
    dnd_phone_deprecated        = bits(u, 51, 1)
    has_phone                   = bits(u, 52, 1)
    dnd_push                    = bits(u, 53, 1)
    has_push                    = bits(u, 54, 1)
    os_ord                      = bits(u, 55, 3)
    device_type_ord             = bits(u, 58, 2)
    browser_ord                 = bits(u, 60, 4)

    return {
        "input_signed":                pn_signed,
        "unsigned_u64":                u,
        "hex":                         f"0x{u:016x}",
        "binary":                      f"{u:064b}",
        "identity":                    identity,
        "isMigrated":                  is_migrated,
        "profileMarkedForDeletion":    profile_marked_for_deletion,
        "DDND":                        ddnd,
        "guidPresentOnIIDShard":       guid_present_on_iid_shard,
        "DNDWhatsapp_deprecated":      dnd_whatsapp_deprecated,
        "blacklistedForShard":         blacklisted_for_shard,
        "DNDPhone_deprecated":         dnd_phone_deprecated,
        "hasPhone":                    has_phone,
        "DNDPush":                     dnd_push,
        "hasPush":                     has_push,
        "OS_ordinal":                  os_ord,
        "OS":                          OS.get(os_ord, f"Unknown({os_ord})"),
        "DeviceType_ordinal":          device_type_ord,
        "DeviceType":                  DEVICE_TYPE.get(device_type_ord, f"Unknown({device_type_ord})"),
        "Browser_ordinal":             browser_ord,
        "Browser":                     BROWSER.get(browser_ord, f"Unknown({browser_ord})"),
    }


def print_decoded(decoded: dict) -> None:
    print()
    print(f"  Input (signed)        : {decoded['input_signed']}")
    print(f"  Unsigned (u64)        : {decoded['unsigned_u64']}")
    print(f"  Hex                   : {decoded['hex']}")
    print(f"  Binary                : {decoded['binary']}")
    print()
    print("  === Device fields ===")
    print(f"  Browser (bits 60-63)  : {decoded['Browser_ordinal']} ({decoded['Browser']})")
    print(f"  DeviceType (bits 58-59): {decoded['DeviceType_ordinal']} ({decoded['DeviceType']})")
    print(f"  OS (bits 55-57)       : {decoded['OS_ordinal']} ({decoded['OS']})")
    print()
    print("  === Reachability / DND flags ===")
    print(f"  hasPush (bit 54)      : {decoded['hasPush']}")
    print(f"  DNDPush (bit 53)      : {decoded['DNDPush']}")
    print(f"  hasPhone (bit 52)     : {decoded['hasPhone']}")
    print(f"  DNDPhone [dep] (51)   : {decoded['DNDPhone_deprecated']}")
    print(f"  blacklistedShard (50) : {decoded['blacklistedForShard']}")
    print(f"  DNDWhatsapp [dep] (49): {decoded['DNDWhatsapp_deprecated']}")
    print(f"  guidPresentOnII (48)  : {decoded['guidPresentOnIIDShard']}")
    print(f"  DDND (bit 47)         : {decoded['DDND']}")
    print(f"  profileMarkedDel (46) : {decoded['profileMarkedForDeletion']}")
    print()
    print("  === Identity ===")
    print(f"  isMigrated (bit 40)   : {decoded['isMigrated']}")
    print(f"  identity (bits 0-39)  : {decoded['identity']}")
    print()


def resolve_pn() -> int:
    """Return the pn value from CLI arg if provided, else the hardcoded fallback."""
    if len(sys.argv) > 1:
        raw = sys.argv[1]
        try:
            return int(raw)
        except ValueError:
            print(f"Error: '{raw}' is not a valid integer.")
            sys.exit(1)
    return PN


if __name__ == "__main__":
    print_decoded(decode_pn(resolve_pn()))
