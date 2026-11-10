# Copyright (c) 2026 Intel Corporation.
# SPDX-License-Identifier: Apache-2.0
#
# TFLM and HiFi4 nnlib libraries of the mww component, shared by the built-in
# (CMakeLists.txt) and the LLEXT (llext/CMakeLists.txt) builds so both compile
# the same sources. Defines mww_tflm_lib and, with the Cadence toolchain,
# mww_nn_hifi_lib. MWW_NN_HIFI_LIB is set to the nnlib library target or "".

# Own dedicated target (mww_tflm_lib), distinct from src/audio/tensorflow/'s
# tflm_lib -- this model needs a materially different TFLM kernel set
# (resource variables, CALL_ONCE, Concatenation, StridedSlice, Logistic,
# Quantize) that tflmcly's build does not compile in, so the two components
# do not share libraries yet.

# paths for dependencies
set(TFLM_PATH ${sof_top_dir}/../tflite-micro)
set(FLATBUFFERS_PATH ${sof_top_dir}/../flatbuffers)
set(GEMMLOWP_PATH ${sof_top_dir}/../gemmlowp)
set(RUY_PATH ${sof_top_dir}/../ruy)

set(MWW_HAL_SOC_PATH ${sof_top_dir}/../modules/hal/xtensa/zephyr/soc/${SOC_TOOLCHAIN_NAME})

# TFLM kernel sources beyond what tensorflow/CMakeLists.txt currently builds:
# the hey_jarvis.tflite MixConv streaming graph uses CALL_ONCE, VAR_HANDLE,
# READ_VARIABLE, ASSIGN_VARIABLE, CONCATENATION, STRIDED_SLICE, LOGISTIC and
# QUANTIZE in addition to CONV_2D/DEPTHWISE_CONV_2D/FULLY_CONNECTED/RESHAPE.
add_library(mww_tflm_lib STATIC
	${TFLM_PATH}/tensorflow/compiler/mlir/lite/core/api/error_reporter.cc
	${TFLM_PATH}/tensorflow/compiler/mlir/lite/schema/schema_utils.cc
	${TFLM_PATH}/tensorflow/lite/core/c/common.cc
	${TFLM_PATH}/tensorflow/lite/core/api/flatbuffer_conversions.cc
	${TFLM_PATH}/tensorflow/lite/core/api/tensor_utils.cc
	${TFLM_PATH}/tensorflow/lite/kernels/internal/common.cc
	${TFLM_PATH}/tensorflow/lite/kernels/internal/tensor_utils.cc
	${TFLM_PATH}/tensorflow/lite/kernels/internal/runtime_shape.cc
	${TFLM_PATH}/tensorflow/lite/kernels/internal/quantization_util.cc
	${TFLM_PATH}/tensorflow/lite/kernels/internal/tensor_ctypes.cc
	${TFLM_PATH}/tensorflow/lite/kernels/internal/portable_tensor_utils.cc
	${TFLM_PATH}/tensorflow/lite/kernels/internal/reference/portable_tensor_utils.cc
	${TFLM_PATH}/tensorflow/lite/kernels/kernel_util.cc
	${TFLM_PATH}/tensorflow/lite/micro/kernels/kernel_util.cc
	${TFLM_PATH}/tensorflow/lite/micro/kernels/call_once.cc
	${TFLM_PATH}/tensorflow/lite/micro/kernels/var_handle.cc
	${TFLM_PATH}/tensorflow/lite/micro/kernels/read_variable.cc
	${TFLM_PATH}/tensorflow/lite/micro/kernels/assign_variable.cc
	${TFLM_PATH}/tensorflow/lite/micro/kernels/concatenation.cc
	${TFLM_PATH}/tensorflow/lite/micro/kernels/conv_common.cc
	${TFLM_PATH}/tensorflow/lite/micro/kernels/depthwise_conv_common.cc
	${TFLM_PATH}/tensorflow/lite/micro/kernels/fully_connected_common.cc
	${TFLM_PATH}/tensorflow/lite/micro/kernels/logistic_common.cc
	${TFLM_PATH}/tensorflow/lite/micro/kernels/quantize_common.cc
	${TFLM_PATH}/tensorflow/lite/micro/kernels/reshape_common.cc
	${TFLM_PATH}/tensorflow/lite/micro/kernels/strided_slice_common.cc
	${TFLM_PATH}/tensorflow/lite/micro/kernels/dequantize_common.cc
	${TFLM_PATH}/tensorflow/lite/micro/mock_micro_graph.cc
	${TFLM_PATH}/tensorflow/lite/micro/flatbuffer_utils.cc
	${TFLM_PATH}/tensorflow/lite/micro/micro_interpreter_graph.cc
	${TFLM_PATH}/tensorflow/lite/micro/micro_resource_variable.cc
	${TFLM_PATH}/tensorflow/lite/micro/micro_interpreter_context.cc
	${TFLM_PATH}/tensorflow/lite/micro/fake_micro_context.cc
	${TFLM_PATH}/tensorflow/lite/micro/arena_allocator/persistent_arena_buffer_allocator.cc
	${TFLM_PATH}/tensorflow/lite/micro/arena_allocator/recording_single_arena_buffer_allocator.cc
	${TFLM_PATH}/tensorflow/lite/micro/arena_allocator/non_persistent_arena_buffer_allocator.cc
	${TFLM_PATH}/tensorflow/lite/micro/arena_allocator/single_arena_buffer_allocator.cc
	${TFLM_PATH}/tensorflow/lite/micro/micro_allocator.cc
	${TFLM_PATH}/tensorflow/lite/micro/tflite_bridge/flatbuffer_conversions_bridge.cc
	${TFLM_PATH}/tensorflow/lite/micro/tflite_bridge/micro_error_reporter.cc
	${TFLM_PATH}/tensorflow/lite/micro/system_setup.cc
	${TFLM_PATH}/tensorflow/lite/micro/test_helper_custom_ops.cc
	${TFLM_PATH}/tensorflow/lite/micro/micro_context.cc
	${TFLM_PATH}/tensorflow/lite/micro/micro_log.cc
	${TFLM_PATH}/tensorflow/lite/micro/micro_profiler.cc
	${TFLM_PATH}/tensorflow/lite/micro/micro_time.cc
	# debug_log.cc is replaced by our printk-backed DebugLog() in mww.c.
	#${TFLM_PATH}/tensorflow/lite/micro/debug_log.cc
	${TFLM_PATH}/tensorflow/lite/micro/test_helpers.cc
	${TFLM_PATH}/tensorflow/lite/micro/micro_op_resolver.cc
	${TFLM_PATH}/tensorflow/lite/micro/recording_micro_allocator.cc
	${TFLM_PATH}/tensorflow/lite/micro/memory_helpers.cc
	${TFLM_PATH}/tensorflow/lite/micro/micro_allocation_info.cc
	${TFLM_PATH}/tensorflow/lite/micro/memory_planner/greedy_memory_planner.cc
	${TFLM_PATH}/tensorflow/lite/micro/memory_planner/non_persistent_buffer_planner_shim.cc
	${TFLM_PATH}/tensorflow/lite/micro/memory_planner/linear_memory_planner.cc
	${TFLM_PATH}/tensorflow/lite/micro/micro_utils.cc
	${TFLM_PATH}/tensorflow/lite/micro/micro_interpreter.cc
	${CMAKE_CURRENT_LIST_DIR}/mww_model.cc
)

