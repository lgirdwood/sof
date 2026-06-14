# Rust in SOF

This document describes how to build and run Rust code inside the
Sound Open Firmware (SOF) tree — both for host-side ztests
(`native_sim`) and for actual Xtensa DSP firmware loaded as an
[llext](https://docs.zephyrproject.org/latest/services/llext/index.html)
module.

A working "hello world" ztest lives at
[`sof/test/ztest/unit/rust_hello/`](../test/ztest/unit/rust_hello/) and is
the canonical reference. The reusable CMake helper that builds Rust
staticlibs is
[`sof/scripts/cmake/rust.cmake`](../scripts/cmake/rust.cmake).

---

## 1. Why a custom toolchain

Mainline `rustup` channels (stable / nightly) ship LLVM **without** the
Xtensa backend. To target Intel ACE / ESP32 / any Xtensa SoC from Rust
you need a `rustc` linked against an LLVM that has the Xtensa target
enabled.

We get there by:

1. Building Xtensa-enabled LLVM from source.
2. Building `rustc` and `cargo` from source against that LLVM.
3. Linking the resulting toolchain into rustup under a custom name
   (we use `xtensa-llvm`).
4. Pointing `cargo` at a custom `--target` JSON spec for the specific
   Xtensa CPU (e.g. `intel_ace30_ptl`).
5. Using the Zephyr SDK's `xtensa-…-zephyr-elf-gcc` as the assembler
   and linker for the final ELF/llext.

For host-only code (ztests on `native_sim`) no Xtensa is involved — any
Rust toolchain will do, but we still default to `xtensa-llvm` for
consistency.

---

## 2. Required source repositories

| Repo | Purpose | Suggested path |
| --- | --- | --- |
| [`lgirdwood/llvm-project`](https://github.com/lgirdwood/llvm-project) branch `upstream/dev` | Xtensa-capable LLVM toolchain (with the `intel_ace30_ptl` CPU and Intel ACE workarounds) | `~/work/llvm-project` |
| [`lgirdwood/rust`](https://github.com/lgirdwood/rust) branch `rust-dev` | Rust compiler + std with the patches in §4.2 and the Xtensa Intel ACE 3.0 PTL target spec applied | `~/work/rust` |
| Zephyr SDK (binary release) | Xtensa GCC for assembly + linking final binary | `~/zephyr-sdk-1.0.1` |
| `thesofproject/sof` (this tree) | SOF firmware sources | `~/work/sof3` |

Clone the patched trees:

```bash
git clone --branch upstream/dev git@github.com:lgirdwood/llvm-project.git ~/work/llvm-project
git clone --branch rust-dev      git@github.com:lgirdwood/rust.git         ~/work/rust
cd ~/work/rust && git submodule update --init --recursive
```

The `rust-dev` branch tracks `rust-lang/rust` `main` and currently adds two commits on top:

| Commit | Subject |
|---|---|
| `73e36e39ef1` | `rustc_llvm: handle LLVM forks tagged 23 with pre-23 getMCSubtargetInfo signature` |
| `abba2b0ec04` | `Add Xtensa Intel ACE 3.0 PTL custom target spec` (`xtensa-intel_ace30_ptl-zephyr-elf.json` at the repo root) |

Versions used for the reference setup:

| Component        | Version                                 |
| ---------------- | --------------------------------------- |
| LLVM             | 23.0.0git (Xtensa-capable fork)         |
| rustc            | 1.97.0-nightly                          |
| Zephyr SDK       | 1.0.1                                   |

Rust requires LLVM ≥ 21
(see `src/bootstrap/src/core/build_steps/llvm.rs::check_llvm_version`).

---

## 3. System packages

Standard Zephyr / SOF host requirements plus a Python venv:

- A C/C++ toolchain capable of building LLVM and rustc
  (`gcc` ≥ 11 or `clang` ≥ 14, `cmake` ≥ 3.20, `ninja`, `python3`,
  `git`, `curl`).
- `rustup` (the Rust installer; we only use it to *link* our locally
  built toolchain, not to download anything):
  ```bash
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- --default-toolchain none
  ```
- The Python packages required by Twister (in your Zephyr venv):
  ```bash
  pip install natsort junitparser pyelftools tabulate ply colorama \
              anytree packaging pyserial PyYAML pytest psutil
  ```

---

## 4. Building the toolchain

### 4.1 Build Xtensa LLVM

The `upstream/dev` branch of
[`lgirdwood/llvm-project`](https://github.com/lgirdwood/llvm-project)
carries the Intel ACE Xtensa work (CPU descriptions for
`intel_ace30_ptl` / `intel_ace40`, plus the workarounds documented in
`LLVM_XTENSA_WORKAROUNDS.md` at the repo root). It is the LLVM that the
rest of this guide assumes.

```bash
git clone --branch upstream/dev git@github.com:lgirdwood/llvm-project.git ~/work/llvm-project
cd ~/work/llvm-project
cmake -S llvm -B build -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    -DLLVM_ENABLE_PROJECTS="clang;lld" \
    -DLLVM_TARGETS_TO_BUILD="X86" \
    -DLLVM_EXPERIMENTAL_TARGETS_TO_BUILD="Xtensa"
ninja -C build
```

Verify the Xtensa target and the ACE CPU descriptions are present:
```bash
~/work/llvm-project/build/bin/llvm-config --targets-built
# expected: X86 Xtensa

~/work/llvm-project/build/bin/llc -march=xtensa -mattr=help | grep intel_ace
# expected:
#   intel_ace30_ptl - Select the intel_ace30_ptl processor.
#   intel_ace40     - Select the intel_ace40 processor.
```

### 4.2 Build `rustc` against that LLVM

In `~/work/rust/bootstrap.toml` (a stripped example):

```toml
profile = 'dist'

[llvm]
download-ci-llvm = false

[build]
configure-args = []

[install]
[rust]
[dist]

[target.x86_64-unknown-linux-gnu]
llvm-config = "/home/<user>/work/llvm-project/build/bin/llvm-config"
```

Then:
```bash
cd ~/work/rust
./x build library     # stage1+2 rustc + std
./x build cargo       # stage2 cargo
```

#### Known patches required against current rust HEAD

These patches are already applied on the `rust-dev` branch of
[`lgirdwood/rust`](https://github.com/lgirdwood/rust); this section
describes what they do for anyone working from upstream
`rust-lang/rust` directly.

The Xtensa LLVM fork at the pinned commit reports version 23 but still
exposes the **pre-23** API
`TargetMachine::getMCSubtargetInfo() -> const MCSubtargetInfo*`
(upstream LLVM 23 changed it to return a reference).
`compiler/rustc_llvm/llvm-wrapper/PassWrapper.cpp` gates on
`LLVM_VERSION_GE(23, 0)` and takes the wrong branch for that fork.
Those four call sites (in `LLVMRustHasFeature`, `LLVMRustPrintTargetCPUs`,
`LLVMRustGetTargetFeaturesCount`, `LLVMRustGetTargetFeature`) need to
use the pointer-dereference form:

```cpp
const MCSubtargetInfo &MCInfo = *Target->getMCSubtargetInfo();
```

If the fork is later rebased onto an upstream LLVM that includes the
reference-returning change, this patch can be reverted.

### 4.3 Link the toolchain into rustup

```bash
ln -sf "$HOME/work/rust/build/host/stage2-tools-bin/cargo" \
       "$HOME/work/rust/build/host/stage2/bin/cargo"
rustup toolchain link xtensa-llvm "$HOME/work/rust/build/host/stage2"
rustup run xtensa-llvm rustc --version    # expect: 1.97.0-nightly
rustup run xtensa-llvm cargo --version
```

`cargo +xtensa-llvm …` now works.

### 4.4 Custom Xtensa target spec (only for DSP firmware)

For Intel ACE 3.0 PTL the target JSON lives at
`~/work/rust/xtensa-intel_ace30_ptl-zephyr-elf.json` (committed at the
repo root on the `rust-dev` branch of
[`lgirdwood/rust`](https://github.com/lgirdwood/rust/blob/rust-dev/xtensa-intel_ace30_ptl-zephyr-elf.json)):

```json
{
  "llvm-target": "xtensa-none-elf",
  "arch": "xtensa",
  "cpu": "intel_ace30_ptl",
  "data-layout": "e-m:e-p:32:32-i8:8:32-i16:16:32-i64:64-n32",
  "target-endian": "little",
  "target-pointer-width": 32,
  "target-c-int-width": 32,
  "os": "none",
  "vendor": "intel",
  "executables": true,
  "linker-flavor": "gnu-cc",
  "linker": "xtensa-intel_ace30_ptl_zephyr-elf-gcc",
  "panic-strategy": "abort",
  "relocation-model": "static",
  "emit-debug-gdb-scripts": false,
  "max-atomic-width": 32,
  "atomic-cas": true,
  "metadata": {
    "description": "Xtensa Intel ACE30 PTL (Zephyr SDK toolchain)",
    "tier": 3, "host_tools": false, "std": false
  }
}
```

Notes:

- `data-layout` **must** match LLVM's default for `xtensa-none-elf` —
  rustc rejects mismatches. Verify with
  `rustc --print target-spec-json -Zunstable-options --target xtensa-esp32-none-elf`.
- `target-pointer-width` and `target-c-int-width` are integers, not
  strings.
- Other Xtensa CPUs are similar; replace `cpu` and `linker`.
- See `~/work/llvm-project/build/bin/llc -march=xtensa -mattr=help` for
  the list of CPUs and features your LLVM exposes.

### 4.5 Zephyr SDK on PATH

For Xtensa builds the Zephyr SDK provides `as`, `ld`, `gcc`:

```bash
export PATH="$HOME/zephyr-sdk-1.0.1/xtensa-intel_ace30_ptl_zephyr-elf/bin:$PATH"
```

For host-only `native_sim` ztests this is **not** needed.

---

## 5. The CMake helper

Single function, zero magic:

```cmake
sof_rust_staticlib(<name>
    CRATE_DIR  <path>            # required; dir containing Cargo.toml
    [PROFILE   release|dev]      # default: release
    [TOOLCHAIN <rustup-name>]    # default: $SOF_RUST_TOOLCHAIN or "xtensa-llvm"
    [TARGET    <triple|.json>]   # default: host triple
    [FEATURES  feat1 feat2 ...]
    [BUILD_STD]                  # adds -Zbuild-std=core,alloc + -Zjson-target-spec
)
```

Side effects:

- Runs `cargo build --profile=<profile> --manifest-path=<crate>/Cargo.toml`
  with `RUSTUP_TOOLCHAIN=<TOOLCHAIN>` and a per-target `CARGO_TARGET_DIR`.
- Creates a `STATIC IMPORTED` CMake target `<name>` whose
  `IMPORTED_LOCATION` is the resulting `lib<name>.a`.
- Adds the crate's `include/` dir to the target's
  `INTERFACE_INCLUDE_DIRECTORIES`.
- For host builds on Linux, links `pthread dl m` automatically.

Implementation: [`sof/scripts/cmake/rust.cmake`](../scripts/cmake/rust.cmake).

---

## 6. Anatomy of a Rust ztest

Layout:

```
sof/test/ztest/unit/rust_hello/
├── CMakeLists.txt        # includes rust.cmake and links the staticlib
├── prj.conf              # CONFIG_ZTEST=y
├── testcase.yaml         # platform_allow: native_sim/native/64
├── test_rust_hello_ztest.c
└── rust/
    ├── Cargo.toml        # crate-type = ["staticlib"], panic = "abort"
    ├── include/rust_hello.h
    └── src/lib.rs        # #![no_std]; pub extern "C" fn …
```

Key points:

- **`crate-type = ["staticlib"]`** — produces a `.a` archive cargo can
  hand to the C linker.
- **`#![no_std]`** with a `#[panic_handler]` — Zephyr provides no Rust
  std runtime.
- **`panic = "abort"`** in `[profile.*]` — required because we have no
  unwinder.
- **`#[unsafe(no_mangle)] pub extern "C" fn …`** — keeps the symbol
  alive and gives it a stable C ABI.
- **`platform_allow: native_sim/native/64`** — the host Rust archive is
  x86_64; default `native_sim` is 32-bit and would refuse to link.
- The C side just `#include`s the header and calls the functions.
  See [`test_rust_hello_ztest.c`](../test/ztest/unit/rust_hello/test_rust_hello_ztest.c).

CMakeLists is three lines plus boilerplate:

```cmake
include(${SOF_ROOT}/scripts/cmake/rust.cmake)
sof_rust_staticlib(rust_hello CRATE_DIR ${CMAKE_CURRENT_LIST_DIR}/rust)
target_link_libraries(app PRIVATE rust_hello)
```

---

## 7. Building & running the ztest

```bash
source ~/work/sof3/.venv/bin/activate          # has west + Twister deps
cd ~/work/sof3
./zephyr/scripts/twister \
    -p native_sim/native/64 \
    -T sof/test/ztest/unit/rust_hello \
    --inline-logs
```

Expected last lines:

```
INFO    - 1 of 1 executed test configurations passed (100.00%) …
INFO    - 2 of 2 executed test cases passed (100.00%) …
```

To pick a different Rust toolchain:

```bash
SOF_RUST_TOOLCHAIN=stable \
    ./zephyr/scripts/twister -p native_sim/native/64 \
        -T sof/test/ztest/unit/rust_hello
```

---

## 9. Writing a SOF processing module in Rust

The [`sof_module`](../rust/sof_module/) crate is a thin Rust wrapper
around SOF's processing-module ABI:

- A `#[repr(C)]` mirror of
  [`struct module_interface`](../src/include/module/module/interface.h)
  (matching the public layout, i.e. without `SOF_MODULE_API_PRIVATE`).
- Opaque newtypes for the C handles (`ProcessingModuleHandle`,
  `SofSource`, `SofSink`, `BindInfo`).
- A safe-Rust `ProcessingModule` trait whose methods return
  `Result<(), i32>` (Ok → `0`, Err → negative errno).
- A `define_module!(MyType, my_iface_symbol)` macro that emits the
  C-ABI shim functions and a `#[unsafe(no_mangle)] pub static
  my_iface_symbol: ModuleInterface` symbol the C side can pick up.

Authors opt into optional slots via `const HAS_*: bool = true` items on
the trait impl; slots that are not opted into stay `None`
(i.e. `NULL`) in the resulting `struct module_interface`, matching what
SOF's C code expects for "not provided".

### Minimal Rust module skeleton

```rust
#![no_std]
use sof_module::{
    define_module, err, ProcessingModule, ProcessingModuleHandle, StreamCtx,
};

#[panic_handler] fn panic(_: &core::panic::PanicInfo) -> ! { loop {} }

pub struct MyMod;

impl ProcessingModule for MyMod {
    const HAS_PROCESS: bool = true;
    const HAS_FREE:    bool = true;

    fn init(_m: *mut ProcessingModuleHandle) -> Result<(), i32> {
        Ok(())
    }

    fn process(_m: *mut ProcessingModuleHandle, _ctx: StreamCtx)
        -> Result<(), i32>
    {
        Ok(())
    }

    fn free(_m: *mut ProcessingModuleHandle) -> Result<(), i32> {
        Ok(())
    }
}

define_module!(MyMod, my_module_interface);
```

### C side

```c
#include <module/module/interface.h>
extern const struct module_interface my_module_interface;

static const struct sof_man_module_manifest mod_manifest __section(".module") __used =
    SOF_LLEXT_MODULE_MANIFEST("MYMOD", &my_module_interface, 1,
                              SOF_REG_UUID(my_mod), 40);
```

Nothing else changes for SOF — the C `struct module_interface` is the
ABI boundary; whether the function pointers happen to point at C or
Rust code is invisible to the rest of SOF.

### Reference test

[`sof/test/ztest/unit/rust_module_iface/`](../test/ztest/unit/rust_module_iface/)
builds a Rust module against `sof_module`, exposes the
`rust_demo_module_interface` symbol, and the C ztest invokes the
function pointers directly to verify dispatch (init, process, trigger
argument propagation, set_config_param error path, optional slots
remain `NULL`).

Run with:
```bash
./zephyr/scripts/twister -p native_sim/native/64 \
    -T sof/test/ztest/unit/rust_module_iface --inline-logs
```

---

## 10. Adding Rust to a real Xtensa SOF module (llext)

Use the `sof_rust_llext_module()` helper. It cross-builds the Rust
crate as a staticlib with `-Zbuild-std` and feeds it into
`sof_llext_build()`, wiring the dependency edge so the llext link
waits for cargo:

```cmake
# sof/src/audio/my_dsp/llext/CMakeLists.txt
cmake_path(SET _sof_root NORMALIZE ${APPLICATION_SOURCE_DIR}/..)
include(${_sof_root}/scripts/cmake/rust.cmake)

sof_rust_llext_module(my_dsp
    CRATE_DIR ${CMAKE_CURRENT_LIST_DIR}/../rust
    SOURCES   ../my_dsp.c
    LIB       openmodules
)
```

The helper:

1. Calls `sof_rust_staticlib(my_dsp_rust ...)` with the Xtensa target
   spec (default `$SOF_RUST_XTENSA_TARGET`, falling back to
   `~/work/rust/xtensa-intel_ace30_ptl-zephyr-elf.json`) and
   `BUILD_STD` enabled.
2. Calls `sof_llext_build(my_dsp ...)` with the Rust archive added to
   `LIBS` / `LIBS_PATH`, so the final llext link picks up
   `-lmy_dsp_rust -L<cargo-out-dir>`.
3. Adds `add_dependencies(my_dsp_llext_lib my_dsp_rust_build)` so the
   serialized llext build chain blocks on cargo.

The C glue file (`my_dsp.c`) only carries the SOF metadata—
`SOF_DEFINE_REG_UUID`, `LOG_MODULE_REGISTER`, the
`__section(".module")` `sof_man_module_manifest`, and an `extern
const struct module_interface my_dsp_interface;` declaration; the
actual vtable is the `#[no_mangle] static` emitted by
`sof_module::define_module!()` on the Rust side.

See [`sof/src/audio/rust_template/`](../src/audio/rust_template/) for
a complete reference module.

Useful options:

| Argument | Default | Meaning |
|---|---|---|
| `CRATE_DIR <path>` | (required) | Path to the Rust crate root (Cargo.toml lives here). |
| `SOURCES <files>` | (required) | C glue file(s) compiled into the llext. |
| `LIB <name>` | unset | Forwarded to `sof_llext_build` as `LIB`. |
| `LIBS <l1> ...` | unset | Extra `-l` libs added after the Rust archive. |
| `INCLUDES <dirs>` | unset | Extra include dirs for the C glue. |
| `CFLAGS <flags>` | unset | Extra cflags for the C glue. |
| `TARGET <triple-or-json>` | `$SOF_RUST_XTENSA_TARGET` else `~/work/rust/xtensa-intel_ace30_ptl-zephyr-elf.json` | Rust target spec. |
| `PROFILE release\|dev` | `release` | Cargo profile. |
| `TOOLCHAIN <name>` | `xtensa-llvm` | Rustup toolchain. |
| `FEATURES <f1> ...` | unset | Cargo `--features`. |
| `NO_BUILD_STD` | off | Opt out of `-Zbuild-std=core,alloc` (only useful if you supply prebuilt sysroots). |

Caveats specific to the firmware path:

- ABI flags (call0/windowed, FLIX, HiFi) must match between the
  Zephyr SDK GCC and your Rust target spec. Mismatches show up as
  `Tag_*` ABI errors at link time. Edit the JSON's `features` field.
- `compiler-builtins` may collide with symbols Zephyr provides
  (`memcpy`, `__udivdi3`, …). Add `NO_BUILD_STD` and supply your own
  sysroot if you see duplicate-symbol errors.
- `core` and `alloc` are tier-3 for Xtensa, hence `-Zbuild-std`. The
  custom `xtensa-llvm` rustc is nightly, so `-Z` flags are accepted.
- LLVM Xtensa codegen is incomplete; certain `compiler_builtins`
  routines currently abort with `rustc-LLVM ERROR: Unsupported
  instruction`. This is a backend gap, not a build-system bug —
  see Roadmap.

---

## 11. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `error: 'cargo' is not installed for the custom toolchain 'xtensa-llvm'` | Run `./x build cargo` and re-link as in §4.3. |
| `rustup could not choose a version of cargo to run` | The build is hitting `/usr/bin/cargo` (rustup proxy with no default). The helper sets `RUSTUP_TOOLCHAIN=…` to avoid this. |
| `'.json' target specs require -Zjson-target-spec` | Pass `BUILD_STD` to `sof_rust_staticlib`, or add `-Zjson-target-spec` manually. |
| `target-pointer-width: invalid type: string "32"` | The JSON has integers as strings; quote-strip them. |
| `data-layout for target … differs from LLVM target's default` | Use the data-layout from `rustc --print target-spec-json … --target xtensa-esp32-none-elf`. |
| `can't find crate for 'std'` when building user crate | Add `#![no_std]`; provide a `#[panic_handler]`. |
| `cannot produce dylib for 'rustc_driver' as the target … does not support these crate types` | You ran `cargo build` from inside the rustc workspace. Build from your project directory or pass `--manifest-path`. |
| LLVM 23 build failure: `invalid initialization of reference … from expression of type 'const llvm::MCSubtargetInfo*'` | Apply the PassWrapper.cpp patch in §4.2. |

---

## 12. File map

| Path | Purpose |
|---|---|
| [`sof/scripts/cmake/rust.cmake`](../scripts/cmake/rust.cmake) | `sof_rust_staticlib()` and `sof_rust_llext_module()` helpers |
| [`sof/rust/sof_module/`](../rust/sof_module/) | Rust crate wrapping `struct module_interface` |
| [`sof/src/audio/rust_template/`](../src/audio/rust_template/) | Reference Rust llext audio module |
| [`sof/test/ztest/unit/rust_hello/`](../test/ztest/unit/rust_hello/) | Hello-world ztest |
| [`sof/test/ztest/unit/rust_module_iface/`](../test/ztest/unit/rust_module_iface/) | Module-interface ztest |
| [`xtensa-intel_ace30_ptl-zephyr-elf.json`](https://github.com/lgirdwood/rust/blob/rust-dev/xtensa-intel_ace30_ptl-zephyr-elf.json) (in `lgirdwood/rust@rust-dev`) | Custom Rust target spec |
| `~/work/rust/bootstrap.toml` | Rust build config pointing at external LLVM (local-only, git-ignored) |

---

## 13. Roadmap / known limitations

- Only Intel ACE 3.0 PTL has a target spec checked in (out-of-tree at
  `~/work/rust/`). Other Xtensa SoCs need their own JSON.
- No global allocator is wired up — Rust modules are restricted to
  `core` (and `alloc` only if you provide a `#[global_allocator]` that
  delegates to Zephyr's heap).
- No async runtime, no panic unwinding, no `std::thread` etc. This is
  bare-metal Rust by design.
- The custom rustc patches (§4.2) need to be re-applied any time the
  `rust-dev` branch is rebased onto a new `rust-lang/rust` HEAD, until
  the local LLVM fork picks up upstream's `getMCSubtargetInfo()`
  reference change.
