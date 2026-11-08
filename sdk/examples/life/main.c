/*
 * life — игра «Жизнь» Конвея на поле 10×10, которая считается на RISC-V внутри USB-ЦАПа.
 *
 * ПК шлёт vendor HID-отчёт (report ID 75) с полем и числом шагов, чип считает поколения
 * и возвращает итоговое поле. Рисует его программа на ПК (host/).
 *
 * Протокол (64 байта, байт 0 = report ID 75):
 *   запрос: [75, 0xCB, seq, flags, steps u32 @4, поле 13 Б @8]
 *           flags бит 0: 1 = края мёртвые, 0 = тор (края склеены)
 *   ответ:  [75, 0xCB, seq, status, steps u32 @4, поле 13 Б @8, время мкс u32 @24, население u8 @28,
 *            период цикла u32 @32 (0 — не найден), цикл найден на шаге u32 @36]
 *   поле: клетка (строка r, столбец c) = бит (r*10 + c), младший бит первого байта — левый верхний угол.
 *
 * Считается порциями (steps_per_pass за проход главного цикла), чтобы не блокировать прошивку.
 * Цикл ищется по Бренту: состояние запоминается на шагах 1, 2, 4, 8…; совпадение -> период известен,
 * остаток шагов берётся по модулю периода. Так 4 миллиарда шагов считаются мгновенно.
 */
#include "spv.h"

/* --------------------------------------------------------------- адреса прошивки (Nov 17 2025) */
#define FW_HID_FN       0x8d84u
#define FW_HID_W0       0xc78341d4u
#define FW_HID_W1       0xd55b0006u
#define FW_HID_REST     0x8d94u
#define FW_HID_SEND     0x8418u
#define REPORT_ID       75
#define CMD_LIFE        0xCB

typedef uint32_t (*fw_rest_t)(void *ctx, volatile uint32_t *msg, uint32_t a2, volatile uint8_t *buf);
typedef void (*fw_send_t)(void *ctx, volatile uint8_t *buf, uint32_t len);

#define N               10
#define MASK            ((1u << N) - 1)

/* --------------------------------------------------------------- поле */
typedef struct { uint16_t r[N]; } field_t;

static bool same(const field_t *a, const field_t *b)
{
    for (int i = 0; i < N; i++) if (a->r[i] != b->r[i]) return false;
    return true;
}

static inline uint32_t rotl(uint32_t x) { return ((x << 1) | (x >> (N - 1))) & MASK; }  /* столбец c -> c+1 */
static inline uint32_t rotr(uint32_t x) { return ((x >> 1) | (x << (N - 1))) & MASK; }

/* побитовый сумматор: прибавить 1-битную маску x к счётчику (s2 s1 s0), по модулю 8 */
#define ADD(x) do { uint32_t c0 = s0 & (x); s0 ^= (x); uint32_t c1 = s1 & c0; s1 ^= c0; s2 ^= c1; } while (0)

static bool wrap = true;

static void step(field_t *f)
{
    field_t o = *f;
    for (int i = 0; i < N; i++) {
        uint32_t up, dn, me = o.r[i];
        if (wrap) { up = o.r[(i + N - 1) % N]; dn = o.r[(i + 1) % N]; }
        else      { up = i ? o.r[i - 1] : 0;   dn = i < N - 1 ? o.r[i + 1] : 0; }
        uint32_t s0 = 0, s1 = 0, s2 = 0, l, r;
#define SIDES(row) do { if (wrap) { l = rotl(row); r = rotr(row); } \
                        else { l = ((row) << 1) & MASK; r = (row) >> 1; } } while (0)
        SIDES(up); ADD(up); ADD(l); ADD(r);
        SIDES(dn); ADD(dn); ADD(l); ADD(r);
        SIDES(me); ADD(l); ADD(r);
        /* живая в следующем: ровно 3 соседа, или 2 и сама живая (8 соседей = 0 mod 8 — тоже смерть) */
        uint32_t three = s0 & s1 & ~s2, two = ~s0 & s1 & ~s2;
        f->r[i] = (uint16_t)((three | (two & me)) & MASK);
    }
}

static void unpack(field_t *f, const volatile uint8_t *b)
{
    for (int i = 0; i < N; i++) f->r[i] = 0;
    for (int k = 0; k < N * N; k++)
        if (b[k >> 3] & (1u << (k & 7))) f->r[k / N] |= (uint16_t)(1u << (k % N));
}

