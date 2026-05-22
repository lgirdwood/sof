// SPDX-License-Identifier: BSD-3-Clause
//
// Copyright(c) 2025 Intel Corporation.
//
// Rust wrapper for `struct module_interface` — the SOF processing-module
// client API defined in `src/include/module/module/interface.h`.
//
// Authors are expected to:
//   1. Implement the [`ProcessingModule`] trait on a unit / ZST.
//   2. Invoke the [`define_module!`] macro to emit the C-ABI shim functions
//      and a `#[no_mangle] pub static <NAME>: ModuleInterface` symbol that
//      the existing SOF C code (rimage manifest, llext loader, module
//      adapter) can pick up unchanged.
//
// The struct layout MUST stay in sync with
// `struct module_interface` (no `SOF_MODULE_API_PRIVATE`).

#![no_std]
#![allow(non_camel_case_types)]

// Pull in the `alloc` crate under a different name so it doesn't
// collide with our own `pub mod alloc` (the SOF global-allocator
// adapter). Inner modules that need `Vec`, `Box`, etc. import via
// `use rust_alloc::...`. Gated behind the `alloc` cargo feature so
// modules that don't want to install a global allocator still
// build.
#[cfg(feature = "alloc")]
extern crate alloc as rust_alloc;


use core::ffi::c_void;

// -------------------------------------------------------------------------
// Opaque handle types matching the C side. These are never constructed in
// Rust; we only ever hold raw pointers passed to us from C.
// -------------------------------------------------------------------------

/// Opaque mirror of `struct processing_module`. Held only as a raw pointer
/// passed in by the C side; never constructed in Rust.
#[repr(C)]
pub struct ProcessingModuleHandle {
    _private: [u8; 0],
}

/// Safe handle wrapping a `*mut ProcessingModuleHandle` for the lifetime of
/// a processing callback. Constructed by the shims emitted by
/// [`define_module!`]; [`ProcessingModule`] trait methods receive
/// `&ModuleHandle` instead of a raw pointer.
pub struct ModuleHandle {
    pub(crate) ptr: *mut ProcessingModuleHandle,
}

impl ModuleHandle {
    /// Internal constructor called from [`define_module!`] shims.
    /// Not part of the stable public API; subject to change.
    #[doc(hidden)]
    pub fn __from_raw(ptr: *mut ProcessingModuleHandle) -> Self {
        Self { ptr }
    }

    /// Return the raw `struct processing_module *`.
    ///
    /// # Safety
    /// The pointer is valid only for the duration of the callback that
    /// provided this `ModuleHandle`. Do not store or copy the pointer past
    /// the return of the trait method.
    pub unsafe fn as_ptr(&self) -> *mut ProcessingModuleHandle {
        self.ptr
    }
}

/// Opaque mirror of `struct sof_source`.
#[repr(C)]
pub struct SofSource {
    _private: [u8; 0],
}

/// Opaque mirror of `struct sof_sink`.
#[repr(C)]
pub struct SofSink {
    _private: [u8; 0],
}

/// Opaque marker for `struct bind_info`. Only used as `*mut BindInfo`
/// in C-ABI function pointers; safe Rust code uses [`BindData`] instead.
#[repr(C)]
pub struct BindInfo {
    _private: [u8; 0],
}

/// Which side of the pipeline is being bound: a source (data provider)
/// or a sink (data consumer). Mirrors `enum bind_type` from
/// `<sof/audio/component.h>`.
#[repr(u32)]
#[derive(Copy, Clone, PartialEq, Eq, Debug)]
pub enum BindType {
    Source = 0,
    Sink   = 1,
}

// -------------------------------------------------------------------------
// Plain enums mirrored from interface.h.
// -------------------------------------------------------------------------

#[repr(C)]
#[derive(Copy, Clone, PartialEq, Eq, Debug)]
pub enum ModuleCfgFragmentPosition {
    Middle = 0,
    First = 1,
    Last = 2,
    Single = 3,
}

#[repr(C)]
#[derive(Copy, Clone, PartialEq, Eq, Debug)]
pub enum ModuleProcessingMode {
    Normal = 0,
    Bypass = 1,
}

#[repr(C)]
pub struct InputStreamBuffer {
    pub data: *mut c_void,
    pub size: u32,
    pub consumed: u32,
    pub end_of_stream: bool,
}

#[repr(C)]
pub struct OutputStreamBuffer {
    pub data: *mut c_void,
    pub size: u32,
}

// -------------------------------------------------------------------------
// `struct module_interface` mirror.
//
// Field order, types, and `Option<extern "C" fn>` (= nullable function
// pointer) MUST match interface.h exactly. The SOF C side declares
// instances of this struct and reads function pointers by offset; any
// mismatch is silent UB.
// -------------------------------------------------------------------------

pub type InitFn = unsafe extern "C" fn(m: *mut ProcessingModuleHandle) -> i32;
pub type PrepareFn = unsafe extern "C" fn(
    m: *mut ProcessingModuleHandle,
    sources: *mut *mut SofSource,
    num_sources: i32,
    sinks: *mut *mut SofSink,
    num_sinks: i32,
) -> i32;
pub type IsReadyFn = unsafe extern "C" fn(
    m: *mut ProcessingModuleHandle,
    sources: *mut *mut SofSource,
    num_sources: i32,
    sinks: *mut *mut SofSink,
    num_sinks: i32,
) -> bool;
pub type ProcessFn = PrepareFn;
pub type ProcessAudioStreamFn = unsafe extern "C" fn(
    m: *mut ProcessingModuleHandle,
    inputs: *mut InputStreamBuffer,
    n_in: i32,
    outputs: *mut OutputStreamBuffer,
    n_out: i32,
) -> i32;
pub type ProcessRawDataFn = ProcessAudioStreamFn;
pub type SetConfigParamFn =
    unsafe extern "C" fn(m: *mut ProcessingModuleHandle, param_id_data: u32) -> i32;
pub type GetConfigParamFn =
    unsafe extern "C" fn(m: *mut ProcessingModuleHandle, param_id_data: *mut u32) -> i32;
pub type SetConfigurationFn = unsafe extern "C" fn(
    m: *mut ProcessingModuleHandle,
    config_id: u32,
    pos: ModuleCfgFragmentPosition,
    data_offset_size: u32,
    fragment: *const u8,
    fragment_size: usize,
    response: *mut u8,
    response_size: usize,
) -> i32;
pub type GetConfigurationFn = unsafe extern "C" fn(
    m: *mut ProcessingModuleHandle,
    config_id: u32,
    data_offset_size: *mut u32,
    fragment: *mut u8,
    fragment_size: usize,
) -> i32;
pub type SetProcessingModeFn =
    unsafe extern "C" fn(m: *mut ProcessingModuleHandle, mode: ModuleProcessingMode) -> i32;
pub type GetProcessingModeFn =
    unsafe extern "C" fn(m: *mut ProcessingModuleHandle) -> ModuleProcessingMode;
pub type ResetFn = InitFn;
pub type FreeFn = InitFn;
pub type BindFn =
    unsafe extern "C" fn(m: *mut ProcessingModuleHandle, bind_data: *mut BindInfo) -> i32;
pub type UnbindFn = BindFn;
pub type TriggerFn = unsafe extern "C" fn(m: *mut ProcessingModuleHandle, cmd: i32) -> i32;

/// `#[repr(C)]` mirror of `struct module_interface` (interface.h, public API
/// — without `SOF_MODULE_API_PRIVATE`).
#[repr(C)]
pub struct ModuleInterface {
    pub init: Option<InitFn>,
    pub prepare: Option<PrepareFn>,
    pub is_ready_to_process: Option<IsReadyFn>,
    pub process: Option<ProcessFn>,
    pub process_audio_stream: Option<ProcessAudioStreamFn>,
    pub process_raw_data: Option<ProcessRawDataFn>,
    pub set_config_param: Option<SetConfigParamFn>,
    pub get_config_param: Option<GetConfigParamFn>,
    pub set_configuration: Option<SetConfigurationFn>,
    pub get_configuration: Option<GetConfigurationFn>,
    pub set_processing_mode: Option<SetProcessingModeFn>,
    pub get_processing_mode: Option<GetProcessingModeFn>,
    pub reset: Option<ResetFn>,
    pub free: Option<FreeFn>,
    pub bind: Option<BindFn>,
    pub unbind: Option<UnbindFn>,
    pub trigger: Option<TriggerFn>,
}

// -------------------------------------------------------------------------
// Safe-Rust trait. All methods default to `Err(ENOSYS)` so an implementor
// only writes what they need.
// -------------------------------------------------------------------------

