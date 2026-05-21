# SPDX-License-Identifier: BSD-3-Clause
#
# Helpers for building Rust staticlibs that can be linked into SOF C code
# (ztest binaries, llext modules, etc.).
#
# Usage from a ztest CMakeLists.txt:
#
#   include(${SOF_ROOT}/scripts/cmake/rust.cmake)
#
#   sof_rust_staticlib(rust_hello
#       CRATE_DIR ${CMAKE_CURRENT_LIST_DIR}/rust
#   )
#   target_link_libraries(app PRIVATE rust_hello)
#
# Optional arguments:
#   PROFILE <release|dev>            (default: release)
#   TOOLCHAIN <rustup-toolchain>     (default: $SOF_RUST_TOOLCHAIN env var,
#                                     else "xtensa-llvm")
#   TARGET <triple-or-json-path>     (default: empty = host)
#   FEATURES <f1;f2;...>             (cargo --features)
#   BUILD_STD                        (pass -Zbuild-std=core,alloc and
#                                     -Zjson-target-spec; required for custom
#                                     JSON targets and for cross-compiling
#                                     core/alloc to a tier-3 target)
#
# After the call, a CMake STATIC IMPORTED target named <name> exists and can
# be linked with target_link_libraries().

if(COMMAND sof_rust_staticlib)
    return()
endif()

# Locate cargo once, at include time.
find_program(SOF_RUST_CARGO cargo)
if(NOT SOF_RUST_CARGO)
    message(FATAL_ERROR
        "rust.cmake: `cargo` not found on PATH. Install Rust (https://rustup.rs)"
        " or set CARGO via -DSOF_RUST_CARGO=/path/to/cargo.")
endif()

# Default toolchain: env var > "xtensa-llvm" (the locally-built toolchain
# linked via `rustup toolchain link xtensa-llvm <rust-build>/host/stage2`).
# Cache as INTERNAL so the value survives the include guard above when
# rust.cmake is re-included from a different CMake subdirectory scope
# (e.g. both sof/app/ and sof/zephyr/test/).
if(DEFINED ENV{SOF_RUST_TOOLCHAIN})
    set(SOF_RUST_DEFAULT_TOOLCHAIN $ENV{SOF_RUST_TOOLCHAIN}
        CACHE INTERNAL "Default rustup toolchain for SOF Rust crates")
else()
    set(SOF_RUST_DEFAULT_TOOLCHAIN xtensa-llvm
        CACHE INTERNAL "Default rustup toolchain for SOF Rust crates")
endif()