static void pack(const field_t *f, uint8_t *b)
{
    for (int k = 0; k < 13; k++) b[k] = 0;
    for (int k = 0; k < N * N; k++)
        if (f->r[k / N] & (1u << (k % N))) b[k >> 3] |= (uint8_t)(1u << (k & 7));
}

/* --------------------------------------------------------------- задание */
static struct {
    bool busy;
    void *ctx;
    uint8_t seq;
    uint32_t total, done;          /* шагов нужно / сделано */
    field_t cur, check;            /* текущее поле, контрольная точка Брента */
    uint32_t check_at, next_check; /* шаг контрольной точки, следующий шаг сохранения */
    uint32_t period, found_at;
    uint32_t t0;
} job;

static uint32_t jobs;

static void finish(void)
{
    uint8_t out[64] = { REPORT_ID, CMD_LIFE, job.seq, 0 };
    uint32_t us = spv_micros() - job.t0;
    memcpy(&out[4], &job.total, 4);
    pack(&job.cur, &out[8]);
    memcpy(&out[24], &us, 4);
    uint32_t pop = 0;
    for (int i = 0; i < N; i++) pop += (uint32_t)__builtin_popcount(job.cur.r[i]);
    out[28] = (uint8_t)pop;
    memcpy(&out[32], &job.period, 4);
    memcpy(&out[36], &job.found_at, 4);
    static uint8_t tx[64];                        /* отправка копирует в FIFO сразу (0x7f68) */
    memcpy(tx, out, 64);
    ((fw_send_t)FW_HID_SEND)(job.ctx, tx, 64);
    job.busy = false;
    jobs++;
    spv_log("life #%u: %u шагов, население %u, период %u (на шаге %u), %u мкс\n",
            jobs, job.total, pop, job.period, job.found_at, us);
}

static void run(uint32_t budget)
{
    while (budget-- && job.done < job.total) {
        step(&job.cur);
        job.done++;
        if (job.period) continue;
        if (same(&job.cur, &job.check)) {         /* цикл: оставшееся — по модулю периода */
            job.period = job.done - job.check_at;
            job.found_at = job.done;
            job.total = job.done + (job.total - job.done) % job.period;   /* в ответ уйдёт requested */
        } else if (job.done == job.next_check) {
            job.check = job.cur; job.check_at = job.done; job.next_check = job.done * 2;
        }
    }
}

static uint32_t requested;                        /* сколько шагов просили (для ответа) */

/* вызывается вместо 0x8d84 */
uint32_t life_hid(void *ctx, volatile uint32_t *msg, uint32_t a2)
{
    volatile uint8_t *buf = (volatile uint8_t *)(uintptr_t)msg[1];
    if (buf[0] != REPORT_ID) return 0;
    if (msg[0] == 2 && buf[1] == CMD_LIFE) {
        job.ctx = ctx;
        job.seq = buf[2];
        wrap = !(buf[3] & 1);
        uint32_t steps = (uint32_t)buf[4] | (uint32_t)buf[5] << 8 | (uint32_t)buf[6] << 16 | (uint32_t)buf[7] << 24;
        unpack(&job.cur, &buf[8]);
        msg[0] = 1;                               /* буфер свободен, считаем в app_loop */
        requested = job.total = steps;
        job.done = 0;
        job.check = job.cur; job.check_at = 0; job.next_check = 1;
        job.period = job.found_at = 0;
        job.t0 = spv_micros();
        job.busy = true;                          /* новый запрос заменяет недосчитанный */
        return 0;
    }
    return ((fw_rest_t)FW_HID_REST)(ctx, msg, a2, buf);
}

/* --------------------------------------------------------------- точки входа */
static uint32_t blink_until;
volatile uint32_t steps_per_pass = 500;    /* шагов за проход главного цикла (оценка ≈1 мс); тест меняет */

void app_init(void)
{
    bool ok = spv_ram_redirect(FW_HID_FN, FW_HID_W0, FW_HID_W1, life_hid);
    spv_log("life: HID hook %s\n", ok ? "ok" : "FAIL");
}

void app_loop(void)
{
    if (job.busy) {
        if (!spv_led_claimed() && SPV_LED_ENABLED) { spv_led_claim(true); spv_led_rgb(0, 255, 120); }
        run(steps_per_pass);
        if (job.done >= job.total) {
            job.total = requested;                /* в ответе — сколько просили */
            finish();
            blink_until = spv_millis() + 120;
        }
    } else if (blink_until && (int32_t)(spv_millis() - blink_until) >= 0) {
        blink_until = 0;
        if (spv_led_claimed()) spv_led_claim(false);
    }
}
