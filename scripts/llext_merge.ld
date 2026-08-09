SECTIONS
{
  ._sof_uuid_entry : {
    *(._sof_uuid_entry*)
  }
  .text : {
    *(.text .text.* .literal .literal.*)
  }
  .rodata : {
    *(.rodata .rodata.*)
  }
  .data : {
    *(.data .data.*)
  }
  .bss : {
    *(.bss .bss.*)
  }
}

