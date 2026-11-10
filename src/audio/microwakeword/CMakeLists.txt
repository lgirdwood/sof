# Copyright (c) 2026 Intel Corporation.
# SPDX-License-Identifier: Apache-2.0

# are we building the llext module ?
if(CONFIG_COMP_MWW STREQUAL "m" AND DEFINED CONFIG_LLEXT)
  add_subdirectory(llext ${PROJECT_BINARY_DIR}/mww_llext)
  add_dependencies(app mww)
  return()
endif()

set(MWW_TOOLCHAIN_LIBC_ARCHIVE "")
if(CMAKE_C_COMPILER_ID STREQUAL "GNU")
	execute_process(
		COMMAND ${CMAKE_C_COMPILER} -print-file-name=libc.a
		OUTPUT_VARIABLE MWW_RAW_LIBC_PATH
		OUTPUT_STRIP_TRAILING_WHITESPACE
	)
	if(EXISTS "${MWW_RAW_LIBC_PATH}")
		get_filename_component(MWW_TOOLCHAIN_LIBC_ARCHIVE "${MWW_RAW_LIBC_PATH}" REALPATH)
	endif()
endif()

if(NOT EXISTS "${MWW_TOOLCHAIN_LIBC_ARCHIVE}")
	if(DEFINED CMAKE_SYSROOT AND EXISTS "${CMAKE_SYSROOT}/lib/libc.a")
		set(MWW_TOOLCHAIN_LIBC_ARCHIVE "${CMAKE_SYSROOT}/lib/libc.a")
	else()
		set(MWW_SDK_DIR "${ZEPHYR_SDK_INSTALL_DIR}")
		if("${MWW_SDK_DIR}" STREQUAL "" AND NOT "$ENV{ZEPHYR_SDK_INSTALL_DIR}" STREQUAL "")
			set(MWW_SDK_DIR "$ENV{ZEPHYR_SDK_INSTALL_DIR}")
		endif()
		if("${MWW_SDK_DIR}" STREQUAL "")
			file(GLOB MWW_POSSIBLE_SDKS "$ENV{HOME}/zephyr-sdk*")
			if(MWW_POSSIBLE_SDKS)
				list(REVERSE MWW_POSSIBLE_SDKS)
				list(GET MWW_POSSIBLE_SDKS 0 MWW_SDK_DIR)
			endif()
		endif()

		if(NOT "${MWW_SDK_DIR}" STREQUAL "")
			file(GLOB MWW_SDK_LIBC
				"${MWW_SDK_DIR}/gnu/*${SOC_TOOLCHAIN_NAME}*/*${SOC_TOOLCHAIN_NAME}*/lib/libc.a")
			if(MWW_SDK_LIBC)
				list(GET MWW_SDK_LIBC 0 MWW_TOOLCHAIN_LIBC_ARCHIVE)
			endif()
		endif()
	endif()
endif()

if(NOT EXISTS "${MWW_TOOLCHAIN_LIBC_ARCHIVE}" AND NOT "$ENV{XTENSA_SYSTEM}" STREQUAL "")
       get_filename_component(MWW_XTENSA_CORE_DIR "$ENV{XTENSA_SYSTEM}/.." REALPATH)
       set(MWW_XTENSA_LIBC_ARCHIVE "${MWW_XTENSA_CORE_DIR}/xtensa-elf/lib/libc.a")
       if(EXISTS "${MWW_XTENSA_LIBC_ARCHIVE}")
               set(MWW_TOOLCHAIN_LIBC_ARCHIVE "${MWW_XTENSA_LIBC_ARCHIVE}")
       endif()
endif()

set(MWW_TOOLCHAIN_INCLUDE_ROOT "")
if(EXISTS "${MWW_TOOLCHAIN_LIBC_ARCHIVE}")
	get_filename_component(MWW_TOOLCHAIN_LIB_DIR "${MWW_TOOLCHAIN_LIBC_ARCHIVE}" DIRECTORY)
	get_filename_component(MWW_TOOLCHAIN_SYSROOT "${MWW_TOOLCHAIN_LIB_DIR}" DIRECTORY)
	set(MWW_TOOLCHAIN_INCLUDE_ROOT "${MWW_TOOLCHAIN_SYSROOT}/include")
endif()

