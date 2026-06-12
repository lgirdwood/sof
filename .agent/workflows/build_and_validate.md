---
description: Build and validate new C code features
---

This workflow describes the process for building and validating any new C code features in the SOF repository.

**Note:** The QEMU build targets must be used for both building and testing. The user requires the build must be error and warning free and the ztests must all pass.

// turbo-all
1. Build the new C code feature using the `xtensa-build-zephyr.py` script.
    ```bash
    source ../.venv/bin/activate
    ./scripts/xtensa-build-zephyr.py qemu_xtensa
    ```

2. Validate the feature with a ztest run using the `sof-qemu-run.sh` script.
    ```bash
    source ../.venv/bin/activate
    ./scripts/sof-qemu-run.sh build-qemu_xtensa
    ```

3. Ensure that all new features and functions have appropriate Doxygen comments and that the Doxygen documentation builds without errors or warnings.

## Investigating DSP crashes or test failures on TGL hardware (spider DUT)

When `aplay` or a sof-test case triggers a DSP panic or produces incorrect results on the TGL spider DUT, and the firmware was built with LLVM (`--llvm-clang`), always rule out a compiler bug first by repeating the test with a Zephyr SDK (GCC) build before spending time on code-level analysis.

### Step 1 – build the same commit with the Zephyr SDK GCC toolchain

```bash
cd /home/lrg/work/sof-tgl
source .venv/bin/activate
# GCC build: no --llvm-clang flag; use a distinct suffix to keep the tree separate
./sof/scripts/xtensa-build-zephyr.py --build-dir-suffix="-tgl-gcc" -p tgl
```

### Step 2 – deploy the GCC firmware

```bash
sudo cp build-tgl-tgl-gcc/zephyr/zephyr.ri \
    /srv/nfs/spider-rootfs/lib/firmware/intel/sof/sof-tgl.ri
ssh root@spider 'rmmod snd_sof_pci_intel_tgl; modprobe snd_sof_pci_intel_tgl'
```

### Step 3 – reproduce the failing test

```bash
ssh root@spider 'aplay -f dat -d 3 /dev/zero'
# or the original sof-test command that failed
```

### Interpreting the results

| GCC result | LLVM result | Conclusion |
|---|---|---|
| Pass | Fail/crash | Likely LLVM code-gen bug — report with a minimal reproducer |
| Fail/crash | Fail/crash | Logic bug in C source — proceed with code analysis |
| Both fail at the **same** EPC1 address | — | Same object code; confirm both ELFs agree with `addr2line` |

If the GCC build passes but LLVM crashes, decode the LLVM EPC1 with the LLVM ELF:

```bash
ADDR2LINE=/home/lrg/zephyr-sdk-1.0.1/gnu/xtensa-intel_tgl_adsp_zephyr-elf/bin/xtensa-intel_tgl_adsp_zephyr-elf-addr2line
$ADDR2LINE -e build-tgl-shell-llvm/zephyr/zephyr.elf -f <EPC1>
```

and open a bug against the LLVM backend with the crashing function, its source, and the difference in generated assembly between GCC and LLVM (`objdump -d`).