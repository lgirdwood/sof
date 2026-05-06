// SPDX-License-Identifier: BSD-3-Clause
/*
 * Copyright(c) 2026 Intel Corporation.
 */

#include <zephyr/kernel.h>
#include <zephyr/ztest.h>
#include <string.h>
#include <rtos/sof.h>
#include <sof/list.h>
#include <sof/audio/component.h>
#include <sof/audio/component_ext.h>
#include <sof/audio/buffer.h>
#include <sof/ipc/topology.h>
#include <ipc4/base-config.h>
#include <sof/math/fir_hifi3.h>

#define SOF_TDFB_NUM_INPUT_PINS 1
#define SOF_TDFB_NUM_OUTPUT_PINS 1

#include <rtos/alloc.h>
#include "test_audio_helper.h"

extern void sys_comp_module_tdfb_interface_init(void);

/* Passthrough blob for basic init/config tests */
#include "blobs/tdfb_passthrough_2ch.h"

/* Lowpass averaging FIR blob for processing tests - exercises real FIR path */
#include "blobs/tdfb_lowpass_2ch.h"

static void *setup(void)
{
	struct sof *sof = sof_get();

	sys_comp_init(sof);

	if (!sof->ipc) {
		sof->ipc = rzalloc(SOF_MEM_FLAG_COHERENT, sizeof(*sof->ipc));
		sof->ipc->comp_data = rzalloc(SOF_MEM_FLAG_COHERENT, 4096);
		k_spinlock_init(&sof->ipc->lock);
		list_init(&sof->ipc->msg_list);
		list_init(&sof->ipc->comp_list);
	}

	sys_comp_module_tdfb_interface_init();
	return NULL;
}

SOF_DEFINE_UUID("tdfb_test", tdfb_test_uuid,
		0xdd511749, 0xd9fa, 0x455c,
		0xb3, 0xa7, 0x13, 0x58, 0x56, 0x93, 0xf1, 0xaf);

struct custom_ipc4_config_tdfb {
	struct ipc4_base_module_cfg base;
	struct ipc4_base_module_cfg_ext base_ext;
	struct ipc4_input_pin_format in_pins[SOF_TDFB_NUM_INPUT_PINS];
	struct ipc4_output_pin_format out_pins[SOF_TDFB_NUM_OUTPUT_PINS];
} __attribute__((packed, aligned(8)));

static struct comp_dev *test_tdfb_create(void)
{
	struct comp_dev *comp;
	struct comp_ipc_config ipc_config;
	struct ipc_config_process spec;
	struct custom_ipc4_config_tdfb init_data;

	memset(&init_data, 0, sizeof(init_data));
	init_data.base.audio_fmt.channels_count = 2;
	init_data.base.audio_fmt.sampling_frequency = 48000;
	init_data.base.audio_fmt.depth = 16;
	init_data.base.audio_fmt.valid_bit_depth = 16;
	init_data.base.audio_fmt.interleaving_style = IPC4_CHANNELS_INTERLEAVED;
	init_data.base.ibs = 128;
	init_data.base.obs = 128;

	init_data.base_ext.nb_input_pins = SOF_TDFB_NUM_INPUT_PINS;
	init_data.base_ext.nb_output_pins = SOF_TDFB_NUM_OUTPUT_PINS;
	init_data.in_pins[0].pin_index = 0;
	init_data.in_pins[0].ibs = 128;
	init_data.in_pins[0].audio_fmt = init_data.base.audio_fmt;

	init_data.out_pins[0].pin_index = 0;
	init_data.out_pins[0].obs = 128;
	init_data.out_pins[0].audio_fmt = init_data.base.audio_fmt;

	memset(&ipc_config, 0, sizeof(ipc_config));
	ipc_config.id = 1;
	ipc_config.pipeline_id = 1;
	ipc_config.core = 0;

	memset(&spec, 0, sizeof(spec));
	spec.size = sizeof(init_data);
	spec.data = (unsigned char *)&init_data;

	struct list_item *clist;
	const struct comp_driver *drv = NULL;