/// Standard Linux/POSIX-ish error codes used by SOF.
pub mod err {
    pub const EPERM: i32 = -1;
    pub const ENOENT: i32 = -2;
    pub const EIO: i32 = -5;
    pub const ENOMEM: i32 = -12;
    pub const EFAULT: i32 = -14;
    pub const EBUSY: i32 = -16;
    pub const ENODEV: i32 = -19;
    pub const EINVAL: i32 = -22;
    pub const ENOSYS: i32 = -38;
}

/// Slim Rust-side view of `struct sof_source*` / `struct sof_sink*` pairs
/// passed to processing callbacks. The slices are valid for the duration
/// of the call only.
///
/// The raw pointers are exposed for backwards-compatibility with code
/// that talks to the legacy FFI helpers directly, but new module code
/// should prefer the safe accessors [`StreamCtx::source`] /
/// [`StreamCtx::sink`], which hand out borrow-checked
/// [`audio::Source<'_>`] / [`audio::Sink<'_>`] wrappers tied to the
/// lifetime of the callback.
pub struct StreamCtx<'a> {
    pub sources: &'a mut [*mut SofSource],
    pub sinks: &'a mut [*mut SofSink],
}

impl<'a> StreamCtx<'a> {
    /// Return a safe borrow over `sources[i]`, or `None` if `i` is
    /// out of range or the pointer is null. The returned
    /// [`audio::Source`] borrows from `self`, so two sources cannot
    /// be held simultaneously through this method; for that case
    /// pair this with [`StreamCtx::source_pair`].
    pub fn source(&mut self, i: usize) -> Option<audio::Source<'_>> {
        let p = *self.sources.get(i)?;
        if p.is_null() {
            None
        } else {
            // SAFETY: the SOF module adapter promises that every
            // non-null entry in `sources[]` points at a live
            // `struct sof_source` for the duration of this callback,
            // which is at least `'a`, and that two callbacks for the
            // same module never run concurrently. The returned
            // `Source<'_>` borrow lifetime is tied to `self`, so the
            // wrapper cannot outlive the callback.
            Some(unsafe { audio::Source::from_raw(p) })
        }
    }

    /// Same as [`StreamCtx::source`] for sinks.
    pub fn sink(&mut self, i: usize) -> Option<audio::Sink<'_>> {
        let p = *self.sinks.get(i)?;
        if p.is_null() {
            None
        } else {
            // SAFETY: see `source` above.
            Some(unsafe { audio::Sink::from_raw(p) })
        }
    }

    /// Return safe borrows over `sources[0]` and `sinks[0]`, the
    /// common "single source, single sink" pipeline shape used by
    /// effects modules. Returns `Err(EINVAL)` if either is missing.
    pub fn primary_pair(&mut self) -> Result<(audio::Source<'_>, audio::Sink<'_>), i32> {
        let s = *self.sources.first().unwrap_or(&core::ptr::null_mut());
        let k = *self.sinks.first().unwrap_or(&core::ptr::null_mut());
        if s.is_null() || k.is_null() {
            return Err(err::EINVAL);
        }
        // SAFETY: distinct sof_source / sof_sink objects; borrows
        // are tied to `self`'s lifetime via the wrapper types.
        Ok(unsafe { (audio::Source::from_raw(s), audio::Sink::from_raw(k)) })
    }
}

/// Slim Rust-side view of the deprecated
/// `struct input_stream_buffer *` / `struct output_stream_buffer *`
/// arrays passed to `process_audio_stream` / `process_raw_data`. The
/// slices are valid for the duration of the call only.
pub struct LegacyStreamCtx<'a> {
    pub inputs: &'a mut [InputStreamBuffer],
    pub outputs: &'a mut [OutputStreamBuffer],
}

/// One fragment of a multi-fragment `set_configuration` blob. The
/// SOF host driver splits any blob >`MAX_BLOB_SIZE` bytes into a
/// sequence of fragments; `config_id` is only meaningful on the
/// first fragment, otherwise it is 0. The trait method is invoked
/// once per fragment.
pub struct ConfigFragmentIn<'a> {
    pub config_id: u32,
    pub pos: ModuleCfgFragmentPosition,
    pub data_offset_size: u32,
    pub fragment: &'a [u8],
    pub response: &'a mut [u8],
}

/// In/out arguments for the host's "get a configuration blob"
/// transaction. `fragment` is the caller's output buffer, sized to
/// the available room; the implementor writes up to `fragment.len()`
/// bytes and updates `*data_offset_size` to report the total size of
/// the configuration (used by the host to drive multi-fragment
/// retrieval).
pub struct ConfigFragmentOut<'a> {
    pub config_id: u32,
    pub data_offset_size: &'a mut u32,
    pub fragment: &'a mut [u8],
}
// -------------------------------------------------------------------------
// Global-allocator adapter routed to SOF's `mod_alloc` / `mod_free`.
//
// Rust's `GlobalAlloc` trait does not pass any context to `alloc()` /
// `dealloc()`, but `mod_alloc()` / `mod_free()` require the
// `struct processing_module *` that owns the allocation. We bridge that
// gap by stashing the currently-active module pointer in a global slot
// (`CURRENT_MODULE`) on entry to each callback emitted by
// [`define_module!`] and restoring the previous value on exit, so any
// Rust allocations performed during a callback land in the right
// per-module pool.
//
// Caveats:
//
//   * `alloc()` outside an active callback returns null. The SOF
//     allocator is module-scoped, so an allocation that escapes the
//     module's lifetime would not have a sensible owner anyway.
//   * The current-module slot is a single `AtomicPtr` and is therefore
//     racy across cores. Today's SOF pipeline processing model is
//     single-DSP-core, so callbacks for a given module never run
//     concurrently; multi-core support will need a per-CPU slot
//     (e.g. via `arch_proc_id()`).
// -------------------------------------------------------------------------

pub mod alloc {
    use super::ProcessingModuleHandle;
    use core::alloc::{GlobalAlloc, Layout};
    use core::ptr;

    unsafe extern "C" {
        /// C-side storage for the currently-active
        /// `processing_module *`. Defined in `sof_rust_mod_alloc.c`.
        /// Accessed directly (not via a function call) so that each
        /// `define_module!` shim needs at most one literal reference
        /// — keeping the Xtensa BFD linker's per-function `.literal`
        /// pools small enough to satisfy `l32r`'s ±256 KiB range.
        ///
        /// Single-DSP-core only for now; multi-core support will
        /// need a per-CPU slot keyed off `arch_proc_id()`.
        pub(crate) static mut sof_rust_current_module: *mut ProcessingModuleHandle;

        fn sof_rust_mod_alloc(
            m: *mut ProcessingModuleHandle,
            size: usize,
            align: usize,
        ) -> *mut u8;
        fn sof_rust_mod_free(m: *mut ProcessingModuleHandle, ptr: *const u8) -> i32;
    }

    /// Publish a new `processing_module *` to the global allocator,
    /// returning the previous value. Pair with [`leave`] in the same
    /// callback. Called exclusively from the shims emitted by
    /// [`crate::define_module!`]; not part of the public API.
    ///
    /// # Single-core assumption
    /// Accesses a plain pointer slot without atomic operations. Safe
    /// only because SOF's pipeline model guarantees callbacks for a
    /// given module never run concurrently on today's single-DSP-core
    /// hardware. Multi-core support will need a per-CPU slot keyed
    /// off `arch_proc_id()`.
    #[doc(hidden)]
    #[inline(always)]
    pub fn enter(new: *mut ProcessingModuleHandle) -> *mut ProcessingModuleHandle {
        // SAFETY: single-DSP-core access to a plain pointer slot; no
        // aliasing or tearing concerns on Xtensa.
        unsafe {
            let prev = sof_rust_current_module;
            sof_rust_current_module = new;
            prev
        }
    }

    /// Restore the previous `processing_module *` returned by an
    /// earlier [`enter`] call. Called exclusively from
    /// [`crate::define_module!`] shims; not part of the public API.
    #[doc(hidden)]
    #[inline(always)]
    pub fn leave(prev: *mut ProcessingModuleHandle) {
        // SAFETY: ditto.
        unsafe {
            sof_rust_current_module = prev;
        }
    }

    /// Returns the `processing_module *` currently published to the
    /// global allocator, or null if no callback is active. Used only
    /// by [`SofModuleAlloc`]; not part of the public API.
    #[doc(hidden)]
    #[inline(always)]
    pub(crate) fn current_module() -> *mut ProcessingModuleHandle {
        // SAFETY: ditto.
        unsafe { sof_rust_current_module }
    }

    /// `#[global_allocator]`-compatible adapter that routes Rust
    /// allocations through SOF's `mod_alloc` / `mod_free`, bound to
    /// the processing module currently active on this thread.
    ///
    /// Install via [`crate::install_global_allocator!`] from a leaf
    /// crate (staticlib / cdylib produced by an audio module). Rust
    /// only permits one `#[global_allocator]` per linked binary, so
    /// only the leaf may install it.
    pub struct SofModuleAlloc;

