/*
 * calc — калькулятор, который считает на RISC-V внутри USB-ЦАПа.
 *
 * ПК шлёт vendor HID-отчёт (report ID 75) с выражением, чип разбирает и считает его
 * (double, рекурсивный спуск) и отвечает HID-отчётом с результатом и числом тактов.
 *
 * Протокол (64 байта, байт 0 = report ID 75):
 *   запрос: [75, 0xCA, seq, len, выражение ASCII (до 60 символов)]
 *   ответ:  [75, 0xCA, seq, status, errpos, 0,0,0, result double LE @8, cycles u32 @16, count u32 @20]
 *   status: 0 ок, 1 синтаксис, 2 деление на ноль, 3 слишком длинно/глубоко, 4 вне области определения
 *
 * Как встроено: функция прошивки 0x8d84 (обработчик vendor-отчётов, её зовёт главный цикл) перенаправлена
 * на calc_hid(). Свои отчёты (байт 1 = 0xCA) обрабатываются здесь, все остальные — включая команду входа
 * в загрузчик — уходят в заводской код как раньше. Звук не трогается.
 */
#include "spv.h"

/* --------------------------------------------------------------- адреса прошивки (Nov 17 2025) */
#define FW_HID_FN       0x8d84u
#define FW_HID_W0       0xc78341d4u        /* lw a3,4(a1) ; lbu a5,0(a3)… */
#define FW_HID_W1       0xd55b0006u
#define FW_HID_REST     0x8d94u            /* продолжение заводского обработчика (a3 = буфер) */
#define FW_HID_SEND     0x8418u            /* send(ctx, buf, len) — IN-отчёт */
#define REPORT_ID       75
#define CMD_CALC        0xCA

typedef uint32_t (*fw_rest_t)(void *ctx, volatile uint32_t *msg, uint32_t a2, volatile uint8_t *buf);
typedef void (*fw_send_t)(void *ctx, volatile uint8_t *buf, uint32_t len);

enum { ST_OK, ST_SYNTAX, ST_DIV0, ST_LONG, ST_DOMAIN };

/* --------------------------------------------------------------- разбор выражения */
static const char *p, *start;
static int err, depth;

static void fail(int e) { if (!err) err = e; }
static void skip(void) { while (*p == ' ' || *p == '\t') p++; }

static double expr(void);

static double number(void)
{
    double v = 0, scale = 1;
    int any = 0;
    while (*p >= '0' && *p <= '9') { v = v * 10 + (*p++ - '0'); any = 1; }
    if (*p == '.' || *p == ',') {
        p++;
        while (*p >= '0' && *p <= '9') { scale /= 10; v += (*p++ - '0') * scale; any = 1; }
    }
    if (!any) { fail(ST_SYNTAX); return 0; }
    if (*p == 'e' || *p == 'E') {                 /* 1.5e3 */
        const char *q = p + 1;
        int neg = 0, ex = 0, dig = 0;
        if (*q == '+' || *q == '-') neg = *q++ == '-';
        while (*q >= '0' && *q <= '9') { ex = ex * 10 + (*q++ - '0'); dig = 1; if (ex > 400) ex = 400; }
        if (dig) { p = q; while (ex--) v = neg ? v / 10 : v * 10; }
    }
    return v;
}

static double my_sqrt(double x)
{
    if (x < 0) { fail(ST_DOMAIN); return 0; }
    if (x == 0) return 0;
    double g = x > 1 ? x : 1;
    for (int i = 0; i < 200; i++) {               /* Ньютон */
        double n = 0.5 * (g + x / g);
        if (n == g) break;
        g = n;
    }
    return g;
}

