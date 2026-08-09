# Copyright (c) 2025 Intel Corporation.
# SPDX-License-Identifier: Apache-2.0

# TODO: detect Hifi4/5 for NN lib kernels
set(NN_HIFI_PATH ${sof_top_dir}/../nnlib-hifi4/xa_nnlib)

# paths for dependencies
set(TFLM_PATH ${sof_top_dir}/../tflite-micro)
set(FLATBUFFERS_PATH ${sof_top_dir}/../flatbuffers)
set(GEMMLOWP_PATH ${sof_top_dir}/../gemmlowp)
set(RUY_PATH ${sof_top_dir}/../ruy)

if(CMAKE_C_COMPILER_ID STREQUAL "Xtensa" OR CMAKE_C_COMPILER_ID STREQUAL "Clang")



set(TFLM_EXTRA_LIBS nn_hifi_lib)
add_library(nn_hifi_lib STATIC


	${NN_HIFI_PATH}/algo/common/src/xa_nnlib_common_api.c
	${NN_HIFI_PATH}/algo/kernels/activations/hifi4/xa_nn_activations_32_32.c
	${NN_HIFI_PATH}/algo/kernels/activations/hifi4/xa_nn_activations_f32_f32.c
	# ${NN_HIFI_PATH}/algo/kernels/basic/hifi4/xa_nn_elm_abs_f32.c
	# ${NN_HIFI_PATH}/algo/kernels/basic/hifi4/xa_nn_elm_add_f32.c
	# ${NN_HIFI_PATH}/algo/kernels/basic/hifi4/xa_nn_elm_sub_f32.c
	# ${NN_HIFI_PATH}/algo/kernels/basic/hifi4/xa_nn_elm_mul_f32.c
	# ${NN_HIFI_PATH}/algo/kernels/basic/hifi4/xa_nn_elm_div_f32.c
	# ${NN_HIFI_PATH}/algo/kernels/basic/hifi4/xa_nn_elm_neg_f32.c
	# ${NN_HIFI_PATH}/algo/kernels/basic/hifi4/xa_nn_elm_square_f32.c
	${NN_HIFI_PATH}/algo/ndsp/hifi4/src/nanf_tbl.c
	${NN_HIFI_PATH}/algo/ndsp/hifi4/src/inff_tbl.c
	${NN_HIFI_PATH}/algo/ndsp/hifi4/src/pow2f_tbl.c
	${NN_HIFI_PATH}/algo/ndsp/hifi4/src/expf_tbl.c

	# ${NN_HIFI_PATH}/algo/kernels/cnn/hifi4/xa_nn_conv2d_depthwise_sym8sxasym8s.c
	${NN_HIFI_PATH}/algo/kernels/cnn/hifi4/xa_nn_matXvec_8x8_8_circ.c
	# ${NN_HIFI_PATH}/algo/kernels/cnn/hifi4/xa_nn_conv2d_pointwise_sym8sxsym16s.c
	${NN_HIFI_PATH}/algo/kernels/cnn/hifi4/xa_nn_conv2d_pointwise_8x16.c
	${NN_HIFI_PATH}/algo/kernels/cnn/hifi4/xa_nn_circ_buf.c
	${NN_HIFI_PATH}/algo/kernels/cnn/hifi4/xa_nn_conv2d_std_8x8.c
	${NN_HIFI_PATH}/algo/kernels/cnn/hifi4/xa_nn_conv2d_depthwise.c
	${NN_HIFI_PATH}/algo/kernels/cnn/hifi4/xa_nn_matXvec_16x16_16_circ.c
	${NN_HIFI_PATH}/algo/kernels/cnn/hifi4/xa_nn_conv2d_std_circ_buf.c
	# ${NN_HIFI_PATH}/algo/kernels/cnn/hifi4/xa_nn_transpose_conv_sym8sxasym8s.c
	${NN_HIFI_PATH}/algo/kernels/cnn/hifi4/xa_nn_matXvec_8x16_16_circ.c
	# ${NN_HIFI_PATH}/algo/kernels/cnn/hifi4/xa_nn_matXvec_sym8sxsym16s_sym16s_circ.c
	${NN_HIFI_PATH}/algo/kernels/cnn/hifi4/xa_nn_conv1d_std_8x8.c
	${NN_HIFI_PATH}/algo/kernels/cnn/hifi4/xa_nn_conv2d_std_8x16.c
	# ${NN_HIFI_PATH}/algo/kernels/cnn/hifi4/xa_nn_conv2d_std_sym8sxsym16s.c
	# ${NN_HIFI_PATH}/algo/kernels/cnn/hifi4/xa_nn_conv2d_std_sym8sxasym8s.c
	# ${NN_HIFI_PATH}/algo/kernels/cnn/hifi4/xa_nn_matXvec_asym8xasym8_asym8_circ.c
	# ${NN_HIFI_PATH}/algo/kernels/cnn/hifi4/xa_nn_conv2d_pointwise_sym8sxasym8s.c
	${NN_HIFI_PATH}/algo/kernels/cnn/hifi4/xa_nn_conv2d_depthwise_8x8.c
	# ${NN_HIFI_PATH}/algo/kernels/cnn/hifi4/xa_nn_transpose_conv_sym8sxsym16s.c
	${NN_HIFI_PATH}/algo/kernels/cnn/hifi4/xa_nn_conv1d_std_8x16.c
	${NN_HIFI_PATH}/algo/kernels/cnn/hifi4/xa_nn_conv2d_pointwise_8x8.c
	${NN_HIFI_PATH}/algo/kernels/cnn/hifi4/xa_nn_conv2d_std_16x16.c
	# ${NN_HIFI_PATH}/algo/kernels/cnn/hifi4/xa_nn_matXvec_sym8sxasym8s_asym8s_circ.c
	${NN_HIFI_PATH}/algo/kernels/cnn/hifi4/xa_nn_conv2d_pointwise_asym8xasym8.c
	${NN_HIFI_PATH}/algo/kernels/cnn/hifi4/xa_nn_conv2d_std_asym8xasym8.c
	${NN_HIFI_PATH}/algo/kernels/cnn/hifi4/xa_nn_conv1d_std_circ_buf.c
	${NN_HIFI_PATH}/algo/kernels/cnn/hifi4/xa_nn_conv1d_std_asym8xasym8.c
	${NN_HIFI_PATH}/algo/kernels/cnn/hifi4/xa_nn_matXvec_8x16_16_circ_nb.c
	${NN_HIFI_PATH}/algo/kernels/cnn/hifi4/xa_nn_conv1d_std_16x16.c
	${NN_HIFI_PATH}/algo/kernels/cnn/hifi4/xa_nn_matXvec_8x8_8_circ_nb.c
	# ${NN_HIFI_PATH}/algo/kernels/cnn/hifi4/xa_nn_conv2d_std_sym4sxasym8s.c
	# ${NN_HIFI_PATH}/algo/kernels/cnn/hifi4/xa_nn_conv2d_depthwise_sym8sxsym16s.c
	${NN_HIFI_PATH}/algo/kernels/cnn/hifi4/xa_nn_conv2d_depthwise_asym8xasym8.c
	# ${NN_HIFI_PATH}/algo/kernels/cnn/hifi4/xa_nn_conv2d_depthwise_8x16.c

	${NN_HIFI_PATH}/algo/kernels/fc/hifi4/xa_nn_fully_connected.c
	# ${NN_HIFI_PATH}/algo/kernels/matXvec/hifi4/xa_nn_matXvec_sym8sxasym8s.c
	# ${NN_HIFI_PATH}/algo/kernels/matXvec/hifi4/xa_nn_matXvec_f32.c
	# ${NN_HIFI_PATH}/algo/kernels/matXvec/hifi4/xa_nn_matmul_f32.c
	# ${NN_HIFI_PATH}/algo/kernels/norm/hifi4/xa_nn_l2_norm_f32.c
	# ${NN_HIFI_PATH}/algo/kernels/norm/hifi4/xa_nn_l2_norm_asym8s.c
	${NN_HIFI_PATH}/algo/kernels/pool/hifi4/xa_nn_avgpool_f32.c
	${NN_HIFI_PATH}/algo/kernels/pool/hifi4/xa_nn_maxpool_f32.c
	${NN_HIFI_PATH}/algo/kernels/pool/hifi4/xa_nn_inv_256_tbl.c
	${NN_HIFI_PATH}/algo/kernels/reorg/hifi4/xa_nn_transpose_8.c
	${NN_HIFI_PATH}/algo/kernels/reorg/hifi4/xa_nn_pad_8.c
)