    unsafe impl GlobalAlloc for SofModuleAlloc {
        #[inline]
        unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
            let m = current_module();
            if m.is_null() {
                return ptr::null_mut();
            }
            // SAFETY: `sof_rust_mod_alloc` is a thin wrapper around
            // SOF's `mod_alloc`; it returns null or a valid pointer
            // owned by `m`'s allocator.
            unsafe { sof_rust_mod_alloc(m, layout.size(), layout.align()) }
        }

        #[inline]
        unsafe fn dealloc(&self, ptr: *mut u8, _layout: Layout) {
            let m = current_module();
            if m.is_null() || ptr.is_null() {
                // No callback active: the module's allocator is being
                // torn down (or this is a stray drop). SOF will
                // reclaim everything via `mod_free_all` at unload, so
                // we silently drop the request rather than calling
                // into the C side without an owner.
                return;
            }
            // SAFETY: `ptr` was returned from a previous
            // `sof_rust_mod_alloc(m, ...)` (the GlobalAlloc contract
            // guarantees layout matches and the original `alloc`
            // captured `m` from the same per-thread slot).
            let _ = unsafe { sof_rust_mod_free(m, ptr as *const u8) };
        }
    }
}

/// Install [`alloc::SofModuleAlloc`] as the `#[global_allocator]` for
/// the consuming module crate.
///
/// Invoke from the leaf crate (staticlib / cdylib) exactly once:
///
/// ```ignore
/// extern crate alloc;
/// sof_module::install_global_allocator!();
/// ```
///
/// After this, `Box`, `Vec`, `String` etc. become available during
/// any SOF callback emitted by [`define_module!`]; allocations
/// outside a callback return null (or, for `Box::new`, abort via the
/// standard alloc-error handler).
#[macro_export]
macro_rules! install_global_allocator {
    () => {
        #[global_allocator]
        static __SOF_MODULE_GLOBAL_ALLOCATOR: $crate::alloc::SofModuleAlloc =
            $crate::alloc::SofModuleAlloc;
    };
}

// -------------------------------------------------------------------------
// `sof_source` / `sof_sink` C API bindings.
//
// The real `source_*` / `sink_*` helpers in
// <module/audio/source_api.h> / <module/audio/sink_api.h> are declared
// `static inline`, so they have no global ELF symbol. We bind to the
// out-of-line wrappers in `sof/rust/sof_module/c_shim/sof_rust_source_sink.c`
// instead (`sof_rust_source_*` / `sof_rust_sink_*`).
//
// Frame format codes match `enum sof_ipc_frame` in
// <module/ipc/stream.h>.
// -------------------------------------------------------------------------

/// `enum sof_ipc_frame` values used by SOF audio-processing modules.
pub mod frame_fmt {
    pub const S16_LE:     u32 = 0;
    pub const S24_4LE:    u32 = 1;
    pub const S32_LE:     u32 = 2;
    pub const FLOAT:      u32 = 3;
    pub const S24_3LE:    u32 = 4;
    pub const S24_4LE_MSB: u32 = 5;
    pub const U8:         u32 = 6;
    pub const S16_4LE:    u32 = 7;
}

unsafe extern "C" {
    #[link_name = "sof_rust_bind_get_type"]
    fn bind_get_type(bd: *const BindInfo) -> u32;
    #[link_name = "sof_rust_bind_get_source"]
    fn bind_get_source(bd: *const BindInfo) -> *mut SofSource;
    #[link_name = "sof_rust_bind_get_sink"]
    fn bind_get_sink(bd: *const BindInfo) -> *mut SofSink;
}

/// Safe borrow over a SOF `struct bind_info *` for the lifetime of a
/// `bind` / `unbind` callback. Obtain from the
/// [`ProcessingModule::bind`] / [`ProcessingModule::unbind`] argument.
///
/// Provides safe accessors for the bind type and the associated
/// source / sink pointer without exposing the C union directly.
pub struct BindData<'a> {
    ptr: *mut BindInfo,
    _marker: core::marker::PhantomData<&'a BindInfo>,
}

impl<'a> BindData<'a> {
    /// Internal constructor called from [`define_module!`] shims.
    /// Not part of the stable public API.
    #[doc(hidden)]
    pub fn __from_raw(ptr: *mut BindInfo) -> Self {
        Self { ptr, _marker: core::marker::PhantomData }
    }

    /// Whether the binding is for a source (data provider) or a
    /// sink (data consumer).
    pub fn bind_type(&self) -> BindType {
        // SAFETY: ptr is valid for 'a per from_raw contract.
        match unsafe { bind_get_type(self.ptr) } {
            0 => BindType::Source,
            _ => BindType::Sink,
        }
    }

    /// Return the [`audio::Source`] being bound, or `None` if the
    /// bind type is `Sink`.
    pub fn source(&self) -> Option<audio::Source<'a>> {
        // SAFETY: bind_get_source returns NULL if type != SOURCE;
        // non-null pointer is valid for 'a per the SOF module-adapter
        // contract (the sof_source lives at least as long as the callback).
        let p = unsafe { bind_get_source(self.ptr) };
        if p.is_null() {
            None
        } else {
            Some(unsafe { audio::Source::from_raw(p) })
        }
    }

    /// Return the [`audio::Sink`] being bound, or `None` if the
    /// bind type is `Source`.
    pub fn sink(&self) -> Option<audio::Sink<'a>> {
        // SAFETY: mirrors source() above.
        let p = unsafe { bind_get_sink(self.ptr) };
        if p.is_null() {
            None
        } else {
            Some(unsafe { audio::Sink::from_raw(p) })
        }
    }
}

unsafe extern "C" {
    #[link_name = "sof_rust_source_get_data_available"]
    pub fn source_get_data_available(source: *mut SofSource) -> usize;
    #[link_name = "sof_rust_source_get_data_frames_available"]
    pub fn source_get_data_frames_available(source: *mut SofSource) -> usize;
    #[link_name = "sof_rust_source_get_frm_fmt"]
    pub fn source_get_frm_fmt(source: *mut SofSource) -> u32;
    #[link_name = "sof_rust_source_get_channels"]
    pub fn source_get_channels(source: *mut SofSource) -> u32;
    #[link_name = "sof_rust_source_get_data"]
    pub fn source_get_data(
        source: *mut SofSource,
        req_size: usize,
        data_ptr: *mut *const c_void,
        buffer_start: *mut *const c_void,
        buffer_size: *mut usize,
    ) -> i32;
    #[link_name = "sof_rust_source_release_data"]
    pub fn source_release_data(source: *mut SofSource, free_size: usize) -> i32;

    #[link_name = "sof_rust_sink_get_free_size"]
    pub fn sink_get_free_size(sink: *mut SofSink) -> usize;
    #[link_name = "sof_rust_sink_get_buffer"]
    pub fn sink_get_buffer(
        sink: *mut SofSink,
        req_size: usize,
        data_ptr: *mut *mut c_void,
        buffer_start: *mut *mut c_void,
        buffer_size: *mut usize,
    ) -> i32;
    #[link_name = "sof_rust_sink_commit_buffer"]
    pub fn sink_commit_buffer(sink: *mut SofSink, commit_size: usize) -> i32;
    #[link_name = "sof_rust_source_to_sink_copy"]
    pub fn source_to_sink_copy(
        source: *mut SofSource,
        sink: *mut SofSink,
        free_source: bool,
        size: usize,
    ) -> i32;
}

/// Audio-buffer helpers built on top of the raw FFI bindings above.
///
/// The `source_get_data()` / `sink_get_buffer()` calls return a pointer
/// into a *circular* buffer along with the buffer's start and size, and
/// the producer / consumer is responsible for handling the wrap. These
/// helpers take care of the bookkeeping for common sample formats.
pub mod audio {
    use super::*;
    use core::marker::PhantomData;

    // ---------------------------------------------------------------
    // Safe wrappers around `struct sof_source *` / `struct sof_sink *`.
    //
    // A `*mut SofSource` handed to us from C is unvalidated raw FFI:
    // Rust can't prove it points at a live object, isn't null, isn't
    // aliased, or outlives the call. The SOF module-adapter contract
    // _does_ guarantee all of those for the duration of each
    // callback invocation, so we wrap that contract in a borrow-
    // checked type whose lifetime is tied to the callback (via
    // [`crate::StreamCtx`]).
    //
    // The wrappers carry the pointer as a private field and expose
    // only safe methods. Constructing one is the only `unsafe` step
    // — and it is performed exactly once, inside [`crate::StreamCtx`]
    // accessors, after the null check.
    // ---------------------------------------------------------------

