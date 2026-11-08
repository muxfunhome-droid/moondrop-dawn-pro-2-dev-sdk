/*
 * Пример: радуга на светодиоде, пока ЦАП в обычном режиме (EQ-пресет 8).
 * В режиме эквалайзера и во время событий (предел громкости и т.п.) LED отдаётся заводской логике,
 * так что индикация прошивки не теряется.
 */
#include "spv.h"

static uint32_t last_ms;
static uint16_t hue;

void app_init(void)
{
    spv_log("rainbow: start, preset %u, LED %s\n", SPV_EQ_PRESET, SPV_LED_ENABLED ? "on" : "off");
}

void app_loop(void)
{
    bool mine = SPV_LED_ENABLED && SPV_EQ_PRESET == 8 && SPV_LED_EVENT == 0;
    if (mine != spv_led_claimed()) {
        spv_led_claim(mine);
        spv_log("rainbow: LED %s\n", mine ? "claimed" : "released");
    }
    if (!mine) return;

    uint32_t now = spv_millis();
    if (now - last_ms < 20) return;          /* 50 кадров/с, без блокировки цикла */
    last_ms = now;
    hue = (uint16_t)((hue + 4) % 1536);      /* полный круг ≈ 7.7 с */
    uint8_t r, g, b;
    spv_hsv_to_rgb(hue, 255, 255, &r, &g, &b);
    spv_led_rgb(r, g, b);
}