target_compile_definitions(nn_hifi_lib PRIVATE
	-DHIFI4=1
	-DHAVE_VFPU=0
	-DHAVE_VFPU_SINGLE_PRECISION=0
	-DXCHAL_HAVE_HIFI4=1
	-DXCHAL_HAVE_HIFI4_VFPU=0

	__xtensa__=1
	__XTENSA__=1
	__XCC__
	__XCC_CLANG__
	"XT_MAX(a,b)=((a)>(b)?(a):(b))"
	"XT_MIN(a,b)=((a)<(b)?(a):(b))"
	"AE_MOVINT16_FROMINT32(v)=((ae_int16)(v))"
	"AE_CVT64F32_H(v)=((ae_int64)(int64_t)(int32_t)AE_MOVAD32_H(v))"
	"AE_CVT64F32_L(v)=((ae_int64)(int64_t)(int32_t)AE_MOVAD32_L(v))"
)




target_include_directories(nn_hifi_lib PRIVATE

	${NN_HIFI_PATH}/include
	${NN_HIFI_PATH}/algo/common/include/
	${NN_HIFI_PATH}/include/nnlib/
	${NN_HIFI_PATH}/algo/ndsp/hifi4/include
	${sof_top_dir}/../modules/hal/xtensa/include
	${sof_top_dir}/../modules/hal/xtensa/zephyr/soc/intel_ace15_mtpm
	/home/lrg/zephyr-sdk-1.0.1/gnu/xtensa-intel_ace15_mtpm_zephyr-elf/xtensa-intel_ace15_mtpm_zephyr-elf/include
	/home/lrg/work/llvm-project-nnlib/build/lib/clang/24/include
)


