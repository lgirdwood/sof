# Copyright (c) 2026 Intel Corporation.
# SPDX-License-Identifier: Apache-2.0
#
# LLEXT build path for the mww component (CONFIG_COMP_MWW=m). The TFLM and
# nnlib libraries are the same as for the built-in component, see
# ../mww_tflm.cmake. The module is linked against the toolchain C library
# for the math functions the TFLM kernels use, the base firmware doesn't
# export a libm.

include(${CMAKE_CURRENT_LIST_DIR}/../mww_tflm.cmake)

set(MWW_LLEXT_LIBS mww_tflm_lib ${MWW_NN_HIFI_LIB})
set(MWW_LIBS_PATHS .)

if(ZEPHYR_TOOLCHAIN_VARIANT STREQUAL "xt-clang")
	# Cadence toolchain: newlib libc.a and libm.a of the core configuration,
	# and its libgcc.a for the soft-float helpers. These libraries aren't
	# built with -mlongcalls, so their calls must be resolved within the
	# module, a direct call can't reach the base firmware.
	get_filename_component(MWW_XTENSA_LIB_DIR "$ENV{XTENSA_SYSTEM}/../xtensa-elf/lib" REALPATH)
	if(NOT EXISTS "${MWW_XTENSA_LIB_DIR}/libc.a")
		message(FATAL_ERROR "mww llext: libc.a not found in ${MWW_XTENSA_LIB_DIR}, "
			"is XTENSA_SYSTEM set?")
	endif()
	list(APPEND MWW_LIBS_PATHS ${MWW_XTENSA_LIB_DIR} ${MWW_XTENSA_LIB_DIR}/xcc)
	list(APPEND MWW_LLEXT_LIBS m c gcc)
	target_compile_options(mww_tflm_lib PRIVATE -stdlib=libc++)
else()
	set(MWW_TOOLCHAIN_ROOT "")
	if(CMAKE_C_COMPILER_ID STREQUAL "GNU")
		execute_process(
			COMMAND ${CMAKE_C_COMPILER} -print-file-name=libc.a
			OUTPUT_VARIABLE MWW_RAW_LIBC_PATH
			OUTPUT_STRIP_TRAILING_WHITESPACE
		)
		if(EXISTS "${MWW_RAW_LIBC_PATH}")
			get_filename_component(MWW_RESOLVED_LIBC "${MWW_RAW_LIBC_PATH}" REALPATH)
			get_filename_component(MWW_TOOLCHAIN_LIB_DIR "${MWW_RESOLVED_LIBC}" DIRECTORY)
			get_filename_component(MWW_TOOLCHAIN_ROOT "${MWW_TOOLCHAIN_LIB_DIR}" DIRECTORY)
		endif()
	endif()

	if(NOT EXISTS "${MWW_TOOLCHAIN_ROOT}")
		if(DEFINED CMAKE_SYSROOT AND EXISTS "${CMAKE_SYSROOT}/lib")
			set(MWW_TOOLCHAIN_ROOT "${CMAKE_SYSROOT}")
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
				file(GLOB MWW_SDK_SYSROOT
					"${MWW_SDK_DIR}/gnu/*${SOC_TOOLCHAIN_NAME}*/*${SOC_TOOLCHAIN_NAME}*")
				if(MWW_SDK_SYSROOT)
					list(GET MWW_SDK_SYSROOT 0 MWW_TOOLCHAIN_ROOT)
				endif()
			endif()
		endif()
	endif()

	if(NOT "${MWW_TOOLCHAIN_ROOT}" STREQUAL "")
		list(APPEND MWW_LIBS_PATHS ${MWW_TOOLCHAIN_ROOT}/lib)
	endif()

	if(CMAKE_C_COMPILER_ID STREQUAL "GNU")
		# expf(), logf() etc. used by the TFLM kernels: the base firmware
		# doesn't export a libm, so link the needed toolchain libc members
		# into the module. These aren't built with -mlongcalls, so their
		# soft-float helper calls must be resolved within the module too,
		# a direct call can't reach the base firmware.
		list(APPEND MWW_LLEXT_LIBS c gcc)
	endif()

	if(CMAKE_CXX_COMPILER_ID MATCHES "Clang")
		# Discover the C++ standard library headers of the GNU toolchain
		set(MWW_CXX_INCLUDE_DIRS "")
		if(NOT "${MWW_TOOLCHAIN_ROOT}" STREQUAL "" AND EXISTS "${MWW_TOOLCHAIN_ROOT}/include")
			set(MWW_INC_ROOT "${MWW_TOOLCHAIN_ROOT}/include")
			file(GLOB MWW_CXX_VERSION_DIRS LIST_DIRECTORIES true "${MWW_INC_ROOT}/c++/*")
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
			list(APPEND MWW_CXX_INCLUDE_DIRS "${MWW_INC_ROOT}")
		endif()

		target_compile_options(mww_tflm_lib PRIVATE
			-stdlib=libc++
			-fno-vectorize
			-fno-slp-vectorize
			-mcpu=${SOC_TOOLCHAIN_NAME}
		)
		if(MWW_CXX_INCLUDE_DIRS)
			target_include_directories(mww_tflm_lib SYSTEM PRIVATE ${MWW_CXX_INCLUDE_DIRS})
		endif()
	endif()