target_include_directories(mww_tflm_lib PRIVATE
	${TFLM_PATH}
	${FLATBUFFERS_PATH}/include
	${GEMMLOWP_PATH}
	${RUY_PATH}
	${sof_top_dir}/posix/include
	${sof_top_dir}/../modules/hal/xtensa/include
	${MWW_HAL_SOC_PATH}
)

target_compile_options(mww_tflm_lib PRIVATE
	-std=c++17
	-fno-rtti
	-fno-exceptions
	-fno-threadsafe-statics
	-Wnon-virtual-dtor
	-fno-unwind-tables
	-fmessage-length=0
	-DTF_LITE_STATIC_MEMORY
	-DTF_LITE_DISABLE_X86_NEON
	-Wsign-compare
	-Wdouble-promotion
	-Wunused-variable
	-Wswitch
	-Wvla
	-Wall
	-Wextra
	-Wmissing-field-initializers
	-Wstrict-aliasing
	-Wno-unused-parameter
	-DXTENSA
	-DTF_LITE_MCU_DEBUG_LOG
	-DTF_LITE_USE_CTIME
	-mlongcalls
	-Wno-shadow
	-fno-tree-vectorize
	-fno-tree-slp-vectorize
	-ffunction-sections
	-fdata-sections
	-imacros ${AUTOCONF_H}
)

# tflite-micro trees differ here: upstream (the commit pinned by
# submanifests/tflm.yml) declares IBufferAllocator's methods pure virtual in
# the header only, while trees carrying the Xtensa ICF vtable-folding fix move them
# out of line into ibuffer_allocator.cc. Compile the file when it is present so both
# layouts link.
if(EXISTS "${TFLM_PATH}/tensorflow/lite/micro/arena_allocator/ibuffer_allocator.cc")
	target_sources(mww_tflm_lib PRIVATE
		${TFLM_PATH}/tensorflow/lite/micro/arena_allocator/ibuffer_allocator.cc)
endif()

if(CONFIG_COMP_MWW_PROFILE)
	target_compile_definitions(mww_tflm_lib PRIVATE MWW_PROFILE=1)
