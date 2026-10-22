/* Test-only NTFS-3G context constructor for disposable regular-file images.
 *
 * The public initializer requires UID 0 even for an image owned by the caller.
 * This harness uses the public structs/mount API to exercise REAL NTFS reads,
 * writes, enumeration and flushes without root. It refuses all device files.
 * It is never loaded or shipped as part of the application.
 * SPDX-License-Identifier: GPL-2.0-or-later
 */
#include <errno.h>
#include <stdarg.h>
#include <stdlib.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>
#include <ntfs-3g/volume.h>
#include <ntfs-3g/attrib.h>
#include <ntfs-3g/security.h>
#include <ntfs-3g/unistr.h>

struct SECURITY_API *xmd_image_initialize(const char *path, unsigned long flags)
{
    struct stat st;
    if (stat(path, &st) || !S_ISREG(st.st_mode) || st.st_uid != getuid()) {
        errno = EPERM;
        return NULL;
    }
    ntfs_volume *vol = ntfs_mount(path, flags);
    if (!vol)
        return NULL;
    struct SECURITY_API *api = calloc(1, sizeof(*api));
    if (!api || ntfs_volume_get_free_space(vol)) {
        free(api);
        ntfs_umount(vol, 0);
        return NULL;
    }
    api->magic = MAGIC_API;
    api->security.vol = vol;
    api->security.uid = getuid();
    api->security.gid = getgid();
    api->security.pseccache = &api->seccache;
    vol->secure_flags = 0;
    ntfs_build_mapping(&api->security, NULL, TRUE);
    return api;
}

/* Fixture helpers: the caller only holds a regular-file image context above. */
int xmd_fixture_directory(struct SECURITY_API *api, const char *name)
{
    ntfschar *unicode = NULL;
    int length = ntfs_mbstoucs(name, &unicode);
    if (length < 0)
        return -1;
    ntfs_inode *root = ntfs_pathname_to_inode(api->security.vol, NULL, "/");
    if (!root) {
        free(unicode);
        return -1;
    }
    ntfs_inode *created = ntfs_create(root, root->security_id, unicode, length, S_IFDIR);
    free(unicode);
    int result = created ? ntfs_inode_close(created) : -1;
    if (ntfs_inode_close(root))
        result = -1;
    return result;
}

int xmd_fixture_link(struct SECURITY_API *api, const char *existing, const char *name)
{
    ntfschar *unicode = NULL;
    int length = ntfs_mbstoucs(name, &unicode);
    if (length < 0)
        return -1;
    ntfs_inode *root = ntfs_pathname_to_inode(api->security.vol, NULL, "/");
    ntfs_inode *file = ntfs_pathname_to_inode(api->security.vol, NULL, existing);
    int result = -1;
    if (root && file)
        result = ntfs_link(file, root, unicode, length);
    free(unicode);
    if (file && ntfs_inode_close(file))
        result = -1;
    if (root && ntfs_inode_close(root))
        result = -1;
    return result;
}

/* Invalidate restart pages without making the journal an empty attribute. */
int xmd_fixture_open_journal(struct SECURITY_API *api)
{
    char zeros[4096] = {0};
    ntfs_inode *inode = ntfs_inode_open(api->security.vol, FILE_LogFile);
    if (!inode)
        return -1;
    ntfs_attr *attr = ntfs_attr_open(inode, AT_DATA, AT_UNNAMED, 0);
    int result = -1;
    if (attr) {
        result = ntfs_attr_pwrite(attr, 0, sizeof(zeros), zeros) == sizeof(zeros) ? 0 : -1;
        ntfs_attr_close(attr);
    }
    if (ntfs_inode_close(inode))
        result = -1;
    return result;
}
