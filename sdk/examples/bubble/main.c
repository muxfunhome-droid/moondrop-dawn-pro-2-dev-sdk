/*
 * bubble — «тёплый» звук + мягкий булькающий звук на кнопках громкости.
 *
 * Что делает:
 *   1. Звук. Заводская функция эквалайзера 0x1668 (её зовёт USB-прерывание на каждый пакет
 *      аудио) заменяется своей: тот же заводской EQ (10 биквадов прошивки, бит в бит),
 *      а после него — эффект «warm»: подъём баса, кроссфид для наушников и мягкий
 *      ограничитель вместо жёсткого клиппинга.
 *   2. Бульк. На нажатие «+» (пин 8) и «−» (пин 34) в звук подмешивается короткий
 *      синусоидальный «пузырёк» с растущим тоном (как у настоящего пузыря): у «+» выше,
 *      у «−» ниже. При удержании кнопки бульк звучит на каждый шаг громкости.
 *      Смена режима EQ (долгое «+») — двойной бульк.
 *   3. Двойной клик «−» выключает/включает эффект «warm» (A/B-сравнение).
 *      Включение — два пузырька вверх, выключение — два вниз. Бульки работают всегда.
 *
 * Бульк слышен, только пока хост шлёт аудиопоток (идёт воспроизведение или поток открыт):
 * звук синтезируется внутри обработки USB-пакетов. Громкость булька следует громкости ЦАПа.
 *
 * Адреса — сборка прошивки «Nov 17 2025 13:50:18» (см. firm/ARCHITECTURE.md).
 */
#include "spv.h"

/* =============================================================== настройки звука */
#define FX_DEFAULT_ON   1          /* эффект «warm» при старте */
#define FX_PREGAIN      0.80f      /* запас перед подъёмом баса (−1.9 дБ) */
#define FX_BASS_HZ      140.0f     /* срез полки баса */
#define FX_BASS_GAIN    0.65f      /* +4.3 дБ на самом низу */
#define FX_XFEED_HZ     700.0f     /* кроссфид: ФНЧ противоположного канала */
#define FX_XFEED_GAIN   0.30f      /* −10.5 дБ в соседний канал */
#define CLIP_KNEE       0.80f      /* мягкий ограничитель начинается с −1.9 дБFS */

#define BUB_LEVEL       0.16f      /* пик булька, доля полной шкалы (−16 дБFS) */

/* =============================================================== адреса прошивки */
#define FW_EQ_FN        0x1668u                 /* eq(buf, frames, bands, fs) в RAM-коде */
#define FW_EQ_W0        0x1cc05663u             /* bge zero,a2,0x1834 */
#define FW_EQ_W1        0xc686715du             /* addi sp,sp,-80 ; sw ra,76(sp) */
#define FW_BIQUAD       ((void (*)(uint32_t, int32_t *, const int32_t *, int32_t *))0x9a2cu)
#define FW_EQ_STATE     ((int32_t *)0x11c8a4u)  /* состояние: 10 слов на полосу */

/* Кнопки — структуры библиотеки MultiButton прошивки: +4 id, +12+4*event колбэки */
#define FW_BTN_PLUS     ((volatile uint32_t *)0x11ca60u)   /* id 8  (пин 8)  */
#define FW_BTN_MINUS    ((volatile uint32_t *)0x11ca34u)   /* id 34 (пин 34) */
#define EV_PRESS_DOWN   0
#define EV_DOUBLE_CLICK 4
#define EV_LONG_HOLD    6
#define CB(btn, ev)     ((btn)[3 + (ev)])
#define FW_CB_DOWN      0x48003410u
#define FW_CB_HOLD      0x00002414u
typedef void (*btn_cb_t)(volatile uint32_t *);

/* =============================================================== связь кнопки -> звук
 * Кнопки обрабатываются в таймерном прерывании, звук — в USB-прерывании (они вложенные).
 * Каждая сторона пишет только свои счётчики. */
enum { BUB_PLUS, BUB_MINUS, BUB_PLUS_SOFT, BUB_MINUS_SOFT, BUB_MODE, BUB_FX_ON, BUB_FX_OFF, BUB_KINDS };
static volatile uint8_t req[BUB_KINDS];   /* пишут кнопки/app_loop */
static uint8_t seen[BUB_KINDS];           /* читает аудио */
volatile uint8_t fx_on = FX_DEFAULT_ON;   /* не static: тест в эмуляторе меняет его */
static volatile uint32_t stat_calls, stat_bubbles;