function(sof_rust_staticlib name)
    set(options BUILD_STD STRIP_COMPILER_BUILTINS)
    set(single_args CRATE_DIR PROFILE TOOLCHAIN TARGET)
    set(multi_args FEATURES)
    cmake_parse_arguments(ARG "${options}" "${single_args}" "${multi_args}" ${ARGN})

    if(NOT ARG_CRATE_DIR)
        message(FATAL_ERROR "sof_rust_staticlib(${name}): CRATE_DIR is required")
    endif()
    if(NOT IS_ABSOLUTE "${ARG_CRATE_DIR}")
        get_filename_component(ARG_CRATE_DIR "${ARG_CRATE_DIR}" ABSOLUTE
            BASE_DIR "${CMAKE_CURRENT_LIST_DIR}")
    endif()
    if(NOT EXISTS "${ARG_CRATE_DIR}/Cargo.toml")
        message(FATAL_ERROR
            "sof_rust_staticlib(${name}): no Cargo.toml in ${ARG_CRATE_DIR}")
    endif()

    if(NOT ARG_PROFILE)
        set(ARG_PROFILE release)
    endif()
    if(NOT ARG_TOOLCHAIN)
        set(ARG_TOOLCHAIN ${SOF_RUST_DEFAULT_TOOLCHAIN})
    endif()

    # Per-target build dir under the CMake binary tree.
    set(target_dir "${CMAKE_CURRENT_BINARY_DIR}/rust/${name}")

    # Compute output path. cargo places the staticlib at:
    #   <target_dir>[/<triple>]/<profile>/lib<name>.a
    # where <triple> is the file stem if TARGET is a .json path.
    set(out_subdir "${ARG_PROFILE}")
    set(cargo_args build "--profile=${ARG_PROFILE}"
                   "--manifest-path=${ARG_CRATE_DIR}/Cargo.toml")

    # cargo treats `--profile=dev` as an error (use `--profile=dev` only since
    # cargo 1.57; older spelling is no flag). For modern cargo this is fine.

    if(ARG_TARGET)
        list(APPEND cargo_args "--target=${ARG_TARGET}")
        if(ARG_TARGET MATCHES "\\.json$")
            get_filename_component(triple "${ARG_TARGET}" NAME_WE)
        else()
            set(triple "${ARG_TARGET}")
        endif()
        set(out_subdir "${triple}/${ARG_PROFILE}")
    endif()

    if(ARG_FEATURES)
        string(JOIN "," features ${ARG_FEATURES})
        list(APPEND cargo_args "--features=${features}")
    endif()

    if(ARG_BUILD_STD)
        list(APPEND cargo_args
            "-Zbuild-std=core,alloc"
            # `compiler-builtins-mem` enables mem*() (memcpy/memset/memmove)
            # so no_std code links cleanly.
            "-Zbuild-std-features=compiler-builtins-mem"
            "-Zjson-target-spec")
    endif()

    set(out_lib "${target_dir}/${out_subdir}/lib${name}.a")

    # Globbing source files for DEPENDS so edits trigger rebuilds. Cargo
    # itself will detect no-op rebuilds quickly, but listing sources here
    # makes Ninja happy.
    file(GLOB_RECURSE rust_sources CONFIGURE_DEPENDS
        "${ARG_CRATE_DIR}/Cargo.toml"
        "${ARG_CRATE_DIR}/Cargo.lock"
        "${ARG_CRATE_DIR}/src/*.rs"
        "${ARG_CRATE_DIR}/build.rs"
    )

    # Optional post-build step: drop compiler_builtins .o files from the
    # archive. Workaround for an Xtensa LLVM backend limitation: literals
    # are emitted into a separate `<section>.literal` section, so when
    # compiler_builtins functions (math/int helpers) reference each other
    # via l32r the GNU BFD linker rejects them with
    # "dangerous relocation: l32r: literal target out of range / misaligned".
    # The `-shared` llext link path is unaffected (dynamic relocations are
    # left for the loader), but a static firmware link fails. Crates that
    # only use trivial integer ops (no soft-float / 64-bit shifts on Xtensa)
    # don't actually need any compiler_builtins symbols beyond mem*(), which
    # the firmware libc already provides, so stripping them is safe.
    set(strip_cmd "")
    if(ARG_STRIP_COMPILER_BUILTINS)
        # Prefer the cross AR if reachable via the SDK, fall back to host ar.
        find_program(SOF_RUST_XTENSA_AR
            NAMES xtensa-intel_ace30_ptl_zephyr-elf-ar
            HINTS $ENV{ZEPHYR_SDK_INSTALL_DIR}/gnu/xtensa-intel_ace30_ptl_zephyr-elf/bin
            NO_CACHE)
        if(NOT SOF_RUST_XTENSA_AR)
            set(SOF_RUST_XTENSA_AR ar)
        endif()
        set(strip_cmd
            COMMAND ${CMAKE_COMMAND} -DAR=${SOF_RUST_XTENSA_AR} -DLIB=${out_lib}
                    -P ${CMAKE_CURRENT_FUNCTION_LIST_DIR}/rust_strip_compiler_builtins.cmake
        )
    endif()

    add_custom_command(
        OUTPUT  "${out_lib}"
        COMMAND ${CMAKE_COMMAND} -E env
                "CARGO_TARGET_DIR=${target_dir}"
                "RUSTUP_TOOLCHAIN=${ARG_TOOLCHAIN}"
                ${SOF_RUST_CARGO} ${cargo_args}
        ${strip_cmd}
        DEPENDS ${rust_sources}
        WORKING_DIRECTORY "${ARG_CRATE_DIR}"
        COMMENT "Building Rust staticlib '${name}' (toolchain=${ARG_TOOLCHAIN}, profile=${ARG_PROFILE})"
        VERBATIM
    )

    add_custom_target(${name}_build DEPENDS "${out_lib}")

    add_library(${name} STATIC IMPORTED GLOBAL)
    set_target_properties(${name} PROPERTIES
        IMPORTED_LOCATION "${out_lib}")
    if(IS_DIRECTORY "${ARG_CRATE_DIR}/include")
        set_property(TARGET ${name} APPEND PROPERTY
            INTERFACE_INCLUDE_DIRECTORIES "${ARG_CRATE_DIR}/include")
    endif()
    add_dependencies(${name} ${name}_build)

    # Rust's libcore on Linux pulls in pthread/dl/m for unwind support and
    # similar runtime hooks; these are no-ops on bare-metal targets that
    # statically link only the Rust archive.
    if(NOT ARG_TARGET AND CMAKE_HOST_SYSTEM_NAME STREQUAL "Linux")
        set_property(TARGET ${name} APPEND PROPERTY
            INTERFACE_LINK_LIBRARIES pthread dl m)
    endif()
endfunction()