    /// Safe borrow over a SOF `struct sof_source *` for the lifetime
    /// of a processing callback. Obtain from
    /// [`crate::StreamCtx::source`] / [`crate::StreamCtx::primary_pair`].
    pub struct Source<'a> {
        ptr: *mut SofSource,
        _marker: PhantomData<&'a mut SofSource>,
    }

    /// Safe borrow over a SOF `struct sof_sink *` for the lifetime
    /// of a processing callback. Obtain from
    /// [`crate::StreamCtx::sink`] / [`crate::StreamCtx::primary_pair`].
    pub struct Sink<'a> {
        ptr: *mut SofSink,
        _marker: PhantomData<&'a mut SofSink>,
    }

    impl<'a> Source<'a> {
        /// Adopt a raw `struct sof_source *` as a safe borrow.
        ///
        /// # Safety
        ///
        /// `ptr` must point at a live `struct sof_source` for the
        /// entire lifetime `'a`, must not alias any other live
        /// `Source<'_>` over the same object, and must be safe to
        /// pass to the `source_*` family of helpers exported by
        /// [`sof/rust/sof_module/c_shim/sof_rust_source_sink.c`].
        ///
        /// Most callers should obtain a `Source` via
        /// [`crate::StreamCtx::source`] instead, which performs the
        /// null check and ties the borrow to the surrounding
        /// callback.
        pub unsafe fn from_raw(ptr: *mut SofSource) -> Self {
            Self { ptr, _marker: PhantomData }
        }

        /// Internal raw pointer, used by the `audio::passthrough` /
        /// `process_stereo_*` helpers inside this crate.
        #[inline]
        pub(crate) fn as_ptr(&mut self) -> *mut SofSource {
            self.ptr
        }

        /// Channel count of the source stream.
        #[inline]
        pub fn channels(&self) -> u32 {
            // SAFETY: thin call into a const-correct C accessor on
            // a pointer the caller of `from_raw` promised is live.
            unsafe { source_get_channels(self.ptr) }
        }

        /// Frame format code (matches `enum sof_ipc_frame`; see
        /// [`crate::frame_fmt`]).
        #[inline]
        pub fn frame_fmt(&self) -> u32 {
            // SAFETY: ditto.
            unsafe { source_get_frm_fmt(self.ptr) }
        }

        /// Bytes currently available to consume from the source.
        #[inline]
        pub fn available(&self) -> usize {
            // SAFETY: ditto.
            unsafe { source_get_data_available(self.ptr) }
        }

        /// Frames currently available to consume from the source.
        #[inline]
        pub fn frames_available(&self) -> usize {
            // SAFETY: ditto.
            unsafe { source_get_data_frames_available(self.ptr) }
        }
    }

    impl<'a> Sink<'a> {
        /// Adopt a raw `struct sof_sink *` as a safe borrow.
        ///
        /// # Safety
        ///
        /// Mirrors [`Source::from_raw`].
        pub unsafe fn from_raw(ptr: *mut SofSink) -> Self {
            Self { ptr, _marker: PhantomData }
        }

        /// Internal raw pointer, used by the `audio::passthrough` /
        /// `process_stereo_*` helpers inside this crate.
        #[inline]
        pub(crate) fn as_ptr(&mut self) -> *mut SofSink {
            self.ptr
        }

        /// Bytes of free space currently available in the sink.
        #[inline]
        pub fn free_size(&self) -> usize {
            // SAFETY: thin call into a const-correct C accessor on
            // a pointer the caller of `from_raw` promised is live.
            unsafe { sink_get_free_size(self.ptr) }
        }
    }

    /// Straight pass-through copy from `src` to `snk` of up to
    /// `min(source available, sink free)` bytes. Wraps SOF's
    /// `source_to_sink_copy()` helper, which already handles the
    /// circular-buffer wrap and the source-release / sink-commit
    /// bookkeeping. Returns the number of bytes copied on success.
    ///
    /// Use this from a `process()` implementation that wants a
    /// "module is bypassed" path without duplicating the wrap-aware
    /// memcpy in Rust.
    pub fn passthrough(src: &mut Source<'_>, snk: &mut Sink<'_>) -> Result<usize, i32> {
        let avail = src.available();
        let free = snk.free_size();
        let bytes = if avail < free { avail } else { free };
        if bytes == 0 {
            return Ok(0);
        }
        // SAFETY: pointers come from live `Source` / `Sink` borrows;
        // `source_to_sink_copy` does its own release/commit pairing.
        let rc = unsafe { source_to_sink_copy(src.as_ptr(), snk.as_ptr(), true, bytes) };
        if rc != 0 { Err(rc) } else { Ok(bytes) }
    }

    // ---------------------------------------------------------------
    // Per-frame closure-based processors.
    //
    // These hide the entire `source_get_data` / `sink_get_buffer`
    // / circular-wrap / release-commit dance behind a safe API: the
    // caller supplies an `FnMut(&[T; N], &mut [T; N])` and the
    // wrapper takes care of acquiring the windows, advancing the
    // wrap pointers in N-sample frames, and pairing the release /
    // commit calls on every exit path (including early `Err`s from
    // the FFI). All `unsafe` lives here; client modules can implement
    // a sample-by-sample DSP loop in 100% safe Rust.
    //
    // The closure receives fixed-length array references (`&[T; 2]`
    // for stereo), which the optimizer reduces to direct loads /
    // stores with no bounds-check or panic edge — important on
    // Xtensa where `core::panicking::*` paths emit literal-pool
    // entries that have hit cross-section l32r issues in the past.
    //
    // Frame-aligned access is required: the helpers reject
    // `channels() != 2`, round the byte count down to whole stereo
    // frames, and step the buffer pointer by one frame at a time so
    // the wrap check fires *between* frames rather than mid-frame.
    // SOF audio buffers are aligned far above the 4-byte / 8-byte
    // frame size, so the typed-pointer cast inside is well-defined.
    // ---------------------------------------------------------------

    /// Run `f(src_frame, dst_frame)` over each stereo `[i16; 2]`
    /// frame in up to `min(source available, sink free)` bytes
    /// (rounded down to whole 4-byte stereo frames). Handles
    /// circular-buffer wrap and the source-release / sink-commit
    /// pairing internally. Returns the number of bytes processed.
    ///
    /// If the source channel count is not 2, returns `Ok(0)` without
    /// touching either buffer; callers that want a copy in that
    /// case should fall back to [`passthrough`].
    pub fn process_stereo_s16<F>(
        src: &mut Source<'_>,
        snk: &mut Sink<'_>,
        mut f: F,
    ) -> Result<usize, i32>
    where
        F: FnMut(&[i16; 2], &mut [i16; 2]),
    {
        const FRAME_BYTES: usize = 4;

        if src.channels() != 2 {
            return Ok(0);
        }
        let avail = src.available();
        let free = snk.free_size();
        let mut bytes = if avail < free { avail } else { free };
        bytes &= !(FRAME_BYTES - 1);
        if bytes == 0 {
            return Ok(0);
        }

        let src_p = src.as_ptr();
        let snk_p = snk.as_ptr();
        // SAFETY: pointers come from live `Source` / `Sink` borrows;
        // `source_get_data` / `sink_get_buffer` populate the (head,
        // start, size) triple describing a contiguous run plus the
        // wrap target. We honor the release/commit contract on every
        // exit path. Frame-aligned strides and `bytes % FRAME_BYTES
        // == 0` keep the per-frame array cast in bounds.
        unsafe {
            let mut x: *const c_void = core::ptr::null();
            let mut x_start: *const c_void = core::ptr::null();
            let mut x_size: usize = 0;
            let rc = source_get_data(src_p, bytes, &mut x, &mut x_start, &mut x_size);
            if rc != 0 {
                return Err(rc);
            }

            let mut y: *mut c_void = core::ptr::null_mut();
            let mut y_start: *mut c_void = core::ptr::null_mut();
            let mut y_size: usize = 0;
            let rc = sink_get_buffer(snk_p, bytes, &mut y, &mut y_start, &mut y_size);
            if rc != 0 {
                let _ = source_release_data(src_p, 0);
                return Err(rc);
            }

            let mut xp = x as *const [i16; 2];
            let mut yp = y as *mut [i16; 2];
            // Number of whole stereo frames in each buffer's wrap
            // range. `>> 2` is the divide by FRAME_BYTES.
            let x_end = (x_start as *const [i16; 2]).wrapping_add(x_size >> 2);
            let y_end = (y_start as *mut [i16; 2]).wrapping_add(y_size >> 2);
            let mut frames = bytes >> 2;

            while frames > 0 {
                if xp >= x_end {
                    xp = x_start as *const [i16; 2];
                }
                if yp >= y_end {
                    yp = y_start as *mut [i16; 2];
                }
                f(&*xp, &mut *yp);
                xp = xp.add(1);
                yp = yp.add(1);
                frames -= 1;
            }

            let rc = source_release_data(src_p, bytes);
            if rc != 0 {
                return Err(rc);
            }
            let rc = sink_commit_buffer(snk_p, bytes);
            if rc != 0 { Err(rc) } else { Ok(bytes) }
        }
    }

    /// Same as [`process_stereo_s16`] but for 32-bit interleaved
    /// stereo PCM. Equally applicable to `S32_LE` and `S24_4LE` —
    /// the byte layout of an (L, R) pair is identical. Frame size
    /// is 8 bytes.
    pub fn process_stereo_s32<F>(
        src: &mut Source<'_>,
        snk: &mut Sink<'_>,
        mut f: F,
    ) -> Result<usize, i32>
    where
        F: FnMut(&[i32; 2], &mut [i32; 2]),
    {
        const FRAME_BYTES: usize = 8;

        if src.channels() != 2 {
            return Ok(0);
        }
        let avail = src.available();
        let free = snk.free_size();
        let mut bytes = if avail < free { avail } else { free };
        bytes &= !(FRAME_BYTES - 1);
        if bytes == 0 {
            return Ok(0);
        }

        let src_p = src.as_ptr();
        let snk_p = snk.as_ptr();
        // SAFETY: same shape as `process_stereo_s16` above.
        unsafe {
            let mut x: *const c_void = core::ptr::null();
            let mut x_start: *const c_void = core::ptr::null();
            let mut x_size: usize = 0;
            let rc = source_get_data(src_p, bytes, &mut x, &mut x_start, &mut x_size);
            if rc != 0 {
                return Err(rc);
            }

            let mut y: *mut c_void = core::ptr::null_mut();
            let mut y_start: *mut c_void = core::ptr::null_mut();
            let mut y_size: usize = 0;
            let rc = sink_get_buffer(snk_p, bytes, &mut y, &mut y_start, &mut y_size);
            if rc != 0 {
                let _ = source_release_data(src_p, 0);
                return Err(rc);
            }

            let mut xp = x as *const [i32; 2];
            let mut yp = y as *mut [i32; 2];
            // `>> 3` is the divide by FRAME_BYTES.
            let x_end = (x_start as *const [i32; 2]).wrapping_add(x_size >> 3);
            let y_end = (y_start as *mut [i32; 2]).wrapping_add(y_size >> 3);
            let mut frames = bytes >> 3;

            while frames > 0 {
                if xp >= x_end {
                    xp = x_start as *const [i32; 2];
                }
                if yp >= y_end {
                    yp = y_start as *mut [i32; 2];
                }
                f(&*xp, &mut *yp);
                xp = xp.add(1);
                yp = yp.add(1);
                frames -= 1;
            }

            let rc = source_release_data(src_p, bytes);
            if rc != 0 {
                return Err(rc);
            }
            let rc = sink_commit_buffer(snk_p, bytes);
            if rc != 0 { Err(rc) } else { Ok(bytes) }
        }
    }
}

