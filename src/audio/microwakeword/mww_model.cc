// SPDX-License-Identifier: BSD-3-Clause
//
// Copyright(c) 2026 Intel Corporation. All rights reserved.

#include <algorithm>
#include <cstdint>
#include <iterator>

#include "tensorflow/lite/core/c/common.h"
#include "tensorflow/lite/micro/micro_allocator.h"
#include "tensorflow/lite/micro/micro_interpreter.h"
#include "tensorflow/lite/micro/micro_log.h"
#include "tensorflow/lite/micro/micro_mutable_op_resolver.h"
#include "tensorflow/lite/micro/micro_resource_variable.h"
#include "tensorflow/lite/micro/testing/micro_test.h"
#include "mww_model.h"

extern "C" int printk(const char *fmt, ...);

// hard code the model today
#include "mww_model_data.h"

static constexpr int kFeatureSize = MWW_FEATURE_SIZE;
static constexpr int kFeatureElementCount = MWW_FEATURE_ELEM_COUNT;

// Arena size is a generous guesstimate for the streaming MixConv graph (12
// ops incl. Conv2D/DepthwiseConv2D/FullyConnected plus 6 persistent resource
// -variable ring buffers). Refine down using
// MicroInterpreter::arena_used_bytes() once measured on hardware (logged in
// MWW_InitOps() below).
static constexpr size_t kDefaultArenaSize = 262144;

// inference
static const tflite::Model *model;
static TfLiteTensor *input;
static TfLiteTensor *output;
static tflite::MicroInterpreter *interpreter;
static tflite::MicroAllocator *allocator;
static tflite::MicroResourceVariables *resource_variables;

#include <new>

// stream, stream_1..stream_5: the MixConv graph's 6 persistent ring-buffer
// state variables (VAR_HANDLE/ASSIGN_VARIABLE/READ_VARIABLE), confirmed by
// direct flatbuffer inspection (see plan Stage 1) -- CALL_ONCE invokes a
// second subgraph that ASSIGN_VARIABLEs their zero initial state.
static constexpr int kNumResourceVariables = 6;

// 12 ops used by the hey_jarvis.tflite MixConv streaming graph, confirmed by
// direct flatbuffer inspection (see plan Stage 1): CALL_ONCE, VAR_HANDLE,
// READ_VARIABLE, ASSIGN_VARIABLE, RESHAPE, CONCATENATION, STRIDED_SLICE,
// CONV_2D, DEPTHWISE_CONV_2D, FULLY_CONNECTED, LOGISTIC, QUANTIZE.
using MwwOpResolver = tflite::MicroMutableOpResolver<12>;
static MwwOpResolver *op_resolver;

alignas(16) static uint8_t g_op_resolver_buf[sizeof(MwwOpResolver)];
alignas(16) static uint8_t g_interpreter_buf[sizeof(tflite::MicroInterpreter)];
alignas(16) static uint8_t g_mww_arena[kDefaultArenaSize];

int RegisterOps(MwwOpResolver *op_resolver) {
	printk("[MWW DBG] RegisterOps entry op_resolver=%p\n", (void *)op_resolver);
	TF_LITE_ENSURE_STATUS(op_resolver->AddCallOnce());
	printk("[MWW DBG] AddCallOnce OK\n");
	TF_LITE_ENSURE_STATUS(op_resolver->AddVarHandle());
	printk("[MWW DBG] AddVarHandle OK\n");
	TF_LITE_ENSURE_STATUS(op_resolver->AddReadVariable());
	printk("[MWW DBG] AddReadVariable OK\n");
	TF_LITE_ENSURE_STATUS(op_resolver->AddAssignVariable());
	printk("[MWW DBG] AddAssignVariable OK\n");
	TF_LITE_ENSURE_STATUS(op_resolver->AddReshape());
	printk("[MWW DBG] AddReshape OK\n");
	TF_LITE_ENSURE_STATUS(op_resolver->AddConcatenation());
	printk("[MWW DBG] AddConcatenation OK\n");
	TF_LITE_ENSURE_STATUS(op_resolver->AddStridedSlice());
	printk("[MWW DBG] AddStridedSlice OK\n");
	TF_LITE_ENSURE_STATUS(op_resolver->AddConv2D());
	printk("[MWW DBG] AddConv2D OK\n");
	TF_LITE_ENSURE_STATUS(op_resolver->AddDepthwiseConv2D());
	printk("[MWW DBG] AddDepthwiseConv2D OK\n");
	TF_LITE_ENSURE_STATUS(op_resolver->AddFullyConnected());
	printk("[MWW DBG] AddFullyConnected OK\n");
	TF_LITE_ENSURE_STATUS(op_resolver->AddLogistic());
	printk("[MWW DBG] AddLogistic OK\n");
	TF_LITE_ENSURE_STATUS(op_resolver->AddQuantize());
	printk("[MWW DBG] AddQuantize OK\n");
	return 0;
}

static int Init_Interpreter(struct mww_classify *mwc);

