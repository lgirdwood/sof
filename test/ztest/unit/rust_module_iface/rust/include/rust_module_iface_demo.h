/* SPDX-License-Identifier: BSD-3-Clause */
#ifndef RUST_MODULE_IFACE_DEMO_H
#define RUST_MODULE_IFACE_DEMO_H

/*
 * The Rust crate exports a `ModuleInterface` whose layout matches the
 * SOF C `struct module_interface`. The C side sees it as the same struct.
 */
#include <module/module/interface.h>

extern const struct module_interface rust_demo_module_interface;

#endif /* RUST_MODULE_IFACE_DEMO_H */