if(CMAKE_C_COMPILER_ID STREQUAL "Clang")
	target_compile_definitions(nn_hifi_lib PRIVATE
		__XCC__
		__XCC_CLANG__
	)
	target_compile_options(nn_hifi_lib PRIVATE
		-mcpu=intel_ace15_mtpm
		"SHELL:-include xtensahifiintrin.h"
	)
endif()









# TODO: Need to detect and add mul16/32 options.
#ifeq "$(has_mul16)" "0"
#CFLAGS += -mno-mul16
#endif
#ifeq "$(has_mul32)" "0"
#CFLAGS += -mno-mul32 -mno-div32
#endif
target_compile_options(nn_hifi_lib PRIVATE
	-O2
	"SHELL:-include ${NN_HIFI_PATH}/algo/common/include/xa_nnlib_hifi_isa_compat.h"
	-fsigned-char
	-fno-exceptions
	-mlongcalls
	-fno-zero-initialized-in-bss
	-mtext-section-literals
	-Wsign-compare
	-DMODEL_INT16
	-DNNLIB_V2
	-Dhifi4
	-DTFLITE_SINGLE_ROUNDING=1
)


if(CMAKE_C_COMPILER_ID STREQUAL "Xtensa")
target_compile_options(nn_hifi_lib PRIVATE
	-INLINE:requested
	-mcoproc
	-m32
)
endif()
else()

set(TFLM_EXTRA_LIBS "")
endif()



