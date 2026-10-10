/* Test-only allocator accounting for repeated native/JNI staging work. */
#define _GNU_SOURCE
#include "backup_staging.h"
#include <fcntl.h>
#include <malloc.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

struct Permission {
    unsigned calls;
    unsigned limit;
    bool cancel;
};

static bool current(void *context) {
    struct Permission *permission = context;
    permission->calls++;
    return !permission->cancel || permission->calls < permission->limit;
}

static bool stage(int fd, int64_t size, String root, bool cancel) {
    unsigned char account[32] = {0}, key[32] = {0};
    struct Permission permission = {0, size ? 4 : 2, cancel};
    String result = backup_staging_Stage(fd, size, root,
        (Slice){account, sizeof(account)}, (Slice){key, sizeof(key)},
        &permission, current);
    bool valid = cancel ? result.length == 0 : result.length != 0;
    if (result.length) free((void *)result.data);
    return valid && permission.calls == permission.limit;
}

int main(int argc, char **argv) {
    if (argc != 4) return 2;
    int fd = open(argv[1], O_RDONLY | O_CLOEXEC);
    if (fd < 0) return 2;
    struct stat source;
    if (fstat(fd, &source) != 0 || source.st_size < 0 || source.st_size > 4096) return 2;
    String root = {argv[2], strlen(argv[2])};
    bool cancel = argv[3][0] == '1';
    for (unsigned i = 0; i < 16; i++) {
        if (!stage(fd, source.st_size, root, cancel)) return 2;
    }
    size_t before = mallinfo2().uordblks;
    for (unsigned i = 0; i < 128; i++) {
        if (!stage(fd, source.st_size, root, cancel)) return 2;
    }
    size_t after = mallinfo2().uordblks;
    close(fd);
    size_t retained = after > before ? after - before : 0;
    printf("retained_bytes=%zu\n", retained);
    return retained > 32768;
}
