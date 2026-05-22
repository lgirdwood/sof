# SPDX-License-Identifier: BSD-3-Clause

if(CONFIG_SOF_BOOT_TEST)
  zephyr_library_sources_ifdef(CONFIG_VIRTUAL_HEAP
    vmh.c
  )
  zephyr_library_sources_ifdef(CONFIG_SOF_VREGIONS
    vpage.c vregion.c
  )
  zephyr_library_sources_ifdef(CONFIG_USERSPACE
    userspace/ksem.c
  )
endif()

if(CONFIG_SOF_BOOT_TEST_STANDALONE AND CONFIG_SOF_USERSPACE_INTERFACE_DMA)
  if(CONFIG_DT_HAS_INTEL_ADSP_HDA_HOST_IN_ENABLED)
    zephyr_library_sources(userspace/test_intel_hda_dma.c)
  endif()
  if(CONFIG_DT_HAS_INTEL_ADSP_HDA_SSP_CAP_ENABLED)
    zephyr_library_sources(userspace/test_intel_ssp_dai.c)
  endif()
endif()

if(CONFIG_SOF_BOOT_TEST_STANDALONE AND CONFIG_USERSPACE)
  zephyr_library_sources(userspace/test_mailbox.c)
endif()

if(CONFIG_SOF_BOOT_TEST_STANDALONE AND CONFIG_SOF_USERSPACE_LL)
  zephyr_library_sources(userspace/test_ll_task.c)
endif()

