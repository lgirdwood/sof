# SPDX-License-Identifier: BSD-3-Clause

if(CONFIG_COMP_ECNS STREQUAL "m" AND DEFINED CONFIG_LLEXT)
	add_subdirectory(llext ${PROJECT_BINARY_DIR}/ecns_llext)
	add_dependencies(app ecns)
else()
	add_local_sources(sof ecns.c)
endif()