if(CMAKE_C_COMPILER_ID STREQUAL "Xtensa" OR CMAKE_C_COMPILER_ID STREQUAL "Clang")
	set(TFLM_KERNEL_SOURCES
		${TFLM_PATH}/tensorflow/lite/micro/kernels/xtensa/sub.cc
		${TFLM_PATH}/tensorflow/lite/micro/kernels/xtensa/conv_hifi.cc
		${TFLM_PATH}/tensorflow/lite/micro/kernels/xtensa/pooling_vision.cc
		${TFLM_PATH}/tensorflow/lite/micro/kernels/xtensa/softmax_vision.cc
		# ${TFLM_PATH}/tensorflow/lite/micro/kernels/xtensa/unidirectional_sequence_lstm.cc
		${TFLM_PATH}/tensorflow/lite/micro/kernels/xtensa/transpose_conv.cc
		${TFLM_PATH}/tensorflow/lite/micro/kernels/xtensa/fully_connected_common_xtensa.cc
		# ${TFLM_PATH}/tensorflow/lite/micro/kernels/xtensa/lstm_eval_hifi.cc
		${TFLM_PATH}/tensorflow/lite/micro/kernels/xtensa/add_vision.cc
		${TFLM_PATH}/tensorflow/lite/micro/kernels/xtensa/depthwise_conv.cc
		${TFLM_PATH}/tensorflow/lite/micro/kernels/xtensa/reshape.cc
		${TFLM_PATH}/tensorflow/lite/micro/kernels/xtensa/softmax.cc
		${TFLM_PATH}/tensorflow/lite/micro/kernels/xtensa/depthwise_conv_hifi.cc
		${TFLM_PATH}/tensorflow/lite/micro/kernels/xtensa/conv_int16_reference.cc
		${TFLM_PATH}/tensorflow/lite/micro/kernels/xtensa/conv_int8_reference.cc
		${TFLM_PATH}/tensorflow/lite/micro/kernels/xtensa/fully_connected_int8.cc
		${TFLM_PATH}/tensorflow/lite/micro/kernels/xtensa/pad_vision.cc
		${TFLM_PATH}/tensorflow/lite/micro/kernels/xtensa/fully_connected_vision.cc
		${TFLM_PATH}/tensorflow/lite/micro/kernels/xtensa/conv_int8_int16.cc
		${TFLM_PATH}/tensorflow/lite/micro/kernels/xtensa/conv.cc
		${TFLM_PATH}/tensorflow/lite/micro/kernels/xtensa/softmax_int8_int16.cc
		${TFLM_PATH}/tensorflow/lite/micro/kernels/xtensa/logistic.cc
		${TFLM_PATH}/tensorflow/lite/micro/kernels/xtensa/reduce.cc
		# ${TFLM_PATH}/tensorflow/lite/micro/kernels/xtensa/lstm_eval.cc
		${TFLM_PATH}/tensorflow/lite/micro/kernels/xtensa/reshape_vision.cc
		${TFLM_PATH}/tensorflow/lite/micro/kernels/xtensa/pad.cc
		${TFLM_PATH}/tensorflow/lite/micro/kernels/xtensa/reduce_vision.cc
		${TFLM_PATH}/tensorflow/lite/micro/kernels/xtensa/dequantize.cc
		${TFLM_PATH}/tensorflow/lite/micro/kernels/xtensa/add.cc
		${TFLM_PATH}/tensorflow/lite/micro/kernels/xtensa/pooling_int8.cc
		${TFLM_PATH}/tensorflow/lite/micro/kernels/xtensa/depthwise_conv_vision.cc
		${TFLM_PATH}/tensorflow/lite/micro/kernels/xtensa/strided_slice.cc
		${TFLM_PATH}/tensorflow/lite/micro/kernels/xtensa/conv_common_xtensa.cc
		${TFLM_PATH}/tensorflow/lite/micro/kernels/xtensa/svdf.cc
		${TFLM_PATH}/tensorflow/lite/micro/kernels/xtensa/fully_connected.cc
		${TFLM_PATH}/tensorflow/lite/micro/kernels/xtensa/leaky_relu.cc
		${TFLM_PATH}/tensorflow/lite/micro/kernels/xtensa/quantize.cc
		${TFLM_PATH}/tensorflow/lite/micro/kernels/xtensa/pooling.cc
		${TFLM_PATH}/tensorflow/lite/micro/kernels/xtensa/conv_vision.cc
	)
