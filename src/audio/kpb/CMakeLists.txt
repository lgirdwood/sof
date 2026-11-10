# SPDX-License-Identifier: BSD-3-Clause

if(CONFIG_IPC_MAJOR_3)
	add_local_sources(sof kpb_ipc3.c)
else()
	add_local_sources(sof kpb.c)
endif()
