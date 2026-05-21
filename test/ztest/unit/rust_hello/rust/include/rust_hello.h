/* SPDX-License-Identifier: BSD-3-Clause */
#ifndef RUST_HELLO_H
#define RUST_HELLO_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

uint32_t rust_hello_answer(void);
uint32_t rust_hello_add(uint32_t a, uint32_t b);

#ifdef __cplusplus
}
#endif

#endif /* RUST_HELLO_H */
