/*
 * life — игра «Жизнь» на поле 10×10, которую считает RISC-V внутри USB-ЦАПа Moondrop DAWN PRO2.
 * ПК отправляет поле и число шагов, чип возвращает итоговое поле, эта программа его рисует.
 *
 *   ./life glider -n 4            готовый узор: glider, blinker, toad, beacon, rpent, random[:seed]
 *   ./life field.txt -n 100       поле из файла: 10 строк, живая клетка — # * o O X 1, мёртвая — . 0 пробел
 *   ./life - -n 5 < field.txt     поле из stdin
 *   ./life glider -a 40           анимация: 40 поколений, каждое посчитано на чипе (-t мс между кадрами)
 *   ./life glider -n 4000000000   миллиарды шагов: чип находит цикл и берёт остаток по модулю периода
 *   опции: --no-wrap (края мёртвые; по умолчанию тор), --plain (вывести итог текстом для файла), -d /dev/hidrawN
 *
 * Linux, без зависимостей: /dev/hidraw*. Протокол — в ../main.c.
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
#define CMD_LIFE   0xCB
#define N          10

typedef struct { uint8_t c[N][N]; } field_t;

static int find_device(char *path, size_t n)          /* hidraw ЦАПа с report ID 75 в дескрипторе */
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
            if (desc[i] == 0x85 && desc[i + 1] == REPORT_ID) { snprintf(path, n, "/dev/%s", e->d_name); found = 0; break; }
    }
    closedir(d);
    return found;
}

/* ---------------------------------------------------------------- поле */
static void put(field_t *f, int r, int c) { f->c[((r % N) + N) % N][((c % N) + N) % N] = 1; }

static int preset(const char *name, field_t *f)
{
    memset(f, 0, sizeof *f);
    if (!strcmp(name, "glider"))  { put(f,0,1); put(f,1,2); put(f,2,0); put(f,2,1); put(f,2,2); return 0; }
    if (!strcmp(name, "blinker")) { put(f,4,3); put(f,4,4); put(f,4,5); return 0; }
    if (!strcmp(name, "toad"))    { put(f,4,4); put(f,4,5); put(f,4,6); put(f,5,3); put(f,5,4); put(f,5,5); return 0; }
    if (!strcmp(name, "beacon"))  { put(f,2,2); put(f,2,3); put(f,3,2); put(f,4,5); put(f,5,4); put(f,5,5); return 0; }
    if (!strcmp(name, "rpent"))   { put(f,4,5); put(f,4,6); put(f,5,4); put(f,5,5); put(f,6,5); return 0; }
    if (!strncmp(name, "random", 6)) {
        unsigned seed = name[6] == ':' ? (unsigned)strtoul(name + 7, NULL, 0) : (unsigned)time(NULL);
        srand(seed);
        for (int r = 0; r < N; r++) for (int c = 0; c < N; c++) f->c[r][c] = rand() % 100 < 35;
        fprintf(stderr, "random: seed %u\n", seed);
        return 0;
    }
    return -1;
}

static int load(const char *path, field_t *f)
{
    FILE *in = strcmp(path, "-") ? fopen(path, "r") : stdin;
    if (!in) { fprintf(stderr, "%s: %s\n", path, strerror(errno)); return -1; }
    memset(f, 0, sizeof *f);
    char line[256];
    for (int r = 0; r < N && fgets(line, sizeof line, in); r++)
        for (int c = 0; c < N && line[c] && line[c] != '\n'; c++)
            f->c[r][c] = strchr("#*oOxX1@", line[c]) != NULL;
    if (in != stdin) fclose(in);
    return 0;
}

static void pack(const field_t *f, uint8_t *b)
{
    memset(b, 0, 13);
    for (int k = 0; k < N * N; k++) if (f->c[k / N][k % N]) b[k >> 3] |= (uint8_t)(1u << (k & 7));
}

static void unpack(const uint8_t *b, field_t *f)
{
    for (int k = 0; k < N * N; k++) f->c[k / N][k % N] = b[k >> 3] >> (k & 7) & 1;
}

static void pad(const char *s, int width)            /* выравнивание по буквам, а не байтам UTF-8 */
{
    int n = 0;
    for (const char *q = s; *q; q++) n += ((unsigned char)*q & 0xc0) != 0x80;
    printf("%s%*s", s, width > n ? width - n : 0, "");
}

static void draw(const field_t *a, const char *ta, const field_t *b, const char *tb)
{
    printf("  ");
    pad(ta, 22);
    if (b) printf("      %s", tb);
    printf("\n  ┌────────────────────┐");
    if (b) printf("      ┌────────────────────┐");
    printf("\n");
    for (int r = 0; r < N; r++) {
        printf("  │");
        for (int c = 0; c < N; c++) printf("%s", a->c[r][c] ? "██" : "··");
        printf("│");
        if (b) {
            printf(r == N / 2 - 1 ? "  ──► " : "      ");
            printf("│");
            for (int c = 0; c < N; c++) printf("%s", b->c[r][c] ? "██" : "··");
            printf("│");
        }
        printf("\n");
    }
    printf("  └────────────────────┘");
    if (b) printf("      └────────────────────┘");
    printf("\n");
}

