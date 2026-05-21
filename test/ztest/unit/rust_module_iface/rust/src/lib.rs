// SPDX-License-Identifier: BSD-3-Clause
//
// Demo `ProcessingModule` impl. The C ztest invokes the function pointers
// stored in `rust_demo_module_interface` directly and checks the return
// codes match what the Rust trait impl returns.

#![no_std]

use sof_module::{
    define_module, err, ProcessingModule, ProcessingModuleHandle, StreamCtx,
};

#[panic_handler]
fn panic(_: &core::panic::PanicInfo) -> ! {
    loop {}
}

pub struct DemoMod;

impl ProcessingModule for DemoMod {
    // Wire only the slots we actually implement; the macro leaves the rest
    // as `None` so the C side sees null function pointers (= "not provided").
    const HAS_PROCESS: bool = true;
    const HAS_RESET: bool = true;
    const HAS_FREE: bool = true;
    const HAS_TRIGGER: bool = true;
    const HAS_SET_CONFIG_PARAM: bool = true;

    fn init(_m: *mut ProcessingModuleHandle) -> Result<(), i32> {
        Ok(())
    }

    fn process(
        _m: *mut ProcessingModuleHandle,
        _ctx: StreamCtx,
    ) -> Result<(), i32> {
        Ok(())
    }

    fn reset(_m: *mut ProcessingModuleHandle) -> Result<(), i32> {
        Ok(())
    }

    fn free(_m: *mut ProcessingModuleHandle) -> Result<(), i32> {
        Ok(())
    }

    fn trigger(_m: *mut ProcessingModuleHandle, cmd: i32) -> Result<(), i32> {
        // Echo the command back so the test can verify dispatch carries
        // arguments through.
        if cmd == 0 {
            Ok(())
        } else {
            Err(-cmd)
        }
    }

    fn set_config_param(
        _m: *mut ProcessingModuleHandle,
        param_id_data: u32,
    ) -> Result<(), i32> {
        // Only id == 0xCAFE is accepted; anything else is EINVAL.
        if (param_id_data >> 16) & 0x3FFF == 0xCAFE & 0x3FFF {
            Ok(())
        } else {
            Err(err::EINVAL)
        }
    }
}

define_module!(DemoMod, rust_demo_module_interface);
