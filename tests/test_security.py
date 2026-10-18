import struct
import unittest

from xbox_media_drive import DriveError
from xbox_media_drive.security import (APP_PACKAGES, Descriptor, FULL, HEADER, MODIFY,
                                       acl_entries, grant, signature)

SYSTEM = bytes.fromhex("010100000000000512000000")


def ace(sid=SYSTEM, mask=FULL, flags=0, kind=0):
    return struct.pack("<BBHI", kind, flags, 8 + len(sid), mask) + sid


def descriptor(entries=None, control=0x9004, sacl=b""):
    acl = b"" if entries is None else struct.pack("<BBHHH", 2, 0, 8 + sum(map(len, entries)), len(entries), 0) + b"".join(entries)
    return Descriptor(1, 0, control, SYSTEM, SYSTEM, sacl, acl).encode()


class SecurityTests(unittest.TestCase):
    def test_grant_preserves_every_other_component_and_existing_ace(self):
        # SACL is opaque to the editor but must be preserved, including audit ACEs.
        audit = ace(kind=2)
        sacl = struct.pack("<BBHHH", 2, 0, 8 + len(audit), 1, 0) + audit
        raw = descriptor([ace()], control=0x9014, sacl=sacl)
        result = grant(raw, True)
        before, after = Descriptor.parse(raw), Descriptor.parse(result)
        self.assertEqual(before.control, after.control)
        self.assertEqual((before.owner, before.group, before.sacl), (after.owner, after.group, after.sacl))
        self.assertEqual(acl_entries(after.dacl), [ace(), ace(APP_PACKAGES, MODIFY, 3)])
        self.assertEqual(grant(result, True), result)

    def test_files_have_no_inheritance_flags_and_full_is_explicit(self):
        result = Descriptor.parse(grant(descriptor([]), False, "full"))
        self.assertEqual(acl_entries(result.dacl), [ace(APP_PACKAGES, FULL)])

    def test_null_and_absent_dacl_are_never_restricted(self):
        for control in (0x8000, 0x8004):
            raw = descriptor(control=control)
            self.assertEqual(grant(raw, True), raw)

    def test_existing_app_permissions_are_retained(self):
        raw = descriptor([ace(APP_PACKAGES, FULL, 3)])
        self.assertEqual(grant(raw, True), raw)
        read_only = descriptor([ace(APP_PACKAGES, 0x120089)])
        result = acl_entries(Descriptor.parse(grant(read_only, False)).dacl)
        self.assertEqual(result[0], ace(APP_PACKAGES, 0x120089))
        self.assertEqual(len(result), 2)

    def test_inherit_only_and_no_propagate_do_not_count_as_full_grant(self):
        for flags in (0x0B, 0x07, 0):
            raw = descriptor([ace(APP_PACKAGES, FULL, flags)])
            self.assertNotEqual(signature(grant(raw, True)), signature(raw))

    def test_explicit_grant_precedes_inherited_entries(self):
        inherited = ace(flags=0x10)
        result = acl_entries(Descriptor.parse(grant(descriptor([ace(), inherited]), True)).dacl)
        self.assertEqual(result, [ace(), ace(APP_PACKAGES, MODIFY, 3), inherited])

    def test_deny_complex_and_noncanonical_acls_are_refused(self):
        for entries in ([ace(kind=1)], [ace(kind=5)], [ace(flags=0x10), ace()]):
            with self.subTest(entries=entries), self.assertRaises(DriveError):
                grant(descriptor(entries), True)

    def test_truncated_and_out_of_bounds_descriptors_are_refused(self):
        raw = descriptor([ace()])
        corrupt = bytearray(raw)
        struct.pack_into("<I", corrupt, 16, 0xFFFFFFFC)
        for bad in (b"", raw[:19], raw[:-2], bytes(corrupt)):
            with self.subTest(bad=bad), self.assertRaises(DriveError):
                grant(bad, False)

    def test_semantic_comparison_accepts_different_component_order(self):
        raw = descriptor([ace()])
        sd = Descriptor.parse(raw)
        body = sd.dacl + sd.owner + sd.group
        reordered = HEADER.pack(1, 0, sd.control, 20 + len(sd.dacl),
                                20 + len(sd.dacl) + len(sd.owner), 0, 20) + body
        self.assertEqual(signature(raw), signature(reordered))


if __name__ == "__main__":
    unittest.main()