static void request(int kind) { req[kind]++; }

/* =============================================================== мелкая математика */
static float fexp(float x)                       /* e^x, достаточно для коэффициентов */
{
    int k = 0;
    while (x > 0.01f || x < -0.01f) { x *= 0.5f; k++; }
    float y = 1.0f + x * (1.0f + x * (0.5f + x * (1.0f / 6.0f + x * (1.0f / 24.0f))));
    while (k--) y *= y;
    return y;
}

static float sine(float p)                       /* sin(2πp), p ∈ [0,1) */
{
    float h = p < 0.5f ? p : p - 0.5f;           /* полупериод: h ∈ [0,0.5) */
    float x = 2.0f * h;                          /* 0..1 */
    float y = 4.0f * x * (1.0f - x);             /* парабола ≈ sin(πx) */
    y = 0.225f * (y * y - y) + y;                /* уточнение (ошибка < 0.1 %) */
    return p < 0.5f ? y : -y;
}

static float softclip(float v)                   /* линейно до колена, дальше плавно к 1.0 */
{
    float a = v < 0 ? -v : v;
    if (a <= CLIP_KNEE) return v;
    float r = 1.0f - CLIP_KNEE, t = (a - CLIP_KNEE) / r;       /* t ≥ 0 */
    float th = t * (27.0f + t * t) / (27.0f + 9.0f * t * t);  /* ≈ tanh(t) */
    if (th > 1.0f) th = 1.0f;
    float o = CLIP_KNEE + r * th;
    return v < 0 ? -o : o;
}

/* =============================================================== пузырьки */
typedef struct {
    uint32_t delay, n, len, att;     /* в сэмплах */
    float phase, freq, fmul, env, dec, amp, inv_fs;
} voice_t;
#define NVOICES 4
static voice_t voices[NVOICES];
static uint32_t cur_fs;

typedef struct { float f0, f1, sweep_s, tau_s, len_s, amp; } bubble_t;
/* Пузырь: тон растёт f0 -> f1 экспоненциально за sweep_s, огибающая — атака 2 мс и спад tau_s. */
static const bubble_t BUB_HI   = {  620.0f, 1350.0f, 0.040f, 0.020f, 0.110f, 1.00f };
static const bubble_t BUB_LO   = {  360.0f,  760.0f, 0.050f, 0.026f, 0.140f, 1.10f };
static const bubble_t BUB_TOP  = {  900.0f, 1900.0f, 0.030f, 0.016f, 0.090f, 0.85f };

static void voice_start(const bubble_t *b, float amp_mul, float delay_s)
{
    if (!cur_fs) return;
    voice_t *v = &voices[0];
    for (int i = 0; i < NVOICES; i++) {          /* свободный голос или самый старый */
        if (voices[i].len == 0) { v = &voices[i]; break; }
        if (voices[i].n > v->n) v = &voices[i];
    }
    float fs = (float)cur_fs;
    v->inv_fs = 1.0f / fs;
    v->delay = (uint32_t)(delay_s * fs);
    v->n = 0;
    v->len = (uint32_t)(b->len_s * fs);
    v->att = (uint32_t)(0.002f * fs) + 1;
    v->phase = 0.0f;
    v->freq = b->f0;
    /* ln(f1/f0) без libm: ln(x) = 2·atanh((x−1)/(x+1)) рядом из трёх членов, x ≤ ~2.2 */
    float q = (b->f1 - b->f0) / (b->f1 + b->f0), q2 = q * q;
    float ln = 2.0f * q * (1.0f + q2 * (1.0f / 3.0f + q2 * (1.0f / 5.0f + q2 * (1.0f / 7.0f))));
    v->fmul = fexp(ln / (b->sweep_s * fs));
    v->dec = fexp(-1.0f / (b->tau_s * fs));
    v->env = 0.0f;
    v->amp = BUB_LEVEL * b->amp * amp_mul;
    stat_bubbles++;
}

