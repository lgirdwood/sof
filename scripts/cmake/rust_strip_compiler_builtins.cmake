# Helper invoked by sof_rust_staticlib(... STRIP_COMPILER_BUILTINS).
#
# Removes every member of the static archive ${LIB} whose file name
# matches *compiler_builtins*. See the comment block in rust.cmake
# for why this is needed for static Xtensa firmware links.
#
# Inputs (set on the cmake -D command line):
#   AR  - archiver to use (cross or host)
#   LIB - absolute path to the .a archive to edit in-place

if(NOT AR)
    message(FATAL_ERROR "rust_strip_compiler_builtins.cmake: AR not set")
endif()
if(NOT LIB)
    message(FATAL_ERROR "rust_strip_compiler_builtins.cmake: LIB not set")
endif()
if(NOT EXISTS "${LIB}")
    message(FATAL_ERROR "rust_strip_compiler_builtins.cmake: LIB does not exist: ${LIB}")
endif()

execute_process(
    COMMAND ${AR} t ${LIB}
    OUTPUT_VARIABLE members
    RESULT_VARIABLE rc
    OUTPUT_STRIP_TRAILING_WHITESPACE)
if(NOT rc EQUAL 0)
    message(FATAL_ERROR "ar t ${LIB} failed: ${rc}")
endif()

string(REPLACE "\n" ";" member_list "${members}")
set(removed 0)
foreach(m IN LISTS member_list)
    if(m MATCHES "compiler_builtins")
        execute_process(COMMAND ${AR} d ${LIB} ${m} RESULT_VARIABLE drc)
        if(NOT drc EQUAL 0)
            message(FATAL_ERROR "ar d ${LIB} ${m} failed: ${drc}")
        endif()
        math(EXPR removed "${removed} + 1")
    endif()
endforeach()

message(STATUS "rust_strip_compiler_builtins: removed ${removed} member(s) from ${LIB}")
