# SPDX-License-Identifier: BSD-3-Clause

if(CONFIG_COMP_RUST_TEMPLATE STREQUAL "m" AND DEFINED CONFIG_LLEXT)
  add_subdirectory(llext ${PROJECT_BINARY_DIR}/rust_template_llext)
  add_dependencies(app rust_template)
else()
  # Built-in build is not supported for this Rust example component: the
  # whole point is the dynamically-loaded llext path. Fail loudly so users
  # don't get a silent no-op.
  if(CONFIG_COMP_RUST_TEMPLATE)
    message(FATAL_ERROR
      "CONFIG_COMP_RUST_TEMPLATE=y is not supported; set =m to build the "
      "Rust llext, or =n to disable.")
  endif()
endif()