endif()

# TF_LITE_STRIP_ERROR_STRINGS drops DebugLog()/DebugVsnprintf()'s calls
# into vprintf/vsnprintf; NDEBUG drops libc assert(). Both chains pull
# in libc.a objects (vfprintf/fwrite/abort) that are not built PIC-safe.
target_compile_options(mww_tflm_lib PRIVATE
	-DTF_LITE_STRIP_ERROR_STRINGS
	-DNDEBUG
)

# Shared (ET_DYN) modules must be position independent
if(NOT CONFIG_LLEXT_TYPE_ELF_RELOCATABLE)
	target_compile_options(mww_tflm_lib PRIVATE -fPIC)
	if(MWW_NN_HIFI_LIB)
		target_compile_options(${MWW_NN_HIFI_LIB} PRIVATE -fPIC)
	endif()
endif()

sof_llext_build("mww"
	SOURCES
		../mww.c
		../llext-wrap.c
	INCLUDES
		${TFLM_PATH}
		${FLATBUFFERS_PATH}/include
		${GEMMLOWP_PATH}
		${RUY_PATH}
		${sof_top_dir}/posix/include
	LIBS
		${MWW_LLEXT_LIBS}
	LIBS_PATH
		${MWW_LIBS_PATHS}
)

if(CMAKE_CXX_COMPILER_ID STREQUAL "Clang" AND NOT ZEPHYR_TOOLCHAIN_VARIANT STREQUAL "xt-clang")
	set(MWW_LD_PATH "")
	if(DEFINED CMAKE_LINKER AND EXISTS "${CMAKE_LINKER}")
		set(MWW_LD_PATH "${CMAKE_LINKER}")
	else()
		execute_process(
			COMMAND ${CMAKE_C_COMPILER} -print-prog-name=ld
			OUTPUT_VARIABLE MWW_PROG_LD
			OUTPUT_STRIP_TRAILING_WHITESPACE
		)
		if(EXISTS "${MWW_PROG_LD}")
			set(MWW_LD_PATH "${MWW_PROG_LD}")
		elseif(EXISTS "${MWW_TOOLCHAIN_ROOT}-ld")
			set(MWW_LD_PATH "${MWW_TOOLCHAIN_ROOT}-ld")
		endif()
	endif()

	if(NOT "${MWW_LD_PATH}" STREQUAL "")
		llext_link_options(mww
			--target=xtensa-${SOC_TOOLCHAIN_NAME}_zephyr-elf
			--ld-path=${MWW_LD_PATH}
		)
	endif()
endif()