# Discover C++ standard library directories dynamically
set(MWW_CXX_INCLUDE_DIRS "")
if(NOT "${MWW_TOOLCHAIN_INCLUDE_ROOT}" STREQUAL "" AND EXISTS "${MWW_TOOLCHAIN_INCLUDE_ROOT}")
	file(GLOB MWW_CXX_VERSION_DIRS LIST_DIRECTORIES true
		"${MWW_TOOLCHAIN_INCLUDE_ROOT}/c++/*")
	foreach(ver_dir ${MWW_CXX_VERSION_DIRS})
		get_filename_component(ver_name "${ver_dir}" NAME)
		if(IS_DIRECTORY "${ver_dir}" AND NOT "${ver_name}" MATCHES "^(backward|tr1|tr2|bits|ext|debug|decimal|parallel|pstl)$")
			list(APPEND MWW_CXX_INCLUDE_DIRS "${ver_dir}")
			file(GLOB MWW_ARCH_SUBDIRS LIST_DIRECTORIES true
				"${ver_dir}/xtensa*"
				"${ver_dir}/*${SOC_TOOLCHAIN_NAME}*")
			foreach(dir ${MWW_ARCH_SUBDIRS})
				if(IS_DIRECTORY "${dir}")
					list(APPEND MWW_CXX_INCLUDE_DIRS "${dir}")
				endif()
			endforeach()
		endif()
	endforeach()
	list(APPEND MWW_CXX_INCLUDE_DIRS "${MWW_TOOLCHAIN_INCLUDE_ROOT}")
endif()

include(mww_tflm.cmake)

add_local_sources(sof mww.c)

zephyr_link_libraries(mww_tflm_lib ${MWW_NN_HIFI_LIB})