static void start_requested(void)
{
    for (int k = 0; k < BUB_KINDS; k++) {
        uint8_t r = req[k];
        if (r == seen[k]) continue;
        seen[k] = r;
        switch (k) {
        case BUB_PLUS:       voice_start(&BUB_HI, 1.0f, 0.0f); break;
        case BUB_MINUS:      voice_start(&BUB_LO, 1.0f, 0.0f); break;
        case BUB_PLUS_SOFT:  voice_start(&BUB_HI, 0.6f, 0.0f); break;
        case BUB_MINUS_SOFT: voice_start(&BUB_LO, 0.6f, 0.0f); break;
        case BUB_MODE:       voice_start(&BUB_HI, 1.0f, 0.0f); voice_start(&BUB_TOP, 1.0f, 0.075f); break;
        case BUB_FX_ON:      voice_start(&BUB_LO, 0.9f, 0.0f); voice_start(&BUB_HI, 0.9f, 0.085f); break;
        case BUB_FX_OFF:     voice_start(&BUB_HI, 0.9f, 0.0f); voice_start(&BUB_LO, 0.9f, 0.085f); break;
        }
    }
}

static inline float voices_next(void)
{
    float s = 0.0f;
    for (int i = 0; i < NVOICES; i++) {
        voice_t *v = &voices[i];
        if (!v->len) continue;
        if (v->delay) { v->delay--; continue; }
        if (v->n < v->att) v->env = (float)v->n / (float)v->att;
        else v->env *= v->dec;
        s += v->amp * v->env * sine(v->phase);
        v->phase += v->freq * v->inv_fs;
        if (v->phase >= 1.0f) v->phase -= 1.0f;
        v->freq *= v->fmul;
        if (++v->n >= v->len) v->len = 0;
    }
    return s;
}

static bool voices_active(void)
{
    for (int i = 0; i < NVOICES; i++) if (voices[i].len) return true;
    return false;
}

/* =============================================================== эффект «warm» */
static float a_bass, a_xf;                 /* коэффициенты 1-полюсных ФНЧ */
static float lp_b[2], lp_x[2];             /* состояние */

static void set_rate(uint32_t fs)
{
    cur_fs = fs;
    float k = 6.2831853f / (float)fs;
    a_bass = 1.0f - fexp(-FX_BASS_HZ * k);
    a_xf   = 1.0f - fexp(-FX_XFEED_HZ * k);
    lp_b[0] = lp_b[1] = lp_x[0] = lp_x[1] = 0.0f;
    for (int i = 0; i < NVOICES; i++) voices[i].len = 0;
}

#define FULL  536870912.0f                 /* 2^29: полная шкала внутри тракта (после >>2) */
#define LIM   536870911.0f

static void process(int32_t *buf, uint32_t frames, uint32_t fs)
{
    if (fs != cur_fs) set_rate(fs);
    start_requested();
    bool fx = fx_on, bub = voices_active();
    if (!fx && !bub) return;               /* заводской звук бит в бит */

    const float in = 1.0f / FULL, xn = 1.0f / (1.0f + FX_XFEED_GAIN);
    for (uint32_t i = 0; i < frames; i++, buf += 2) {
        float l = (float)buf[0] * in, r = (float)buf[1] * in;
        if (fx) {
            l *= FX_PREGAIN; r *= FX_PREGAIN;
            lp_b[0] += a_bass * (l - lp_b[0]);  lp_b[1] += a_bass * (r - lp_b[1]);
            l += FX_BASS_GAIN * lp_b[0];        r += FX_BASS_GAIN * lp_b[1];
            lp_x[0] += a_xf * (l - lp_x[0]);    lp_x[1] += a_xf * (r - lp_x[1]);
            float l2 = (l + FX_XFEED_GAIN * lp_x[1]) * xn;
            float r2 = (r + FX_XFEED_GAIN * lp_x[0]) * xn;
            l = l2; r = r2;
        }
        if (bub) { float s = voices_next(); l += s; r += s; }
        l = softclip(l) * FULL; r = softclip(r) * FULL;
        if (l > LIM) l = LIM;
        if (l < -FULL) l = -FULL;
        if (r > LIM) r = LIM;
        if (r < -FULL) r = -FULL;
        buf[0] = (int32_t)l; buf[1] = (int32_t)r;
    }
}

/* =============================================================== замена 0x1668 */
static const int32_t *eq_table(uint32_t fs)      /* как в 0x1668: таблица коэффициентов по fs */
{
    switch (fs) {
    case 44100:  return (const int32_t *)0x11c7dcu;
    case 48000:  return (const int32_t *)0x11c714u;
    case 88200:  return (const int32_t *)0x11c64cu;
    case 96000:  return (const int32_t *)0x11c584u;
    case 176400: return (const int32_t *)0x11c4bcu;
    case 192000: return (const int32_t *)0x11c3f4u;
    case 352800: return (const int32_t *)0x11c32cu;
    case 384000: return (const int32_t *)0x11c264u;
    case 705600: return (const int32_t *)0x11c19cu;
    case 768000: return (const int32_t *)0x11c0d4u;
    default:     return 0;
    }
}

