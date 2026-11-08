/*
 * spv_rt.c — рантайм SDK: заголовок образа, крючки в прошивку, crt-инициализация, время, LED, журнал.
 *
 * Крючки (ставит spv_build.py, flash-код main(), 4 Б -> 4 Б):
 *   0x48002d88  jalr ra,0xf4(zero)   (init прошивки)       ->  jal ra,__spv_init_hook
 *   0x48002d8c  jalr ra,0x360(zero)  (тело главного цикла) ->  jal ra,__spv_loop_hook
 * Тело главного цикла 0x360 переписано здесь на C один в один, плюс вызов app_loop()
 * и возможность отключить заводскую логику LED.
 */
#include <stdarg.h>
#include "spv.h"

/* ---------------------------------------------------------------- заголовок образа */
#define SPV_APP_MAGIC 0x41565053u   /* "SPVA" */
void __spv_init_hook(void);
void __spv_loop_hook(void);

struct spv_app_header {
    uint32_t magic;
    uint32_t base_cpu_size;   /* размер секции CPU до вставки программы (заполняет spv_build.py) */
    void (*init_hook)(void);
    void (*loop_hook)(void);
};
__attribute__((section(".spv_header"), used))
const struct spv_app_header __spv_header = { SPV_APP_MAGIC, 0, __spv_init_hook, __spv_loop_hook };

/* ---------------------------------------------------------------- функции прошивки */
#define FW_INIT          ((uint32_t (*)(void))0xf4)
#define FW_AUDIO_TASK    ((void (*)(void))0x1138)
#define FW_LED_TASK      ((void (*)(void *, void *, void *))0x5fc)
#define FW_LED_PADS_OFF  ((void (*)(uint32_t, uint32_t, uint32_t))0x48002e70)

/* ---------------------------------------------------------------- crt */
extern uint32_t __data_load[], __data_start[], __data_end[], __bss_start[], __bss_end[];

static bool led_owned;
static uint64_t t0_cycles;
static uint32_t cyc_per_ms, cyc_per_us;

__attribute__((weak)) void app_init(void) {}
__attribute__((weak)) void app_loop(void) {}

void __spv_init_hook(void)
{
    /* сначала прошивка (тактирование, USB, аудио) — как в заводском main() */
    FW_INIT();

    /* .data/.bss: RAM 0x130000.. прошивка не трогает, при старте там мусор */
    for (uint32_t *s = __data_load, *d = __data_start; d < __data_end;) *d++ = *s++;
    for (uint32_t *d = __bss_start; d < __bss_end;) *d++ = 0;

    uint32_t hz = spv_cpu_hz();
    cyc_per_ms = hz / 1000u ? hz / 1000u : 1u;
    cyc_per_us = hz / 1000000u ? hz / 1000000u : 1u;
    t0_cycles = spv_cycles();
    spv_log("spv: init, cpu %u Hz\n", hz);
    app_init();
}

void __spv_loop_hook(void)
{
    /* == 0x360 */
    FW_AUDIO_TASK();
    if (!led_owned) {
        if (SPV_LED_ENABLED)
            FW_LED_TASK((void *)0x11c090, (void *)0x11cb34, (void *)0x1064b8);
        else
            FW_LED_PADS_OFF(0, 0, 0);
    }
    app_loop();
}

/* ---------------------------------------------------------------- время */
uint64_t spv_cycles(void)
{
    uint32_t hi, lo, hi2;
    do {
        __asm__ volatile ("csrr %0, mcycleh" : "=r"(hi));
        __asm__ volatile ("csrr %0, mcycle"  : "=r"(lo));
        __asm__ volatile ("csrr %0, mcycleh" : "=r"(hi2));
    } while (hi != hi2);
    return ((uint64_t)hi << 32) | lo;
}

uint32_t spv_millis(void) { return (uint32_t)((spv_cycles() - t0_cycles) / cyc_per_ms); }
uint32_t spv_micros(void) { return (uint32_t)((spv_cycles() - t0_cycles) / cyc_per_us); }

/* ---------------------------------------------------------------- светодиод */
void spv_led_claim(bool own)
{
    led_owned = own;
    if (!own) SPV_LED_EVENT = 0;   /* заводская логика продолжит с обычного состояния */
}

bool spv_led_claimed(void) { return led_owned; }

static inline uint32_t duty(uint8_t v) { return SPV_LED_ACTIVE_LOW ? 255u - v : v; }

void spv_led_rgb(uint8_t r, uint8_t g, uint8_t b)
{
    spv_fw_pwm_set(SPV_LED_PIN_B, 1, duty(b));
    spv_fw_pwm_set(SPV_LED_PIN_R, 1, duty(r));
    spv_fw_pwm_set(SPV_LED_PIN_G, 1, duty(g));
}

void spv_led_off(void)
{
    SPV_PAD(SPV_LED_PIN_R) = SPV_PAD_OFF;
    SPV_PAD(SPV_LED_PIN_G) = SPV_PAD_OFF;
    SPV_PAD(SPV_LED_PIN_B) = SPV_PAD_OFF;
}

