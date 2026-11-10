# SPDX-License-Identifier: BSD-3-Clause

if(CONFIG_IPC_MAJOR_3)
	if(CONFIG_COMP_KPB STREQUAL "m")
		message(FATAL_ERROR "KPB can only be built as a loadable module with IPC4")
	endif()
	add_local_sources(sof kpb_ipc3.c)
elseif(CONFIG_COMP_KPB STREQUAL "m" AND DEFINED CONFIG_LLEXT)
	add_subdirectory(llext ${PROJECT_BINARY_DIR}/kpb_llext)
	add_dependencies(app kpb)
else()
	add_local_sources(sof kpb.c)
endif()
