# SPDX-License-Identifier: BSD-3-Clause

is_zephyr(zephyr)
if(zephyr) ###  Zephyr ###

  if(CONFIG_COMP_HIFI_INTRIN_TEST STREQUAL "m" AND DEFINED CONFIG_LLEXT)

    add_subdirectory(llext ${PROJECT_BINARY_DIR}/hifi_intrin_test_llext)
    add_dependencies(app hifi_intrin_test)

  endif()

endif()