// -------------------------------------------------------------------------
// Safe helpers for the "large config" multi-fragment blob handlers
// (`set_configuration` / `get_configuration`, the trait methods that
// back `module_set_large_config` / `module_get_large_config` on the C
// side). Both helpers require a heap, which is supplied by
// [`install_global_allocator!`] in the consuming module crate.
// -------------------------------------------------------------------------

/// Safe parsers and constants for the IPC4 kcontrol payload that the
/// host driver delivers via `set_configuration` / `get_configuration`.
///
/// On IPC4 the `config_id` argument doubles as a *param id*: the host
/// uses the well-known constants below to multiplex switch / enum /
/// bytes controls over the same configuration channel. For switch
/// and enum controls the fragment payload is a
/// `struct sof_ipc4_control_msg_payload` followed by a packed
/// `sof_ipc4_ctrl_value_chan[]` array.
///
/// All accessors take `&[u8]` and parse with byte-granular loads so
/// callers don't need to worry about the C struct's `__packed`
/// layout.
pub mod ipc4_control {
    use super::err;

    /// `SOF_IPC4_SWITCH_CONTROL_PARAM_ID` from `<ipc4/header.h>`.
    pub const SWITCH_CONTROL_PARAM_ID: u32 = 200;
    /// `SOF_IPC4_ENUM_CONTROL_PARAM_ID` from `<ipc4/header.h>`.
    pub const ENUM_CONTROL_PARAM_ID: u32 = 201;
    /// `SOF_IPC4_BYTES_CONTROL_PARAM_ID` from `<ipc4/header.h>`.
    pub const BYTES_CONTROL_PARAM_ID: u32 = 202;

    /// Size of the fixed `sof_ipc4_control_msg_payload` header
    /// (u16 id + u16 num_elems + u32 reserved[4]).
    pub const HEADER_BYTES: usize = 2 + 2 + 4 * 4;
    /// Size of one `sof_ipc4_ctrl_value_chan` element
    /// (u32 channel + u32 value).
    pub const CHAN_BYTES: usize = 4 + 4;

    /// One element of `chanv[]`: a (channel, value) pair.
    #[derive(Copy, Clone, Debug, PartialEq, Eq)]
    pub struct ChanValue {
        pub channel: u32,
        pub value: u32,
    }

    /// Parsed view of a switch- or enum-control payload.
    #[derive(Copy, Clone, Debug)]
    pub struct SwitchPayload<'a> {
        /// Control id (matches the `id` in the host's `mixer_control`
        /// / `enum_control` ALSA tag, not the IPC4 `config_id`).
        pub id: u16,
        /// Number of valid (channel, value) pairs available via
        /// [`SwitchPayload::iter`].
        pub num_elems: u16,
        /// Raw `chanv[]` bytes; access via [`SwitchPayload::iter`].
        chanv: &'a [u8],
    }

    impl<'a> SwitchPayload<'a> {
        /// Iterate the `chanv[]` array as decoded
        /// [`ChanValue`] entries.
        pub fn iter(&self) -> impl Iterator<Item = ChanValue> + '_ {
            (0..self.num_elems as usize).map(|i| {
                let off = i * CHAN_BYTES;
                let c = &self.chanv[off..off + CHAN_BYTES];
                // Byte-level loads — payload is `__packed`, so don't
                // pretend the buffer is u32-aligned.
                let channel = u32::from_le_bytes([c[0], c[1], c[2], c[3]]);
                let value = u32::from_le_bytes([c[4], c[5], c[6], c[7]]);
                ChanValue { channel, value }
            })
        }

        /// Convenience: return the value for the first element, or
        /// `Err(EINVAL)` if the payload is empty.
        pub fn first_value(&self) -> Result<u32, i32> {
            self.iter().next().map(|c| c.value).ok_or(err::EINVAL)
        }
    }

    /// Parse a switch- or enum-control fragment. Validates the
    /// header size and that `num_elems` fits the remaining buffer.
    /// Use with `config_id == SWITCH_CONTROL_PARAM_ID` or
    /// `ENUM_CONTROL_PARAM_ID`.
    pub fn parse_switch(fragment: &[u8]) -> Result<SwitchPayload<'_>, i32> {
        if fragment.len() < HEADER_BYTES {
            return Err(err::EINVAL);
        }
        let id = u16::from_le_bytes([fragment[0], fragment[1]]);
        let num_elems = u16::from_le_bytes([fragment[2], fragment[3]]);
        let need = (num_elems as usize)
            .checked_mul(CHAN_BYTES)
            .and_then(|n| n.checked_add(HEADER_BYTES))
            .ok_or(err::EINVAL)?;
        if fragment.len() < need {
            return Err(err::EINVAL);
        }
        Ok(SwitchPayload {
            id,
            num_elems,
            chanv: &fragment[HEADER_BYTES..need],
        })
    }
}

/// Accumulator and emitter helpers around [`ProcessingModule::set_configuration`]
/// / [`ProcessingModule::get_configuration`] fragments.
///
/// The SOF host driver splits any configuration blob larger than the
/// host/DSP mailbox into a sequence of fragments and invokes the
/// module's `set_configuration` / `get_configuration` callback once
/// per fragment. The position field (`First` / `Middle` / `Last` /
/// `Single`) and `data_offset_size` together describe where the
/// fragment lives in the full blob — see
/// `src/audio/module_adapter/module_adapter_ipc4.c` for the C-side
/// definition of `module_set_large_config` /
/// `module_get_large_config`.
///
/// Module authors typically own a single [`LargeConfigAccumulator`]
/// per `config_id` (or one shared accumulator + a switch on
/// `config_id`); feed each `set_configuration` fragment into it via
/// [`LargeConfigAccumulator::feed`] and act on the completed blob
/// returned by the `Last` / `Single` fragment.
///
/// For the get side, use [`emit_config_fragment`] to copy a slice
/// of an in-memory blob into a `ConfigFragmentOut` view in line
/// with the SOF convention (`*data_offset_size` is the read offset
/// on entry, the number of bytes written on exit).
#[cfg(feature = "alloc")]
pub mod large_config {
    use super::{err, ConfigFragmentIn, ConfigFragmentOut, ModuleCfgFragmentPosition};
    use rust_alloc::vec::Vec;