endif()

# nnlib uses HiFi intrinsics, which only the Cadence toolchain provides.
set(MWW_NN_HIFI_LIB "")
if(CONFIG_COMP_MWW_HIFI_NNLIB AND ZEPHYR_TOOLCHAIN_VARIANT STREQUAL "xt-clang")
	set(NN_HIFI_PATH ${sof_top_dir}/../nnlib-hifi4/xa_nnlib)
	set(MWW_XTENSA_KERNELS ${TFLM_PATH}/tensorflow/lite/micro/kernels/xtensa)

	target_sources(mww_tflm_lib PRIVATE
		${MWW_XTENSA_KERNELS}/conv.cc
		${MWW_XTENSA_KERNELS}/conv_common_xtensa.cc
		${MWW_XTENSA_KERNELS}/conv_hifi.cc
		${MWW_XTENSA_KERNELS}/conv_int8_int16.cc
		${MWW_XTENSA_KERNELS}/conv_int8_reference.cc
		${MWW_XTENSA_KERNELS}/conv_int16_reference.cc
		${MWW_XTENSA_KERNELS}/conv_vision.cc
		${MWW_XTENSA_KERNELS}/depthwise_conv.cc
		${MWW_XTENSA_KERNELS}/depthwise_conv_hifi.cc
		${MWW_XTENSA_KERNELS}/depthwise_conv_vision.cc
		${MWW_XTENSA_KERNELS}/fully_connected.cc
		${MWW_XTENSA_KERNELS}/fully_connected_common_xtensa.cc
		${MWW_XTENSA_KERNELS}/fully_connected_int8.cc
		${MWW_XTENSA_KERNELS}/fully_connected_vision.cc
		${MWW_XTENSA_KERNELS}/logistic.cc
		${MWW_XTENSA_KERNELS}/quantize.cc
		${MWW_XTENSA_KERNELS}/dequantize.cc
		${MWW_XTENSA_KERNELS}/reshape.cc
		${MWW_XTENSA_KERNELS}/reshape_vision.cc
		${MWW_XTENSA_KERNELS}/strided_slice.cc
	)
	target_include_directories(mww_tflm_lib PRIVATE
		${NN_HIFI_PATH}
		${NN_HIFI_PATH}/include
	)
	target_compile_options(mww_tflm_lib PRIVATE
		-mcoproc
		-DHIFI4
		-DNNLIB_V2
		-DKERNELS_OPTIMIZED_FOR_SPEED
	)

	# All HiFi4 nnlib sources, the linker drops what the kernels don't use.
	file(GLOB MWW_NN_HIFI_SOURCES
		${NN_HIFI_PATH}/algo/common/src/*.c
		${NN_HIFI_PATH}/algo/kernels/*/hifi4/*.c
		${NN_HIFI_PATH}/algo/ndsp/hifi4/src/*.c
	)

	add_library(mww_nn_hifi_lib STATIC ${MWW_NN_HIFI_SOURCES})
	target_include_directories(mww_nn_hifi_lib PRIVATE
		${NN_HIFI_PATH}/include
		${NN_HIFI_PATH}/algo/common/include/
		${NN_HIFI_PATH}/include/nnlib/
		${NN_HIFI_PATH}/algo/ndsp/hifi4/include
	)
	target_compile_options(mww_nn_hifi_lib PRIVATE
		-fsigned-char
		-fno-exceptions
		-mlongcalls
		-INLINE:requested
		-mcoproc
		-fno-zero-initialized-in-bss
		-mtext-section-literals
		-Wsign-compare
		-m32
		-DMODEL_INT16
		-DNNLIB_V2
		-Dhifi4
		-DTFLITE_SINGLE_ROUNDING=1
		# nnlib exports a global gcd() that clashes with SOF's
		-Dgcd=xa_nn_gcd
		-ffunction-sections
		-fdata-sections
	)
	set(MWW_NN_HIFI_LIB mww_nn_hifi_lib)
else()
	target_sources(mww_tflm_lib PRIVATE
		${TFLM_PATH}/tensorflow/lite/micro/kernels/conv.cc
		${TFLM_PATH}/tensorflow/lite/micro/kernels/depthwise_conv.cc
		${TFLM_PATH}/tensorflow/lite/micro/kernels/fully_connected.cc
		${TFLM_PATH}/tensorflow/lite/micro/kernels/logistic.cc
		${TFLM_PATH}/tensorflow/lite/micro/kernels/quantize.cc
		${TFLM_PATH}/tensorflow/lite/micro/kernels/reshape.cc
		${TFLM_PATH}/tensorflow/lite/micro/kernels/strided_slice.cc
		${TFLM_PATH}/tensorflow/lite/micro/kernels/dequantize.cc
	)
endif()
