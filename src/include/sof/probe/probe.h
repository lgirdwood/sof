/* SPDX-License-Identifier: BSD-3-Clause
 *
 * Copyright(c) 2019 Intel Corporation. All rights reserved.
 *
 * Author: Artur Kloniecki <arturx.kloniecki@linux.intel.com>
 */

#ifndef __SOF_PROBE_PROBE_H__
#define __SOF_PROBE_PROBE_H__

#include <ipc/probe.h>
#include <sys/types.h>


/**
 * A buffer of logging data is available for processing.
 */
typedef ssize_t(*probe_logging_hook_t)(uint8_t *buffer, size_t length);

#if CONFIG_LOG_BACKEND_SOF_PROBE
const struct log_backend *log_backend_probe_get(void);
bool probe_is_backend_configured(void);
#endif

/**
 * @brief Initialize the probe logging backend.
 *
 * @param hook Function is called when new logging data is written
 *             out by the logger.
 */
void probe_logging_init(probe_logging_hook_t hook);

/**
 * @brief Write shell output text to the probe extraction stream.
 *
 * The payload is packetized with the probe extraction header and can be
 * consumed from compressed capture endpoints together with other probe data.
 * If extraction DMA is not active, bytes are dropped and 0 is returned.
 *
 * @param buffer Shell output bytes to transmit.
 * @param length Number of bytes in @p buffer.
 * @return Number of bytes written (can be less than length when buffer is
 *         near full), 0 when probes are inactive, negative errno on error.
 */
ssize_t probe_shell_output(const uint8_t *buffer, size_t length);

/*
 * \brief Initialize probes subsystem
 *
 * param[in,optional] extraction_probe_dma - DMA associated with extraction
 *		      In case extraction_probe_dma is NULL, extraction probes
 *		      are unavailable.
 */
int probe_init(const struct probe_dma *extraction_probe_dma);

/*
 * \brief Deinitialize probes subsystem.
 *
 * Detach extraction DMA if was enabled. Return -EINVAL in case some probes
 * are still in use.
 */
int probe_deinit(void);

/*
 * \brief Initialise the optional shell output DMA slot.
 *
 * Called from IPC4 probe module init when the kernel provides a second HDA
 * capture stream for Zephyr shell stdout.  If shell_dma->stream_tag is
 * PROBE_DMA_INVALID (0xFFFFFFFF), this is a no-op.
 *
 * param[in] shell_dma - DMA config (stream_tag + dma_buffer_size) from kernel
 */
int probe_shell_dma_init(const struct probe_dma *shell_dma);

/*
 * \brief Setup injection DMAs for probes.
 *
 * param[in] count - number of DMAs configured during this call
 * param[in] probe_dma - Array of size 'count' with configuration data for DMAs
 */
int probe_dma_add(uint32_t count, const struct probe_dma *probe_dma);

/*
 * \brief Get info about connected injection DMAs
 *
 * param[in,out] data - reply to write data to
 * param[in] max_size - maximum number of bytes available in data
 */
int probe_dma_info(struct sof_ipc_probe_info_params *data, uint32_t max_size);

/*
 * \brief Remove injection DMAs
 *
 * param[in] count - number of DMAs removed during this call
 * param[in] stream_tag - array for size 'count' with stream tags associated
 *			  with DMAs to be removed
 */
int probe_dma_remove(uint32_t count, const uint32_t *stream_tag);

/*
 * \brief Set probe points
 *
 * param[in] count - number of probe points configured this call
 * param[in] probe - array of size 'count' with configuration of probe points
 */
int probe_point_add(uint32_t count, const struct probe_point *probe);

/*
 * \brief Get info about connected probe points
 *
 * param[in,out] data - reply to write data to
 * param[in] max_size - maximum number of bytes available in data
 */
int probe_point_info(struct sof_ipc_probe_info_params *data, uint32_t max_size);

/*
 * \brief Remove probe points
 *
 * param[in] count - number of probe points removed this call
 * param[in] buffer_id - array of size 'count' with IDs of buffers to which
 *			 probes were attached
 */
int probe_point_remove(uint32_t count, const uint32_t *buffer_id);

/**
 * \brief Retrieves probes structure.
 * \return Pointer to probes structure.
 */
static inline struct probe_pdata *probe_get(void)
{
	return sof_get()->probe;
}

#endif /* __SOF_PROBE_PROBE_H__ */