    /// Accumulates the fragments of one multi-part configuration
    /// blob into a single owned `Vec<u8>`. Lives as long as the
    /// module instance.
    pub struct LargeConfigAccumulator {
        buf: Vec<u8>,
        expected: usize,
        config_id: u32,
    }

    impl LargeConfigAccumulator {
        /// Construct an empty accumulator. Allocates no heap until
        /// the first fragment arrives.
        pub const fn new() -> Self {
            Self {
                buf: Vec::new(),
                expected: 0,
                config_id: 0,
            }
        }

        /// Drop any half-collected blob and return the accumulator
        /// to its initial state. Called automatically on `Single`
        /// and `First` fragments; modules may also call it on error
        /// paths.
        pub fn reset(&mut self) {
            self.buf.clear();
            self.expected = 0;
            self.config_id = 0;
        }

        /// Capacity hint from the most recent `First` fragment, or 0
        /// for a `Single` blob (which already carries its full
        /// payload).
        pub fn expected_size(&self) -> usize {
            self.expected
        }

        /// `config_id` of the blob currently being accumulated.
        pub fn config_id(&self) -> u32 {
            self.config_id
        }

        /// Feed one fragment from a `set_configuration` callback.
        ///
        /// Returns:
        ///
        ///   * `Ok(Some((config_id, &blob)))` when the *complete*
        ///     blob is ready (on `Last` or `Single`). The borrow is
        ///     tied to `self` so it cannot outlive the accumulator.
        ///   * `Ok(None)` when more fragments are expected
        ///     (`First` or `Middle`).
        ///   * `Err(ENOMEM)` if the buffer could not be grown to
        ///     hold the announced blob size.
        ///   * `Err(EINVAL)` if fragments arrive out of order (e.g.
        ///     `Middle` without a prior `First`).
        pub fn feed<'a>(
            &'a mut self,
            frag: &ConfigFragmentIn<'_>,
        ) -> Result<Option<(u32, &'a [u8])>, i32> {
            match frag.pos {
                ModuleCfgFragmentPosition::Single => {
                    self.buf.clear();
                    self.buf.try_reserve(frag.fragment.len()).map_err(|_| err::ENOMEM)?;
                    self.buf.extend_from_slice(frag.fragment);
                    self.expected = frag.fragment.len();
                    self.config_id = frag.config_id;
                    Ok(Some((self.config_id, self.buf.as_slice())))
                }
                ModuleCfgFragmentPosition::First => {
                    self.buf.clear();
                    let total = frag.data_offset_size as usize;
                    self.expected = total;
                    self.config_id = frag.config_id;
                    // Reserve the announced total size up-front so
                    // later Middle/Last fragments cannot fail with
                    // ENOMEM mid-blob.
                    self.buf.try_reserve_exact(total).map_err(|_| err::ENOMEM)?;
                    self.buf.extend_from_slice(frag.fragment);
                    Ok(None)
                }
                ModuleCfgFragmentPosition::Middle => {
                    if self.expected == 0 {
                        return Err(err::EINVAL);
                    }
                    self.buf.extend_from_slice(frag.fragment);
                    Ok(None)
                }
                ModuleCfgFragmentPosition::Last => {
                    if self.expected == 0 {
                        return Err(err::EINVAL);
                    }
                    self.buf.extend_from_slice(frag.fragment);
                    Ok(Some((self.config_id, self.buf.as_slice())))
                }
            }
        }
    }

    impl Default for LargeConfigAccumulator {
        fn default() -> Self {
            Self::new()
        }
    }

    /// Copy a slice of `blob` into a `get_configuration` fragment
    /// view, following the SOF convention:
    ///
    ///   * On entry, `*frag.data_offset_size` is interpreted as the
    ///     byte offset within `blob` where this fragment should
    ///     start. Pass `0` for the first call.
    ///   * On exit, `*frag.data_offset_size` is updated to the
    ///     *new* read offset (`offset + bytes_written`), so the
    ///     next call can pick up where this one left off. For the
    ///     last fragment that equals `blob.len()`.
    ///
    /// Returns the number of bytes written into `frag.fragment`.
    pub fn emit_config_fragment(
        blob: &[u8],
        frag: &mut ConfigFragmentOut<'_>,
    ) -> Result<usize, i32> {
        let offset = *frag.data_offset_size as usize;
        if offset > blob.len() {
            return Err(err::EINVAL);
        }
        let remaining = blob.len() - offset;
        let n = if remaining < frag.fragment.len() {
            remaining
        } else {
            frag.fragment.len()
        };
        // `n <= frag.fragment.len()` and `n <= remaining`, so both
        // slices are valid; copy_from_slice does a memcpy.
        frag.fragment[..n].copy_from_slice(&blob[offset..offset + n]);
        *frag.data_offset_size = (offset + n) as u32;
        Ok(n)
    }
}


/// SOF processing-module operations, in safe Rust.
///
/// Implementors override only the methods they care about. Each method
/// returns `Result<(), i32>` where the error variant carries the negative
/// errno that the C caller will see. The default impl returns `Err(ENOSYS)`
/// for anything the module hasn't implemented; the macro then leaves the
/// corresponding C function-pointer slot as `None`, matching what the C
/// SOF code expects for "not provided".
#[allow(unused_variables)]
pub trait ProcessingModule: 'static + Sync {
    /// Whether to populate the optional slot in the generated
    /// `ModuleInterface`. Override and return `true` to wire up the
    /// matching shim. (We can't ask "is this method overridden" directly,
    /// so the macro emits all shims and reads these flags to decide which
    /// `Option<fn>` slots to populate.)
    const HAS_PREPARE: bool = false;
    const HAS_IS_READY: bool = false;
    const HAS_PROCESS: bool = false;
    const HAS_PROCESS_AUDIO_STREAM: bool = false;
    const HAS_PROCESS_RAW_DATA: bool = false;
    const HAS_SET_CONFIG_PARAM: bool = false;
    const HAS_GET_CONFIG_PARAM: bool = false;
    const HAS_SET_CONFIGURATION: bool = false;
    const HAS_GET_CONFIGURATION: bool = false;
    const HAS_SET_PROCESSING_MODE: bool = false;
    const HAS_GET_PROCESSING_MODE: bool = false;
    const HAS_RESET: bool = false;
    const HAS_FREE: bool = false;
    const HAS_BIND: bool = false;
    const HAS_UNBIND: bool = false;
    const HAS_TRIGGER: bool = false;

    /// Required: module-specific init.
    fn init(handle: &ModuleHandle) -> Result<(), i32>;

    fn prepare(handle: &ModuleHandle, ctx: StreamCtx) -> Result<(), i32> {
        Err(err::ENOSYS)
    }
    fn is_ready_to_process(handle: &ModuleHandle, ctx: StreamCtx) -> bool {
        true
    }
    fn process(handle: &ModuleHandle, ctx: StreamCtx) -> Result<(), i32> {
        Err(err::ENOSYS)
    }
    /// Deprecated: `process_audio_stream` variant. Only override if
    /// your module needs the legacy `input_stream_buffer` /
    /// `output_stream_buffer` layout where each buffer carries an
    /// `audio_stream *`.
    fn process_audio_stream(
        handle: &ModuleHandle,
        ctx: LegacyStreamCtx,
    ) -> Result<(), i32> {
        Err(err::ENOSYS)
    }
    /// Deprecated: `process_raw_data` variant. Only override if your
    /// module needs the legacy raw-data layout where each buffer's
    /// `data` field points at a flat raw audio buffer.
    fn process_raw_data(
        handle: &ModuleHandle,
        ctx: LegacyStreamCtx,
    ) -> Result<(), i32> {
        Err(err::ENOSYS)
    }
    fn set_config_param(handle: &ModuleHandle, param_id_data: u32) -> Result<(), i32> {
        Err(err::ENOSYS)
    }
    fn get_config_param(handle: &ModuleHandle, param_id_data: &mut u32) -> Result<(), i32> {
        Err(err::ENOSYS)
    }
    /// Multi-fragment configuration blob set. Called once per
    /// fragment; the implementor is expected to accumulate fragments
    /// until `pos == Last` or `pos == Single`, then act on the full
    /// blob. The host driver guarantees `pos`/`data_offset_size`
    /// semantics described in `interface.h`.
    fn set_configuration(
        handle: &ModuleHandle,
        frag: ConfigFragmentIn<'_>,
    ) -> Result<(), i32> {
        Err(err::ENOSYS)
    }
    /// Multi-fragment configuration blob get. Called once per
    /// fragment; the implementor writes up to `frag.fragment.len()`
    /// bytes and updates `*frag.data_offset_size` to report the
    /// total size of the configuration.
    fn get_configuration(
        handle: &ModuleHandle,
        frag: ConfigFragmentOut<'_>,
    ) -> Result<(), i32> {
        Err(err::ENOSYS)
    }
    /// Switch the module between `Normal` and `Bypass` processing
    /// modes. Default returns ENOSYS so the C side leaves the slot
    /// null and the module adapter applies its own fallback.
    fn set_processing_mode(
        handle: &ModuleHandle,
        mode: ModuleProcessingMode,
    ) -> Result<(), i32> {
        Err(err::ENOSYS)
    }
    /// Report the currently-active processing mode. Default returns
    /// `Normal` for modules that don't track the bit themselves.
    fn get_processing_mode(handle: &ModuleHandle) -> ModuleProcessingMode {
        ModuleProcessingMode::Normal
    }
    fn reset(handle: &ModuleHandle) -> Result<(), i32> {
        Err(err::ENOSYS)
    }
    fn free(handle: &ModuleHandle) -> Result<(), i32> {
        Err(err::ENOSYS)
    }
    /// Called when this module is bound to a peer. `data` describes
    /// which side is being connected (source or sink) and carries the
    /// associated [`audio::Source`] or [`audio::Sink`] pointer.
    fn bind(handle: &ModuleHandle, data: &BindData<'_>) -> Result<(), i32> {
        Err(err::ENOSYS)
    }
    /// Called when this module is unbound from a peer.
    fn unbind(handle: &ModuleHandle, data: &BindData<'_>) -> Result<(), i32> {
        Err(err::ENOSYS)
    }
    fn trigger(handle: &ModuleHandle, cmd: i32) -> Result<(), i32> {
        Err(err::ENOSYS)
    }
}