int MWW_InitOps(struct mww_classify *mwc, uint8_t *arena_buf, size_t arena_size)
{
	if (!arena_buf || arena_size == 0) {
		arena_buf = g_mww_arena;
		arena_size = kDefaultArenaSize;
	}
	printk("[MWW DBG] MWW_InitOps entry &model=%p model=%p arena_buf=%p arena_size=%zu\n",
	       (void *)&model, (void *)model, (void *)arena_buf, arena_size);

	op_resolver = new (g_op_resolver_buf) MwwOpResolver();
	printk("[MWW DBG] op_resolver=%p\n", (void *)op_resolver);

	if (RegisterOps(op_resolver) != 0) {
		mwc->error = "register ops failed";
		return -EINVAL;
	}
	printk("[MWW DBG] RegisterOps OK, model=%p arena_buf=%p arena_size=%zu\n",
	       (void *)model, (void *)arena_buf, arena_size);

	allocator = tflite::MicroAllocator::Create(arena_buf, arena_size);
	printk("[MWW DBG] allocator=%p\n", (void *)allocator);
	if (!allocator) {
		mwc->error = "allocator alloc failed (OOM)";
		return -ENOMEM;
	}

	resource_variables = tflite::MicroResourceVariables::Create(allocator, kNumResourceVariables);
	printk("[MWW DBG] resource_variables=%p\n", (void *)resource_variables);
	if (!resource_variables) {
		mwc->error = "resource_variables alloc failed (OOM)";
		return -ENOMEM;
	}

	// create the interpreter via placement new
	interpreter = new (g_interpreter_buf) tflite::MicroInterpreter(model, *op_resolver,
								       allocator, resource_variables);
	printk("[MWW DBG] interpreter=%p\n", (void *)interpreter);
	if (!interpreter) {
		mwc->error = "interpreter alloc failed (OOM)";
		return -ENOMEM;
	}

	printk("[MWW DBG] calling AllocateTensors\n");
	// and allocate the tensors
	if (interpreter->AllocateTensors() != kTfLiteOk) {
		mwc->error = "interpreter tensor allocate failed";
		return -EINVAL;
	}

	printk("[MWW DBG] AllocateTensors OK, arena_used=%u\n", (unsigned)interpreter->arena_used_bytes());

	// fetch input/output tensors + quantization params once; the
	// interpreter/tensors are stable for the lifetime of this instance
	return Init_Interpreter(mwc);
}

static int Init_Interpreter(struct mww_classify *mwc)
{
	input = interpreter->input(0);
	if (!input) {
		mwc->error = "input interpreter NULL";
		return -EINVAL;
	}

	// check input tensor element count is compatible with our feature
	// data size (shape is (1, MWW_FEATURE_SLICE_COUNT, MWW_FEATURE_SIZE),
	// not flattened to a single trailing dim, so check the full product)
	int in_elems = 1;
	for (int i = 0; i < input->dims->size; i++)
		in_elems *= input->dims->data[i];
	if (kFeatureElementCount != in_elems) {
		mwc->error = "input interpreter shape incompatible";
		return -EINVAL;
	}

	output = interpreter->output(0);
	if (!output) {
		mwc->error = "output interpreter NULL";
		return -EINVAL;
	}

	// single sigmoid wake-word probability, quantized uint8
	if (output->type != kTfLiteUInt8) {
		mwc->error = "output tensor type != uint8";
		return -EINVAL;
	}

	// expose the model's real input quantization params so callers can
	// requantize their features correctly instead of assuming a fixed
	// scale/zero_point.
	mwc->input_scale = input->params.scale;
	mwc->input_zero_point = input->params.zero_point;

	return 0;
}

int MWW_SetModel(struct mww_classify *mwc, unsigned char *model_tflite)
{
	const unsigned char *src_model = model_tflite ? model_tflite : g_mww_model_data;

	// Map the model into a usable data structure.
	model = tflite::GetModel(src_model);
	printk("[MWW DBG] src_model=%p &model=%p model=%p\n", (void *)src_model, (void *)&model, (void *)model);
	if (!model || model->version() != TFLITE_SCHEMA_VERSION) {
		mwc->error = "failed to load model";
		return -EINVAL;
	}
	printk("[MWW DBG] MWW_SetModel OK version=%u\n", (unsigned)model->version());

	return 0;
}

int MWW_ProcessClassify(struct mww_classify *mwc)
{
	float output_scale = output->params.scale;
	int output_zero_point = output->params.zero_point;

	// copy features to input then invoke()
	std::copy_n(mwc->audio_features, kFeatureElementCount,
		    tflite::GetTensorData<int8_t>(input));

	// run the interpreter
	if (interpreter->Invoke() != kTfLiteOk) {
		mwc->error = "invoke failed";
		return -EINVAL;
	}

	// Dequantize the single sigmoid probability output
	uint8_t raw = tflite::GetTensorData<uint8_t>(output)[0];

	mwc->raw_output = raw;
	mwc->probability = (raw - output_zero_point) * output_scale;

	return 0;
}
