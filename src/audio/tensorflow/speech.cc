// SPDX-License-Identifier: BSD-3-Clause
//
// Copyright(c) 2025 Intel Corporation. All rights reserved.

#include <stdint.h>
#include <string.h>

#include "tensorflow/lite/core/c/common.h"
#include "tensorflow/lite/micro/micro_interpreter.h"
#include "tensorflow/lite/micro/micro_log.h"
#include "tensorflow/lite/micro/micro_mutable_op_resolver.h"
#include "tensorflow/lite/micro/testing/micro_test.h"
#include "speech.h"

extern "C" {
int printk(const char *fmt, ...);
void sof_ut_log(const char *msg);
void tflm_yield(void);
uint32_t g_node_cycles[10] = {0};
int g_node_codes[10] = {0};
int g_node_count = 0;
}

#if defined(__xtensa__)
extern "C" uint64_t sys_clock_cycle_get_64(void);
static inline uint32_t get_ccount(void)
{
	return (uint32_t)sys_clock_cycle_get_64();
}
#else
static inline uint32_t get_ccount(void) { return 0; }
#endif

// hard code the model today
#include "micro_speech_quantized_model_data.h"

// The following values are derived from values used during model training.
// If you change the way you preprocess the input, update all these constants.
//constexpr int kAudioSampleFrequency = TFLM_SAMPLE_RATE;
static constexpr int kFeatureSize = TFLM_FEATURE_SIZE;
static constexpr int kFeatureCount = TFLM_FEATURE_COUNT;
static constexpr int kFeatureElementCount = TFLM_FEATURE_ELEM_COUNT;

#include <new>

// Arena size set to 64KB tensor arena
static constexpr size_t kArenaSize = 65536;  // 64KB tensor arena
alignas(16) static uint8_t g_arena[kArenaSize];

// type for features
using Features = int8_t[kFeatureCount][kFeatureSize];

// inference
static const tflite::Model *model;
static TfLiteTensor *input;
static TfLiteTensor *output;
static tflite::MicroInterpreter *interpreter;

using MicroSpeechOpResolver = tflite::MicroMutableOpResolver<5>;
alignas(16) static uint8_t op_resolver_buf[sizeof(MicroSpeechOpResolver)];
static MicroSpeechOpResolver *op_resolver_ptr = nullptr;
alignas(16) static uint8_t static_interpreter_buf[sizeof(tflite::MicroInterpreter)];

// Forward declaration
static int Init_Interpreter(struct tf_classify *tfc);

// Adding more kernels is quite efficient.
int RegisterOps(MicroSpeechOpResolver *op_resolver) {
	TF_LITE_ENSURE_STATUS(op_resolver->AddReshape());
	TF_LITE_ENSURE_STATUS(op_resolver->AddFullyConnected());
	TF_LITE_ENSURE_STATUS(op_resolver->AddConv2D());
	TF_LITE_ENSURE_STATUS(op_resolver->AddDepthwiseConv2D());
	TF_LITE_ENSURE_STATUS(op_resolver->AddSoftmax());
	return 0;
}

int TF_InitOps(struct tf_classify *tfc)
{
	sof_ut_log("[SPEECH DBG] Entering TF_InitOps...");
	printk("[SPEECH DBG] Entering TF_InitOps...\n");
	if (!op_resolver_ptr) {
		op_resolver_ptr = new (op_resolver_buf) MicroSpeechOpResolver();
	}
	if (RegisterOps(op_resolver_ptr) != 0) {
		tfc->error = "register ops failed";
		sof_ut_log("[SPEECH DBG] RegisterOps failed!");
		printk("[SPEECH DBG] RegisterOps failed!\n");
		return -EINVAL;
	}

	sof_ut_log("[SPEECH DBG] RegisterOps SUCCESS, instantiating MicroInterpreter...");
	printk("[SPEECH DBG] RegisterOps SUCCESS, instantiating MicroInterpreter...\n");
	interpreter = new (static_interpreter_buf) tflite::MicroInterpreter(model, *op_resolver_ptr,
									   g_arena, kArenaSize);

	sof_ut_log("[SPEECH DBG] Calling AllocateTensors...");
	printk("[SPEECH DBG] Calling AllocateTensors...\n");
	uint32_t start_cycles = get_ccount();
	TfLiteStatus alloc_status = interpreter->AllocateTensors();
	uint32_t end_cycles = get_ccount();
	sof_ut_log("[SPEECH DBG] AllocateTensors COMPLETE!");
	printk("[SPEECH DBG] AllocateTensors took %u cycles, status=%d\n", end_cycles - start_cycles, (int)alloc_status);

	if (alloc_status != kTfLiteOk) {
		tfc->error = "interpreter tensor allocate failed";
		sof_ut_log("[SPEECH DBG] AllocateTensors failed!");
		printk("[SPEECH DBG] AllocateTensors failed!\n");
		return -EINVAL;
	}

	int ret = Init_Interpreter(tfc);
	if (ret < 0) {
		sof_ut_log("[SPEECH DBG] Init_Interpreter failed inside TF_InitOps!");
		printk("[SPEECH DBG] Init_Interpreter failed inside TF_InitOps ret=%d!\n", ret);
		return ret;
	}

	sof_ut_log("[SPEECH DBG] TF_InitOps SUCCESS!");
	printk("[SPEECH DBG] TF_InitOps SUCCESS!\n");
	return 0;
}

