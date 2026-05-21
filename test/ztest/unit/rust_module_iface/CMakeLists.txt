# SPDX-License-Identifier: BSD-3-Clause
cmake_minimum_required(VERSION 3.20.0)

find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(test_rust_module_iface)

set(SOF_ROOT "${PROJECT_SOURCE_DIR}/../../../..")
include(${SOF_ROOT}/scripts/cmake/rust.cmake)

# Rust crate uses a path dep on sof_module; cargo will pull it in.
sof_rust_staticlib(rust_module_iface_demo
    CRATE_DIR ${CMAKE_CURRENT_LIST_DIR}/rust
)

target_include_directories(app PRIVATE
    ${SOF_ROOT}/src/include
    ${CMAKE_CURRENT_LIST_DIR}/rust/include
)
target_sources(app PRIVATE test_rust_module_iface_ztest.c)
target_link_libraries(app PRIVATE rust_module_iface_demo)
