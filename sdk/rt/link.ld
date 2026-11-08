/* Линковка программы для SPV4040: код во flash за прошивкой, данные в свободной RAM.
 * FLASH: 0x48020c00 .. 0x48037000 (дальше секция EXT прошивки) — ~89 КБ.
 * RAM:   0x130000 .. 0x13a000 — 40 КБ. Прошивка: .data/.bss до 0x122000, стек сверху от 0x140000
 *        (в эмуляторе глубина ~3.3 КБ; оставлено 24 КБ запаса). Стек программы = стек прошивки.
 * gp прошивки (0x106c18) трогать нельзя: __global_pointer$ не определяется, сборка с -mno-relax. */
OUTPUT_ARCH(riscv)
ENTRY(__spv_init_hook)

MEMORY
{
    FLASH (rx)  : ORIGIN = 0x48020c00, LENGTH = 0x48037000 - 0x48020c00
    RAM   (rwx) : ORIGIN = 0x00130000, LENGTH = 0xa000
}

SECTIONS
{
    .text : ALIGN(4)
    {
        KEEP(*(.spv_header))
        *(.text .text.*)
        *(.rodata .rodata.* .srodata .srodata.*)
        . = ALIGN(4);
    } > FLASH

    .data : ALIGN(4)
    {
        __data_start = .;
        *(.data .data.* .sdata .sdata.*)
        . = ALIGN(4);
        __data_end = .;
    } > RAM AT > FLASH
    __data_load = LOADADDR(.data);

    .bss (NOLOAD) : ALIGN(4)
    {
        __bss_start = .;
        *(.bss .bss.* .sbss .sbss.* COMMON)
        . = ALIGN(4);
        __bss_end = .;
    } > RAM

    __flash_end = LOADADDR(.data) + SIZEOF(.data);

    /DISCARD/ : { *(.comment) *(.note*) *(.eh_frame*) *(.riscv.attributes) }
}