else()
	set(TFLM_KERNEL_SOURCES
		${TFLM_PATH}/tensorflow/lite/micro/kernels/sub.cc
		${TFLM_PATH}/tensorflow/lite/micro/kernels/conv.cc
		${TFLM_PATH}/tensorflow/lite/micro/kernels/depthwise_conv.cc
		${TFLM_PATH}/tensorflow/lite/micro/kernels/fully_connected.cc
		${TFLM_PATH}/tensorflow/lite/micro/kernels/reshape.cc
		${TFLM_PATH}/tensorflow/lite/micro/kernels/softmax.cc
	)
endif()

# TODO: complete sources have been added here from userspace build but
# not all are needed so this is a list of "needed" sources to build
# a memory and performance optimized TFLM for SOF.
# All sources have been left in for completeness now, but will be removed
# when support is mature.
add_library(tflm_lib STATIC



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
	${TFLM_PATH}/tensorflow/lite/kernels/internal/reference/comparisons.cc
	${TFLM_PATH}/tensorflow/lite/kernels/internal/reference/portable_tensor_utils.cc
	${TFLM_PATH}/tensorflow/lite/kernels/kernel_util.cc
	# Stock micro kernels - not needed atm
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/activations_common.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/activations.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/add_common.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/add_n.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/arg_min_max.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/assign_variable.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/batch_matmul.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/batch_to_space_nd.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/broadcast_args.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/broadcast_to.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/call_once.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/cast.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/ceil.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/circular_buffer_common.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/circular_buffer.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/comparisons.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/concatenation.cc
	${TFLM_PATH}/tensorflow/lite/micro/kernels/conv_common.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/cumsum.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/depth_to_space.cc
	${TFLM_PATH}/tensorflow/lite/micro/kernels/depthwise_conv_common.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/dequantize_common.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/detection_postprocess.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/div.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/elementwise.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/elu.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/embedding_lookup.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/ethosu.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/expand_dims.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/exp.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/fill.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/floor_div.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/floor_mod.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/floor.cc
	${TFLM_PATH}/tensorflow/lite/micro/kernels/fully_connected_common.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/gather_nd.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/gather.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/hard_swish_common.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/hard_swish.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/if.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/kernel_runner.cc
	${TFLM_PATH}/tensorflow/lite/micro/kernels/kernel_util.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/l2norm.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/l2_pool_2d.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/leaky_relu_common.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/logical_common.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/logical.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/logistic_common.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/log_softmax.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/lstm_eval_common.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/maximum_minimum.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/micro_tensor_utils.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/mirror_pad.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/mul_common.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/mul.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/neg.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/pack.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/pooling_common.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/prelu_common.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/prelu.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/quantize_common.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/read_variable.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/reduce_common.cc
	${TFLM_PATH}/tensorflow/lite/micro/kernels/reshape_common.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/resize_bilinear.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/resize_nearest_neighbor.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/round.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/select.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/shape.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/slice.cc
	${TFLM_PATH}/tensorflow/lite/micro/kernels/softmax_common.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/space_to_batch_nd.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/space_to_depth.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/split.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/split_v.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/squared_difference.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/squeeze.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/strided_slice_common.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/sub_common.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/svdf_common.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/tanh.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/transpose.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/unpack.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/var_handle.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/while.cc
	#${TFLM_PATH}/tensorflow/lite/micro/kernels/zeros_like.cc

	${TFLM_KERNEL_SOURCES}

	${TFLM_PATH}/tensorflow/lite/micro/mock_micro_graph.cc
	${TFLM_PATH}/tensorflow/lite/micro/micro_interpreter_graph.cc
	${TFLM_PATH}/tensorflow/lite/micro/micro_resource_variable.cc
	${TFLM_PATH}/tensorflow/lite/micro/micro_interpreter_context.cc
	${TFLM_PATH}/tensorflow/lite/micro/fake_micro_context.cc
	${TFLM_PATH}/tensorflow/lite/micro/arena_allocator/persistent_arena_buffer_allocator.cc
	${TFLM_PATH}/tensorflow/lite/micro/arena_allocator/recording_single_arena_buffer_allocator.cc
	${TFLM_PATH}/tensorflow/lite/micro/arena_allocator/non_persistent_arena_buffer_allocator.cc
	${TFLM_PATH}/tensorflow/lite/micro/arena_allocator/single_arena_buffer_allocator.cc
	${TFLM_PATH}/tensorflow/lite/micro/micro_allocator.cc
	../micro_speech_quantized_model_data.cc
	../speech.cc
)


