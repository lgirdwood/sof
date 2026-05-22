// SPDX-License-Identifier: BSD-3-Clause
/*
 * Copyright(c) 2026 Intel Corporation.
 *
 * Out-of-line C accessors for `struct bind_info`. The Rust
 * `sof_module::BindData<'_>` wrapper calls these instead of reading
 * the struct's fields directly, keeping all C-struct layout knowledge
 * on the C side.
 *
 * Built into libmodules_sof.a (and into the firmware ZTest app)
 * whenever any in-tree Rust audio module is enabled.
 */

#include <stdint.h>
#include <stddef.h>
#include <module/audio/source_api.h>
#include <module/audio/sink_api.h>
#include <sof/audio/component.h>

/*
 * Return the bind type as a u32 matching Rust's BindType enum:
 *   0 == BindType::Source (COMP_BIND_TYPE_SOURCE)
 *   1 == BindType::Sink   (COMP_BIND_TYPE_SINK)
 */
uint32_t sof_rust_bind_get_type(const struct bind_info *bd)
{
	return (uint32_t)bd->bind_type;
}

/*
 * Return `bd->source` if the bind type is SOURCE, otherwise NULL.
 * Allows safe Rust code to retrieve the source pointer without
 * touching the union directly.
 */
struct sof_source *sof_rust_bind_get_source(const struct bind_info *bd)
{
	if (bd->bind_type != COMP_BIND_TYPE_SOURCE)
		return NULL;
	return bd->source;
}

/*
 * Return `bd->sink` if the bind type is SINK, otherwise NULL.
 */
struct sof_sink *sof_rust_bind_get_sink(const struct bind_info *bd)
{
	if (bd->bind_type != COMP_BIND_TYPE_SINK)
		return NULL;
	return bd->sink;
}