# ----------------------------------------------------------------------------
# sof_rust_llext_module(<module_name>
#     CRATE_DIR <path>
#     SOURCES   <c-glue.c> [<more.c> ...]
#     [LIB     <lib-name>]                # forwarded to sof_llext_build LIB
#     [LIBS    <extra-llext-libs> ...]    # extra -l names beyond the rust lib
#     [INCLUDES <dirs> ...]               # extra include dirs for the C glue
#     [CFLAGS  <flags> ...]               # extra cflags for the C glue
#     [TARGET   <triple-or-json>]         # default: $SOF_RUST_XTENSA_TARGET
#                                         # else  ~/work/rust/xtensa-intel_ace30_adsp-zephyr-elf.json
#     [PROFILE  release|dev]              # default: release
#     [TOOLCHAIN <rustup-toolchain>]      # default: $SOF_RUST_TOOLCHAIN, else xtensa-llvm
#     [FEATURES <f1> ...]                 # cargo --features
#     [NO_BUILD_STD]                      # opt out of -Zbuild-std
# )
#
# Convenience wrapper that:
#   1. cross-builds the Rust crate as a staticlib for the Xtensa target
#      (BUILD_STD on by default), giving a CMake target <module>_rust pointing
#      at lib<module>_rust.a;
#   2. invokes sof_llext_build(<module> ...) with that staticlib added to the
#      llext link line via LIBS / LIBS_PATH;
#   3. adds the build-edge so the llext link waits for cargo.
#
# The Rust crate is expected to expose a `#[no_mangle] static
# <module>_interface: ModuleInterface` symbol via sof_module's
# `define_module!()` macro. The C glue file should then carry the SOF
# manifest stanza pointing at that symbol; see
# sof/src/audio/rust_template/ for a reference module.
# ----------------------------------------------------------------------------
function(sof_rust_llext_module module)
    set(options NO_BUILD_STD)
    set(single_args CRATE_DIR LIB TARGET PROFILE TOOLCHAIN)
    set(multi_args SOURCES LIBS INCLUDES CFLAGS FEATURES)
    cmake_parse_arguments(RLE "${options}" "${single_args}" "${multi_args}" ${ARGN})

    if(NOT RLE_CRATE_DIR)
        message(FATAL_ERROR "sof_rust_llext_module(${module}): CRATE_DIR is required")
    endif()
    if(NOT RLE_SOURCES)
        message(FATAL_ERROR "sof_rust_llext_module(${module}): SOURCES is required (at least the C glue file)")
    endif()

    # Resolve the Xtensa target spec once.
    if(NOT RLE_TARGET)
        if(DEFINED ENV{SOF_RUST_XTENSA_TARGET})
            set(RLE_TARGET $ENV{SOF_RUST_XTENSA_TARGET})
        else()
            set(RLE_TARGET "$ENV{HOME}/work/rust/xtensa-intel_ace30_adsp-zephyr-elf.json")
        endif()
    endif()
    if(NOT EXISTS "${RLE_TARGET}")
        message(WARNING
            "sof_rust_llext_module(${module}): TARGET file '${RLE_TARGET}' "
            "does not exist; cargo will likely fail. Set SOF_RUST_XTENSA_TARGET "
            "or pass TARGET <path>.")
    endif()

    set(rustlib "${module}_rust")

    set(staticlib_args CRATE_DIR "${RLE_CRATE_DIR}" TARGET "${RLE_TARGET}")
    if(NOT RLE_NO_BUILD_STD)
        list(APPEND staticlib_args BUILD_STD)
    endif()
    if(RLE_PROFILE)
        list(APPEND staticlib_args PROFILE "${RLE_PROFILE}")
    endif()
    if(RLE_TOOLCHAIN)
        list(APPEND staticlib_args TOOLCHAIN "${RLE_TOOLCHAIN}")
    endif()
    if(RLE_FEATURES)
        list(APPEND staticlib_args FEATURES ${RLE_FEATURES})
    endif()

    sof_rust_staticlib(${rustlib} ${staticlib_args})

    # Derive the directory holding lib${rustlib}.a for -L on the llext link.
    get_target_property(_rust_lib_path ${rustlib} IMPORTED_LOCATION)
    get_filename_component(_rust_lib_dir "${_rust_lib_path}" DIRECTORY)

    # Hand off to sof_llext_build. We pre-pend ${rustlib} so it shows up first
    # on the link line (-l<rustlib>) before any other LIBS the caller adds.
    set(llext_args SOURCES ${RLE_SOURCES}
                   LIBS    ${rustlib} ${RLE_LIBS}
                   LIBS_PATH ${_rust_lib_dir})
    if(RLE_LIB)
        list(APPEND llext_args LIB ${RLE_LIB})
    endif()
    if(RLE_INCLUDES)
        list(APPEND llext_args INCLUDES ${RLE_INCLUDES})
    endif()
    if(RLE_CFLAGS)
        list(APPEND llext_args CFLAGS ${RLE_CFLAGS})
    endif()

    sof_llext_build("${module}" ${llext_args})

    # Make sure cargo finishes before the llext link step runs.
    if(TARGET ${module}_llext_lib)
        add_dependencies(${module}_llext_lib ${rustlib}_build)
    endif()
endfunction()
