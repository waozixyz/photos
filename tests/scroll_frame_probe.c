/* Test-only cadence probe. Both comparison bundles use the same SDL hook. */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <fcntl.h>
#include <link.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/time.h>
#include <time.h>
#include <ucontext.h>
#include <unistd.h>

static FILE *output;
static FILE *poll_output;
static double previous;
static int samples = -1;
static uintptr_t image_base;

static int executable_base(struct dl_phdr_info *info, size_t size, void *context) {
    (void)size;
    (void)context;
    if (!info->dlpi_name || !*info->dlpi_name) {
        image_base = info->dlpi_addr;
        return 1;
    }
    return 0;
}

static int record_image(struct dl_phdr_info *info, size_t size, void *context) {
    (void)size;
    FILE *images = context;
    for (unsigned i = 0; i < info->dlpi_phnum; i++) {
        const ElfW(Phdr) *part = &info->dlpi_phdr[i];
        if (part->p_type != PT_LOAD || !(part->p_flags & PF_X)) continue;
        fprintf(images, "%llx %llx %llx %s\n",
                (unsigned long long)(info->dlpi_addr + part->p_vaddr - image_base),
                (unsigned long long)(info->dlpi_addr + part->p_vaddr + part->p_memsz - image_base),
                (unsigned long long)(info->dlpi_addr - image_base),
                info->dlpi_name && *info->dlpi_name ? info->dlpi_name : "executable");
    }
    return 0;
}

static void sample_cpu(int signal, siginfo_t *info, void *context) {
    (void)signal;
    (void)info;
#if defined(__x86_64__)
    const ucontext_t *state = context;
    uint64_t offset = (uintptr_t)state->uc_mcontext.gregs[REG_RIP] - image_base;
    if (samples >= 0) {
        ssize_t result = write(samples, &offset, sizeof(offset));
        (void)result;
    }
#else
    (void)context;
#endif
}

__attribute__((constructor)) static void start_cpu_samples(void) {
    const char *path = getenv("PHOTOS_CPU_PROBE");
    if (!path) return;
    samples = open(path, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0600);
    if (samples < 0) return;
    dl_iterate_phdr(executable_base, NULL);
    char images_path[4096];
    int length = snprintf(images_path, sizeof(images_path), "%s.images", path);
    if (length > 0 && length < (int)sizeof(images_path)) {
        FILE *images = fopen(images_path, "w");
        if (images) {
            dl_iterate_phdr(record_image, images);
            fclose(images);
        }
    }
    struct sigaction action = {0};
    action.sa_sigaction = sample_cpu;
    action.sa_flags = SA_SIGINFO | SA_RESTART;
    sigemptyset(&action.sa_mask);
    if (sigaction(SIGPROF, &action, NULL) != 0) return;
    struct itimerval timer = {{0, 1000}, {0, 1000}};
    setitimer(ITIMER_PROF, &timer, NULL);
}

void SDL_RenderPresent(void *renderer) {
    static void (*present)(void *);
    if (!present) present = dlsym(RTLD_NEXT, "SDL_RenderPresent");
    if (!present) return;
    present(renderer);
    if (!output) {
        const char *path = getenv("PHOTOS_FRAME_PROBE");
        if (path) output = fopen(path, "w");
    }
    struct timespec clock;
    clock_gettime(CLOCK_MONOTONIC, &clock);
    double current = clock.tv_sec + clock.tv_nsec * 1e-9;
    if (output) {
        fprintf(output, "%.9f %.6f\n", current, previous ? 1000 * (current - previous) : 0);
        fflush(output);
    }
    previous = current;
}

/* A stable window can continue polling timers without presenting its pixels.
 * Record drained event polls separately so timer checks measure that cadence. */
int SDL_PollEvent(void *event) {
    static int (*poll)(void *);
    if (!poll) poll = dlsym(RTLD_NEXT, "SDL_PollEvent");
    if (!poll) return 0;
    int result = poll(event);
    const char *path = getenv("PHOTOS_POLL_PROBE");
    if (!result && path) {
        if (!poll_output) poll_output = fopen(path, "w");
        if (poll_output) {
            struct timespec clock;
            clock_gettime(CLOCK_MONOTONIC, &clock);
            fprintf(poll_output, "%.9f\n", clock.tv_sec + clock.tv_nsec * 1e-9);
            fflush(poll_output);
        }
    }
    return result;
}
