import struct
import unittest

from xbox_media_drive import DriveError
from xbox_media_drive.security import (APP_PACKAGES, AUTHENTICATED_USERS, EVERYONE, Descriptor, FULL, HEADER, MODIFY,
                                       acl_entries, grant, lost_auto_inherited, ntfs3g_dacl_write, signature)

SYSTEM = bytes.fromhex("010100000000000512000000")
BUILTIN_USERS = bytes.fromhex("01020000000000052000000021020000")


def ace(sid=SYSTEM, mask=FULL, flags=0, kind=0):
    return struct.pack("<BBHI", kind, flags, 8 + len(sid), mask) + sid


def descriptor(entries=None, control=0x9004, sacl=b""):
    acl = b"" if entries is None else struct.pack("<BBHHH", 2, 0, 8 + sum(map(len, entries)), len(entries), 0) + b"".join(entries)
    return Descriptor(1, 0, control, SYSTEM, SYSTEM, sacl, acl).encode()


class SecurityTests(unittest.TestCase):
    def test_dacl_write_requests_auto_inherited_only_when_present(self):
        windows = descriptor([ace()], control=0x9404)  # SELF_RELATIVE | PROTECTED | AUTO_INHERITED | PRESENT
        self.assertEqual(struct.unpack_from("<H", ntfs3g_dacl_write(windows), 2)[0], 0x9504)
        self.assertEqual(ntfs3g_dacl_write(windows)[4:], windows[4:])
        plain = descriptor([ace()])
        self.assertEqual(ntfs3g_dacl_write(plain), plain)
        # The request bit is not stored, so it does not change the descriptor's signature.
        self.assertEqual(signature(ntfs3g_dacl_write(windows)), signature(windows))

    def test_lost_auto_inherited_matches_only_that_difference(self):
        prepared = signature(grant(descriptor([ace()], control=0x9404), True))
        dropped = (prepared[0] & ~0x0400,) + prepared[1:]
        self.assertTrue(lost_auto_inherited(dropped, prepared))
        self.assertFalse(lost_auto_inherited(prepared, prepared))
        self.assertFalse(lost_auto_inherited(dropped[:4] + (b"",), prepared))
        plain = signature(grant(descriptor([ace()]), True))
        self.assertFalse(lost_auto_inherited(plain, plain))

    def test_grant_preserves_every_other_component_and_existing_ace(self):
        # SACL is opaque to the editor but must be preserved, including audit ACEs.
        audit = ace(kind=2)
        sacl = struct.pack("<BBHHH", 2, 0, 8 + len(audit), 1, 0) + audit
        raw = descriptor([ace()], control=0x9014, sacl=sacl)
        result = grant(raw, True)
        before, after = Descriptor.parse(raw), Descriptor.parse(result)
        self.assertEqual(before.control, after.control)
        self.assertEqual((before.owner, before.group, before.sacl), (after.owner, after.group, after.sacl))
        self.assertEqual(acl_entries(after.dacl), [ace(), ace(APP_PACKAGES, MODIFY, 3), ace(AUTHENTICATED_USERS, MODIFY, 3)])
        self.assertEqual(grant(result, True), result)

    def test_files_have_no_inheritance_flags_and_full_is_explicit(self):
        result = Descriptor.parse(grant(descriptor([]), False, "full"))
        self.assertEqual(acl_entries(result.dacl), [ace(APP_PACKAGES, FULL), ace(AUTHENTICATED_USERS, FULL)])

    def test_null_and_absent_dacl_are_never_restricted(self):
        for control in (0x8000, 0x8004):
            raw = descriptor(control=control)
            self.assertEqual(grant(raw, True), raw)

    def test_existing_app_permissions_are_retained(self):
        user = ace(AUTHENTICATED_USERS, FULL, 3)
        raw = descriptor([ace(APP_PACKAGES, FULL, 3), user])
        self.assertEqual(grant(raw, True), raw)
        read_only = descriptor([ace(APP_PACKAGES, 0x120089), user])
        result = acl_entries(Descriptor.parse(grant(read_only, False)).dacl)
        self.assertEqual(result[0], ace(APP_PACKAGES, 0x120089))
        self.assertEqual(result, [ace(APP_PACKAGES, 0x120089), user, ace(APP_PACKAGES, MODIFY)])

    def test_generic_rights_count_toward_an_existing_grant(self):
        # GENERIC_READ|WRITE|EXECUTE plus DELETE is exactly Modify once NTFS maps it for files.
        generic_modify = ace(APP_PACKAGES, 0xE0010000, 3)
        raw = descriptor([generic_modify, ace(AUTHENTICATED_USERS, FULL, 3)])
        self.assertEqual(grant(raw, True), raw)
        self.assertNotEqual(signature(grant(raw, True, "full")), signature(raw))
        generic_read = descriptor([ace(APP_PACKAGES, 0x80000000), ace(AUTHENTICATED_USERS, FULL)])
        self.assertNotEqual(signature(grant(generic_read, False)), signature(generic_read))

    def test_inherit_only_and_no_propagate_do_not_count_as_full_grant(self):
        for flags in (0x0B, 0x07, 0):
            raw = descriptor([ace(APP_PACKAGES, FULL, flags), ace(AUTHENTICATED_USERS, FULL, 3)])
            self.assertNotEqual(signature(grant(raw, True)), signature(raw))

    def test_explicit_grant_precedes_inherited_entries(self):
        inherited = ace(flags=0x10)
        result = acl_entries(Descriptor.parse(grant(descriptor([ace(), inherited]), True)).dacl)
        self.assertEqual(result, [ace(), ace(APP_PACKAGES, MODIFY, 3), ace(AUTHENTICATED_USERS, MODIFY, 3), inherited])

    def test_missing_user_grant_is_added_for_files_and_directories(self):
        for directory in (False, True):
            with self.subTest(directory=directory):
                flags = 3 if directory else 0
                app = ace(APP_PACKAGES, MODIFY, flags)
                result = grant(descriptor([app]), directory)
                self.assertEqual(acl_entries(Descriptor.parse(result).dacl), [app, ace(AUTHENTICATED_USERS, MODIFY, flags)])
                self.assertEqual(grant(result, directory), result)

    def test_everyone_or_authenticated_users_can_supply_user_rights(self):
        for sid in (EVERYONE, AUTHENTICATED_USERS):
            for directory in (False, True):
                with self.subTest(sid=sid, directory=directory):
                    raw = descriptor([ace(APP_PACKAGES, FULL, 3), ace(sid, FULL, 3)])
                    self.assertEqual(grant(raw, directory, "full"), raw)

    def test_user_rights_are_combined_across_both_sids(self):
        raw = descriptor([ace(APP_PACKAGES, MODIFY, 3), ace(EVERYONE, MODIFY & ~0x10000, 3),
                          ace(AUTHENTICATED_USERS, 0x10000, 3)])
        self.assertEqual(grant(raw, True), raw)

    def test_windows_default_user_directory_permissions_are_sufficient(self):
        # Inherit-only Modify reaches children; the explicit Modify covers this directory.
        raw = descriptor([ace(APP_PACKAGES, MODIFY, 3), ace(AUTHENTICATED_USERS, MODIFY, 0x0B),
                          ace(AUTHENTICATED_USERS, MODIFY)])
        self.assertEqual(grant(raw, True), raw)

    def test_inherited_child_permissions_count_for_both_sides(self):
        raw = descriptor([ace(APP_PACKAGES, MODIFY, 0x10), ace(AUTHENTICATED_USERS, MODIFY, 0x10)])
        self.assertEqual(grant(raw, False), raw)
        self.assertEqual(acl_entries(Descriptor.parse(grant(raw, True)).dacl),
                         [ace(APP_PACKAGES, MODIFY, 3), ace(AUTHENTICATED_USERS, MODIFY, 3)] + acl_entries(Descriptor.parse(raw).dacl))
        inheritable = descriptor([ace(APP_PACKAGES, MODIFY, 0x13), ace(AUTHENTICATED_USERS, MODIFY, 0x13)])
        self.assertEqual(grant(inheritable, True), inheritable)

    def test_builtin_users_do_not_supply_user_rights(self):
        entries = [ace(APP_PACKAGES, FULL, 3), ace(BUILTIN_USERS, MODIFY, 3)]
        result = acl_entries(Descriptor.parse(grant(descriptor(entries), True)).dacl)
        self.assertEqual(result, entries + [ace(AUTHENTICATED_USERS, MODIFY, 3)])

    def test_generic_user_rights_count_toward_existing_grant(self):
        for sid in (EVERYONE, AUTHENTICATED_USERS):
            for mask, access in ((0xE0010000, "modify"), (0x10000000, "full")):
                with self.subTest(sid=sid, access=access):
                    raw = descriptor([ace(APP_PACKAGES, FULL, 3), ace(sid, mask, 3)])
                    self.assertEqual(grant(raw, True, access), raw)
            entries = [ace(APP_PACKAGES, FULL), ace(sid, 0x80000000)]
            self.assertEqual(acl_entries(Descriptor.parse(grant(descriptor(entries), False)).dacl),
                             entries + [ace(AUTHENTICATED_USERS, MODIFY)])

    def test_user_inherit_only_and_no_propagate_require_a_grant(self):
        for flags in (0x0B, 0x07, 0):
            with self.subTest(flags=flags):
                entries = [ace(APP_PACKAGES, FULL, 3), ace(AUTHENTICATED_USERS, FULL, flags)]
                self.assertEqual(acl_entries(Descriptor.parse(grant(descriptor(entries), True)).dacl),
                                 entries + [ace(AUTHENTICATED_USERS, MODIFY, 3)])

    def test_full_adds_requested_mask_for_each_side_with_only_modify(self):
        for directory in (False, True):
            flags = 3 if directory else 0
            for app_mask, user_mask in ((MODIFY, FULL), (FULL, MODIFY), (MODIFY, MODIFY)):
                with self.subTest(directory=directory, app_mask=app_mask, user_mask=user_mask):
                    entries = [ace(APP_PACKAGES, app_mask, flags), ace(AUTHENTICATED_USERS, user_mask, flags)]
                    additions = [ace(sid, FULL, flags) for sid, mask in ((APP_PACKAGES, app_mask), (AUTHENTICATED_USERS, user_mask)) if mask != FULL]
                    self.assertEqual(acl_entries(Descriptor.parse(grant(descriptor(entries), directory, "full")).dacl), entries + additions)

    def test_satisfied_descriptor_is_byte_identical_with_nonstandard_layout(self):
        sd = Descriptor.parse(descriptor([ace(APP_PACKAGES, MODIFY, 3), ace(EVERYONE, MODIFY, 3)]))
        body = sd.dacl + sd.owner + b"\0" * 8
        raw = HEADER.pack(1, 0, sd.control, 20 + len(sd.dacl), 20 + len(sd.dacl), 0, 20) + body
        self.assertEqual(grant(raw, True), raw)

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