static int word(const char *w)
{
    int n = 0;
    while (w[n]) { if (p[n] != w[n]) return 0; n++; }
    char c = p[n];
    if ((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9')) return 0;
    p += n;
    return 1;
}

static double primary(void)
{
    skip();
    if (*p == '(') {
        p++;
        double v = expr();
        skip();
        if (*p == ')') p++; else fail(ST_SYNTAX);
        return v;
    }
    if (word("pi")) return 3.14159265358979323846;
    if (word("e"))  return 2.71828182845904523536;
    if (word("sqrt")) {
        skip();
        if (*p != '(') { fail(ST_SYNTAX); return 0; }
        return my_sqrt(primary());
    }
    if ((*p >= '0' && *p <= '9') || *p == '.') return number();
    fail(ST_SYNTAX);
    return 0;
}

static double power(void);

static double unary(void)                        /* знак слабее ^: -3^2 = -(3^2) */
{
    skip();
    if (++depth > 40) { fail(ST_LONG); return 0; }
    double v;
    if (*p == '-') { p++; v = -unary(); }
    else if (*p == '+') { p++; v = unary(); }
    else v = power();
    depth--;
    return v;
}

static double power(void)                        /* a ^ b, правоассоциативно, b — целое */
{
    double a = primary();
    skip();
    if (*p != '^') return a;
    p++;
    double b = unary();                          /* 2^-2, 2^3^2 = 2^9 */
    long n = (long)b;
    if ((double)n != b || n > 4096 || n < -4096) { fail(ST_DOMAIN); return 0; }
    if (a == 0 && n < 0) { fail(ST_DIV0); return 0; }
    double r = 1, x = a;
    unsigned long k = n < 0 ? (unsigned long)-n : (unsigned long)n;
    while (k) { if (k & 1) r *= x; x *= x; k >>= 1; }
    return n < 0 ? 1 / r : r;
}

static double term(void)
{
    double v = unary();
    for (;;) {
        skip();
        char op = *p;
        if (op != '*' && op != '/' && op != '%') return v;
        p++;
        double r = unary();
        if (op == '*') v *= r;
        else if (r == 0) { fail(ST_DIV0); return 0; }
        else if (op == '/') v /= r;
        else {                                    /* остаток как fmod */
            double q = v / r;
            long long t = (long long)q;
            v -= (double)t * r;
        }
    }
}

static double expr(void)
{
    if (++depth > 40) { fail(ST_LONG); return 0; }
    double v = term();
    for (;;) {
        skip();
        if (*p == '+') { p++; v += term(); }
        else if (*p == '-') { p++; v -= term(); }
        else break;
    }
    depth--;
    return v;
}

/* --------------------------------------------------------------- обработчик отчётов */
static uint32_t count;
static char text[64];
static uint32_t blink_until;

static void calc_report(void *ctx, volatile uint8_t *buf)
{
    uint8_t seq = buf[2], len = buf[3];
    if (len > 60) len = 60;
    for (int i = 0; i < len; i++) text[i] = (char)buf[4 + i];
    text[len] = 0;

    uint64_t t0 = spv_cycles();
    p = start = text; err = 0; depth = 0;
    double r = expr();
    skip();
    if (!err && *p) fail(ST_SYNTAX);              /* хвост, который не разобрался */
    uint32_t cyc = (uint32_t)(spv_cycles() - t0);
    count++;

    uint8_t out[64] = { REPORT_ID, CMD_CALC, seq, (uint8_t)err, (uint8_t)(p - start) };
    if (err) r = 0;
    memcpy(&out[8], &r, 8);
    memcpy(&out[16], &cyc, 4);
    memcpy(&out[20], &count, 4);
    for (int i = 0; i < 64; i++) buf[i] = out[i];  /* ответ — в том же буфере, как у прошивки */
    ((fw_send_t)FW_HID_SEND)(ctx, buf, 64);

    spv_log("calc #%u: \"%s\" -> status %u, %u cycles\n", count, text, err, cyc);
    if (SPV_LED_ENABLED) blink_until = spv_millis() + 150;   /* мигнуть светодиодом */
}

/* вызывается вместо 0x8d84: (ctx 0x121ec0, msg 0x106554 = {u32 state; u8 *buf}, a2) */
uint32_t calc_hid(void *ctx, volatile uint32_t *msg, uint32_t a2)
{
    volatile uint8_t *buf = (volatile uint8_t *)(uintptr_t)msg[1];
    if (buf[0] != REPORT_ID) return 0;            /* как в оригинале */
    if (msg[0] == 2 && buf[1] == CMD_CALC) {
        calc_report(ctx, buf);
        msg[0] = 1;                               /* отчёт обработан, можно принимать следующий */
        return 0;
    }
    return ((fw_rest_t)FW_HID_REST)(ctx, msg, a2, buf);
}

/* --------------------------------------------------------------- точки входа */
void app_init(void)
{
    bool ok = spv_ram_redirect(FW_HID_FN, FW_HID_W0, FW_HID_W1, calc_hid);
    spv_log("calc: HID hook %s\n", ok ? "ok" : "FAIL");
}

void app_loop(void)
{
    /* после каждого вычисления LED на 150 мс становится голубым, потом снова заводской */
    if (blink_until) {
        if ((int32_t)(spv_millis() - blink_until) < 0) {
            if (!spv_led_claimed()) { spv_led_claim(true); spv_led_rgb(0, 200, 255); }
        } else {
            blink_until = 0;
            spv_led_claim(false);
        }
    }
}