/* Вызывается вместо 0x1668 из USB-прерывания: buf — int32 L/R вперемешку (уже >>2), frames ≤ 255. */
void bubble_eq(int32_t *buf, uint32_t frames, int32_t bands, uint32_t fs)
{
    if (bands <= 0) return;
    stat_calls++;
    const int32_t *t = eq_table(fs);
    if (t)
        for (int32_t i = 0; i < bands; i++)
            FW_BIQUAD(frames, buf, t + 5 * i, FW_EQ_STATE + 10 * i);
    if (frames) process(buf, frames, fs);
}

/* =============================================================== кнопки */
static void on_press_down(volatile uint32_t *btn)        /* таймерное прерывание */
{
    ((btn_cb_t)FW_CB_DOWN)(btn);
    uint8_t id = ((volatile uint8_t *)btn)[4];
    if (id == 8) request(BUB_PLUS);
    else if (id == 34) request(BUB_MINUS);
}

static void on_long_hold(volatile uint32_t *btn)         /* автоповтор громкости при удержании */
{
    int16_t v0 = SPV_VOLUME;
    ((btn_cb_t)FW_CB_HOLD)(btn);
    int16_t v1 = SPV_VOLUME;
    if (v1 > v0) request(BUB_PLUS_SOFT);
    else if (v1 < v0) request(BUB_MINUS_SOFT);
}

static volatile uint8_t fx_toggle_req;
static void on_double_minus(volatile uint32_t *btn)
{
    (void)btn;
    fx_toggle_req++;
}

static bool hook_button(volatile uint32_t *btn, bool dbl)
{
    btn_cb_t d = on_press_down, h = on_long_hold, c = on_double_minus;
    bool ok = true;
    if (CB(btn, EV_PRESS_DOWN) == FW_CB_DOWN) CB(btn, EV_PRESS_DOWN) = (uint32_t)(uintptr_t)d;
    else ok &= CB(btn, EV_PRESS_DOWN) == (uint32_t)(uintptr_t)d;
    if (CB(btn, EV_LONG_HOLD) == FW_CB_HOLD) CB(btn, EV_LONG_HOLD) = (uint32_t)(uintptr_t)h;
    else ok &= CB(btn, EV_LONG_HOLD) == (uint32_t)(uintptr_t)h;
    if (dbl) {
        if (CB(btn, EV_DOUBLE_CLICK) == 0) CB(btn, EV_DOUBLE_CLICK) = (uint32_t)(uintptr_t)c;
        else ok &= CB(btn, EV_DOUBLE_CLICK) == (uint32_t)(uintptr_t)c;
    }
    return ok;
}

/* =============================================================== точки входа */
static bool eq_hooked;
static uint8_t last_preset, seen_toggle;
static uint32_t last_check;

void app_init(void)
{
    eq_hooked = spv_ram_redirect(FW_EQ_FN, FW_EQ_W0, FW_EQ_W1, bubble_eq);
    bool b1 = hook_button(FW_BTN_PLUS, false);
    bool b2 = hook_button(FW_BTN_MINUS, true);
    last_preset = SPV_EQ_PRESET;
    spv_log("bubble: eq hook %s, buttons +%s -%s, fx %s\n", eq_hooked ? "ok" : "FAIL",
            b1 ? "ok" : "FAIL", b2 ? "ok" : "FAIL", fx_on ? "on" : "off");
}

void app_loop(void)
{
    uint8_t p = SPV_EQ_PRESET;                   /* смена режима EQ (долгое «+») */
    if (p != last_preset) { last_preset = p; request(BUB_MODE); spv_log("bubble: preset %u\n", p); }

    uint8_t t = fx_toggle_req;                   /* двойной клик «−» */
    if (t != seen_toggle) {
        seen_toggle = t;
        fx_on = !fx_on;
        request(fx_on ? BUB_FX_ON : BUB_FX_OFF);
        spv_log("bubble: fx %s\n", fx_on ? "on" : "off");
    }

    uint32_t now = spv_millis();                 /* раз в секунду: колбэки на месте? */
    if (now - last_check >= 1000) {
        last_check = now;
        hook_button(FW_BTN_PLUS, false);
        hook_button(FW_BTN_MINUS, true);
    }
}
