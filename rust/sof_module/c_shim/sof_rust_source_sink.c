// SPDX-License-Identifier: BSD-3-Clause
/*
 * Copyright(c) 2026 Intel Corporation.
 *
 * Out-of-line C wrappers for the SOF source/sink helpers. The real
 * helpers in <module/audio/source_api.h> / <module/audio/sink_api.h>
 * are declared `static inline`, so they're inlined into each C call
 * site and never get a global ELF symbol. Rust crates that import
 * them via `unsafe extern "C"` therefore see undefined references
 * at static-link time. This file re-emits each one as a regular
 * function with the same signature so the linker can resolve them.
 *
 * Built into libmodules_sof.a (and into the firmware ZTest app)
 * whenever any in-tree Rust audio module is enabled.
 */

#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>
#include <module/audio/source_api.h>
#include <module/audio/sink_api.h>
#include <module/ipc/stream.h>
#include <sof/audio/sink_source_utils.h>

size_t sof_rust_source_get_data_available(struct sof_source *source)
{
	return source_get_data_available(source);
}

size_t sof_rust_source_get_data_frames_available(struct sof_source *source)
{
	return source_get_data_frames_available(source);
}

uint32_t sof_rust_source_get_frm_fmt(struct sof_source *source)
{
	return (uint32_t)source_get_frm_fmt(source);
}

uint32_t sof_rust_source_get_channels(struct sof_source *source)
{
	return source_get_channels(source);
}

int sof_rust_source_get_data(struct sof_source *source, size_t req_size,
			     void const **data_ptr, void const **buffer_start,
			     size_t *buffer_size)
{
	return source_get_data(source, req_size, data_ptr, buffer_start,
			       buffer_size);
}

int sof_rust_source_release_data(struct sof_source *source, size_t free_size)
{
	return source_release_data(source, free_size);
}

size_t sof_rust_sink_get_free_size(struct sof_sink *sink)
{
	return sink_get_free_size(sink);
}

int sof_rust_sink_get_buffer(struct sof_sink *sink, size_t req_size,
			     void **data_ptr, void **buffer_start,
			     size_t *buffer_size)
{
	return sink_get_buffer(sink, req_size, data_ptr, buffer_start,
			       buffer_size);
}

int sof_rust_sink_commit_buffer(struct sof_sink *sink, size_t commit_size)
{
	return sink_commit_buffer(sink, commit_size);
}

int sof_rust_source_to_sink_copy(struct sof_source *source,
				 struct sof_sink *sink,
				 bool free_source,
				 size_t size)
{
	return source_to_sink_copy(source, sink, free_source, size);
}
