# SPDX-License-Identifier: BSD-3-Clause

if(CONFIG_COMP_WOV_ARBITER STREQUAL "m" AND DEFINED CONFIG_LLEXT)
	add_subdirectory(llext ${PROJECT_BINARY_DIR}/wov_arbiter_llext)
	add_dependencies(app wov_arbiter)
else()
	add_local_sources(sof wov_arbiter.c)
endif()