// Helper used by the macro. Converts `Result<(), i32>` to the `int` the C
// caller expects: 0 on success, negative errno on failure.
#[doc(hidden)]
#[inline]
pub fn to_c_int(r: Result<(), i32>) -> i32 {
    match r {
        Ok(()) => 0,
        Err(e) => e,
    }
}

// -------------------------------------------------------------------------
// Macro: emit C-ABI shims and the static `ModuleInterface`.
// -------------------------------------------------------------------------

/// Emit the C-ABI shims and a `#[no_mangle] pub static <SYMBOL>:
/// ModuleInterface` for the type implementing [`ProcessingModule`].
///
/// Usage:
///
/// ```ignore
/// pub struct MyMod;
/// impl ProcessingModule for MyMod {
///     const HAS_PROCESS: bool = true;
///     fn init(_handle: &ModuleHandle) -> Result<(), i32> { Ok(()) }
///     fn process(_handle: &ModuleHandle, _c: StreamCtx) -> Result<(), i32> { Ok(()) }
/// }
///
/// sof_module::define_module!(MyMod, my_module_interface);
/// ```
///
/// The C side then references it via:
///
/// ```c
/// extern const struct module_interface my_module_interface;
/// ```
#[macro_export]
macro_rules! define_module {
    ($ty:ty, $sym:ident) => {
        const _: () = {
            use $crate::*;
            use core::ffi::c_void as _c_void;

            unsafe extern "C" fn _init(m: *mut ProcessingModuleHandle) -> i32 {
                let __sof_prev = $crate::alloc::enter(m);
                let __handle = $crate::ModuleHandle::__from_raw(m);
                let __sof_ret = to_c_int(<$ty as ProcessingModule>::init(&__handle));
                $crate::alloc::leave(__sof_prev);
                __sof_ret
            }
            unsafe extern "C" fn _prepare(
                m: *mut ProcessingModuleHandle,
                sources: *mut *mut SofSource, n_src: i32,
                sinks: *mut *mut SofSink, n_snk: i32,
            ) -> i32 {
                let __sof_prev = $crate::alloc::enter(m);
                let __handle = $crate::ModuleHandle::__from_raw(m);
                let ctx = StreamCtx {
                    sources: core::slice::from_raw_parts_mut(sources, n_src.max(0) as usize),
                    sinks:   core::slice::from_raw_parts_mut(sinks,   n_snk.max(0) as usize),
                };
                let __sof_ret = to_c_int(<$ty as ProcessingModule>::prepare(&__handle, ctx));
                $crate::alloc::leave(__sof_prev);
                __sof_ret
            }
            unsafe extern "C" fn _is_ready(
                m: *mut ProcessingModuleHandle,
                sources: *mut *mut SofSource, n_src: i32,
                sinks: *mut *mut SofSink, n_snk: i32,
            ) -> bool {
                let __sof_prev = $crate::alloc::enter(m);
                let __handle = $crate::ModuleHandle::__from_raw(m);
                let ctx = StreamCtx {
                    sources: core::slice::from_raw_parts_mut(sources, n_src.max(0) as usize),
                    sinks:   core::slice::from_raw_parts_mut(sinks,   n_snk.max(0) as usize),
                };
                let __sof_ret = <$ty as ProcessingModule>::is_ready_to_process(&__handle, ctx);
                $crate::alloc::leave(__sof_prev);
                __sof_ret
            }
            unsafe extern "C" fn _process(
                m: *mut ProcessingModuleHandle,
                sources: *mut *mut SofSource, n_src: i32,
                sinks: *mut *mut SofSink, n_snk: i32,
            ) -> i32 {
                let __sof_prev = $crate::alloc::enter(m);
                let __handle = $crate::ModuleHandle::__from_raw(m);
                let ctx = StreamCtx {
                    sources: core::slice::from_raw_parts_mut(sources, n_src.max(0) as usize),
                    sinks:   core::slice::from_raw_parts_mut(sinks,   n_snk.max(0) as usize),
                };
                let __sof_ret = to_c_int(<$ty as ProcessingModule>::process(&__handle, ctx));
                $crate::alloc::leave(__sof_prev);
                __sof_ret
            }
            unsafe extern "C" fn _set_cfg_param(m: *mut ProcessingModuleHandle, p: u32) -> i32 {
                let __sof_prev = $crate::alloc::enter(m);
                let __handle = $crate::ModuleHandle::__from_raw(m);
                let __sof_ret = to_c_int(<$ty as ProcessingModule>::set_config_param(&__handle, p));
                $crate::alloc::leave(__sof_prev);
                __sof_ret
            }
            unsafe extern "C" fn _get_cfg_param(m: *mut ProcessingModuleHandle, p: *mut u32) -> i32 {
                if p.is_null() { return err::EINVAL; }
                let __sof_prev = $crate::alloc::enter(m);
                let __handle = $crate::ModuleHandle::__from_raw(m);
                let __sof_ret = to_c_int(<$ty as ProcessingModule>::get_config_param(&__handle, &mut *p));
                $crate::alloc::leave(__sof_prev);
                __sof_ret
            }
            unsafe extern "C" fn _set_configuration(
                m: *mut ProcessingModuleHandle,
                config_id: u32,
                pos: ModuleCfgFragmentPosition,
                data_offset_size: u32,
                fragment: *const u8,
                fragment_size: usize,
                response: *mut u8,
                response_size: usize,
            ) -> i32 {
                let __sof_prev = $crate::alloc::enter(m);
                let frag_slice = if fragment.is_null() || fragment_size == 0 {
                    &[][..]
                } else {
                    core::slice::from_raw_parts(fragment, fragment_size)
                };
                let resp_slice = if response.is_null() || response_size == 0 {
                    &mut [][..]
                } else {
                    core::slice::from_raw_parts_mut(response, response_size)
                };
                let frag = ConfigFragmentIn {
                    config_id,
                    pos,
                    data_offset_size,
                    fragment: frag_slice,
                    response: resp_slice,
                };
                let __handle = $crate::ModuleHandle::__from_raw(m);
                let __sof_ret =
                    to_c_int(<$ty as ProcessingModule>::set_configuration(&__handle, frag));
                $crate::alloc::leave(__sof_prev);
                __sof_ret
            }
            unsafe extern "C" fn _get_configuration(
                m: *mut ProcessingModuleHandle,
                config_id: u32,
                data_offset_size: *mut u32,
                fragment: *mut u8,
                fragment_size: usize,
            ) -> i32 {
                if data_offset_size.is_null() { return err::EINVAL; }
                let __sof_prev = $crate::alloc::enter(m);
                let frag_slice = if fragment.is_null() || fragment_size == 0 {
                    &mut [][..]
                } else {
                    core::slice::from_raw_parts_mut(fragment, fragment_size)
                };
                let frag = ConfigFragmentOut {
                    config_id,
                    data_offset_size: &mut *data_offset_size,
                    fragment: frag_slice,
                };
                let __handle = $crate::ModuleHandle::__from_raw(m);
                let __sof_ret =
                    to_c_int(<$ty as ProcessingModule>::get_configuration(&__handle, frag));
                $crate::alloc::leave(__sof_prev);
                __sof_ret
            }
            unsafe extern "C" fn _set_processing_mode(
                m: *mut ProcessingModuleHandle,
                mode: ModuleProcessingMode,
            ) -> i32 {
                let __sof_prev = $crate::alloc::enter(m);
                let __handle = $crate::ModuleHandle::__from_raw(m);
                let __sof_ret =
                    to_c_int(<$ty as ProcessingModule>::set_processing_mode(&__handle, mode));
                $crate::alloc::leave(__sof_prev);
                __sof_ret
            }
            unsafe extern "C" fn _get_processing_mode(
                m: *mut ProcessingModuleHandle,
            ) -> ModuleProcessingMode {
                let __sof_prev = $crate::alloc::enter(m);
                let __handle = $crate::ModuleHandle::__from_raw(m);
                let __sof_ret = <$ty as ProcessingModule>::get_processing_mode(&__handle);
                $crate::alloc::leave(__sof_prev);
                __sof_ret
            }
            unsafe extern "C" fn _process_audio_stream(
                m: *mut ProcessingModuleHandle,
                inputs: *mut InputStreamBuffer, n_in: i32,
                outputs: *mut OutputStreamBuffer, n_out: i32,
            ) -> i32 {
                let __sof_prev = $crate::alloc::enter(m);
                let __handle = $crate::ModuleHandle::__from_raw(m);
                let ctx = LegacyStreamCtx {
                    inputs:  core::slice::from_raw_parts_mut(inputs,  n_in.max(0)  as usize),
                    outputs: core::slice::from_raw_parts_mut(outputs, n_out.max(0) as usize),
                };
                let __sof_ret =
                    to_c_int(<$ty as ProcessingModule>::process_audio_stream(&__handle, ctx));
                $crate::alloc::leave(__sof_prev);
                __sof_ret
            }
            unsafe extern "C" fn _process_raw_data(
                m: *mut ProcessingModuleHandle,
                inputs: *mut InputStreamBuffer, n_in: i32,
                outputs: *mut OutputStreamBuffer, n_out: i32,
            ) -> i32 {
                let __sof_prev = $crate::alloc::enter(m);
                let __handle = $crate::ModuleHandle::__from_raw(m);
                let ctx = LegacyStreamCtx {
                    inputs:  core::slice::from_raw_parts_mut(inputs,  n_in.max(0)  as usize),
                    outputs: core::slice::from_raw_parts_mut(outputs, n_out.max(0) as usize),
                };
                let __sof_ret =
                    to_c_int(<$ty as ProcessingModule>::process_raw_data(&__handle, ctx));
                $crate::alloc::leave(__sof_prev);
                __sof_ret
            }
            unsafe extern "C" fn _reset(m: *mut ProcessingModuleHandle) -> i32 {
                let __sof_prev = $crate::alloc::enter(m);
                let __handle = $crate::ModuleHandle::__from_raw(m);
                let __sof_ret = to_c_int(<$ty as ProcessingModule>::reset(&__handle));
                $crate::alloc::leave(__sof_prev);
                __sof_ret
            }
            unsafe extern "C" fn _free(m: *mut ProcessingModuleHandle) -> i32 {
                let __sof_prev = $crate::alloc::enter(m);
                let __handle = $crate::ModuleHandle::__from_raw(m);
                let __sof_ret = to_c_int(<$ty as ProcessingModule>::free(&__handle));
                $crate::alloc::leave(__sof_prev);
                __sof_ret
            }
            unsafe extern "C" fn _bind(
                m: *mut ProcessingModuleHandle, bd: *mut BindInfo,
            ) -> i32 {
                if bd.is_null() { return err::EINVAL; }
                let __sof_prev = $crate::alloc::enter(m);
                let __handle = $crate::ModuleHandle::__from_raw(m);
                let __bind = $crate::BindData::__from_raw(bd);
                let __sof_ret = to_c_int(<$ty as ProcessingModule>::bind(&__handle, &__bind));
                $crate::alloc::leave(__sof_prev);
                __sof_ret
            }
            unsafe extern "C" fn _unbind(
                m: *mut ProcessingModuleHandle, bd: *mut BindInfo,
            ) -> i32 {
                if bd.is_null() { return err::EINVAL; }
                let __sof_prev = $crate::alloc::enter(m);
                let __handle = $crate::ModuleHandle::__from_raw(m);
                let __bind = $crate::BindData::__from_raw(bd);
                let __sof_ret = to_c_int(<$ty as ProcessingModule>::unbind(&__handle, &__bind));
                $crate::alloc::leave(__sof_prev);
                __sof_ret
            }
            unsafe extern "C" fn _trigger(m: *mut ProcessingModuleHandle, cmd: i32) -> i32 {
                let __sof_prev = $crate::alloc::enter(m);
                let __handle = $crate::ModuleHandle::__from_raw(m);
                let __sof_ret = to_c_int(<$ty as ProcessingModule>::trigger(&__handle, cmd));
                $crate::alloc::leave(__sof_prev);
                __sof_ret
            }

            // The actual public symbol the C side links against.
            //
            // This is a vtable: a struct of function pointers built
            // from a Rust trait impl. It must end up in a section
            // that ld accepts dynamic relocations into when
            // performing the `gcc -shared` link of the llext.
            //
            // Subtleties on Xtensa BFD with `-shared`:
            //
            //  * Plain `pub static` (immutable) ends up in a section
            //    with only SHF_ALLOC set, *regardless* of the
            //    `link_section` name. ld then refuses to put the
            //    R_XTENSA_32 absolute relocations against the
            //    function pointers there ("dangerous relocation:
            //    dynamic relocation in read-only section").
            //
            //  * `.data.rel.ro` is the textbook section for
            //    "constant after relocation" tables, but the
            //    Xtensa BFD linker also rejects it under `-shared`
            //    for the same reason.
            //
            // We therefore declare the symbol as `pub static mut`
            // and place it in `.data`. That gives it SHF_WRITE +
            // SHF_ALLOC, which is what ld needs in order to emit
            // dynamic relocations and produce a clean `-shared`
            // ELF. The C side declares the symbol as a plain
            // `struct module_interface` (const-ness is not
            // expressed in ELF), and the SOF llext loader treats
            // the section as effectively read-only after fixups,
            // exactly like an equivalent C `static const struct
            // module_interface` table.
            #[used]
            #[unsafe(link_section = ".data")]
            #[unsafe(no_mangle)]
            pub static mut $sym: ModuleInterface = ModuleInterface {
                init: Some(_init),
                prepare: if <$ty as ProcessingModule>::HAS_PREPARE { Some(_prepare) } else { None },
                is_ready_to_process:
                    if <$ty as ProcessingModule>::HAS_IS_READY { Some(_is_ready) } else { None },
                process: if <$ty as ProcessingModule>::HAS_PROCESS { Some(_process) } else { None },
                process_audio_stream:
                    if <$ty as ProcessingModule>::HAS_PROCESS_AUDIO_STREAM { Some(_process_audio_stream) } else { None },
                process_raw_data:
                    if <$ty as ProcessingModule>::HAS_PROCESS_RAW_DATA { Some(_process_raw_data) } else { None },
                set_config_param:
                    if <$ty as ProcessingModule>::HAS_SET_CONFIG_PARAM { Some(_set_cfg_param) } else { None },
                get_config_param:
                    if <$ty as ProcessingModule>::HAS_GET_CONFIG_PARAM { Some(_get_cfg_param) } else { None },
                set_configuration:
                    if <$ty as ProcessingModule>::HAS_SET_CONFIGURATION { Some(_set_configuration) } else { None },
                get_configuration:
                    if <$ty as ProcessingModule>::HAS_GET_CONFIGURATION { Some(_get_configuration) } else { None },
                set_processing_mode:
                    if <$ty as ProcessingModule>::HAS_SET_PROCESSING_MODE { Some(_set_processing_mode) } else { None },
                get_processing_mode:
                    if <$ty as ProcessingModule>::HAS_GET_PROCESSING_MODE { Some(_get_processing_mode) } else { None },
                reset: if <$ty as ProcessingModule>::HAS_RESET { Some(_reset) } else { None },
                free: if <$ty as ProcessingModule>::HAS_FREE { Some(_free) } else { None },
                bind: if <$ty as ProcessingModule>::HAS_BIND { Some(_bind) } else { None },
                unbind: if <$ty as ProcessingModule>::HAS_UNBIND { Some(_unbind) } else { None },
                trigger: if <$ty as ProcessingModule>::HAS_TRIGGER { Some(_trigger) } else { None },
            };
        };
    };
}
