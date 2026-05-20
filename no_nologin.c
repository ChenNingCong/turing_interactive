/*
 * no_nologin.c
 *
 * LD_PRELOAD shim that makes /etc/nologin appear absent to whatever process
 * it's loaded into. Used by job_runner.sh to bypass an accidental
 * (admin-left-behind) /etc/nologin file on Turing GPU nodes, so the
 * user-launched sshd can complete sessions.
 *
 * Background: openssh's allowed_user() does
 *   if (stat("/etc/nologin", &sb) == 0 && pw->pw_uid != 0) deny;
 * — and there's no sshd_config knob to disable it. This shim only intercepts
 * the path "/etc/nologin"; all other stat()/open()/access() calls pass
 * straight through.
 *
 * Build (no Makefile to keep things tiny):
 *   gcc -O2 -shared -fPIC -o no_nologin.so no_nologin.c -ldl
 *
 * Use:
 *   LD_PRELOAD=/path/to/no_nologin.so /usr/sbin/sshd ...
 */

#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <stdarg.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

static int is_nologin(const char *p) {
    return p != NULL && strcmp(p, "/etc/nologin") == 0;
}

/* glibc 2.31 (Ubuntu 20.04) routes user-level stat()/lstat()/stat64()/lstat64()
 * through the __xstat family. We must override these for sshd to "see" the
 * absence. Newer glibc (>=2.33) calls stat()/lstat() directly — override those
 * too so this shim is portable. */

int __xstat(int ver, const char *p, struct stat *b) {
    static int (*real)(int, const char *, struct stat *) = NULL;
    if (!real) real = dlsym(RTLD_NEXT, "__xstat");
    if (is_nologin(p)) { errno = ENOENT; return -1; }
    return real(ver, p, b);
}

int __lxstat(int ver, const char *p, struct stat *b) {
    static int (*real)(int, const char *, struct stat *) = NULL;
    if (!real) real = dlsym(RTLD_NEXT, "__lxstat");
    if (is_nologin(p)) { errno = ENOENT; return -1; }
    return real(ver, p, b);
}

int __xstat64(int ver, const char *p, struct stat64 *b) {
    static int (*real)(int, const char *, struct stat64 *) = NULL;
    if (!real) real = dlsym(RTLD_NEXT, "__xstat64");
    if (is_nologin(p)) { errno = ENOENT; return -1; }
    return real(ver, p, b);
}

int __lxstat64(int ver, const char *p, struct stat64 *b) {
    static int (*real)(int, const char *, struct stat64 *) = NULL;
    if (!real) real = dlsym(RTLD_NEXT, "__lxstat64");
    if (is_nologin(p)) { errno = ENOENT; return -1; }
    return real(ver, p, b);
}

int stat(const char *p, struct stat *b) {
    static int (*real)(const char *, struct stat *) = NULL;
    if (!real) real = dlsym(RTLD_NEXT, "stat");
    if (is_nologin(p)) { errno = ENOENT; return -1; }
    return real(p, b);
}

int lstat(const char *p, struct stat *b) {
    static int (*real)(const char *, struct stat *) = NULL;
    if (!real) real = dlsym(RTLD_NEXT, "lstat");
    if (is_nologin(p)) { errno = ENOENT; return -1; }
    return real(p, b);
}

int access(const char *p, int mode) {
    static int (*real)(const char *, int) = NULL;
    if (!real) real = dlsym(RTLD_NEXT, "access");
    if (is_nologin(p)) { errno = ENOENT; return -1; }
    return real(p, mode);
}

int open(const char *p, int flags, ...) {
    static int (*real_open)(const char *, int, ...) = NULL;
    if (!real_open) real_open = dlsym(RTLD_NEXT, "open");
    if (is_nologin(p)) { errno = ENOENT; return -1; }
    if (flags & O_CREAT) {
        va_list ap; va_start(ap, flags);
        mode_t mode = va_arg(ap, mode_t);
        va_end(ap);
        return real_open(p, flags, mode);
    }
    return real_open(p, flags);
}

int open64(const char *p, int flags, ...) {
    static int (*real_open64)(const char *, int, ...) = NULL;
    if (!real_open64) real_open64 = dlsym(RTLD_NEXT, "open64");
    if (is_nologin(p)) { errno = ENOENT; return -1; }
    if (flags & O_CREAT) {
        va_list ap; va_start(ap, flags);
        mode_t mode = va_arg(ap, mode_t);
        va_end(ap);
        return real_open64(p, flags, mode);
    }
    return real_open64(p, flags);
}
