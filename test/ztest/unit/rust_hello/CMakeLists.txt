# SPDX-License-Identifier: BSD-3-Clause
cmake_minimum_required(VERSION 3.20.0)

find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(test_rust_hello)

set(SOF_ROOT "${PROJECT_SOURCE_DIR}/../../../..")
include(${SOF_ROOT}/scripts/cmake/rust.cmake)

# Build a Rust staticlib alongside the test. Default toolchain is
# `xtensa-llvm` (overridable via $SOF_RUST_TOOLCHAIN). For native_sim the
# host triple is used; no -Zbuild-std required.
sof_rust_staticlib(rust_hello
    CRATE_DIR ${CMAKE_CURRENT_LIST_DIR}/rust
)

target_include_directories(app PRIVATE ${SOF_ROOT}/src/include)
target_sources(app PRIVATE test_rust_hello_ztest.c)
target_link_libraries(app PRIVATE rust_hello)
