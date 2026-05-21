# SPDX-License-Identifier: Apache-2.0

cmake_minimum_required(VERSION 3.21.0)

find_package(Zephyr HINTS $ENV{ZEPHYR_BASE})
project(sample_sof)

target_sources(app PRIVATE
    src/main.c
  )

zephyr_library_include_directories(app PUBLIC
    ${CMAKE_CURRENT_SOURCE_DIR}/../src/arch/xtensa/include
    ${CMAKE_CURRENT_SOURCE_DIR}/../src/include
  )

# Optional: pull a Rust staticlib hello-world ztest into the firmware
# ztest build. Enabled via CONFIG_RUST_HELLO_ZTEST (off by default,
# turned on by sof/app/ztest_overlay.conf). The actual ztest sources
# live under sof/test/ztest/unit/rust_hello/, this just reuses them
# in the firmware-app context.
if(CONFIG_RUST_HELLO_ZTEST)
  set(_sof_root ${CMAKE_CURRENT_SOURCE_DIR}/..)
  set(_rust_hello_dir ${_sof_root}/test/ztest/unit/rust_hello)
  include(${_sof_root}/scripts/cmake/rust.cmake)

  # Resolve the Xtensa Rust target spec the same way
  # sof_rust_llext_module() does, so the staticlib is cross-built
  # for Xtensa rather than for the host triple. Without TARGET,
  # cargo would produce a host x86_64-unknown-linux-gnu archive
  # which the Xtensa BFD linker rejects with
  # "file format not recognized".
  if(DEFINED ENV{SOF_RUST_XTENSA_TARGET})
    set(_rust_target $ENV{SOF_RUST_XTENSA_TARGET})
  else()
    set(_rust_target $ENV{HOME}/work/rust/xtensa-intel_ace30_ptl-zephyr-elf.json)
  endif()

  sof_rust_staticlib(rust_hello
    CRATE_DIR ${_rust_hello_dir}/rust
    TARGET    ${_rust_target}
    BUILD_STD
    STRIP_COMPILER_BUILTINS
  )
  target_sources(app PRIVATE ${_rust_hello_dir}/test_rust_hello_ztest.c)
  target_include_directories(app PRIVATE ${_rust_hello_dir}/rust/include)
  target_link_libraries(app PRIVATE rust_hello)
endif()
