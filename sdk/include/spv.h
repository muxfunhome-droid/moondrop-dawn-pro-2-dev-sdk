/*
 * spv.h — SDK для своих программ на C под USB-ЦАП SPV4040 (Moondrop DAWN PRO2).
 * Сборка прошивки «Nov 17 2025 13:50:18». Все адреса ниже относятся только к ней.
 *
 * Как устроено (подробно: sdk/README.md):
 *   - программа живёт во flash за кодом прошивки (0x48020c00…), переменные — в RAM 0x130000…0x13a000;
 *   - при старте прошивка после своей инициализации вызывает app_init(), затем в каждом проходе
 *     главного цикла — app_loop(); аудио, USB и прерывания продолжают работать как обычно;
 *   - app_loop() НЕ должен надолго блокировать: в том же цикле идёт обработка аудио.
 *     Для таймеров используйте spv_millis() и машины состояний, а не длинные задержки.
 *
 * Пометки: [OK] — проверено на железе или в эмуляторе; [?] — вывод из кода, не проверено.
 */
#ifndef SPV_H
#define SPV_H

#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

/* ---------------------------------------------------------------- точки входа программы */
void app_init(void);   /* один раз, после инициализации прошивки (USB/аудио уже подняты) */
void app_loop(void);   /* каждый проход главного цикла (после аудио-задачи и задачи LED) */

/* ---------------------------------------------------------------- регистры */
#define SPV_REG32(a)        (*(volatile uint32_t *)(uintptr_t)(a))
#define SPV_REG8(a)         (*(volatile uint8_t  *)(uintptr_t)(a))
#define SPV_REG16(a)        (*(volatile uint16_t *)(uintptr_t)(a))

#define SPV_PWM_BASE        0x20100000u          /* канал n: +4n, (duty<<16)|0xff00, бит0 = enable [OK] */
#define SPV_PAD_BASE        0x20110000u          /* пад пина p: +4p. 9 = ШИМ, 0x10 = GPIO, 0x210 = откл. [OK] */
#define SPV_PAD(p)          SPV_REG32(SPV_PAD_BASE + 4u * (p))
#define SPV_PAD_PWM         9u
#define SPV_PAD_GPIO_ON     0x10u                /* на этой плате: светодиод ГОРИТ [OK] */
#define SPV_PAD_OFF         0x210u               /* погашен [OK] */
#define SPV_CHIP_ID_LO      0x20160028u          /* уникальный ID кристалла, 64 бит [OK] */
#define SPV_CHIP_ID_HI      0x2016002cu

/* Пины светодиода (имена — как в прошивке; на плате подтверждено: R, G, B) [OK] */
#define SPV_LED_PIN_R       3
#define SPV_LED_PIN_G       33
#define SPV_LED_PIN_B       5
#define SPV_LED_ACTIVE_LOW  1                    /* общий анод: duty 0 = горит [OK] */

/* ---------------------------------------------------------------- переменные прошивки (RAM) */
#define SPV_USB_STATE       SPV_REG32(0x106538)  /* >= 2: USB сконфигурирован, идёт аудио [OK] */
#define SPV_EQ_PRESET       SPV_REG8(0x10648c)   /* 8 = заводской («обычный режим») [OK] */
#define SPV_LED_ENABLED     SPV_REG8(0x11cb1f)   /* LED включён в настройках (сохр. во flash) [OK] */
#define SPV_LED_EVENT       SPV_REG8(0x1064b8)   /* событие LED: 0 обычное, 2 предел громкости, ... [OK] */
#define SPV_UI_STRUCT       ((volatile uint8_t *)0x11cb10)  /* UI-настройки, см. ARCHITECTURE §7 [?] */
#define SPV_VOLUME_STRUCT   ((volatile uint8_t *)0x11cb34)  /* громкость/усиление [?] */

/* ---------------------------------------------------------------- функции прошивки
 * Вызываются по абсолютным адресам. ABI ilp32f (float в f-регистрах), как у прошивки. */
typedef uint32_t (*spv_fn_u32_void)(void);
typedef void     (*spv_fn_void_u32)(uint32_t);