# Same libc-shim rationale as src/audio/tensorflow/CMakeLists.txt: SOF's
# default CONFIG_MINIMAL_LIBC is missing abs()/libm symbols TFLM needs in a
# statically-linked (non-LLEXT) image; extract just those archive members
# from the toolchain's libc.a into a small private archive.
if(NOT CONFIG_COMP_MWW STREQUAL "m")
  if(NOT EXISTS "${MWW_TOOLCHAIN_LIBC_ARCHIVE}")
    message(FATAL_ERROR "Microwakeword libc shim: libc.a not found for ${SOC_TOOLCHAIN_NAME}")
  endif()
	execute_process(
		COMMAND ${CMAKE_AR} t ${MWW_TOOLCHAIN_LIBC_ARCHIVE}
		OUTPUT_VARIABLE MWW_LIBC_ARCHIVE_MEMBERS
		OUTPUT_STRIP_TRAILING_WHITESPACE
	)
	set(MWW_TOOLCHAIN_LIBM_ARCHIVE "")
	set(MWW_LIBM_SHIM_MEMBERS "")
	if(MWW_LIBC_ARCHIVE_MEMBERS MATCHES "(^|\n)lib_a-abs\\.o(\n|$)")
		set(MWW_LIBC_SHIM_MEMBERS
			lib_a-abs.o
			lib_a-errno.o
			lib_a-impure.o
			lib_a-s_nan.o
			lib_a-sf_finite.o
			lib_a-sf_fpclassify.o
		)
		set(MWW_TOOLCHAIN_LIBM_ARCHIVE "${MWW_TOOLCHAIN_LIB_DIR}/libm.a")
		set(MWW_LIBM_SHIM_MEMBERS
			lib_a-ef_exp.o
			lib_a-ef_log.o
			lib_a-s_ceil.o
			lib_a-s_floor.o
			lib_a-s_frexp.o
			lib_a-s_lib_ver.o
			lib_a-s_matherr.o
			lib_a-s_round.o
			lib_a-s_signbit.o
			lib_a-sf_fmax.o
			lib_a-sf_fmin.o
			lib_a-sf_isnan.o
			lib_a-sf_round.o
			lib_a-wf_exp.o
			lib_a-wf_log.o
		)
	else()
		set(MWW_LIBC_SHIM_MEMBERS
			libc_stdlib_abs.c.o
			libm_math_s_floor.c.o
			libm_math_sf_exp.c.o
			libm_math_sf_log.c.o
			libm_math_sf_ceil.c.o
			libm_math_s_frexp.c.o
			libm_common_s_round.c.o
			libm_common_sf_fmax.c.o
			libm_common_sf_fmin.c.o
			libm_common_sf_round.c.o
			libm_common_sf_isnan.c.o
			libm_common_sf_fpclassify.c.o
			libm_common_sf_issignaling.c.o
			libm_common_math_errf_uflowf.c.o
			libm_common_math_errf_oflowf.c.o
			libm_common_math_errf_divzerof.c.o
			libm_common_math_errf_invalidf.c.o
		)
	endif()
  set(MWW_LIBC_SHIM_DIR ${CMAKE_CURRENT_BINARY_DIR}/mww_libc_shim)
  set(MWW_LIBC_SHIM_ARCHIVE ${CMAKE_CURRENT_BINARY_DIR}/libmww_libc_shim.a)
  file(MAKE_DIRECTORY ${MWW_LIBC_SHIM_DIR})
	if(MWW_LIBM_SHIM_MEMBERS)
		add_custom_command(
			OUTPUT ${MWW_LIBC_SHIM_ARCHIVE}
			COMMAND ${CMAKE_AR} x ${MWW_TOOLCHAIN_LIBC_ARCHIVE} ${MWW_LIBC_SHIM_MEMBERS}
			COMMAND ${CMAKE_AR} x ${MWW_TOOLCHAIN_LIBM_ARCHIVE} ${MWW_LIBM_SHIM_MEMBERS}
			COMMAND ${PYTHON_EXECUTABLE} ${sof_top_dir}/scripts/mww_shim_flatten.py
				${CMAKE_OBJCOPY} ${MWW_LIBC_SHIM_DIR}
			COMMAND ${CMAKE_AR} rcs ${MWW_LIBC_SHIM_ARCHIVE} ${MWW_LIBC_SHIM_MEMBERS} ${MWW_LIBM_SHIM_MEMBERS}
			WORKING_DIRECTORY ${MWW_LIBC_SHIM_DIR}
			DEPENDS ${MWW_TOOLCHAIN_LIBC_ARCHIVE} ${MWW_TOOLCHAIN_LIBM_ARCHIVE}
			COMMENT "Extracting abs()/libm members TFLM needs from the toolchain archives"
		)
	else()
		add_custom_command(
			OUTPUT ${MWW_LIBC_SHIM_ARCHIVE}
			COMMAND ${CMAKE_AR} x ${MWW_TOOLCHAIN_LIBC_ARCHIVE} ${MWW_LIBC_SHIM_MEMBERS}
			COMMAND ${PYTHON_EXECUTABLE} ${sof_top_dir}/scripts/mww_shim_flatten.py
				${CMAKE_OBJCOPY} ${MWW_LIBC_SHIM_DIR}
			COMMAND ${CMAKE_AR} rcs ${MWW_LIBC_SHIM_ARCHIVE} ${MWW_LIBC_SHIM_MEMBERS}
			WORKING_DIRECTORY ${MWW_LIBC_SHIM_DIR}
			DEPENDS ${MWW_TOOLCHAIN_LIBC_ARCHIVE}
			COMMENT "Extracting abs()/libm members TFLM needs from the toolchain libc.a"
		)
	endif()
  add_custom_target(mww_libc_shim_gen DEPENDS ${MWW_LIBC_SHIM_ARCHIVE})
  add_library(mww_libc_shim STATIC IMPORTED GLOBAL)
  set_target_properties(mww_libc_shim PROPERTIES IMPORTED_LOCATION ${MWW_LIBC_SHIM_ARCHIVE})
  add_dependencies(mww_libc_shim mww_libc_shim_gen)
  zephyr_link_libraries(mww_libc_shim)

  target_compile_definitions(mww_tflm_lib PRIVATE NDEBUG)
endif()
zephyr_include_directories(${TFLM_PATH})
zephyr_include_directories(${FLATBUFFERS_PATH}/include)
zephyr_include_directories(${GEMMLOWP_PATH})
zephyr_include_directories(${RUY_PATH})
if(MWW_CXX_INCLUDE_DIRS)
	target_include_directories(modules_sof SYSTEM PRIVATE ${MWW_CXX_INCLUDE_DIRS})
	if(CMAKE_CXX_COMPILER_ID MATCHES "Clang")
		target_include_directories(mww_tflm_lib SYSTEM PRIVATE ${MWW_CXX_INCLUDE_DIRS})
	endif()
endif()
