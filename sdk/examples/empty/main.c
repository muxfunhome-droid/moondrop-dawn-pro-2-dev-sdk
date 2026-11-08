/*
 * Пустая программа: только рантайм SDK, поведение как у базовой прошивки.
 * Прошейте её ПЕРВОЙ, чтобы на железе проверить механизм (крючки, RAM 0x130000, mcycleh).
 */
#include "spv.h"

void app_init(void) { spv_log("empty: ok\n"); }
void app_loop(void) {}