/* ---------------------------------------------------------------- обмен с чипом */
typedef struct { uint32_t us, period, found_at; uint8_t pop; } stats_t;

static int life_on_chip(int fd, const field_t *in, uint32_t steps, int wrap, field_t *out, stats_t *st)
{
    static uint8_t seq;
    seq++;
    uint8_t req[64] = { REPORT_ID, CMD_LIFE, seq, (uint8_t)(wrap ? 0 : 1) };
    memcpy(req + 4, &steps, 4);
    pack(in, req + 8);
    if (write(fd, req, sizeof req) != (ssize_t)sizeof req) { perror("запись в ЦАП"); return -1; }
    for (;;) {
        struct pollfd pfd = { fd, POLLIN, 0 };
        if (poll(&pfd, 1, 30000) <= 0) { fprintf(stderr, "ЦАП не ответил за 30 с. Прошит fw_life.bin?\n"); return -1; }
        uint8_t r[65];
        ssize_t n = read(fd, r, sizeof r);
        if (n < 40 || r[0] != REPORT_ID || r[1] != CMD_LIFE || r[2] != seq) continue;   /* чужой отчёт */
        unpack(r + 8, out);
        memcpy(&st->us, r + 24, 4);
        memcpy(&st->period, r + 32, 4);
        memcpy(&st->found_at, r + 36, 4);
        st->pop = r[28];
        return 0;
    }
}

static void usage(void)
{
    fprintf(stderr, "использование: life [glider|blinker|toad|beacon|rpent|random[:seed]|файл|-] [-n шагов] [-a кадров [-t мс]]\n"
                    "                    [--no-wrap] [--plain] [-d /dev/hidrawN]\n");
}

int main(int argc, char **argv)
{
    const char *src = "glider";
    char dev[300] = "";
    unsigned long steps = 1, frames = 0, delay = 150;
    int wrap = 1, plain = 0;
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "-n") && i + 1 < argc) steps = strtoul(argv[++i], NULL, 0);
        else if (!strcmp(argv[i], "-a") && i + 1 < argc) frames = strtoul(argv[++i], NULL, 0);
        else if (!strcmp(argv[i], "-t") && i + 1 < argc) delay = strtoul(argv[++i], NULL, 0);
        else if (!strcmp(argv[i], "-d") && i + 1 < argc) snprintf(dev, sizeof dev, "%s", argv[++i]);
        else if (!strcmp(argv[i], "--no-wrap")) wrap = 0;
        else if (!strcmp(argv[i], "--plain")) plain = 1;
        else if (!strcmp(argv[i], "-h") || !strcmp(argv[i], "--help")) { usage(); return 0; }
        else src = argv[i];
    }
    if (steps > 0xffffffffUL) { fprintf(stderr, "шагов не больше 4294967295\n"); return 2; }

    field_t f, out;
    if (preset(src, &f) && load(src, &f)) { usage(); return 2; }
    if (!*dev && find_device(dev, sizeof dev)) {
        fprintf(stderr, "ЦАП %04x:%04x не найден. Подключён? Не в режиме загрузчика?\n", VID, PID);
        return 2;
    }
    int fd = open(dev, O_RDWR);
    if (fd < 0) { fprintf(stderr, "%s: %s (нужно udev-правило из udev/70-moondrop-dac.rules)\n", dev, strerror(errno)); return 2; }

    stats_t st;
    if (frames) {                                      /* анимация: каждое поколение — отдельный запрос */
        printf("\033[2J");
        for (unsigned long g = 0; g <= frames; g++) {
            char t[64];
            snprintf(t, sizeof t, "поколение %lu%s", g, wrap ? " (тор)" : "");
            printf("\033[H");
            draw(&f, t, NULL, NULL);
            fflush(stdout);
            if (g == frames) break;
            if (life_on_chip(fd, &f, 1, wrap, &out, &st)) return 1;
            f = out;
            usleep(delay * 1000);
        }
        close(fd);
        return 0;
    }

    if (life_on_chip(fd, &f, (uint32_t)steps, wrap, &out, &st)) return 1;
    close(fd);
    if (plain) {
        for (int r = 0; r < N; r++) { for (int c = 0; c < N; c++) putchar(out.c[r][c] ? '#' : '.'); putchar('\n'); }
        return 0;
    }
    char ta[64], tb[64];
    snprintf(ta, sizeof ta, "вход%s", wrap ? " (тор)" : " (края мёртвые)");
    snprintf(tb, sizeof tb, "после %lu шаг%s", steps, steps % 10 == 1 && steps % 100 != 11 ? "а" : "ов");
    draw(&f, ta, &out, tb);
    printf("  посчитано на RISC-V в ЦАПе за %u мкс, живых клеток %u", st.us, st.pop);
    if (st.period) printf(", цикл с периодом %u найден на шаге %u", st.period, st.found_at);
    printf("\n");
    return 0;
}