static int Init_Interpreter(struct tf_classify *tfc)
{
	if (!interpreter) {
		tfc->error = "interpreter is NULL";
		printk("[SPEECH DBG] Init_Interpreter: interpreter is NULL!\n");
		return -EINVAL;
	}

	input = interpreter->input(0);
	if (!input){
		tfc->error = "input interpreter NULL";
		printk("[SPEECH DBG] Init_Interpreter: input NULL!\n");
		return -EINVAL;
	}

	printk("[SPEECH DBG] Init_Interpreter: input OK bytes=%zu type=%d\n",
	       input->bytes, (int)input->type);

	if (kFeatureElementCount != (int)input->bytes) {
		tfc->error = "input interpreter shape incompatible";
		printk("[SPEECH DBG] Init_Interpreter: shape mismatch! expected=%d, got=%zu\n",
		       kFeatureElementCount, input->bytes);
		return -EINVAL;
	}

	output = interpreter->output(0);
	if (!output){
		tfc->error = "output interpreter NULL";
		printk("[SPEECH DBG] Init_Interpreter: output NULL!\n");
		return -EINVAL;
	}

	printk("[SPEECH DBG] Init_Interpreter: output OK type=%d bytes=%zu\n",
	       (int)output->type, output->bytes);

	/* Skip dims validation - rely on bytes check */
	printk("[SPEECH DBG] Init_Interpreter: SUCCESS (skipping dims check)\n");
	return 0;
}

int TF_SetModel(struct tf_classify *tfc, unsigned char *model_tflite)
{
	sof_ut_log("[SPEECH DBG] TF_SetModel: calling GetModel...");
	model = tflite::GetModel(g_micro_speech_quantized_model_data);
	sof_ut_log("[SPEECH DBG] TF_SetModel: checking schema version...");
	if (model->version() != TFLITE_SCHEMA_VERSION) {
		tfc->error = "failed to load model";
		sof_ut_log("[SPEECH DBG] TF_SetModel FAILED schema version mismatch!");
		printk("[SPEECH DBG] TF_SetModel failed!\n");
		return -EINVAL;
	}

	flatbuffers::Verifier verifier(g_micro_speech_quantized_model_data, g_micro_speech_quantized_model_data_size);
	if (!tflite::VerifyModelBuffer(verifier)) {
		tfc->error = "model verification failed";
		sof_ut_log("[SPEECH DBG] TF_SetModel FAILED VerifyModelBuffer!");
		printk("[SPEECH DBG] TF_SetModel VerifyModelBuffer failed!\n");
		return -EINVAL;
	}

	sof_ut_log("[SPEECH DBG] TF_SetModel SUCCESS!");
	printk("[SPEECH DBG] TF_SetModel SUCCESS!\n");
	return 0;
}


int TF_ProcessClassify(struct tf_classify *tfc)
{
	Features *features = reinterpret_cast<Features *>(tfc->audio_features);
	int ret;

	ret = Init_Interpreter(tfc);
	if (ret < 0) {
		printk("[SPEECH DBG] TF_ProcessClassify: Init_Interpreter ret=%d, err=%s\n",
		       ret, tfc->error ? tfc->error : "none");
		return ret;
	}

	int8_t *input_ptr = tflite::GetTensorData<int8_t>(input);
	memcpy(input_ptr, features[0][0], kFeatureElementCount);

	int int8_min = 127, int8_max = -128, non_zero_count = 0;
	int32_t sum = 0;
	for (int i = 0; i < kFeatureElementCount; i++) {
		int8_t val = input_ptr[i];
		if (val < int8_min) int8_min = val;
		if (val > int8_max) int8_max = val;
		if (val != 0) non_zero_count++;
		sum += val;
	}

	float output_scale = output->params.scale;
	int output_zero_point = output->params.zero_point;

	tflm_yield();
	uint32_t c_start = get_ccount();
	TfLiteStatus status = interpreter->Invoke();
	uint32_t c_end = get_ccount();
	tflm_yield();

	tfc->op_count = g_node_count;
	for (int i = 0; i < 10 && i < g_node_count; i++) {
		tfc->node_cycles[i] = g_node_cycles[i];
		tfc->node_codes[i] = g_node_codes[i];
	}

	if (status != kTfLiteOk) {
		tfc->error = "invoke failed";
		printk("[SPEECH DBG] TF_ProcessClassify: Invoke failed status=%d!\n", (int)status);
		return -EINVAL;
	}

	for (int i = 0; i < tfc->categories; i++) {
		int8_t raw_i8 = tflite::GetTensorData<int8_t>(output)[i];
		if (output->type == kTfLiteUInt8) {
			uint8_t raw_u8 = tflite::GetTensorData<uint8_t>(output)[i];
			tfc->predictions[i] = (raw_u8 - output_zero_point) * output_scale;
		} else {
			tfc->predictions[i] = (raw_i8 - output_zero_point) * output_scale;
		}
	}

	printk("[SPEECH INFERENCE PASS] Invoke() executed in %u cycles! predictions: sil=%d, unk=%d, yes=%d, no=%d\n",
	       c_end - c_start,
	       (int)(tfc->predictions[0] * 100), (int)(tfc->predictions[1] * 100),
	       (int)(tfc->predictions[2] * 100), (int)(tfc->predictions[3] * 100));

	return 0;
}