target_include_directories(tflm_lib PRIVATE
	${TFLM_PATH}
	${FLATBUFFERS_PATH}/include
	${GEMMLOWP_PATH}
	${RUY_PATH}
	${NN_HIFI_PATH}
	${NN_HIFI_PATH}/include
	${NN_HIFI_PATH}/algo/common/include
	${sof_top_dir}/../modules/hal/xtensa/include
	${sof_top_dir}/../modules/hal/xtensa/zephyr/soc/intel_ace15_mtpm
	${sof_top_dir}/posix/include
)

# TODO: Need to detect and add mul16/32 options.
#ifeq "$(has_mul16)" "0"
#CFLAGS += -mno-mul16
#endif
#ifeq "$(has_mul32)" "0"
#CFLAGS += -mno-mul32 -mno-div32
#endif
target_compile_options(tflm_lib PRIVATE
	-std=c++17
	-fPIC
	-fno-rtti
	-fno-exceptions
	-fno-threadsafe-statics
	-Wnon-virtual-dtor
	-fno-unwind-tables
	-fno-asynchronous-unwind-tables


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
	-DKERNELS_OPTIMIZED_FOR_SPEED
	-DTF_LITE_MCU_DEBUG_LOG
	-DTF_LITE_USE_CTIME
	-DHIFI4
	-mlongcalls
	-DNNLIB_V2
	-fno-function-sections
	-fno-data-sections
	"SHELL:-include xtensahifiintrin.h"
	"SHELL:-include /home/lrg/work/sof-arl/nnlib-hifi4/xa_nnlib/algo/common/include/xa_nnlib_hifi_isa_compat.h"


	-Wno-shadow
)


if(CMAKE_CXX_COMPILER_ID MATCHES "Clang")
	target_include_directories(tflm_lib SYSTEM PRIVATE
		/home/lrg/zephyr-sdk-1.0.1/gnu/xtensa-intel_ace15_mtpm_zephyr-elf/xtensa-intel_ace15_mtpm_zephyr-elf/include/c++/14.3.0
		/home/lrg/zephyr-sdk-1.0.1/gnu/xtensa-intel_ace15_mtpm_zephyr-elf/xtensa-intel_ace15_mtpm_zephyr-elf/include/c++/14.3.0/xtensa-intel_ace15_mtpm_zephyr-elf
		/home/lrg/zephyr-sdk-1.0.1/gnu/xtensa-intel_ace15_mtpm_zephyr-elf/xtensa-intel_ace15_mtpm_zephyr-elf/include
	)
endif()

target_compile_options(tflm_lib PRIVATE
	-ffunction-sections
	-fdata-sections
)





if(CMAKE_C_COMPILER_ID STREQUAL "Xtensa")
	target_compile_options(tflm_lib PRIVATE
		--xtensa-core=ace10_LX7HiFi4_2022_10
		-mcoproc
	)
endif()


if(CMAKE_C_COMPILER_ID STREQUAL "Xtensa")
	set(TFLM_STD_LIBS c++ hal)
else()
	set(TFLM_STD_LIBS stdc++)
endif()

sof_llext_build("tflm"
	SOURCES ../tflm-classify.c ../llext-wrap.c tests/test_basic.c tests/test_activations.c tests/test_cnn.c tests/test_fc.c tests/test_matxvec.c tests/test_norm.c tests/test_pool.c tests/test_reorg.c

	INCLUDES
		${TFLM_PATH}
		${NN_HIFI_PATH}/algo/common/include
		${NN_HIFI_PATH}/algo/kernels/include
		${NN_HIFI_PATH}/include
		${sof_top_dir}/../modules/hal/xtensa/zephyr/soc/intel_ace15_mtpm


	CFLAGS -fno-function-sections -fno-data-sections -D__XCC__ -D__XCC_CLANG__


	LIBS ${TFLM_EXTRA_LIBS}

	LIBS_PATH .
)

llext_link_options("tflm" -Wl,--whole-archive -Wl,-s)












