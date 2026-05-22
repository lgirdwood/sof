// SPDX-License-Identifier: BSD-3-Clause
//
// Reference SOF audio module written entirely in Rust. The C side
// (rust_template.c) provides the SOF manifest stanza and pulls in
// `rust_template_interface`, which is generated below by
// `sof_module::define_module!()`.

#![no_std]
#![allow(unused_imports)]

use core::ffi::{c_char, CStr};
use core::sync::atomic::{AtomicBool, Ordering};

use sof_module::{
    audio, define_module, err, frame_fmt, ipc4_control, ConfigFragmentIn, ConfigFragmentOut,
    ModuleHandle, ProcessingModule, StreamCtx,
};

// -------------------------------------------------------------------------
// Zephyr LOG bindings. The Zephyr LOG_*/SOF comp_* macros are bound at
// build time to the per-module string table created by
// LOG_MODULE_REGISTER(rust_template, ...) in rust_template.c, so they
// can't be called directly from Rust. rust_template.c exports the four
// thin shims below; the `log` module wraps them in a Rust-safe API that
// takes a `&CStr` (typically a `c"..."` literal, no heap involved).
// -------------------------------------------------------------------------
unsafe extern "C" {
    fn sof_rust_template_log_err(msg: *const c_char);
    fn sof_rust_template_log_warn(msg: *const c_char);
    fn sof_rust_template_log_info(msg: *const c_char);
    fn sof_rust_template_log_dbg(msg: *const c_char);
    /// Hook into Zephyr's fatal-error path. Logs `msg` at ERR level
    /// then calls `k_panic()`; declared `-> !` so the Rust panic
    /// handler is well-typed.
    fn sof_rust_template_panic(msg: *const c_char) -> !;
}

/// Per-level loggers that forward to the C-side
/// `LOG_MODULE_REGISTER(rust_template, ...)`. Pass a `c"..."`
/// literal (or any `&CStr`) — the trailing NUL is required because
/// the underlying `LOG_INF("%s", msg)` expects a C string.
pub mod log {
    use super::{
        sof_rust_template_log_dbg, sof_rust_template_log_err,
        sof_rust_template_log_info, sof_rust_template_log_warn, CStr,
    };

    /// Equivalent of `comp_err` / `LOG_ERR`.
    #[inline]
    pub fn err(msg: &CStr) {
        // SAFETY: thin call into a C wrapper that copies / formats
        // the pointer immediately (CONFIG_LOG_MODE_IMMEDIATE=y).
        unsafe { sof_rust_template_log_err(msg.as_ptr()) }
    }

    /// Equivalent of `comp_warn` / `LOG_WRN`.
    #[inline]
    pub fn warn(msg: &CStr) {
        unsafe { sof_rust_template_log_warn(msg.as_ptr()) }
    }

    /// Equivalent of `comp_info` / `LOG_INF`.
    #[inline]
    pub fn info(msg: &CStr) {
        unsafe { sof_rust_template_log_info(msg.as_ptr()) }
    }

    /// Equivalent of `comp_dbg` / `LOG_DBG`.
    #[inline]
    pub fn dbg(msg: &CStr) {
        unsafe { sof_rust_template_log_dbg(msg.as_ptr()) }
    }
}

/// Panic handler: format the panic message and source location into
/// a fixed-size on-stack buffer (NUL-terminated, no heap), then hand
/// off to the C-side `sof_rust_template_panic()` which logs at ERR
/// level via the `rust_template` Zephyr log module and calls
/// `k_panic()`. The buffer is intentionally small to keep panic-path
/// stack usage bounded; longer messages are truncated.
#[panic_handler]
fn panic(info: &core::panic::PanicInfo) -> ! {
    use core::fmt::Write;

    /// `core::fmt::Write` adapter over a fixed byte buffer that
    /// silently truncates instead of erroring. Reserves the last
    /// byte for a NUL terminator so the result is a valid C string.
    struct CBuf {
        buf: [u8; 192],
        len: usize,
    }
    impl CBuf {
        const CAP: usize = 192;
        fn new() -> Self {
            Self { buf: [0; Self::CAP], len: 0 }
        }
        fn as_cstr_ptr(&mut self) -> *const c_char {
            // Reserve room for the NUL terminator.
            if self.len >= Self::CAP {
                self.len = Self::CAP - 1;
            }
            self.buf[self.len] = 0;
            self.buf.as_ptr() as *const c_char
        }
    }
    impl Write for CBuf {
        fn write_str(&mut self, s: &str) -> core::fmt::Result {
            let remaining = Self::CAP.saturating_sub(self.len + 1);
            let n = if s.len() < remaining { s.len() } else { remaining };
            self.buf[self.len..self.len + n].copy_from_slice(&s.as_bytes()[..n]);
            self.len += n;
            Ok(())
        }
    }

    let mut b = CBuf::new();
    let _ = b.write_str("rust_template panic: ");
    let _ = write!(b, "{}", info.message());
    if let Some(loc) = info.location() {
        let _ = write!(b, " at {}:{}", loc.file(), loc.line());
    }
    // SAFETY: noreturn C function; pointer is valid until the call
    // since `b` lives on this stack frame and we never return.
    unsafe { sof_rust_template_panic(b.as_cstr_ptr()) }
}