/* частота ядра, Гц [OK: используется задержками прошивки] */
#define spv_cpu_hz()            (((spv_fn_u32_void)0x47f4)())
/* блокирующие задержки прошивки (по mcycle) [OK] — в app_loop только короткие! */
#define spv_delay_ms(ms)        (((spv_fn_void_u32)0x5b64)((uint32_t)(ms)))
#define spv_delay_us(us)        (((spv_fn_void_u32)0x5b9c)((uint32_t)(us)))
/* pwm_set(pin, en, duty 0..255) — настраивает пад, тактирование и канал ШИМ [OK] */
#define spv_fw_pwm_set(pin, en, duty) \
    (((void (*)(uint32_t, uint32_t, uint32_t))0x48004184)((pin), (en), (duty)))
/* led_set_rgb прошивки. ВНИМАНИЕ: в образах с --invert on полярность уже исправлена внутри,
 * в заводском — нет. Для своих программ используйте spv_led_rgb() ниже. */
#define spv_fw_led_set_rgb(r, g, b, on, br) \
    (((void (*)(uint32_t, uint32_t, uint32_t, uint32_t, uint32_t))0x48004288)((r), (g), (b), (on), (br)))
/* чтение канала АЦП (ручки/кнопки): (канал, &результат) [?] семантика не разобрана */
#define spv_fw_adc_read(ch, out) \
    (((uint32_t (*)(uint32_t, void *))0x5988)((ch), (out)))
/* перезагрузка в USB-загрузчик (устройство появится как 0666:0888) [OK] */
#define spv_reboot_to_bootloader() (((void (*)(void))0x588)())

/* Громкость ЦАПа (кнопки +/−): int16, 0 = максимум, меньше = тише [OK по коду 0x48003170] */
#define SPV_VOLUME          (*(volatile int16_t *)0x11d22c)

/* ---------------------------------------------------------------- перехват функций RAM-кода
 * Заменяет первые 8 байт функции прошивки в RAM (0x0…0x9baa) на «auipc t0 / jalr zero,t0» —
 * прыжок в target. Адрес args/ra сохраняются, т.е. target получает те же аргументы и возвращается
 * прямо к вызывающему. Перед заменой проверяются исходные слова orig0/orig1 (защита от другой сборки).
 * Вызывать из app_init(). Возвращает false, если байты не совпали (ничего не тронуто). [OK эмулятор] */
bool spv_ram_redirect(uint32_t addr, uint32_t orig0, uint32_t orig1, const void *target);

/* ---------------------------------------------------------------- время (rt/spv_rt.c) */
uint64_t spv_cycles(void);        /* 64-битный mcycle */
uint32_t spv_millis(void);        /* мс с момента старта (переполнение через ~49 суток) */
uint32_t spv_micros(void);

/* ---------------------------------------------------------------- светодиод (rt/spv_rt.c) */
/* Пока LED «захвачен» программой, заводская логика LED (события, мигание) не вызывается. */
void spv_led_claim(bool own);
bool spv_led_claimed(void);
/* Настоящий цвет 0..255 на канал, полярность платы учтена. Не зависит от патчей --invert. */
void spv_led_rgb(uint8_t r, uint8_t g, uint8_t b);
void spv_led_off(void);
/* HSV -> RGB: h 0..1535 (6 секторов по 256), s, v 0..255 */
void spv_hsv_to_rgb(uint16_t h, uint8_t s, uint8_t v, uint8_t *r, uint8_t *g, uint8_t *b);

/* ---------------------------------------------------------------- отладочный журнал
 * Кольцевой буфер в RAM (spv_dbg_buf). Эмулятор печатает его после прогона (spv_build.py --emu).
 * На устройстве буфер сейчас не виден (нет канала чтения RAM), вызовы безопасны. */
void spv_log(const char *fmt, ...);   /* %d %u %x %08x %s %c %% */

/* ---------------------------------------------------------------- мелочи libc (rt/spv_rt.c) */
void *memcpy(void *d, const void *s, size_t n);
void *memset(void *d, int c, size_t n);
void *memmove(void *d, const void *s, size_t n);
int   memcmp(const void *a, const void *b, size_t n);
size_t strlen(const char *s);

#ifdef __cplusplus
}
#endif
#endif
