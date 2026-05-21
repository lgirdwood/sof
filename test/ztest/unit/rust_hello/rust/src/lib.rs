// SPDX-License-Identifier: BSD-3-Clause
//
// Tiny Rust "hello world" library callable from C / ztest.
#![no_std]

#[panic_handler]
fn panic(_info: &core::panic::PanicInfo) -> ! {
    loop {}
}

/// Returns a fixed answer. Used by the ztest to verify Rust-from-C linking.
#[unsafe(no_mangle)]
pub extern "C" fn rust_hello_answer() -> u32 {
    42
}

/// Simple pure function: adds two u32s, saturating on overflow.
#[unsafe(no_mangle)]
pub extern "C" fn rust_hello_add(a: u32, b: u32) -> u32 {
    a.saturating_add(b)
}
