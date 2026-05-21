# SOF / Xtensa LLVM Compiler Enablement

This document tracks the status, features, and usage instructions for building Sound Open Firmware (SOF) using the LLVM/Clang compiler toolchain for Xtensa targets.

All development for this effort is currently tracked on the **`clang-dev`** branch across the respective repositories:
- `lgirdwood/llvm-project@clang-dev`
- `lgirdwood/sof@clang-dev`
- `lgirdwood/zephyr@clang-dev`

---

## Current Status & GCC Binutils Dependency

**Status:** SOF can be successfully compiled for Intel ADSP platforms using Clang/LLVM. The resulting firmware is stable and passes the comprehensive audio ZTest suite on QEMU and hardware.

**How it works today (The GCC Binutils hybrid):**
Currently, LLVM acts as the **C/C++ front-end and optimization backend**, but we still rely on the **Zephyr SDK's GCC binutils for assembly and linking**.
1. **Compilation**: Clang processes the C/C++ source code and emits optimized Xtensa assembly or ELF object files.
2. **Assembly & Linking**: The Zephyr build system invokes the Zephyr SDK's GCC toolchain (e.g., `xtensa-intel_ace30_ptl_zephyr-elf-gcc`) to assemble any hand-written `.S` files and link the final `zephyr.elf` firmware image using Zephyr's complex linker scripts.

This hybrid approach is necessary today because:
- LLVM's integrated assembler for Xtensa is still missing support for certain assembly directives and pseudo-ops used in the Zephyr/SOF codebase.
- The `lld` linker for Xtensa does not yet support literal section coalescing (like `xt-ld`), which is required to link the SOF firmware without running into "literal target out of range" (`l32r`) relocation errors.

---

## Supported Features

The `clang-dev` LLVM branch contains a 12-patch series that enables:

- **Full HiFi DSP Support**: Complete intrinsic definitions, CodeGen lowering, ISel, and MC layer support for Cadence Tensilica HiFi 3, HiFi 4, and HiFi 5 Audio Engines.
- **FLIX (Flexible Length Instruction eXtension)**: A VLIW packetizer that correctly bundles multiple Xtensa instructions into 64-bit FLIX formats to maximize DSP throughput.
- **Standardized Intel ADSP Processors**: Proper processor definitions for modern Intel platforms using a standardized naming convention:
  - `intel_ace15_adsp` (ACE 1.5 Meteor Lake / ACE 2.0 Lunar Lake)
  - `intel_ace30_adsp` (ACE 3.0 Panther Lake)
  - `intel_ace40_adsp` (ACE 4.0 NVL)
- **Rust Integration**: The LLVM backend serves as the foundation for `rustc`, enabling safe Rust audio modules in SOF.

---

## Building from Source — Step by Step

### 1. Prerequisites

Ensure you have the **Zephyr SDK (1.0.1 or newer)** installed, as Clang relies on its standard library headers and GCC binutils.

### 2. Build the Xtensa LLVM Compiler

Clone the `llvm-project` repository and build the Clang frontend and Xtensa backend:

```bash
mkdir -p ~/work && cd ~/work
git clone https://github.com/lgirdwood/llvm-project.git
cd llvm-project
git checkout clang-dev

# Configure LLVM with the Xtensa experimental target
cmake -G Ninja -B build -S llvm \
  -DCMAKE_BUILD_TYPE=Release \
  -DLLVM_TARGETS_TO_BUILD="host" \
  -DLLVM_EXPERIMENTAL_TARGETS_TO_BUILD="Xtensa" \
  -DLLVM_ENABLE_PROJECTS="clang;lld"

# Build the compiler
ninja -C build
```

Verify the build succeeded by checking for the Intel ADSP processors:
```bash
./build/bin/llc -march=xtensa -mattr=help 2>&1 | grep intel_ace
```

### 3. Build SOF with LLVM

Ensure you have your SOF west workspace set up and checked out to the `clang-dev` branch for both SOF and Zephyr.

```bash
cd ~/work/sof3
source .venv/bin/activate

# IMPORTANT: Unset the SDK dir so the build script auto-detects SDK 1.0.1+
# (Older SDKs lack the required directory structure for Clang's header search)
unset ZEPHYR_SDK_INSTALL_DIR

# Build SOF for Panther Lake (PTL) using LLVM
./sof/scripts/xtensa-build-zephyr.py \
    --llvm-clang ~/work/llvm-project/build \
    --build-dir-suffix="-llvm" \
    -p ptl
```

#### Important Build Arguments:
- `--llvm-clang <path>`: Points the build system to your compiled LLVM directory. When provided, the build script automatically switches to the `llvm` toolchain variant.
- `--build-dir-suffix`: Useful for keeping GCC and LLVM build directories separate (e.g., `build-ptl-llvm`).
- `-p <platform>`: The build script automatically maps the SOF platform (e.g., `ptl`, `mtl`) to the correct LLVM `XTENSA_CORE_ID` (e.g., `intel_ace30_adsp`).

---

## TODO / Near-term Priorities

1. **Upstream to LLVM Mainline**: Submit the 12-patch series on `upstream/dev` to the official LLVM repository. This includes the HiFi audio engine support, FLIX packetizer, and Intel ADSP processor definitions.
2. **Integrated Assembler (MC Layer)**: Expand LLVM's Xtensa MC layer to support all assembly directives and pseudo-instructions used by Zephyr. This will allow us to drop the GCC assembler dependency.
3. **LLD Linker Support**: Implement literal section coalescing in the `lld` linker for Xtensa. This is the primary blocker preventing us from dropping the Zephyr SDK GCC linker for firmware builds.
4. **ACE 4.0 (NVL) Validation**: Validate the `intel_ace40_adsp` target with a full firmware build and ZTest run once the corresponding Zephyr SDK GCC toolchain is available.
5. **CI Integration**: Add an LLVM-based build matrix to the SOF GitHub Actions CI to prevent regressions.