/// L/R swap enable, driven by the IPC4 switch kcontrol delivered to
/// [`RustTemplate::set_configuration`]. Mirrors the `cd->enable`
/// field that the C `template` component keeps in its per-instance
/// `template_comp_data`; this skeleton uses a single static atomic
/// because it only ever has one live instance.
static SWAP_ENABLE: AtomicBool = AtomicBool::new(false);

pub struct RustTemplate;

impl ProcessingModule for RustTemplate {
    // Opt into the slots we actually implement; everything else stays
    // NULL in the resulting `struct module_interface`.
    const HAS_PROCESS: bool = true;
    const HAS_RESET:   bool = true;
    const HAS_FREE:    bool = true;
    const HAS_SET_CONFIGURATION: bool = true;
    const HAS_GET_CONFIGURATION: bool = true;

    fn init(_handle: &ModuleHandle) -> Result<(), i32> {
        // Real modules would allocate per-instance state here via
        // a SOF-provided allocator; this skeleton keeps it stateless.
        log::info(c"rust_template init");
        Ok(())
    }

    /// Process one buffer: when the kcontrol switch is enabled, swap
    /// left and right channels for 16-bit interleaved stereo PCM;
    /// otherwise straight-copy the source through to the sink. Uses
    /// the helpers in `sof_module::audio` so the FFI to `source_*` /
    /// `sink_*` lives in one place.
    fn process(_handle: &ModuleHandle, mut ctx: StreamCtx) -> Result<(), i32> {
        // The SOF module adapter always invokes `process` with at
        // least one source and one sink wired up; missing wiring is
        // a topology bug, not a runtime data condition.
        let (mut src, mut snk) = ctx.primary_pair()?;
        if SWAP_ENABLE.load(Ordering::Relaxed) {
            // Dispatch on the source frame format: s16 uses a
            // 2-byte stride, s32 / s24-in-32 a 4-byte stride. Any
            // other format falls through to the passthrough below
            // so the module stays a safe no-op on unsupported
            // topologies rather than corrupting samples.
            match src.frame_fmt() {
                frame_fmt::S16_LE => swap_lr_s16(&mut src, &mut snk),
                frame_fmt::S32_LE | frame_fmt::S24_4LE => swap_lr_s32(&mut src, &mut snk),
                _ => audio::passthrough(&mut src, &mut snk).map(|_| ()),
            }
        } else {
            audio::passthrough(&mut src, &mut snk).map(|_| ())
        }
    }

    /// Handle IPC4 kcontrol updates. The host driver demultiplexes
    /// switch / enum / bytes controls via the `config_id` parameter,
    /// matching the C template's `template_set_config()`.
    fn set_configuration(
        _handle: &ModuleHandle,
        frag: ConfigFragmentIn<'_>,
    ) -> Result<(), i32> {
        match frag.config_id {
            ipc4_control::SWITCH_CONTROL_PARAM_ID => {
                let pl = ipc4_control::parse_switch(frag.fragment)?;
                // Only a single switch (id 0) with a single channel
                // is supported, same shape as the C template.
                if pl.id != 0 || pl.num_elems != 1 {
                    log::err(c"rust_template: illegal switch control shape");
                    return Err(err::EINVAL);
                }
                let v = pl.first_value()?;
                SWAP_ENABLE.store(v != 0, Ordering::Relaxed);
                if v != 0 {
                    log::info(c"rust_template: L/R swap enabled");
                } else {
                    log::info(c"rust_template: L/R swap disabled");
                }
                Ok(())
            }
            _ => {
                log::err(c"rust_template: unsupported config_id");
                Err(err::EINVAL)
            }
        }
    }

    /// Companion to [`set_configuration`]: the C template stubs this
    /// out (returns 0 with no fragment data), and we do the same.
    fn get_configuration(
        _handle: &ModuleHandle,
        _frag: ConfigFragmentOut<'_>,
    ) -> Result<(), i32> {
        Ok(())
    }

    fn reset(_handle: &ModuleHandle) -> Result<(), i32> {
        Ok(())
    }

    fn free(_handle: &ModuleHandle) -> Result<(), i32> {
        Ok(())
    }
}

define_module!(RustTemplate, rust_template_interface);

// -------------------------------------------------------------------------
// Test entry point: lets a C ZTest exercise the L/R swap on a linear
// buffer, without needing to stand up real `struct sof_source` /
// `struct sof_sink` plumbing. Operates on `n_samples` interleaved
// s16 samples (so `n_samples / 2` stereo frames). Trailing odd
// samples are copied through unchanged.
// -------------------------------------------------------------------------
#[unsafe(no_mangle)]
pub unsafe extern "C" fn rust_template_swap_lr_s16(
    src: *const i16,
    dst: *mut i16,
    n_samples: usize,
) {
    if src.is_null() || dst.is_null() || n_samples == 0 {
        return;
    }
    let s = core::slice::from_raw_parts(src, n_samples);
    let d = core::slice::from_raw_parts_mut(dst, n_samples);
    swap_lr_s16_buf(s, d);
}