void spv_hsv_to_rgb(uint16_t h, uint8_t s, uint8_t v, uint8_t *r, uint8_t *g, uint8_t *b)
{
    h %= 1536u;
    uint32_t sec = h >> 8, f = h & 255u;
    uint32_t p = v * (255u - s) / 255u;
    uint32_t q = v * (255u - s * f / 255u) / 255u;
    uint32_t t = v * (255u - s * (255u - f) / 255u) / 255u;
    uint32_t R, G, B;
    switch (sec) {
    case 0:  R = v; G = t; B = p; break;
    case 1:  R = q; G = v; B = p; break;
    case 2:  R = p; G = v; B = t; break;
    case 3:  R = p; G = q; B = v; break;
    case 4:  R = t; G = p; B = v; break;
    default: R = v; G = p; B = q; break;
    }
    *r = (uint8_t)R; *g = (uint8_t)G; *b = (uint8_t)B;
}

/* ---------------------------------------------------------------- перехват RAM-функций */
bool spv_ram_redirect(uint32_t addr, uint32_t orig0, uint32_t orig1, const void *target)
{
    volatile uint32_t *p = (volatile uint32_t *)(uintptr_t)addr;
    if (addr & 3u) return false;
    uint32_t off = (uint32_t)(uintptr_t)target - addr;
    uint32_t auipc = ((off + 0x800u) & 0xfffff000u) | (5u << 7) | 0x17u;      /* auipc t0, hi   */
    uint32_t jalr  = ((off & 0xfffu) << 20) | (5u << 15) | 0x67u;            /* jalr zero,lo(t0) */
    if (p[0] == auipc && p[1] == jalr) return true;                          /* уже стоит */
    if (p[0] != orig0 || p[1] != orig1) return false;
    uint32_t mst;
    __asm__ volatile ("csrrci %0, mstatus, 8" : "=r"(mst));                  /* без прерываний */
    p[1] = jalr;                                                             /* вход — последним */
    p[0] = auipc;
    __asm__ volatile (".word 0x0000100f" ::: "memory");   /* fence.i */
    if (mst & 8u) __asm__ volatile ("csrsi mstatus, 8");
    return true;
}

/* ---------------------------------------------------------------- журнал */
#define SPV_DBG_SIZE 2048u
char spv_dbg_buf[SPV_DBG_SIZE];
uint32_t spv_dbg_pos;              /* всего записано символов (позиция = pos % size) */

static void dbg_putc(char c) { spv_dbg_buf[spv_dbg_pos++ % SPV_DBG_SIZE] = c; }
static void dbg_puts(const char *s) { while (*s) dbg_putc(*s++); }

static void dbg_num(uint32_t v, unsigned base, int width, char pad, bool neg)
{
    char tmp[12]; int n = 0;
    do { unsigned d = v % base; tmp[n++] = (char)(d < 10 ? '0' + d : 'a' + d - 10); v /= base; } while (v);
    if (neg) tmp[n++] = '-';
    while (n < width) { dbg_putc(pad); width--; }
    while (n) dbg_putc(tmp[--n]);
}

void spv_log(const char *fmt, ...)
{
    va_list ap; va_start(ap, fmt);
    for (; *fmt; fmt++) {
        if (*fmt != '%') { dbg_putc(*fmt); continue; }
        char pad = ' '; int width = 0;
        if (*++fmt == '0') { pad = '0'; fmt++; }
        while (*fmt >= '0' && *fmt <= '9') width = width * 10 + (*fmt++ - '0');
        switch (*fmt) {
        case 'd': { int32_t v = va_arg(ap, int32_t);
                    dbg_num(v < 0 ? (uint32_t)-v : (uint32_t)v, 10, width, pad, v < 0); break; }
        case 'u': dbg_num(va_arg(ap, uint32_t), 10, width, pad, false); break;
        case 'x': dbg_num(va_arg(ap, uint32_t), 16, width, pad, false); break;
        case 's': dbg_puts(va_arg(ap, const char *)); break;
        case 'c': dbg_putc((char)va_arg(ap, int)); break;
        case '%': dbg_putc('%'); break;
        case 0: va_end(ap); return;
        default: dbg_putc('%'); dbg_putc(*fmt); break;
        }
    }
    va_end(ap);
}

/* ---------------------------------------------------------------- libc (собирается с -fno-tree-loop-distribute-patterns) */
void *memcpy(void *d, const void *s, size_t n)
{
    uint8_t *dp = d; const uint8_t *sp = s;
    while (n--) *dp++ = *sp++;
    return d;
}
void *memset(void *d, int c, size_t n)
{
    uint8_t *dp = d;
    while (n--) *dp++ = (uint8_t)c;
    return d;
}
void *memmove(void *d, const void *s, size_t n)
{
    uint8_t *dp = d; const uint8_t *sp = s;
    if (dp < sp) while (n--) *dp++ = *sp++;
    else { dp += n; sp += n; while (n--) *--dp = *--sp; }
    return d;
}
int memcmp(const void *a, const void *b, size_t n)
{
    const uint8_t *x = a, *y = b;
    for (; n; n--, x++, y++) if (*x != *y) return *x - *y;
    return 0;
}
size_t strlen(const char *s) { const char *p = s; while (*p) p++; return (size_t)(p - s); }
