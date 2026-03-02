---
description: Build and validate new C code features
---

This workflow describes the process for building and validating any new C code features in the SOF repository.

**Note:** The QEMU build targets must be used for both building and testing.

1. Build the new C code feature using the `xtensa-build-zephyr.py` script.
```bash
./scripts/xtensa-build-zephyr.py
```

2. Validate the feature with a ztest run using the `sof-qemu-run.sh` script.
```bash
./scripts/sof-qemu-run.sh
```

3. Ensure that all new features and functions have appropriate Doxygen comments and that the Doxygen documentation builds without errors or warnings.