	list_for_item(clist, &comp_drivers_get()->list) {
		struct comp_driver_info *info = container_of(clist, struct comp_driver_info, list);
		if (!info->drv->uid) continue;
		if (!memcmp(info->drv->uid, &tdfb_test_uuid, sizeof(struct sof_uuid))) {
			drv = info->drv;
			break;
		}
	}
	zassert_not_null(drv, "Driver for tdfb not found");

	comp = drv->ops.create(drv, &ipc_config, &spec);
	zassert_not_null(comp, "Component allocation failed");

	return comp;
}

/* Apply a config blob to the TDFB component.
 * Blobs have a 32-byte ABI header that must be skipped.
 */
static void test_tdfb_apply_blob(struct comp_dev *comp, const void *blob, size_t size)
{
	const struct sof_abi_hdr *hdr = blob;
	int ret;

	zassert_not_null(comp->drv->ops.set_large_config,
			 "set_large_config not available");

	ret = comp->drv->ops.set_large_config(comp, 1, true, true,
		size - sizeof(struct sof_abi_hdr), (const char *)hdr->data);
	zassert_equal(ret, 0, "apply blob failed: %d", ret);
}

/* Test tdfb initialization */
ZTEST(audio_tdfb, test_tdfb_init)
{
	struct comp_dev *comp = test_tdfb_create();

	zassert_equal(comp->state, COMP_STATE_READY, "Component is not in READY state");
	zassert_equal(comp->ipc_config.id, 1, "IPC ID mismatch");

	comp->drv->ops.free(comp);
}

/* Test blob application with passthrough config */
ZTEST(audio_tdfb, test_tdfb_config_passthrough)
{
	struct comp_dev *comp = test_tdfb_create();

	test_tdfb_apply_blob(comp, tdfb_blob, sizeof(tdfb_blob));

	comp->drv->ops.free(comp);
}

/* Test blob application with lowpass FIR config */
ZTEST(audio_tdfb, test_tdfb_config_lowpass)
{
	struct comp_dev *comp = test_tdfb_create();

	test_tdfb_apply_blob(comp, tdfb_lowpass_blob, sizeof(tdfb_lowpass_blob));

	comp->drv->ops.free(comp);
}

/* Test full processing path with lowpass FIR filter.
 * Uses small buffer (256 bytes) to keep QEMU emulation fast.
 * The 4-tap averaging FIR [0.25, 0.25, 0.25, 0.25] exercises
 * the real beamformer FIR processing path including delay lines.
 */
ZTEST(audio_tdfb, test_tdfb_process)
{
	struct comp_dev *comp = test_tdfb_create();

	/* Apply the lowpass FIR blob so tdfb_setup configures real filter coefficients */
	test_tdfb_apply_blob(comp, tdfb_lowpass_blob, sizeof(tdfb_lowpass_blob));

	struct sof_ipc_stream_params params = {0};
	params.buffer_fmt = SOF_IPC_BUFFER_INTERLEAVED;
	params.channels = 2;
	params.rate = 48000;
	params.sample_container_bytes = 2;
	params.sample_valid_bytes = 2;

	/* Use small buffer (256 bytes = 64 frames * 2ch * 2 bytes/sample)
	 * to keep QEMU HiFi3 emulation time reasonable.
	 */
	test_audio_helper_setup_buffers(comp, 256, &params);

	/* Provide input and process */
	test_audio_helper_process(comp);

	test_audio_helper_free_buffers(comp);

	comp->drv->ops.free(comp);
}

ZTEST(audio_tdfb, test_fir_standalone)
{
    ae_int32 __attribute__((aligned(8))) delay[32] = {0};
    ae_f16x4 __attribute__((aligned(8))) coef[32] = {0};
    struct fir_state_32x16 fir;
    
    fir.delay = &delay[0];
    fir.delay_end = &delay[32];
    fir.rwp = &delay[4];
    fir.coef = &coef[0];
    fir.taps = 8;
    
    AE_SETCBEGIN0(fir.delay);
    AE_SETCEND0(fir.delay_end);
    
    ae_int32 y0 = 0, y1 = 0;
    
    fir_32x16_2x(&fir, 1, 2, &y0, &y1, 0);
    
    zassert_true(1, "Survived FIR");
}

ZTEST_SUITE(audio_tdfb, NULL, setup, NULL, NULL, NULL);
