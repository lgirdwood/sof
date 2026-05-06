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

if(CONFIG_SOF_BOOT_TEST AND CONFIG_SOF_USERSPACE_INTERFACE_DMA)
  if(CONFIG_DT_HAS_INTEL_ADSP_HDA_HOST_IN_ENABLED)
    zephyr_library_sources(userspace/test_intel_hda_dma.c)
  endif()
  if(CONFIG_DT_HAS_INTEL_ADSP_HDA_SSP_CAP_ENABLED)
    zephyr_library_sources(userspace/test_intel_ssp_dai.c)
  endif()
endif()

if(CONFIG_SOF_BOOT_TEST AND CONFIG_SOF_USERSPACE_LL)
  zephyr_library_sources(userspace/test_mailbox.c)
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

  set_source_files_properties(${MATH_ZTEST_SOURCES}
    PROPERTIES COMPILE_DEFINITIONS "CONFIG_NUMBERS_VECTOR_FIND=1;CONFIG_NUMBERS_NORM=1;UNIT_TEST=1"
  )
endif()