/// Companion C test entry for the s32 / s24-in-32 swap. Same
/// contract as [`rust_template_swap_lr_s16`] but operates on `i32`
/// samples.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn rust_template_swap_lr_s32(
    src: *const i32,
    dst: *mut i32,
    n_samples: usize,
) {
    if src.is_null() || dst.is_null() || n_samples == 0 {
        return;
    }
    let s = core::slice::from_raw_parts(src, n_samples);
    let d = core::slice::from_raw_parts_mut(dst, n_samples);
    swap_lr_s32_buf(s, d);
}

// -------------------------------------------------------------------------
// L/R swap helpers. These are module-private (the C `template` component
// also keeps its swap routines local to template-generic.c); only the
// generic stream plumbing — Source/Sink wrappers, passthrough, IPC4
// control parsing — lives in sof_module.
// -------------------------------------------------------------------------

/// Swap left and right channels in a linear buffer of 16-bit
/// interleaved stereo PCM. Operates on `n_samples` total i16
/// samples (= `n_samples / 2` frames). Any trailing odd sample
/// is copied through unchanged.
///
/// Pure-data core that backs [`swap_lr_s16`]; no FFI dependency, so
/// it is directly callable from unit tests.
fn swap_lr_s16_buf(src: &[i16], dst: &mut [i16]) {
    let n = if src.len() < dst.len() { src.len() } else { dst.len() };
    // Raw pointers sidestep bounds-check panics; on Xtensa the
    // cross-section l32r references those panic paths emit are a
    // known toolchain limitation.
    let sp = src.as_ptr();
    let dp = dst.as_mut_ptr();
    let pairs = n / 2;
    unsafe {
        let mut i = 0usize;
        while i < pairs {
            let off = i * 2;
            let l = sp.add(off).read();
            let r = sp.add(off + 1).read();
            dp.add(off).write(r);
            dp.add(off + 1).write(l);
            i += 1;
        }
        if n & 1 == 1 {
            dp.add(n - 1).write(sp.add(n - 1).read());
        }
    }
}

/// Swap left and right channels for 16-bit interleaved stereo PCM
/// pulled from `src` and pushed into `snk`. Stereo (`channels ==
/// 2`) is the only supported path; any other channel count is
/// treated as a no-op so the module stays a safe pass-through under
/// unexpected topologies.
///
/// All FFI — `source_get_data`, circular-buffer wrap, sink commit
/// — lives in [`audio::process_stereo_s16`]; this function only
/// supplies the per-frame swap, so the client side is 100% safe
/// Rust.
fn swap_lr_s16(src: &mut audio::Source<'_>, snk: &mut audio::Sink<'_>) -> Result<(), i32> {
    audio::process_stereo_s16(src, snk, |s_in, s_out| {
        s_out[0] = s_in[1];
        s_out[1] = s_in[0];
    })
    .map(|_| ())
}

/// Swap left and right channels in a linear buffer of 32-bit
/// interleaved stereo PCM. Equally applicable to `S32_LE` (true
/// 32-bit samples) and `S24_4LE` (24-bit samples right-aligned
/// inside a 32-bit container): the byte layout of an (L, R) pair
/// is identical in both cases. Operates on `n_samples` total i32
/// samples; any trailing odd sample is copied through unchanged.
///
/// Pure-data core that backs [`swap_lr_s32`]; no FFI dependency,
/// so it is directly callable from unit tests.
fn swap_lr_s32_buf(src: &[i32], dst: &mut [i32]) {
    let n = if src.len() < dst.len() { src.len() } else { dst.len() };
    let sp = src.as_ptr();
    let dp = dst.as_mut_ptr();
    let pairs = n / 2;
    unsafe {
        let mut i = 0usize;
        while i < pairs {
            let off = i * 2;
            let l = sp.add(off).read();
            let r = sp.add(off + 1).read();
            dp.add(off).write(r);
            dp.add(off + 1).write(l);
            i += 1;
        }
        if n & 1 == 1 {
            dp.add(n - 1).write(sp.add(n - 1).read());
        }
    }
}

/// Swap left and right channels for 32-bit interleaved stereo PCM
/// pulled from `src` and pushed into `snk`. Handles both `S32_LE`
/// and the 24-in-32 (`S24_4LE`) layout; the byte pattern is the
/// same. Like [`swap_lr_s16`], only `channels == 2` is processed;
/// any other channel count is a no-op pass-through.
fn swap_lr_s32(src: &mut audio::Source<'_>, snk: &mut audio::Sink<'_>) -> Result<(), i32> {
    audio::process_stereo_s32(src, snk, |s_in, s_out| {
        s_out[0] = s_in[1];
        s_out[1] = s_in[0];
    })
    .map(|_| ())
}
