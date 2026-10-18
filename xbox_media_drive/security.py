"""Bounded parsing of self-relative Windows security descriptors.

Only DACLs are edited; owner, group, SACL, existing ACE bytes and control
flags are retained. Unsupported or deny-bearing ACLs fail before writes.
"""
import struct
from dataclasses import dataclass

from . import DriveError

APP_PACKAGES = bytes.fromhex("010200000000000f0200000001000000")
MODIFY = 0x001301BF
FULL = 0x001F01FF
RIGHTS = {"modify": MODIFY, "full": FULL}
HEADER = struct.Struct("<BBHIIII")
DACL_PRESENT = 0x0004
SELF_RELATIVE = 0x8000
INHERITED = 0x10
INHERIT_ONLY = 0x08


@dataclass(frozen=True)
class Descriptor:
    revision: int
    reserved: int
    control: int
    owner: bytes
    group: bytes
    sacl: bytes
    dacl: bytes

    @classmethod
    def parse(cls, raw: bytes):
        if len(raw) < HEADER.size or len(raw) > 262144:
            raise DriveError("Invalid NTFS security descriptor size.")
        rev, reserved, control, owner, group, sacl, dacl = HEADER.unpack_from(raw)
        if rev != 1 or not control & SELF_RELATIVE:
            raise DriveError("Unsupported NTFS security descriptor format.")
        spans = []

        def component(offset, acl=False):
            if not offset:
                return b""
            if offset < 20 or offset % 4 or offset + 8 > len(raw):
                raise DriveError("Invalid security descriptor offset.")
            length = struct.unpack_from("<H", raw, offset + 2)[0] if acl else 8 + 4 * raw[offset + 1]
            if length < 8 or offset + length > len(raw):
                raise DriveError("Truncated security descriptor component.")
            if not acl and (raw[offset] != 1 or raw[offset + 1] > 15):
                raise DriveError("Unsupported security identifier.")
            span = (offset, offset + length)
            # Owner and group can legally share one SID.
            if any(span != old and span[0] < old[1] and old[0] < span[1] for old in spans):
                raise DriveError("Overlapping security descriptor components.")
            spans.append(span)
            value = raw[offset:offset + length]
            if acl:
                acl_entries(value)
            return value

        result = cls(rev, reserved, control, component(owner), component(group),
                     component(sacl, True), component(dacl, True))
        if result.dacl and not control & DACL_PRESENT:
            raise DriveError("DACL present without its security control flag.")
        return result

    def encode(self, dacl=None):
        payload = bytearray()
        offsets = []
        for value in (self.owner, self.group, self.sacl, self.dacl if dacl is None else dacl):
            offsets.append(20 + len(payload) if value else 0)
            payload.extend(value)
            payload.extend(b"\0" * (-len(payload) % 4))
        return HEADER.pack(self.revision, self.reserved, self.control, *offsets) + payload


def acl_entries(acl):
    if not acl:
        return []
    if len(acl) < 8 or acl[0] not in (2, 4):
        raise DriveError("Unsupported ACL revision.")
    size, count = struct.unpack_from("<HH", acl, 2)
    if size != len(acl):
        raise DriveError("Invalid ACL size.")
    pos = 8
    entries = []
    for _ in range(count):
        if pos + 4 > size:
            raise DriveError("Truncated NTFS access control entry.")
        length = struct.unpack_from("<H", acl, pos + 2)[0]
        if length < 4 or length % 4 or pos + length > size:
            raise DriveError("Invalid NTFS access control entry length.")
        entries.append(acl[pos:pos + length])
        pos += length
    return entries


def signature(raw):
    """Compare semantic components, independent of component placement."""
    sd = Descriptor.parse(raw)
    return sd.control, sd.owner, sd.group, sd.sacl, sd.dacl


def grant(raw, directory, access="modify"):
    sd = Descriptor.parse(raw)
    # A null or absent DACL already allows everyone. Never narrow it.
    if not sd.dacl:
        return raw
    entries = acl_entries(sd.dacl)
    inherited_seen = False
    mask = RIGHTS[access]
    allowed = 0
    inherited_allowed = 0
    for ace in entries:
        if ace[0] not in (0, 1) or len(ace) < 16:
            raise DriveError("Complex ACL found; automatic editing is refused. Review it in Windows.")
        sid = ace[8:]
        if sid[0] != 1 or len(sid) != 8 + 4 * sid[1]:
            raise DriveError("Invalid SID in access control entry.")
        if ace[0] == 1:
            raise DriveError("A deny ACE could block app access; resolve it in Windows before preparing.")
        if inherited_seen and not ace[1] & INHERITED:
            raise DriveError("Noncanonical ACL found; review its inheritance in Windows.")
        inherited_seen |= bool(ace[1] & INHERITED)
        if sid == APP_PACKAGES:
            rights = struct.unpack_from("<I", ace, 4)[0]
            if rights & 0x10000000:  # GENERIC_ALL
                rights |= FULL
            if not ace[1] & INHERIT_ONLY:
                allowed |= rights
            if ace[1] & 3 == 3 and not ace[1] & 4:  # OI, CI; no NO_PROPAGATE
                inherited_allowed |= rights
    if allowed & mask == mask and (not directory or inherited_allowed & mask == mask):
        return raw
    ace = struct.pack("<BBHI", 0, 3 if directory else 0, 8 + len(APP_PACKAGES), mask) + APP_PACKAGES
    # Canonical explicit allow position, before inherited ACEs.
    index = next((i for i, value in enumerate(entries) if value[1] & INHERITED), len(entries))
    entries.insert(index, ace)
    size = 8 + sum(map(len, entries))
    if size > 65535 or len(entries) > 65535:
        raise DriveError("NTFS ACL is too large for another permission entry.")
    acl = bytearray(sd.dacl[:8])
    struct.pack_into("<HH", acl, 2, size, len(entries))
    acl.extend(b"".join(entries))
    return sd.encode(bytes(acl))
