# SPDX-License-Identifier: BSD-3-Clause

is_zephyr(zephyr)
if(zephyr) ###  Zephyr ###

  if(CONFIG_COMP_HIFI_INTRIN_TEST STREQUAL "m" AND DEFINED CONFIG_LLEXT)

    add_subdirectory(llext ${PROJECT_BINARY_DIR}/hifi_intrin_test_llext)
    add_dependencies(app hifi_intrin_test)

  elseif(CONFIG_COMP_HIFI_INTRIN_TEST)

    # Built-in mode: compile directly into the firmware image and auto-run via
    # SYS_INIT at APPLICATION init level.
    zephyr_library()
    zephyr_library_sources(hifi_intrin_test.c)
    zephyr_library_compile_definitions(SOF_HIFI_INTRIN_BUILTIN)

  endif()

endif()