if(CONFIG_ZTEST)
  set(MATH_ZTEST_SOURCES
    ../../test/ztest/unit/math/basic/arithmetic/test_crc32_ztest.c
    ../../test/ztest/unit/math/basic/arithmetic/test_find_equal_int16_ztest.c
    ../../test/ztest/unit/math/basic/arithmetic/test_find_max_abs_int32_ztest.c
    ../../test/ztest/unit/math/basic/arithmetic/test_find_min_int16_ztest.c
    ../../test/ztest/unit/math/basic/arithmetic/test_gcd_ztest.c
    ../../test/ztest/unit/math/basic/arithmetic/test_norm_int32_ztest.c
    ../../test/ztest/unit/math/basic/arithmetic/test_ceil_divide_ztest.c
    ../../test/ztest/unit/math/basic/trigonometry/trig_test.c
    ../../test/ztest/unit/math/basic/complex/test_complex_polar.c
    ../../test/ztest/unit/math/advanced/functions/test_exponential_ztest.c
    ../../test/ztest/unit/math/advanced/functions/test_base2_logarithm_ztest.c
    ../../test/ztest/unit/math/advanced/functions/test_scalar_power_ztest.c)

  zephyr_library_sources_ifdef(CONFIG_SOF_MATH_ZTEST ${MATH_ZTEST_SOURCES})
  zephyr_library_include_directories(../../test/cmocka/include)

  zephyr_library_sources_ifdef(CONFIG_COMP_ASRC audio/test_asrc.c)
  zephyr_library_sources_ifdef(CONFIG_COMP_TONE audio/test_tone.c)
  zephyr_library_sources_ifdef(CONFIG_COMP_MIXIN_MIXOUT audio/test_mixin_mixout.c)
  zephyr_library_sources_ifdef(CONFIG_COMP_UP_DOWN_MIXER audio/test_up_down_mixer.c)
  zephyr_library_sources_ifdef(CONFIG_COMP_ARIA audio/test_aria.c)
  zephyr_library_sources_ifdef(CONFIG_COMP_MFCC audio/test_mfcc.c)
  zephyr_library_sources_ifdef(CONFIG_COMP_CROSSOVER audio/test_crossover.c)
  zephyr_library_sources_ifdef(CONFIG_COMP_DCBLOCK audio/test_dcblock.c)
  zephyr_library_sources_ifdef(CONFIG_COMP_MULTIBAND_DRC audio/test_multiband_drc.c)
  zephyr_library_sources_ifdef(CONFIG_COMP_LEVEL_MULTIPLIER audio/test_level_multiplier.c)
  zephyr_library_sources_ifdef(CONFIG_COMP_RTNR audio/test_rtnr.c)
  zephyr_library_sources_ifdef(CONFIG_COMP_SMART_AMP audio/test_smart_amp.c)
  zephyr_library_sources_ifdef(CONFIG_COMP_TDFB audio/test_tdfb.c)
  zephyr_library_sources_ifdef(CONFIG_COMP_STFT_PROCESS audio/test_stft_process.c)
  zephyr_library_sources_ifdef(CONFIG_COMP_GOOGLE_RTC_AUDIO_PROCESSING audio/test_google.c)
  zephyr_library_sources_ifdef(CONFIG_COMP_IGO_NR audio/test_igo_nr.c)
  zephyr_library_sources_ifdef(CONFIG_COMP_NXP_EAP audio/test_nxp.c)
  zephyr_library_sources_ifdef(CONFIG_COMP_TENSORFLOW audio/test_tensorflow.c)
  zephyr_library_sources_ifdef(CONFIG_DTS_CODEC audio/test_codec.c)
  zephyr_library_sources_ifdef(CONFIG_COMP_SOUND_DOSE audio/test_sound_dose.c)
  zephyr_library_sources_ifdef(CONFIG_COMP_EQ_FIR audio/test_eq_fir.c)
  zephyr_library_sources_ifdef(CONFIG_COMP_EQ_IIR audio/test_eq_iir.c)

  # Rust template module ZTest. Cross-builds the all-Rust SOF audio
  # module crate (sof/src/audio/rust_template/rust) as a staticlib
  # and links it into the firmware ZTest app, so the C test in
  # audio/test_rust_template.c can directly drive the function
  # pointers in `rust_template_interface`. See
  # CONFIG_RUST_TEMPLATE_ZTEST help for context.
  if(CONFIG_RUST_TEMPLATE_ZTEST)
    set(_sof_root ${ZEPHYR_CURRENT_MODULE_DIR})
    set(_rust_template_dir ${_sof_root}/src/audio/rust_template/rust)
    include(${_sof_root}/scripts/cmake/rust.cmake)

    if(DEFINED ENV{SOF_RUST_XTENSA_TARGET})
      set(_rust_target $ENV{SOF_RUST_XTENSA_TARGET})
    else()
      set(_rust_target $ENV{HOME}/work/rust/xtensa-intel_ace30_adsp-zephyr-elf.json)
    endif()

    sof_rust_staticlib(rust_template_rust
      CRATE_DIR ${_rust_template_dir}
      TARGET    ${_rust_target}
      BUILD_STD
      STRIP_COMPILER_BUILTINS
    )
    zephyr_library_sources(audio/test_rust_template.c)
    # Out-of-line wrappers for the static-inline SOF source/sink helpers
    # so the Rust crate can resolve them at link time.
    zephyr_library_sources(${_sof_root}/rust/sof_module/c_shim/sof_rust_source_sink.c)
    # Out-of-line wrappers for the static-inline mod_alloc / mod_free
    # helpers, so the sof_module global allocator (and any Rust code
    # using Box/Vec/String during a callback) resolves cleanly.
    zephyr_library_sources(${_sof_root}/rust/sof_module/c_shim/sof_rust_mod_alloc.c)
    # Out-of-line accessors for struct bind_info fields, used by
    # sof_module::BindData<'_> in the bind/unbind callbacks.
    zephyr_library_sources(${_sof_root}/rust/sof_module/c_shim/sof_rust_bind.c)
    # Zephyr LOG shims so the Rust crate's sof_rust_template_log_*
    # references resolve in the ZTest firmware (the llext build of
    # rust_template.c isn't pulled into the test ELF).
    zephyr_library_sources(${_sof_root}/src/audio/rust_template/rust_template_log.c)
    zephyr_library_link_libraries(rust_template_rust)
  endif()

  # IPC4 pipeline tests
  zephyr_library_sources(userspace/test_ipc4_pipeline_native.c
                         userspace/test_ipc4_pipeline_util.c)
  zephyr_library_sources_ifdef(CONFIG_USERSPACE
                               userspace/test_ipc4_pipeline_user.c)

  set_source_files_properties(${MATH_ZTEST_SOURCES}
    PROPERTIES COMPILE_DEFINITIONS "CONFIG_NUMBERS_VECTOR_FIND=1;CONFIG_NUMBERS_NORM=1;UNIT_TEST=1"
  )
endif()
