/*
 * calc — калькулятор, который считает на RISC-V внутри USB-ЦАПа Moondrop DAWN PRO2.
 * ПК только передаёт выражение и печатает ответ; разбирает и считает его прошивка (../main.c).
 *
 *   ./calc "2+2*2"            одно выражение
 *   ./calc 2 + 2 '*' 2        то же, аргументы склеиваются
 *   ./calc                    интерактивный режим (пустая строка или Ctrl+D — выход)
 *   ./calc -d /dev/hidraw3 …  указать устройство вручную
 *
 * Linux, без зависимостей: /dev/hidraw*. Доступ даёт udev-правило из udev/70-moondrop-dac.rules.
 * Протокол — в ../main.c.
 */
#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#define VID        0x35d8
#define PID        0x011d
#define REPORT_ID  75
#define CMD_CALC   0xCA
#define MAX_EXPR   60
#define CPU_HZ     360000000.0

static const char *status_text[] = {
    "ok", "синтаксическая ошибка", "деление на ноль", "слишком длинно или глубоко", "вне области определения",
};

/* hidraw-узел ЦАПа, у которого в дескрипторе есть report ID 75 (vendor-интерфейс) */
static int find_device(char *path, size_t n)
{
    DIR *d = opendir("/sys/class/hidraw");
    if (!d) return -1;
    struct dirent *e;
    int found = -1;
    while ((e = readdir(d)) && found < 0) {
        if (strncmp(e->d_name, "hidraw", 6)) continue;
        char f[512], line[256];
        snprintf(f, sizeof f, "/sys/class/hidraw/%s/device/uevent", e->d_name);
        FILE *u = fopen(f, "r");
        if (!u) continue;
        unsigned bus, vid = 0, pid = 0;
        while (fgets(line, sizeof line, u))
            if (sscanf(line, "HID_ID=%x:%x:%x", &bus, &vid, &pid) == 3) break;
        fclose(u);
        if (vid != VID || pid != PID) continue;
        snprintf(f, sizeof f, "/sys/class/hidraw/%s/device/report_descriptor", e->d_name);
        FILE *r = fopen(f, "rb");
        if (!r) continue;
        unsigned char desc[4096];
        size_t len = fread(desc, 1, sizeof desc, r);
        fclose(r);
        for (size_t i = 0; i + 1 < len; i++)
            if (desc[i] == 0x85 && desc[i + 1] == REPORT_ID) {   /* Report ID (75) */
                snprintf(path, n, "/dev/%s", e->d_name);
                found = 0;
                break;
            }
    }
    closedir(d);
    return found;
}

static int calc_on_chip(int fd, const char *expr, uint8_t seq)
{
    size_t len = strlen(expr);
    if (len > MAX_EXPR) {
        fprintf(stderr, "слишком длинное выражение (%zu > %d символов)\n", len, MAX_EXPR);
        return 1;
    }
    uint8_t req[64] = { REPORT_ID, CMD_CALC, seq, (uint8_t)len };
    memcpy(req + 4, expr, len);
    if (write(fd, req, sizeof req) != (ssize_t)sizeof req) {
        perror("запись в ЦАП");
        return 1;
    }

    struct timespec t0, t1;
    clock_gettime(CLOCK_MONOTONIC, &t0);
    for (;;) {
        struct pollfd pfd = { fd, POLLIN, 0 };
        int r = poll(&pfd, 1, 1000);
        if (r <= 0) {
            fprintf(stderr, "ЦАП не ответил за 1 с. Прошит fw_calc.bin?\n");
            return 1;
        }
        uint8_t in[65];
        ssize_t n = read(fd, in, sizeof in);
        if (n < 24) continue;
        if (in[0] != REPORT_ID || in[1] != CMD_CALC || in[2] != seq) continue;   /* чужой отчёт */
        clock_gettime(CLOCK_MONOTONIC, &t1);
        double rtt = (t1.tv_sec - t0.tv_sec) * 1e3 + (t1.tv_nsec - t0.tv_nsec) / 1e6;

        uint8_t st = in[3], pos = in[4];
        double result;
        uint32_t cycles, count;
        memcpy(&result, in + 8, 8);
        memcpy(&cycles, in + 16, 4);
        memcpy(&count, in + 20, 4);
        if (st) {
            fprintf(stderr, "ошибка: %s\n  %s\n  %*s^\n", st < 5 ? status_text[st] : "?", expr, pos, "");
            return 1;
        }
        printf("= %.15g\n", result);
        printf("  посчитано на RISC-V в ЦАПе: %u тактов (%.1f мкс), вычисление №%u, USB туда-обратно %.1f мс\n",
               cycles, cycles / CPU_HZ * 1e6, count, rtt);
        return 0;
    }
}

int main(int argc, char **argv)
{
    char dev[300] = "";
    int a = 1;
    if (argc > 2 && !strcmp(argv[1], "-d")) { snprintf(dev, sizeof dev, "%s", argv[2]); a = 3; }
    if (!*dev && find_device(dev, sizeof dev)) {
        fprintf(stderr, "ЦАП %04x:%04x не найден. Подключён? Не в режиме загрузчика?\n", VID, PID);
        return 2;
    }
    int fd = open(dev, O_RDWR);
    if (fd < 0) {
        fprintf(stderr, "%s: %s (нужно udev-правило из udev/70-moondrop-dac.rules)\n", dev, strerror(errno));
        return 2;
    }

    uint8_t seq = (uint8_t)time(NULL);
    if (a < argc) {                                   /* выражение из аргументов */
        char expr[256] = "";
        for (int i = a; i < argc; i++) {
            strncat(expr, argv[i], sizeof expr - strlen(expr) - 2);
            if (i + 1 < argc) strcat(expr, " ");
        }
        int rc = calc_on_chip(fd, expr, ++seq);
        close(fd);
        return rc;
    }

    printf("Калькулятор на ЦАПе (%s). Операции: + - * / %% ^ ( ), sqrt(x), pi, e. Пустая строка — выход.\n", dev);
    char line[256];
    for (;;) {
        printf("> ");
        fflush(stdout);
        if (!fgets(line, sizeof line, stdin)) break;
        line[strcspn(line, "\r\n")] = 0;
        if (!*line) break;
        calc_on_chip(fd, line, ++seq);
    }
    close(fd);
    return 0;
}
