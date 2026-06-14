# SOF / Xtensa Rust enablement — status

This file tracks the state of the work to enable Rust as a first-class
language for [Sound Open Firmware](https://github.com/thesofproject/sof)
modules and tests.

All development uses the **`clang-dev`** branch across all repositories:

| Repository | Branch | Description |
|---|---|---|
| `lgirdwood/llvm-project` | `clang-dev` | Xtensa LLVM backend with HiFi, FLIX, Intel ADSP processors |
| `lgirdwood/rust` | `clang-dev` | Rustc patches + Xtensa target specs + toolchain setup |
| `lgirdwood/sof` | `clang-dev` | SOF with Rust module support, LLVM build, ZTests |
| `lgirdwood/zephyr` | `clang-dev` | Zephyr LLVM toolchain variant + ADSP naming |

Clean per-feature upstream submission branches also exist:
- **LLVM**: `upstream/dev` — 12-patch series
- **Rust**: `upstream/patches` — 3-patch series
- **SOF**: `upstream/dev` — 7-patch series

The companion document on the SOF side is
[`sof/doc/rust.md`](https://github.com/thesofproject/sof/blob/main/doc/rust.md)
in the SOF tree (path may vary while the branch is in review).

---

## Working today

### Toolchain

- `rustc` and `cargo` build cleanly against the
  [`lgirdwood/llvm-project@clang-dev`](https://github.com/lgirdwood/llvm-project)
  Xtensa LLVM (LLVM 23.0.0git, X86 + Xtensa targets).
  - The `compiler/rustc_llvm/llvm-wrapper/PassWrapper.cpp` patch on
    this branch papers over the version mismatch where the fork is
    tagged 23 but still returns `MCSubtargetInfo*` instead of `&`.
- `bootstrap.toml` setup:
  ```toml
  [llvm]
  download-ci-llvm = false

  [target.x86_64-unknown-linux-gnu]
  llvm-config = "/path/to/llvm-project/build/bin/llvm-config"
  ```
- `./x build library` and `./x build cargo` produce a usable stage2
  toolchain at `build/host/stage2/`.
- Toolchain is registered with rustup (after symlinking
  `stage2-tools-bin/cargo` into `stage2/bin/`) as `xtensa-llvm`:
  ```bash
  rustup toolchain link xtensa-llvm build/host/stage2
  ```

### Targets

- **Processor naming convention:** All Intel ADSP processors now use
  a standardized `intel_ace<VER>_adsp` naming scheme in the LLVM
  backend, Rust target specs, and SOF build scripts. The mapping is:
  - `intel_ace15_adsp` — ACE 1.5 (Meteor Lake) and ACE 2.0 (Lunar Lake)
  - `intel_ace30_adsp` — ACE 3.0 (Panther Lake)
  - `intel_ace40_adsp` — ACE 4.0 (NVL)
  Legacy names (`intel_ace30_ptl`, `intel_ace15_mtpm`) are no longer
  used anywhere in the toolchain.
- Custom JSON target spec for **Intel ACE 3.0 PTL** lives at
  [`xtensa-intel_ace30_adsp-zephyr-elf.json`](xtensa-intel_ace30_adsp-zephyr-elf.json)
  at the repo root. It compiles `core` and `alloc` from source via
  `-Zbuild-std` against the Xtensa LLVM backend.
- Custom JSON target spec for **Intel ACE 1.5 MTPM** (Meteor Lake)
  lives at
  [`xtensa-intel_ace15_adsp-zephyr-elf.json`](xtensa-intel_ace15_adsp-zephyr-elf.json).
  Mirrors the PTL spec but selects `cpu = "intel_ace15_adsp"` and
  the matching Zephyr SDK linker
  (`xtensa-intel_ace15_mtpm_zephyr-elf-gcc`). Pair with
  `./sof/scripts/xtensa-build-zephyr.py -p mtl` and
  `SOF_RUST_XTENSA_TARGET=$PWD/xtensa-intel_ace15_adsp-zephyr-elf.json`.
  Verified end-to-end against the SOF firmware ZTest image; build
  ends at `west sign`, no `dangerous relocation` / `l32r` warnings.
  - **Also covers Intel ACE 2.0 LNL (Lunar Lake).** ACE 2.0 LNL
    shares the same Xtensa core, ISA, and Zephyr SDK toolchain
    as ACE 1.5 MTPM (see Zephyr's
    `soc/intel/intel_adsp/ace/Kconfig.soc`:
    `SOC_TOOLCHAIN_NAME default "intel_ace15_adsp" if SOC_ACE20_LNL`).
    Only SoC peripherals differ (interrupt routing, IPC regs,
    power, DMIC layout). Build with
    `./sof/scripts/xtensa-build-zephyr.py -p lnl` against the same
    `xtensa-intel_ace15_adsp-zephyr-elf.json` spec; verified clean
    on the ZTest image.
- Existing in-tree Xtensa targets (`xtensa-esp32-none-elf`,
  `xtensa-esp32s2-none-elf`, `xtensa-esp32s3-none-elf` and their
  `espidf` variants) inherited from upstream rustc continue to work
  with this LLVM.
- Linker = Zephyr SDK's `xtensa-intel_ace30_ptl_zephyr-elf-gcc` /
  `xtensa-intel_ace15_mtpm_zephyr-elf-gcc` (binary releases, on
  `PATH`). Note: while the internal LLVM CPU names use `_adsp`,
  the Zephyr SDK directory names remain platform-specific (`ptl`,
  `mtpm`) for compatibility with existing SDK installations.

### SOF integration

- Reusable CMake helper `sof_rust_staticlib()` lives in
  `sof/scripts/cmake/rust.cmake` (in the SOF repo).
- **`sof_module` crate** at `sof/rust/sof_module/` wraps SOF's
  processing-module client API: a `#[repr(C)]` mirror of
  `struct module_interface`, a safe `ProcessingModule` trait, and a
  `define_module!()` macro that emits the C-ABI shims and the
  `#[no_mangle] static …: ModuleInterface` symbol the C side links
  against. Authors opt into optional ops via
  `const HAS_PROCESS: bool = true` etc.; un-opted slots stay `None`
  (= `NULL`), matching SOF's "not provided" convention.
- **`sof_rust_llext_module()` CMake helper** wraps the Rust
  cross-build + `sof_llext_build()` invocation: cargo runs against
  the Xtensa JSON target with `-Zbuild-std`, the resulting archive
  is fed to the llext link via `LIBS` / `LIBS_PATH`, and the
  build-edge `add_dependencies(<mod>_llext_lib <mod>_rust_build)` is
  installed automatically. The C glue file only carries the SOF
  manifest (`SOF_DEFINE_REG_UUID`, `LOG_MODULE_REGISTER`,
  `__section(".module")` `sof_man_module_manifest`) and an `extern
  const struct module_interface my_iface;` declaration; the vtable
  itself is the `#[no_mangle] static` emitted on the Rust side.
- **Reference Rust llext module** at `sof/src/audio/rust_template/`
  (`CONFIG_COMP_RUST_TEMPLATE=m`): pure-Rust pass-through component
  using the `sof_module` crate. Mirrors `sof/src/audio/template/`
  layout (`Kconfig`, `CMakeLists.txt`, C glue, `.toml`, `llext/`).
- **`rust_template` builds as a real llext** for `intel_ace30_adsp`
  via `sof_rust_llext_module()`. The resulting `rust_template.llext`
  (~13.5 kB) carries the expected sections (`.module`,
  `.mod_buildinfo`, UUID, `log_const`, `.data`, `.got`, `.rela.dyn`)
  and is loadable by the SOF llext loader. The vtable lives in
  `.data` (not `.rodata`) because Xtensa BFD `-shared` rejects
  dynamic relocs into A-only sections; the `define_module!()` macro
  emits the static accordingly.
- **Audio I/O from Rust** is wired up through safe wrappers. The
  `sof_module::audio` module exposes lifetime-checked `Source<'a>`
  / `Sink<'a>` borrow types around `*mut sof_source` / `*mut
  sof_sink`, with method accessors for the common queries
  (`channels()`, `available()`, `free_size()`, `frame_fmt()`). The
  raw `source_get_data*` / `sink_get_buffer*` / `source_release_data`
  / `sink_commit_buffer` FFI is still re-exported as an escape
  hatch, but the public surface a typical `process()` reaches for
  is now entirely safe Rust:
  * `StreamCtx::primary_pair()` hands back `(Source, Sink)`.
  * `audio::passthrough(src, snk)` wraps SOF's
    `source_to_sink_copy()` for the bypass / no-op path.
  * `audio::process_stereo_s16(src, snk, |&[i16; 2], &mut [i16;
    2]| ...)` and `audio::process_stereo_s32(...)` take a
    per-frame closure and absorb the entire `source_get_data` /
    sink_get_buffer / circular-wrap / release-commit dance. The
    closure receives fixed-length array references, so the
    compiler proves bounds at codegen time and never emits
    `core::panicking::*` paths — important on Xtensa where literal
    pools are tight. A stereo DSP module can now be written
    without `unsafe` anywhere on the hot path.
  Because the C-side helpers are `static inline`, an out-of-line
  shim (`sof/rust/sof_module/c_shim/sof_rust_source_sink.c`)
  re-exports them as global `sof_rust_*` symbols that the Rust
  crate links against.
- **`sof_module::ipc4_control`** carries the safe parsers for IPC4
  kcontrol payloads delivered through `set_configuration`:
  `SWITCH_CONTROL_PARAM_ID` / `ENUM_CONTROL_PARAM_ID` /
  `BYTES_CONTROL_PARAM_ID` constants and a `parse_switch(&[u8])`
  that returns a `SwitchPayload<'_>` with iter over (channel,
  value) pairs. Byte-granular loads, no assumption about the C
  struct's `__packed` alignment, `checked_mul` / `checked_add` on
  `num_elems` so an oversized count returns `EINVAL` instead of
  reading out of bounds.
- **`sof_module::large_config`** (feature-gated behind `alloc`)
  provides `LargeConfigAccumulator` (handles `Single` / `First` /
  `Middle` / `Last` fragment positions with up-front
  `try_reserve_exact` so `Middle` / `Last` can't fail with ENOMEM
  mid-blob) and `emit_config_fragment(blob, frag)` for the
  get-side. Off by default so modules that don't need a heap
  (and therefore don't install a `#[global_allocator]`) still
  build.
- **`rust_template` mirrors the C `template/` component end to end.**
  The Rust crate implements `set_configuration` on top of
  `ipc4_control::parse_switch`, drives a `SWAP_ENABLE: AtomicBool`,
  and dispatches in `process()` on `src.frame_fmt()` so `S16_LE`
  goes through `swap_lr_s16`, `S32_LE` / `S24_4LE` through
  `swap_lr_s32`, and any other format falls back to
  `audio::passthrough`. With the new `audio::process_stereo_*`
  closure helpers each swap routine is now a 3-line safe-Rust
  wrapper around a per-frame `s_out[0] = s_in[1]; s_out[1] =
  s_in[0];` body — no FFI, no `unsafe`, no manual circular-buffer
  walking. The linear-buffer test cores (`swap_lr_s16_buf` /
  `swap_lr_s32_buf`) plus the C ZTest entry points still live
  inside `rust_template`.
- **Zephyr LOG bindings.** `LOG_MODULE_REGISTER` is a macro tied at
  compile time to a per-module string table, so a tiny C shim
  (`sof/src/audio/rust_template/rust_template_log.c`) registers the
  module and exports `sof_rust_template_log_{err,warn,info,dbg}`
  trampolines. The Rust side wraps them in a `log` module that
  takes `&CStr` (typically `c"..."` literals — no allocator, no
  copy, NUL termination guaranteed by the type). The shim is built
  into both the llext (`rust_template/llext/CMakeLists.txt`) and
  the firmware ZTest (`zephyr/test/CMakeLists.txt`).
- **Panic handler hooks into Zephyr's fatal-error path.** A second
  entry point in the same shim, `sof_rust_template_panic(const
  char *)`, does `LOG_ERR("%s", msg)` then `k_panic()` (declared
  noreturn). The Rust `#[panic_handler]` builds
  `"rust_template panic: <reason> at <file>:<line>"` into a
  192-byte on-stack buffer via a `core::fmt::Write` adapter that
  silently truncates, then hands it off. No heap involvement, so
  the handler is valid even when the allocator itself is panicking.
  With `CONFIG_LOG_MODE_IMMEDIATE=y` the message is on the wire
  before the CPU goes down.
- Reference ztests built into the SOF firmware ZTest image
  (`xtensa-build-zephyr.py … -o sof/app/ztest_overlay.conf`):
  - `CONFIG_RUST_HELLO_ZTEST` — Rust staticlib called from C
    (`sof_rust_hello_suite`, 2 cases).
  - `CONFIG_RUST_TEMPLATE_ZTEST` — drives `rust_template_interface`
    function pointers and exercises `swap_lr_s16` on real buffers
    (`sof_rust_template_suite`, 9 cases: interface populated,
    init/process/reset/free, swap_lr_s16 basic / odd-tail /
    null-safe / 64-frame block-copy with sentinel guards).
  All build cleanly into `zephyr.elf` for `intel_ace30_adsp` via
  the standard build line:
  ```bash
  unset ZEPHYR_SDK_INSTALL_DIR && \
      ./sof/scripts/xtensa-build-zephyr.py \
          --llvm-clang ~/work/llvm-project/build \
          --build-dir-suffix="-llvm-qemu" -p ptl \
          -C="-DCONFIG_LOG_MODE_IMMEDIATE=y" \
          -C="-DCONFIG_ZTEST=y" \
          -C="-DCONFIG_USERSPACE=n" \
          -o sof/app/ztest_overlay.conf
  ```
- Host-side ztests (`sof/test/ztest/unit/rust_hello/`,
  `sof/test/ztest/unit/rust_module_iface/`) continue to pass on
  `native_sim/native/64` (10 cases total).
- **`#[global_allocator]` bound to SOF's per-module allocator.**
  `sof_module::alloc::SofModuleAlloc` implements `GlobalAlloc` by
  routing through `mod_alloc` / `mod_alloc_align` / `mod_free` (the
  SOF per-module heap). The currently-active `processing_module *`
  lives in a C-side slot (`sof_rust_current_module` in
  `sof/rust/sof_module/c_shim/sof_rust_mod_alloc.c`) and is
  published on entry / cleared on exit of every callback emitted by
  `define_module!()`, so heap allocations from Rust module code are
  automatically charged to the right module instance and reclaimed
  on unload via `mod_free_all`. Leaf modules opt in with
  `sof_module::install_global_allocator!();` which expands to the
  required `#[global_allocator]` static. Storage is kept on the C
  side and accessed via `extern static mut` from Rust to keep each
  shim down to a single literal reference — needed to stay under
  Xtensa BFD's `l32r` ±256 KiB cross-section range on the static
  firmware link path (see §Xtensa LLVM literal-section limitation).
  Single-DSP-core only for now; SMP needs a per-CPU slot.
- End-to-end developer documentation written
  (`sof/doc/rust.md` in the SOF tree, with dedicated sections on the
  `sof_module` crate and the `sof_rust_llext_module()` helper).

---

## Not done yet

### Toolchain / upstream

- **Upstream the LLVM-23 wrapper fix properly.** The current patch
  unconditionally uses the pre-23 pointer form, which is correct for
  the local LLVM fork but will regress as soon as the fork rebases
  onto an upstream LLVM that includes the
  `getMCSubtargetInfo() -> const &` change. Either:
  1. Cherry-pick that LLVM commit into `lgirdwood/llvm-project` and
     revert our PassWrapper patch, or
  2. Switch the gate in PassWrapper from `LLVM_VERSION_GE(23, 0)` to a
     CMake feature check (e.g. probe with `try_compile`), so it works
     against either signature.
- **Promote the Intel ACE Xtensa target to a built-in `rustc`
  target.** Right now it is a `.json` spec which forces every consumer
  to use `-Zjson-target-spec` and `-Zbuild-std`. A real
  `compiler/rustc_target/src/spec/targets/xtensa_intel_ace30_adsp_zephyr_elf.rs`
  (mirroring the existing ESP32 entries) would let people use a stable
  triple `--target xtensa-intel_ace30_adsp-zephyr-elf`. Same for ACE40
  once it has a Zephyr SDK toolchain.
- **Upstream candidate**: once the dust settles, the new target spec
  is suitable for a tier-3 PR to `rust-lang/rust`. Needs an MCP and a
  target maintainer commitment.

### Target / ABI

- **ABI feature flags are empty** in the JSON spec. Real Zephyr SDK
  builds for `intel_ace30_adsp` enable HiFi / FLIX / density / etc. If
  the Rust archive disagrees with Zephyr objects it will either fail
  to link (`Tag_*` ABI mismatch) or produce wrong code. The right list
  needs to be confirmed against the SDK build config and added under
  `"features"`.
- **Atomics**: ACE 3.0 PTL has `s32c1i` so 32-bit CAS works; that's
  set. Need to revisit if any consumer wants 64-bit atomics or
  `forced-atomics`.
- **Pre-link args** in the target spec are minimal (`-nostartfiles`).
  Whether the SOF llext path needs anything else (e.g. `-mlongcalls`)
  needs to be validated once a real Rust llext module is built.

### Runtime

- **Panic handler now routes through Zephyr.** The example
  `rust_template` `#[panic_handler]` formats `<reason> at
  <file>:<line>` into a stack buffer and calls
  `sof_rust_template_panic()` -> `LOG_ERR` + `k_panic()`. The
  pattern (per-module C shim + `extern "C" fn(...) -> !`) is the
  blueprint for other Rust modules; `sof_module` itself does not
  install a panic handler because the `LOG_MODULE_REGISTER` it
  would need is module-name specific.
- In-tree Rust audio code still avoids emitting `core::panicking::*`
  paths where it can (raw-pointer loops, constant divisors, no
  `unwrap()` on hot paths). With the LLVM literal-pool alignment
  fix below this is now a footprint concern rather than a
  correctness one.
- **`compiler-builtins-mem` works for the llext path.** Zephyr's
  libc supplies `memcpy`/`memset`/`memmove` so the static-link
  firmware path doesn't need them from Rust at all; for that path
  we strip every `*compiler_builtins*` member out of the staticlib
  via `STRIP_COMPILER_BUILTINS` (see workaround below). The llext
  path keeps `compiler-builtins-mem` and links cleanly because the
  loader resolves `memcpy` etc. against the firmware exports.

### SOF firmware path

- **`sof_module` coverage is now full for `struct module_interface`.**
  In addition to init / prepare / is_ready_to_process / process /
  set/get_config_param / reset / free / trigger, the trait now
  covers bind / unbind (opaque `struct bind_info *` passthrough),
  set_configuration / get_configuration (multi-fragment blobs via
  `ConfigFragmentIn` / `ConfigFragmentOut`), set_processing_mode /
  get_processing_mode (typed `ModuleProcessingMode` enum), and the
  deprecated process_audio_stream / process_raw_data variants
  (`LegacyStreamCtx`). Authors still only opt in to what they need
  via `HAS_*` associated consts; un-opted slots stay `None`.
- **`sof_module::audio` is now generic stream plumbing only.** It
  ships `Source<'a>` / `Sink<'a>` borrow wrappers, the FFI
  re-exports, `audio::passthrough(src, snk)` on top of SOF's
  `source_to_sink_copy()`, and the closure-based
  `audio::process_stereo_s16` / `audio::process_stereo_s32`
  per-frame processors. All format-specific DSP code
  (`swap_lr_s16` / `swap_lr_s32` etc.) lives in the client module
  that uses it, matching the C side where `template-generic.c`
  keeps that code module-local rather than in the module-adapter
  core. The packed-s24 (`S24_3LE`) variant is currently absent;
  add it module-local on demand.
- **Loader-side llext run on hardware not yet exercised.** The llext
  builds and the firmware ZTest image runs the host-side suites; what
  remains is loading `rust_template.llext` into a running PTL image
  via the SOF kmod loader and confirming `process()` actually swaps
  L/R on a live pipeline.
- **CI**: Twister job for the Rust ztests is not in any SOF CI
  workflow yet. Needs a CI image with `cargo` + the locally-built
  `xtensa-llvm` toolchain (or a stable rustup toolchain for the
  host-only path).

### Xtensa LLVM literal-section limitation (firmware static-link path)

#### Per-function constant pool alignment (FIXED)

For a while a Rust function with several `c"..."` literals (e.g.
the `_set_configuration` shim emitted by `define_module!()`,
which does a handful of `log::*(c"...")` calls) would link with

```
dangerous relocation: l32r: misaligned literal target:
  (.text.<func>+0xN)
```

Root cause was in `XtensaAsmPrinter::emitConstantPool()`:
`startLiteralSection()` only set 4-byte alignment metadata on a
`.literal` subsection that ELF never actually switched to, so the
pool entries were emitted into the function's own `.text.<func>`
subsection, packed flush against the last instruction at whatever
offset the code happened to end at. Fixed locally in
`lgirdwood/llvm-project@clang-dev` (originally commit `129e60e5bf0c`; one
`emitValueToAlignment(Align(4))` before the CPE loop). Upstreamable
verbatim.

#### Remaining: cross-section `l32r` range and `compiler_builtins`

The Xtensa LLVM backend always emits literals into a separate
`<section>.literal` section
(`XtensaTargetStreamer::getLiteralSectionName`). When `compiler_builtins`
functions in different `.text.*` sections reference each other via
`l32r`, GNU BFD rejects the relocation as
`dangerous relocation: l32r: literal target out of range / misaligned`.
The `-shared` llext link path is unaffected (relocs are deferred to the
loader); only the static firmware link is hit.

**Workaround.** `sof_rust_staticlib(... STRIP_COMPILER_BUILTINS)`
runs `ar d` against the staticlib post-build to drop every
`*compiler_builtins*` member. Safe for crates that don't use
soft-float / 64-bit shift helpers — Zephyr's libc supplies the
`mem*()` functions. In addition, in-tree Rust audio code is written
to avoid emitting any `core::panicking::*` paths (which themselves
trip cross-section `l32r`): raw pointers instead of bounds-checked
slice indexing, constant divisors, no `unwrap()`. A proper fix needs
either (a) Xtensa LLVM emitting literals inline next to each
`.text.<sym>` section, or (b) the linker doing literal section
coalescing the way `xt-ld` does.

### Zephyr SDK pin

The LLVM toolchain wrapper for Zephyr (`zephyr/cmake/toolchain/llvm/
target.cmake`) globs `${SDK}/gnu/<target>/lib/gcc/<target>/*/include`
to find `string.h`. Zephyr SDK 0.17.4 has no `gnu/` subdirectory,
so the glob returns empty and a bare `-isystem` flag ends up in the
clang command line, swallowing the next argument and producing
`fatal error: 'string.h' file not found`. Use **Zephyr SDK 1.0.1
or newer**, and **unset `ZEPHYR_SDK_INSTALL_DIR`** so
`xtensa-build-zephyr.py` auto-picks the working SDK.

### Host stdlib wipe from cross-target `./x build`

Running `./x build --stage 2 library --target /path/to/spec.json`
**replaces** the contents of
`build/host/stage2/lib/rustlib/x86_64-unknown-linux-gnu/lib/` with
only the cross-target rlibs \u2014 the host stdlib gets wiped (file
count drops to 0). Subsequent SOF builds then fail in the
`compiler_builtins` host-side build script with
`error[E0463]: can't find crate for 'std'`. Recovery:

```
unset RUSTC RUSTFLAGS_BOOTSTRAP CARGO_BUILD_TARGET_DIR
cd ~/work/rust && ./x build --stage 2 library      # NO --target
cp ~/work/rust/build/x86_64-unknown-linux-gnu/stage2-tools-bin/cargo \
   ~/work/rust/build/host/stage2/bin/              # cargo workaround (always reapply)
```

Note: leaking `RUSTFLAGS_BOOTSTRAP=-Zjson-target-spec` (or
`RUSTC=...`) into a `./x build` invocation causes stage0 cargo to
reject the unstable flag with `unknown unstable option:
'json-target-spec'`. Always `unset` those before running `./x`.

### LLVM patches carried locally

All patches are now on `lgirdwood/llvm-project@clang-dev` (also
available as a clean 12-patch series on `upstream/dev`):

- `[Xtensa] Add intel_ace15_adsp processor (Intel ACE 1.5 / Meteor
  Lake)` — new `def : Proc<"intel_ace15_adsp", ...>` in
  `XtensaProcessors.td`. Feature deltas vs `intel_ace30_adsp`,
  derived from `core-isa.h` at
  `modules/hal/xtensa/zephyr/soc/intel_ace15_mtpm/`:
  `HighPriInterruptsLevel4` (vs Level5), `Timers1` (vs Timers2),
  `RegionProtection` (`MIMIC_CACHEATTR=1` vs PTP MMU), and no
  `MiscSR`. Same ISA core (Density, Windowed, Boolean, Loop, SEXT,
  NSA, Mul16/32/32High, Div32, S32C1I, THREADPTR, HIFI4, etc).
  Upstreamable. Verified with `llc -march=xtensa -mattr=help` and
  with a full SOF mtl firmware build.
- `[Xtensa] Standardize Intel ADSP processor naming convention` —
  renames all Intel processors from legacy platform-specific suffixes
  (`intel_ace30_ptl`, `intel_ace15_mtpm`) to the standardized
  `intel_ace<VER>_adsp` scheme. Updates `XtensaProcessors.td` and
  `Xtensa.h`. Coordinated with matching changes in Rust target
  specs, SOF build scripts, and Zephyr CMake.
- `[Xtensa] Remove FeatureSingleFloat from Intel ACE30/ACE40
  processors` — corrects feature flag sets.
- `[Xtensa] Align per-function constant pool to 4 bytes` —
  see §"Per-function constant pool alignment" above. Upstreamable.
- `Xtensa: expand AE_ZERO32/P48/Q56 pseudos in AsmPrinter` — fixes
  `rustc-LLVM ERROR: Unsupported instruction : <MCInst 563 ...>` that
  fired the first time isel produced an `int_xtensa_ae_zero32` node
  (e.g. when `XtensaISelLowering` lowers a `v2i32` zero BUILD_VECTOR,
  which happens organically from `compiler_builtins` on
  `intel_ace30_adsp`). The fix expands the missing AE_ZERO* pseudos
  to the existing encoded `AE_SUB32S_HIFI3` / `AE_SUB64_HIFI3`
  instructions, both standalone and inside FLIX bundles. Should be
  upstreamable verbatim. AE_ZERO16 / AE_ZERO24 still need encoded
  `AE_SUB16S_HIFI3` / `AE_SUB24S_HIFI3` instruction definitions in
  `XtensaHIFIInstrInfo.td` before they can be expanded the same way.
- HiFi intrinsic definitions, CodeGen lowering, ISel, MC layer,
  calling convention, FLIX VLIW packetizer — full HiFi3/4/5 audio
  engine support.

### Tooling / DX

- **`cargo +xtensa-llvm` workflow is now scripted.** Run
  `src/etc/sof-setup-toolchain.sh` from the rust source root
  (`lgirdwood/rust@clang-dev`) to:
  1. verify the local Xtensa LLVM build (`--llvm-build` or
     `$LLVM_BUILD`, default `~/work/llvm-project/build`),
  2. write `[llvm]` + `[target.<host>]` into `bootstrap.toml` if not
     already present,
  3. `./x build library cargo` (skipped if stage2 is already built;
     pass `--rebuild` to force),
  4. symlink `build/host/stage2-tools-bin/cargo` into
     `build/host/stage2/bin/`,
  5. `rustup toolchain link xtensa-llvm build/host/stage2`
     (re-linked if already pointing somewhere else, or with
     `--force`),
  6. smoke-test with `rustc +xtensa-llvm --version`,
     `cargo +xtensa-llvm --version`, and a `--print target-spec-json`
     parse of the JSON target.
  Idempotent on repeat runs. Pass `--print-env` to emit the
  `SOF_RUST_TOOLCHAIN` / `SOF_RUST_XTENSA_TARGET` exports for shell
  rc files.
- **No `rust-analyzer`** built or shipped for the toolchain. Editors
  will still work against `xtensa-llvm` rustc + a stable `rust-analyzer`
  from rustup, but cross-target completion needs `--target` set in
  the project's `rust-analyzer.toml` / `.cargo/config.toml`.
- **No `clippy` / `rustfmt` built.** Same as above; not blockers but
  should be added to the `./x build` set we install.

### Documentation

- The "Adding a new Rust ztest" section of `sof/doc/rust.md` should
  be promoted to a `cargo generate` template (or a `west` extension)
  so people don't hand-copy directories.
- The patch list in §4.2 of `sof/doc/rust.md` should be auto-generated
  from `git log rust-lang/main..rust-dev` rather than hand-curated.

---

## Building from source — step by step

### Prerequisites

```bash
# System packages (Ubuntu/Debian)
sudo apt install build-essential cmake ninja-build python3 python3-pip \
  python3-venv git curl lld

# Zephyr SDK 1.0.1+ — download from https://github.com/zephyrproject-rtos/sdk-ng/releases
# Install to ~/zephyr-sdk-1.0.1 (default location for auto-detection)
wget https://github.com/zephyrproject-rtos/sdk-ng/releases/download/v1.0.1/zephyr-sdk-1.0.1_linux-x86_64.tar.xz
tar xf zephyr-sdk-1.0.1_linux-x86_64.tar.xz -C ~/
~/zephyr-sdk-1.0.1/setup.sh

# Rust (standard toolchain, needed for bootstrapping)
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
```

### Step 1: Clone repositories

```bash
mkdir -p ~/work && cd ~/work

# LLVM (Xtensa backend with HiFi/FLIX)
git clone https://github.com/lgirdwood/llvm-project.git
cd llvm-project && git checkout clang-dev && cd ..

# Rust (rustc with Xtensa LLVM linkage)
git clone https://github.com/lgirdwood/rust.git
cd rust && git checkout clang-dev && cd ..

# SOF + Zephyr (west workspace)
mkdir sof3 && cd sof3
python3 -m venv .venv && source .venv/bin/activate
pip install west

git clone https://github.com/lgirdwood/sof.git
cd sof && git checkout clang-dev && cd ..
west init -l sof
west update

# Switch Zephyr to clang-dev
cd zephyr && git remote add lrg https://github.com/lgirdwood/zephyr.git
git fetch lrg && git checkout lrg/clang-dev -b clang-dev && cd ..

pip install -r zephyr/scripts/requirements.txt
pip install -r sof/scripts/requirements-build.txt
cd ~/work
```

### Step 2: Build LLVM

```bash
cd ~/work/llvm-project

cmake -G Ninja -B build -S llvm \
  -DCMAKE_BUILD_TYPE=Release \
  -DLLVM_TARGETS_TO_BUILD="host" \
  -DLLVM_EXPERIMENTAL_TARGETS_TO_BUILD="Xtensa" \
  -DLLVM_ENABLE_PROJECTS="clang;lld" \
  -DCMAKE_INSTALL_PREFIX=$PWD/install

ninja -C build
```

Key config options:

| CMake variable | Value | Required |
|---|---|---|
| `LLVM_TARGETS_TO_BUILD` | `host` | Yes — builds X86 (or AArch64) for rustc itself |
| `LLVM_EXPERIMENTAL_TARGETS_TO_BUILD` | `Xtensa` | Yes — the Xtensa backend |
| `LLVM_ENABLE_PROJECTS` | `clang;lld` | Yes — Clang frontend + LLD linker |
| `CMAKE_BUILD_TYPE` | `Release` | Recommended — Debug is very slow |

Verify: `build/bin/llc -march=xtensa -mattr=help 2>&1 | grep intel_ace30_adsp`

### Step 3: Build Rust

```bash
cd ~/work/rust

# Configure bootstrap.toml (if not already present)
cat > bootstrap.toml << 'EOF'
profile = 'dist'

[llvm]
download-ci-llvm = false

[target.x86_64-unknown-linux-gnu]
llvm-config = "/home/$USER/work/llvm-project/build/bin/llvm-config"
EOF

# Fix the llvm-config path to use your actual home directory
sed -i "s|/home/\$USER|$HOME|" bootstrap.toml

# Build stage2 compiler + cargo
./x build --stage 2 library cargo

# Symlink cargo into stage2 (needed for rustup)
ln -sf $PWD/build/host/stage2-tools-bin/cargo \
       $PWD/build/host/stage2/bin/cargo

# Register with rustup
rustup toolchain link xtensa-llvm $PWD/build/host/stage2
```

Verify:
```bash
rustc +xtensa-llvm --version
# rustc 1.89.0-dev (9ca86c218 2026-05-21)

cargo +xtensa-llvm --version
# cargo 1.89.0-nightly (...)

# Check Xtensa target support
rustc +xtensa-llvm -Z unstable-options \
  --target $PWD/xtensa-intel_ace30_adsp-zephyr-elf.json \
  --print target-spec-json 2>/dev/null | head -5
```

> **Warning:** Never run `./x build --stage 2 library --target <spec.json>`
> — it wipes the host stdlib. If this happens, rebuild with
> `./x build --stage 2 library` (no `--target`).

### Step 4: Build SOF firmware

```bash
cd ~/work/sof3
source .venv/bin/activate

# IMPORTANT: unset ZEPHYR_SDK_INSTALL_DIR so the script auto-detects SDK 1.0.1+
unset ZEPHYR_SDK_INSTALL_DIR

# Build PTL firmware with Rust modules
./sof/scripts/xtensa-build-zephyr.py \
    --llvm-clang ~/work/llvm-project/build \
    --build-dir-suffix="-llvm-qemu" \
    -p ptl
```

This produces:
- `build-ptl-llvm-qemu/sof-ptl.ri` — signed firmware image
- `build-ptl-llvm-qemu/sof/app/build-ptl-llvm-qemu/zephyr/sof-ptl/llext/rust_template.llext`

#### Build with ZTests enabled

```bash
./sof/scripts/xtensa-build-zephyr.py \
    --llvm-clang ~/work/llvm-project/build \
    --build-dir-suffix="-llvm-qemu" \
    -p ptl \
    -C="-DCONFIG_LOG_MODE_IMMEDIATE=y" \
    -C="-DCONFIG_ZTEST=y" \
    -C="-DCONFIG_USERSPACE=n" \
    -o sof/app/ztest_overlay.conf
```

#### Build for Meteor Lake (MTL) / Lunar Lake (LNL)

```bash
# MTL — uses intel_ace15_adsp target
./sof/scripts/xtensa-build-zephyr.py \
    --llvm-clang ~/work/llvm-project/build \
    --build-dir-suffix="-llvm-qemu" \
    -p mtl

# LNL — shares the same ACE 1.5 core as MTL
./sof/scripts/xtensa-build-zephyr.py \
    --llvm-clang ~/work/llvm-project/build \
    --build-dir-suffix="-llvm-qemu" \
    -p lnl
```

### Key environment variables

| Variable | Default | Purpose |
|---|---|---|
| `SOF_RUST_TOOLCHAIN` | `xtensa-llvm` | rustup toolchain used by `sof_rust_staticlib()` |
| `SOF_RUST_XTENSA_TARGET` | `~/work/rust/xtensa-intel_ace30_adsp-zephyr-elf.json` | JSON target spec for cross builds |
| `ZEPHYR_SDK_INSTALL_DIR` | (unset) | **Must be unset** for the LLVM build path; SDK 1.0.1+ is auto-detected |
| `LLVM_TOOLCHAIN_PATH` | (auto-set by build script) | Path to the LLVM build directory |

### Quick rebuild after changes

```bash
# After LLVM changes — rebuild LLVM, then rebuild rustc
cd ~/work/llvm-project && ninja -C build
cd ~/work/rust && ./x build --stage 2 library
ln -sf $PWD/build/host/stage2-tools-bin/cargo $PWD/build/host/stage2/bin/cargo

# After SOF/Rust module changes — rebuild SOF only
cd ~/work/sof3 && source .venv/bin/activate && unset ZEPHYR_SDK_INSTALL_DIR
./sof/scripts/xtensa-build-zephyr.py \
    --llvm-clang ~/work/llvm-project/build \
    --build-dir-suffix="-llvm-qemu" -p ptl

# After Rust target spec changes — rebuild rustc stage2
cd ~/work/rust && ./x build --stage 2 library
```

---

## Near-term priorities

In the order they should probably be tackled:

1. Load `rust_template.llext` on hardware / QEMU end-to-end and
   confirm `process()` swaps L/R on a live pipeline (the ztest path
   already proves the helper itself; this is the loader integration).
2. Confirm the correct `intel_ace30_adsp` ABI features against the
   Zephyr SDK build, populate `"features"` in the target spec.
3. Cherry-pick the upstream LLVM `getMCSubtargetInfo()` reference
   change into `lgirdwood/llvm-project@clang-dev` and revert the
   PassWrapper workaround.
4. Upstream the AE_ZERO pseudo expansion patch and add encoded
   `AE_SUB16S_HIFI3` / `AE_SUB24S_HIFI3` instructions so the AE_ZERO16
   / AE_ZERO24 pseudos can be expanded too (currently only AE_ZERO32
   / P48 / Q56 / 64 are wired up, which is enough for the v2i32
   BUILD_VECTOR path that compiler_builtins hits).
5. Fix the Xtensa LLVM literal-section issue at the source (emit
   literals next to their `.text.*` section, or coalesce in lld) so
   `STRIP_COMPILER_BUILTINS` and the panic-path avoidance can be
   retired.
6. Round out `sof_module::audio` to N-channel rotate (the stereo
   swap_lr_* helpers now cover s16 / s32 / S24_4LE / S24_3LE) and
   extend the FFI shims with format-specific source/sink helpers
   (`source_get_data_s16` / `_s32` etc.) where it avoids the
   per-call format dispatch.
7. Add the `xtensa-intel_ace30_adsp-zephyr-elf` target as a built-in
   tier-3 target in `compiler/rustc_target/`.
8. Wire a Zephyr-aware panic hook so a Rust panic in a module
   surfaces as a proper SOF abort with logs (the global allocator
   side of this is already done — see §SOF integration). Bonus:
   extend `sof_module::alloc` with a per-CPU slot keyed off
   `arch_proc_id()` so SMP modules can use `alloc` safely.
9. Get the ztests into SOF CI.

---

## Out of scope (for this branch)

- Porting `std` to Xtensa.
- Async runtimes / `tokio` / etc.
- Rust as the *primary* language for SOF — this work is about
  enabling Rust modules alongside the existing C codebase, not
  replacing it.
