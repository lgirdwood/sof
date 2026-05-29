// SPDX-License-Identifier: BSD-3-Clause
//
// HiFi intrinsic unit tests for intel_ace15_adsp (HiFi4)
//
// Build:   see test_hifi_intrin_ace15_build.sh
// Purpose: bit-exact output for QEMU/HW cross-check
//
// Each test prints lines of the form:
//   [CLASS] name: 0xHHHHHHHH
// Run on QEMU/HW standalone or as an LLEXT module — outputs must be identical.

#include <stdint.h>
#include <xtensahifiintrin.h>

#ifdef LL_EXTENSION_BUILD
// ---------------------------------------------------------------------------
// LLEXT build: printk is resolved from the Zephyr kernel at load time.
// ---------------------------------------------------------------------------
#include <zephyr/sys/printk.h>
#define TEST_PRINTF printk
#elif defined(SOF_HIFI_INTRIN_BUILTIN)
// ---------------------------------------------------------------------------
// Built-in Zephyr firmware build: printk goes to the DSP log.
// ---------------------------------------------------------------------------
#include <zephyr/sys/printk.h>
#include <zephyr/init.h>
#define TEST_PRINTF printk
#else
// ---------------------------------------------------------------------------
// Bare-metal build: stdout via Xtensa simcall (QEMU/HW semihosting)
// simcall a2=SYS_write(4), a3=fd=1 (stdout), a4=buf, a5=len
// ---------------------------------------------------------------------------
#include <stdio.h>

static int _stdout_put(char c, FILE *f)
{
    (void)f;
    char buf = c;
    __asm__ volatile (
        "movi  a2, 4\n"   /* SYS_write */
        "movi  a3, 1\n"   /* fd = 1 (stdout) */
        "mov   a4, %0\n"  /* buf */
        "movi  a5, 1\n"   /* len = 1 */
        "simcall\n"
        : : "r"(&buf) : "a2", "a3", "a4", "a5"
    );
    return 0;
}

static int _stdout_flush(FILE *f) { (void)f; return 0; }

static FILE _stdout_file = FDEV_SETUP_STREAM(_stdout_put, NULL, _stdout_flush,
                                              _FDEV_SETUP_WRITE);
FILE *const stdout = &_stdout_file;
FILE *const stderr = &_stdout_file;
FILE *const stdin  = &_stdout_file;
#define TEST_PRINTF printf
#endif /* LL_EXTENSION_BUILD */

static int g_fail;

// -------------------------------------------------------------------------
// Logging helpers
// -------------------------------------------------------------------------
#define LOG32(cls, name, val) \
    TEST_PRINTF("[" cls "] " name ": 0x%08x\n", (unsigned int)(val))

#define LOG64(cls, name, val) do { \
    unsigned int _h = (unsigned int)AE_MOVAD32_H(AE_MOVINT32X2_FROMINT64(val)); \
    unsigned int _l = (unsigned int)AE_MOVAD32_L(AE_MOVINT32X2_FROMINT64(val)); \
    TEST_PRINTF("[" cls "] " name ": 0x%08x%08x\n", _h, _l); \
} while (0)

#define LOG_AE32X2(cls, name, val) do { \
    unsigned int _h = (unsigned int)AE_MOVAD32_H(val); \
    unsigned int _l = (unsigned int)AE_MOVAD32_L(val); \
    TEST_PRINTF("[" cls "] " name ": 0x%08x_%08x\n", _h, _l); \
} while (0)

// -------------------------------------------------------------------------
// Helper: extract low 16 bits of each lane of ae_int16x4
// -------------------------------------------------------------------------
static unsigned int ae16x4_lane(ae_int16x4 v, int n)
{
    /* Store to memory and reload to extract a lane */
    ae_int16x4 tmp = v;
    short buf[4];
    ae_int16x4 *p = (ae_int16x4 *)buf;
    ae_valign align = AE_ZALIGN64();
    AE_SA16X4_IP(tmp, align, p);
    AE_SA64POS_FP(align, p);
    return (unsigned short)buf[3 - n]; /* HiFi is big-endian lane order */
}

// -------------------------------------------------------------------------
// CLASS: zero_init
// -------------------------------------------------------------------------
static void test_zero_init(void)
{
    ae_int32x2 z32  = AE_ZERO32();
    ae_int64   z64  = AE_ZERO64();
    ae_p24x2s  zp48 = AE_ZEROP48();
    ae_q56s    zq56 = AE_ZEROQ56();
    ae_int16x4 z16  = AE_ZERO16();

    LOG_AE32X2("zero_init", "AE_ZERO32",    z32);
    LOG64      ("zero_init", "AE_ZERO64",    z64);
    /* p48 and q56 — extract via move to int32x2 */
    ae_int32x2 tmp_p = (ae_int32x2)zp48;
    LOG_AE32X2("zero_init", "AE_ZEROP48",   tmp_p);
    ae_int32x2 tmp_q = AE_ROUND32X2F64SSYM(zq56, zq56);
    LOG_AE32X2("zero_init", "AE_ZEROQ56",   tmp_q);
    (void)z16; /* no easy scalar extract, just confirm it compiles */
    TEST_PRINTF("[zero_init] AE_ZERO16: compiled\n");
}

// -------------------------------------------------------------------------
// CLASS: move_convert
// -------------------------------------------------------------------------
static void test_move_convert(void)
{
    ae_int32x2 a = AE_MOVDA32X2(0x12345678u, 0x9ABCDEF0u);
    LOG_AE32X2("move_convert", "AE_MOVDA32X2(H,L)", a);

    ae_int32x2 b = AE_MOVDA32(0xCAFEBABEu);
    LOG_AE32X2("move_convert", "AE_MOVDA32", b);

    unsigned int ah = (unsigned int)AE_MOVAD32_H(a);
    unsigned int al = (unsigned int)AE_MOVAD32_L(a);
    LOG32("move_convert", "AE_MOVAD32_H", ah);
    LOG32("move_convert", "AE_MOVAD32_L", al);

    ae_int32x2 c = AE_MOVDA32X2(0x00010002u, 0x00030004u);
    ae_int16x4 d = AE_MOVINT16X4_FROMINT32X2(c);
    (void)d;
    TEST_PRINTF("[move_convert] AE_MOVINT16X4_FROMINT32X2: compiled\n");

    ae_int32x2 e = AE_MOVINT32X2_FROMINT64(AE_ZERO64());
    LOG_AE32X2("move_convert", "AE_MOVINT32X2_FROMINT64", e);

    ae_int64 f64 = AE_MOVINT64_FROMINT32X2(a);
    LOG64("move_convert", "AE_MOVINT64_FROMINT32X2", f64);
}

// -------------------------------------------------------------------------
// CLASS: arith32
// -------------------------------------------------------------------------
static void test_arith32(void)
{
    ae_int32x2 a = AE_MOVDA32X2(0x10000000u, 0x20000000u);
    ae_int32x2 b = AE_MOVDA32X2(0x01234567u, 0x0FEDCBAu);

    ae_int32x2 add  = AE_ADD32(a, b);
    ae_int32x2 adds = AE_ADD32S(a, b);
    ae_int32x2 sub  = AE_SUB32(a, b);
    ae_int32x2 subs = AE_SUB32S(a, b);
    ae_int32x2 neg  = AE_NEG32S(a);
    ae_int32x2 abss = AE_ABS32S(AE_NEG32S(b));
    ae_int32x2 mx   = AE_MAX32(a, b);
    ae_int32x2 mn   = AE_MIN32(a, b);
    ae_int32x2 hl   = AE_ADD32_HL_LH(a, b);

    LOG_AE32X2("arith32", "AE_ADD32",          add);
    LOG_AE32X2("arith32", "AE_ADD32S",         adds);
    LOG_AE32X2("arith32", "AE_SUB32",          sub);
    LOG_AE32X2("arith32", "AE_SUB32S",         subs);
    LOG_AE32X2("arith32", "AE_NEG32S",         neg);
    LOG_AE32X2("arith32", "AE_ABS32S",         abss);
    LOG_AE32X2("arith32", "AE_MAX32",          mx);
    LOG_AE32X2("arith32", "AE_MIN32",          mn);
    LOG_AE32X2("arith32", "AE_ADD32_HL_LH",    hl);
}

// -------------------------------------------------------------------------
// CLASS: arith64
// -------------------------------------------------------------------------
static void test_arith64(void)
{
    ae_int32x2 a32 = AE_MOVDA32X2(0x00000001u, 0xFFFFFFFFu);
    ae_int32x2 b32 = AE_MOVDA32X2(0x00000002u, 0x00000001u);
    ae_int64 a64 = AE_MOVINT64_FROMINT32X2(a32);
    ae_int64 b64 = AE_MOVINT64_FROMINT32X2(b32);

    ae_int64 add = AE_ADD64S(a64, b64);
    ae_int64 sub = AE_SUB64(a64, b64);

    LOG64("arith64", "AE_ADD64S", add);
    LOG64("arith64", "AE_SUB64",  sub);
}

// -------------------------------------------------------------------------
// CLASS: shift32
// -------------------------------------------------------------------------
static void test_shift32(void)
{
    ae_int32x2 a = AE_MOVDA32X2(0x01000000u, 0x00800001u);
    ae_int32x2 cnt4 = AE_MOVDA32(4);

    ae_int32x2 slai4  = AE_SLAI32(a, 4);
    ae_int32x2 slai4s = AE_SLAI32S(a, 4);
    ae_int32x2 srai4  = AE_SRAI32(a, 4);
    ae_int32x2 srai4r = AE_SRAI32R(a, 4);
    ae_int32x2 slaa   = AE_SLAA32(a, cnt4);
    ae_int32x2 slaas  = AE_SLAA32S(a, cnt4);
    ae_int32x2 sraa   = AE_SRAA32(a, cnt4);
    ae_int32x2 sraasc = AE_SRAA32S(a, cnt4);

    LOG_AE32X2("shift32", "AE_SLAI32",   slai4);
    LOG_AE32X2("shift32", "AE_SLAI32S",  slai4s);
    LOG_AE32X2("shift32", "AE_SRAI32",   srai4);
    LOG_AE32X2("shift32", "AE_SRAI32R",  srai4r);
    LOG_AE32X2("shift32", "AE_SLAA32",   slaa);
    LOG_AE32X2("shift32", "AE_SLAA32S",  slaas);
    LOG_AE32X2("shift32", "AE_SRAA32",   sraa);
    LOG_AE32X2("shift32", "AE_SRAA32S",  sraasc);
}

// -------------------------------------------------------------------------
// CLASS: shift64
// -------------------------------------------------------------------------
static void test_shift64(void)
{
    ae_int64 a = AE_MOVINT64_FROMINT32X2(AE_MOVDA32X2(0x00400000u, 0x80000001u));
    ae_int32x2 cnt = AE_MOVDA32(4);

    ae_int64 slai  = AE_SLAI64(a, 4);
    ae_int64 slais = AE_SLAI64S(a, 4);
    ae_int64 srai  = AE_SRAI64(a, 4);
    ae_int64 sraa  = AE_SRAA64(a, cnt);
    ae_int64 slaas = AE_SLAA64S(a, cnt);

    LOG64("shift64", "AE_SLAI64",  slai);
    LOG64("shift64", "AE_SLAI64S", slais);
    LOG64("shift64", "AE_SRAI64",  srai);
    LOG64("shift64", "AE_SRAA64",  sraa);
    LOG64("shift64", "AE_SLAA64S", slaas);
}

// -------------------------------------------------------------------------
// CLASS: multiply32
// -------------------------------------------------------------------------
static void test_multiply32(void)
{
    ae_int32x2 a = AE_MOVDA32X2(0x00010000u, 0x00020000u);
    ae_int32x2 b = AE_MOVDA32X2(0x00030000u, 0x00040000u);

    ae_int64 hh = AE_MUL32_HH(a, b);
    ae_int64 ll = AE_MUL32_LL(a, b);

    LOG64("multiply32", "AE_MUL32_HH", hh);
    LOG64("multiply32", "AE_MUL32_LL", ll);

    ae_int64 acc = AE_ZERO64();
    AE_MULAF32S_HH(acc, a, b);
    LOG64("multiply32", "AE_MULAF32S_HH(0+HH)", acc);

    acc = AE_ZERO64();
    AE_MULAF32S_LL(acc, a, b);
    LOG64("multiply32", "AE_MULAF32S_LL(0+LL)", acc);

    acc = AE_ZERO64();
    AE_MULAF32S_LH(acc, a, b);
    LOG64("multiply32", "AE_MULAF32S_LH(0+LH)", acc);

    acc = AE_ZERO64();
    AE_MULAF32S_HL(acc, a, b);
    LOG64("multiply32", "AE_MULAF32S_HL(0+HL)", acc);

    ae_int64 fhh = AE_MULF32S_HH(a, b);
    ae_int64 fll = AE_MULF32S_LL(a, b);
    LOG64("multiply32", "AE_MULF32S_HH", fhh);
    LOG64("multiply32", "AE_MULF32S_LL", fll);

    ae_int64 frhh = AE_MULF32R_HH(a, b);
    ae_int64 frll = AE_MULF32R_LL(a, b);
    LOG64("multiply32", "AE_MULF32R_HH", frhh);
    LOG64("multiply32", "AE_MULF32R_LL", frll);

    /* MAC with rounding: AE_MULAF32R_HH / AE_MULAF32R_LL */
    acc = AE_ZERO64();
    AE_MULAF32R_HH(acc, a, b);
    LOG64("multiply32", "AE_MULAF32R_HH(0+HH_R)", acc);

    acc = AE_ZERO64();
    AE_MULAF32R_LL(acc, a, b);
    LOG64("multiply32", "AE_MULAF32R_LL(0+LL_R)", acc);

    /* MULSF: MAC subtract */
    ae_int64 base = AE_MUL32_HH(a, b);
    AE_MULSF32S_LL(base, a, b);
    LOG64("multiply32", "AE_MULSF32S_LL(HH-LL)", base);
}

// -------------------------------------------------------------------------
// CLASS: multiply16
// -------------------------------------------------------------------------
static void test_multiply16(void)
{
    ae_int16x4 a = AE_MOVINT16X4_FROMINT32X2(AE_MOVDA32X2(0x00010002u, 0x00030004u));
    ae_int16x4 b = AE_MOVINT16X4_FROMINT32X2(AE_MOVDA32X2(0x00050006u, 0x00070008u));

    ae_int32x2 r00 = AE_MULF16SS_00(a, b);
    LOG_AE32X2("multiply16", "AE_MULF16SS_00", r00);

    ae_int64 acc = AE_ZERO64();
    AE_MULAF16SS_00(acc, a, b);
    LOG64("multiply16", "AE_MULAF16SS_00(0+)", acc);

    acc = AE_ZERO64();
    AE_MULAF16SS_11(acc, a, b);
    LOG64("multiply16", "AE_MULAF16SS_11(0+)", acc);

    acc = AE_ZERO64();
    AE_MULAF16SS_22(acc, a, b);
    LOG64("multiply16", "AE_MULAF16SS_22(0+)", acc);

    acc = AE_ZERO64();
    AE_MULAF16SS_33(acc, a, b);
    LOG64("multiply16", "AE_MULAF16SS_33(0+)", acc);

    /* AE_MULFP16X4S: multiply Q15 pairs, produce Q30 in ae_int32x2 */
    ae_int32x2 fp16 = AE_MULFP16X4S(a, b);
    LOG_AE32X2("multiply16", "AE_MULFP16X4S", fp16);
}

// -------------------------------------------------------------------------
// CLASS: round_sat
// -------------------------------------------------------------------------
static void test_round_sat(void)
{
    /* AE_ROUND32F64SSYM: round 64-bit to 32-bit, symmetric */
    ae_int64 v64 = AE_MOVINT64_FROMINT32X2(AE_MOVDA32X2(0x00000001u, 0x80000000u));
    ae_int32x2 r32 = AE_ROUND32F64SSYM(v64);
    LOG_AE32X2("round_sat", "AE_ROUND32F64SSYM", r32);

    ae_int64 v64b = AE_MOVINT64_FROMINT32X2(AE_MOVDA32X2(0x00000002u, 0x40000000u));
    ae_int32x2 r32x2 = AE_ROUND32X2F64SSYM(v64, v64b);
    LOG_AE32X2("round_sat", "AE_ROUND32X2F64SSYM", r32x2);

    /* AE_ROUND16X4F32SSYM / SASYM */
    ae_int32x2 a32 = AE_MOVDA32X2(0x00010000u, 0x00020000u);
    ae_int32x2 b32 = AE_MOVDA32X2(0x00030000u, 0x00040000u);
    ae_int16x4 r16s = AE_ROUND16X4F32SSYM(a32, b32);
    ae_int16x4 r16a = AE_ROUND16X4F32SASYM(a32, b32);
    /* extract via int32x2 reinterpret */
    ae_int32x2 s16s = (ae_int32x2)r16s;
    ae_int32x2 s16a = (ae_int32x2)r16a;
    LOG_AE32X2("round_sat", "AE_ROUND16X4F32SSYM_raw", s16s);
    LOG_AE32X2("round_sat", "AE_ROUND16X4F32SASYM_raw", s16a);

    /* AE_SAT24S: saturate 32→24 */
    ae_int32x2 big = AE_MOVDA32X2(0x00FFFFFFu, 0xFF000000u);
    ae_int32x2 sat = AE_SAT24S(big);
    LOG_AE32X2("round_sat", "AE_SAT24S", sat);
}

// -------------------------------------------------------------------------
// CLASS: select
// -------------------------------------------------------------------------
static void test_select(void)
{
    ae_int32x2 a = AE_MOVDA32X2(0xAAAAAAAAu, 0xBBBBBBBBu);
    ae_int32x2 b = AE_MOVDA32X2(0xCCCCCCCCu, 0xDDDDDDDDu);

    LOG_AE32X2("select", "AE_SEL32_HH", AE_SEL32_HH(a, b));
    LOG_AE32X2("select", "AE_SEL32_HL", AE_SEL32_HL(a, b));
    LOG_AE32X2("select", "AE_SEL32_LH", AE_SEL32_LH(a, b));
    LOG_AE32X2("select", "AE_SEL32_LL", AE_SEL32_LL(a, b));
}

// -------------------------------------------------------------------------
// CLASS: sign_extend
// -------------------------------------------------------------------------
static void test_sign_extend(void)
{
    /* AE_SEXT32X2D16_10 / _32: sign-extend 16-bit lanes to 32-bit */
    ae_int32x2 raw = AE_MOVDA32X2(0x00018001u, 0x7FFF8000u);
    ae_int16x4 v16 = AE_MOVINT16X4_FROMINT32X2(raw);
    ae_int32x2 sx10 = AE_SEXT32X2D16_10(v16);
    ae_int32x2 sx32 = AE_SEXT32X2D16_32(v16);
    LOG_AE32X2("sign_extend", "AE_SEXT32X2D16_10", sx10);
    LOG_AE32X2("sign_extend", "AE_SEXT32X2D16_32", sx32);

    /* AE_NSAZ32_L: number of sign bits minus 1 for low lane */
    ae_int32x2 x = AE_MOVDA32(0x00100000u);
    int nsaz = AE_NSAZ32_L(x);
    LOG32("sign_extend", "AE_NSAZ32_L(0x00100000)", nsaz);

    /* AE_CVT32X2F16_10 / _32: zero-extend low/high 16 bits of v16 to 32 */
    ae_int32x2 cv10 = AE_CVT32X2F16_10(v16);
    ae_int32x2 cv32 = AE_CVT32X2F16_32(v16);
    LOG_AE32X2("sign_extend", "AE_CVT32X2F16_10", cv10);
    LOG_AE32X2("sign_extend", "AE_CVT32X2F16_32", cv32);

    /* AE_CVT16X4: saturate 32→16 for two ae_int32x2 */
    ae_int32x2 c = AE_MOVDA32X2(0x00010002u, 0x00030004u);
    ae_int16x4 cv4 = AE_CVT16X4(raw, c);
    ae_int32x2 cv4r = (ae_int32x2)cv4;
    LOG_AE32X2("sign_extend", "AE_CVT16X4_raw", cv4r);
}

// -------------------------------------------------------------------------
// CLASS: load_store_imm  (immediate-offset loads/stores)
// -------------------------------------------------------------------------
static void test_load_store_imm(void)
{
    /* Setup: 8-aligned buffer, load/store pairs */
    ae_int32x2 buf[4];
    ae_int32x2 *ptr;

    /* AE_S32X2_I / AE_L32X2_I */
    ae_int32x2 v = AE_MOVDA32X2(0xDEADBEEFu, 0xCAFEBABEu);
    ptr = buf;
    AE_S32X2_I(v, ptr, 0);
    ae_int32x2 r = AE_L32X2_I(ptr, 0);
    LOG_AE32X2("load_store_imm", "S32X2_I/L32X2_I", r);

    /* AE_S32X2_IP / AE_L32X2_IP */
    ptr = buf;
    AE_S32X2_IP(v, ptr, 8);
    ptr = buf;
    ae_int32x2 rip;
    AE_L32X2_IP(rip, ptr, 8);
    LOG_AE32X2("load_store_imm", "S32X2_IP/L32X2_IP", rip);

    /* AE_S32_L_I / AE_L32_I */
    ae_int32x2 sv = AE_MOVDA32X2(0, 0x11223344u);
    ptr = buf;
    AE_S32_L_I(sv, (ae_int32 *)ptr, 0);
    ae_int32x2 lr;
    ae_int32 *p32 = (ae_int32 *)buf;
    lr = AE_L32_I(p32, 0);
    LOG_AE32X2("load_store_imm", "S32_L_I/L32_I", lr);

    /* AE_S32_L_IP / AE_L32_IP */
    ptr = buf;
    ae_int32 *pi32 = (ae_int32 *)ptr;
    ae_int32x2 sv2 = AE_MOVDA32X2(0, 0xAABBCCDDu);
    AE_S32_L_IP(sv2, pi32, 4);
    pi32 = (ae_int32 *)buf;
    ae_int32x2 lrip;
    AE_L32_IP(lrip, pi32, 4);
    LOG_AE32X2("load_store_imm", "S32_L_IP/L32_IP", lrip);

    /* AE_L32_X */
    pi32 = (ae_int32 *)buf;
    ae_int32x2 lx;
    lx = AE_L32_X(pi32, 4);
    LOG_AE32X2("load_store_imm", "AE_L32_X(offset=4)", lx);

    /* AE_L32_XP */
    pi32 = (ae_int32 *)buf;
    ae_int32x2 lxp;
    AE_L32_XP(lxp, pi32, 4);
    LOG_AE32X2("load_store_imm", "AE_L32_XP(offset=4)", lxp);

    /* AE_S32_L_XP */
    pi32 = (ae_int32 *)buf;
    ae_int32x2 sv3 = AE_MOVDA32X2(0, 0x55AA55AAu);
    AE_S32_L_XP(sv3, pi32, 4);
    pi32 = (ae_int32 *)buf;
    ae_int32x2 after_s32xp;
    AE_L32_IP(after_s32xp, pi32, 4);
    LOG_AE32X2("load_store_imm", "S32_L_XP/L32_IP", after_s32xp);

    /* AE_S32_L_IP: immediate increment variant */
    pi32 = (ae_int32 *)buf;
    ae_int32x2 sv4 = AE_MOVDA32X2(0, 0xFEFEFEFEu);
    AE_S32_L_IP(sv4, pi32, 4);
    pi32 = (ae_int32 *)buf;
    ae_int32x2 after_s32ip;
    AE_L32_IP(after_s32ip, pi32, 4);
    LOG_AE32X2("load_store_imm", "S32_L_IP/L32_IP_verify", after_s32ip);

    /* AE_S32_L_IP with out-of-range offset → combine to ae_s32.l.xp */
    pi32 = (ae_int32 *)buf;
    ae_int32x2 sv5 = AE_MOVDA32X2(0, 0x12121212u);
    AE_S32_L_IP(sv5, pi32, 24); /* max in-range is 28, 24 is fine */
    pi32 = (ae_int32 *)buf;
    ae_int32x2 after_oor;
    AE_L32_IP(after_oor, pi32, 4);
    LOG_AE32X2("load_store_imm", "S32_L_IP(offset=24)/L32_IP", after_oor);
}

// -------------------------------------------------------------------------
// CLASS: load_store_16  (16-bit loads/stores)
// -------------------------------------------------------------------------
static void test_load_store_16(void)
{
    short buf[16] __attribute__((aligned(8)));
    int i;
    for (i = 0; i < 16; i++)
        buf[i] = (short)(0x1000 + i);

    ae_int16x4 v16;
    ae_int16x4 *p16 = (ae_int16x4 *)buf;

    /* AE_L16X4_IP */
    AE_L16X4_IP(v16, p16, 8);
    ae_int32x2 r16i = (ae_int32x2)v16;
    LOG_AE32X2("load_store_16", "AE_L16X4_IP", r16i);

    /* AE_S16X4_IP */
    ae_int16x4 sv16 = AE_MOVINT16X4_FROMINT32X2(AE_MOVDA32X2(0xAABBCCDDu, 0xEEFF0011u));
    p16 = (ae_int16x4 *)buf;
    AE_S16X4_IP(sv16, p16, 8);
    p16 = (ae_int16x4 *)buf;
    ae_int16x4 back;
    AE_L16X4_IP(back, p16, 8);
    ae_int32x2 back32 = (ae_int32x2)back;
    LOG_AE32X2("load_store_16", "S16X4_IP/L16X4_IP", back32);

    /* AE_L16_I */
    ae_int16 *p16s = (ae_int16 *)buf;
    ae_int32x2 r16;
    r16 = AE_L16_I(p16s, 0);
    LOG_AE32X2("load_store_16", "AE_L16_I(0)", r16);

    /* AE_L16_IP */
    p16s = (ae_int16 *)buf;
    ae_int32x2 r16ip;
    AE_L16_IP(r16ip, p16s, 2);
    LOG_AE32X2("load_store_16", "AE_L16_IP(2)", r16ip);

    /* AE_S16_0_IP */
    short sbuf[8] __attribute__((aligned(8)));
    ae_int16 *sp16 = (ae_int16 *)sbuf;
    ae_int32x2 sv = AE_MOVDA32(0x5A5A5A5Au);
    AE_S16_0_IP(sv, sp16, 2);
    p16s = (ae_int16 *)sbuf;
    ae_int32x2 sr;
    sr = AE_L16_I(p16s, 0);
    LOG_AE32X2("load_store_16", "S16_0_IP/L16_I", sr);
}

// -------------------------------------------------------------------------
// CLASS: align_load_store (SIMD alignment helpers)
// -------------------------------------------------------------------------
static void test_align_load_store(void)
{
    /* AE_LA64_PP / AE_LA16X4_IP pattern: unaligned load */
    short buf[8] __attribute__((aligned(8)));
    int i;
    for (i = 0; i < 8; i++)
        buf[i] = (short)(0x0100 + i);

    const ae_int16x4 *up = (const ae_int16x4 *)buf;
    ae_valign align = AE_LA64_PP(up);
    ae_int16x4 v;
    AE_LA16X4_IP(v, align, up);
    ae_int32x2 r = (ae_int32x2)v;
    LOG_AE32X2("align_ls", "LA64_PP/LA16X4_IP", r);

    /* AE_ZALIGN64 / AE_SA16X4_IP / AE_SA64POS_FP: aligned store */
    short obuf[8] __attribute__((aligned(8)));
    ae_int16x4 *op = (ae_int16x4 *)obuf;
    ae_valign oalign = AE_ZALIGN64();
    ae_int16x4 sv = AE_MOVINT16X4_FROMINT32X2(AE_MOVDA32X2(0xAABBCCDDu, 0xEEFF0011u));
    AE_SA16X4_IP(sv, oalign, op);
    AE_SA64POS_FP(oalign, op);
    /* Read back */
    const ae_int16x4 *rp = (const ae_int16x4 *)obuf;
    ae_valign ra = AE_LA64_PP(rp);
    ae_int16x4 rb;
    AE_LA16X4_IP(rb, ra, rp);
    ae_int32x2 rbr = (ae_int32x2)rb;
    LOG_AE32X2("align_ls", "ZALIGN64/SA16X4_IP/SA64POS_FP/LA16X4_IP", rbr);

    /* AE_LA32X2_IP */
    int ibuf[4] __attribute__((aligned(8)));
    ibuf[0] = 0x12345678; ibuf[1] = 0x9ABCDEF0;
    ibuf[2] = 0x11223344; ibuf[3] = 0x55667788;
    const ae_int32x2 *i32p = (const ae_int32x2 *)ibuf;
    ae_valign ia = AE_LA64_PP(i32p);
    ae_int32x2 iv;
    AE_LA32X2_IP(iv, ia, i32p);
    LOG_AE32X2("align_ls", "LA64_PP/LA32X2_IP", iv);

    /* AE_SA32X2_IP / AE_SA64POS_FP */
    int obuf32[4] __attribute__((aligned(8)));
    ae_int32x2 *o32p = (ae_int32x2 *)obuf32;
    ae_valign oa = AE_ZALIGN64();
    AE_SA32X2_IP(iv, oa, o32p);
    AE_SA64POS_FP(oa, o32p);
    const ae_int32x2 *ro32 = (const ae_int32x2 *)obuf32;
    ae_valign roa = AE_LA64_PP(ro32);
    ae_int32x2 rov;
    AE_LA32X2_IP(rov, roa, ro32);
    LOG_AE32X2("align_ls", "ZALIGN64/SA32X2_IP/SA64POS_FP/LA32X2_IP", rov);
}

// -------------------------------------------------------------------------
// CLASS: mac_fir  (FIR/accumulate patterns from iir/fir files)
// -------------------------------------------------------------------------
static void test_mac_fir(void)
{
    /* AE_MULAAFD32X16_H1_L0 / H3_L2: accumulate 32x16 cross */
    ae_int32x2 x32 = AE_MOVDA32X2(0x00010000u, 0x00020000u);
    ae_int16x4 c16 = AE_MOVINT16X4_FROMINT32X2(AE_MOVDA32X2(0x00030004u, 0x00050006u));
    ae_int64 acc = AE_ZERO64();
    AE_MULAAFD32X16_H1_L0(acc, x32, c16);
    LOG64("mac_fir", "AE_MULAAFD32X16_H1_L0", acc);

    acc = AE_ZERO64();
    AE_MULAAFD32X16_H3_L2(acc, x32, c16);
    LOG64("mac_fir", "AE_MULAAFD32X16_H3_L2", acc);

    /* AE_MULAFD32X16X2_FIR_HH / HL */
    ae_int64 q0 = AE_ZERO64(), q1 = AE_ZERO64();
    ae_int32x2 d0 = AE_MOVDA32X2(0x00010000u, 0x00020000u);
    ae_int32x2 d1 = AE_MOVDA32X2(0x00030000u, 0x00040000u);
    AE_MULAFD32X16X2_FIR_HH(q0, q1, d0, d1, c16);
    LOG64("mac_fir", "AE_MULAFD32X16X2_FIR_HH q0", q0);
    LOG64("mac_fir", "AE_MULAFD32X16X2_FIR_HH q1", q1);

    q0 = AE_ZERO64(); q1 = AE_ZERO64();
    AE_MULAFD32X16X2_FIR_HL(q0, q1, d0, d1, c16);
    LOG64("mac_fir", "AE_MULAFD32X16X2_FIR_HL q0", q0);
    LOG64("mac_fir", "AE_MULAFD32X16X2_FIR_HL q1", q1);

    /* AE_MULAFP32X2RS / AE_MULFP32X2RS */
    ae_int32x2 fa = AE_MOVDA32X2(0x10000000u, 0x20000000u);
    ae_int32x2 fb = AE_MOVDA32X2(0x40000000u, 0x20000000u);
    ae_int32x2 acc32 = AE_ZERO32();
    AE_MULAFP32X2RS(acc32, fa, fb);
    LOG_AE32X2("mac_fir", "AE_MULAFP32X2RS(0+)", acc32);

    ae_int32x2 mfp = AE_MULFP32X2RS(fa, fb);
    LOG_AE32X2("mac_fir", "AE_MULFP32X2RS", mfp);

    /* AE_MULAFP32X16X2RS_H / _L */
    ae_int32x2 acc32b = AE_ZERO32();
    AE_MULAFP32X16X2RS_H(acc32b, fa, c16);
    LOG_AE32X2("mac_fir", "AE_MULAFP32X16X2RS_H(0+)", acc32b);

    acc32b = AE_ZERO32();
    AE_MULAFP32X16X2RS_L(acc32b, fa, c16);
    LOG_AE32X2("mac_fir", "AE_MULAFP32X16X2RS_L(0+)", acc32b);

    /* AE_MULFP32X16X2RS_H / _L */
    ae_int32x2 mfp16h = AE_MULFP32X16X2RS_H(fa, c16);
    ae_int32x2 mfp16l = AE_MULFP32X16X2RS_L(fa, c16);
    LOG_AE32X2("mac_fir", "AE_MULFP32X16X2RS_H", mfp16h);
    LOG_AE32X2("mac_fir", "AE_MULFP32X16X2RS_L", mfp16l);

    /* AE_MULFP24X2R */
    ae_p24x2s p24a = AE_MOVINT24X2_FROMF32X2(fa);
    ae_p24x2s p24b = AE_MOVINT24X2_FROMF32X2(fb);
    ae_int64 mfp24 = AE_MULFP24X2R(p24a, p24b);
    LOG64("mac_fir", "AE_MULFP24X2R", mfp24);
}

// -------------------------------------------------------------------------
// CLASS: p24_ops  (24-bit arithmetic)
// -------------------------------------------------------------------------
static void test_p24_ops(void)
{
    ae_int32x2 a32 = AE_MOVDA32X2(0x00ABCD00u, 0x00123400u);
    ae_int32x2 b32 = AE_MOVDA32X2(0x00010200u, 0x00030400u);
    ae_p24x2s pa = AE_MOVINT24X2_FROMF32X2(a32);
    ae_p24x2s pb = AE_MOVINT24X2_FROMF32X2(b32);

    ae_p24x2s add24 = AE_ADD24S(pa, pb);
    ae_p24x2s sub24 = AE_SUB24S(pa, pb);

    ae_int32x2 radd = (ae_int32x2)add24;
    ae_int32x2 rsub = (ae_int32x2)sub24;
    LOG_AE32X2("p24_ops", "AE_ADD24S", radd);
    LOG_AE32X2("p24_ops", "AE_SUB24S", rsub);

    /* AE_CVT48A32: convert ae_int32x2 to ae_int64 (p48) */
    ae_int64 cvt48 = AE_CVT48A32(a32);
    LOG64("p24_ops", "AE_CVT48A32", cvt48);

    /* AE_CVTQ48A32S: saturating convert ae_int32x2 to q56 */
    ae_q56s q56a = AE_CVTQ48A32S(a32);
    ae_int32x2 rq = AE_ROUND32X2F64SSYM(q56a, q56a);
    LOG_AE32X2("p24_ops", "AE_CVTQ48A32S_round", rq);

    /* AE_SAT24S already tested in round_sat, skip */

    /* AE_ROUND24X2F48SSYM */
    ae_p24x2s r24 = AE_ROUND24X2F48SSYM(cvt48, cvt48);
    ae_int32x2 r24i = (ae_int32x2)r24;
    LOG_AE32X2("p24_ops", "AE_ROUND24X2F48SSYM", r24i);

    /* AE_ROUND32F48SSYM / SASYM */
    ae_int32x2 r32ss = AE_ROUND32F48SSYM(cvt48);
    ae_int32x2 r32sa = AE_ROUND32F48SASYM(cvt48);
    LOG_AE32X2("p24_ops", "AE_ROUND32F48SSYM", r32ss);
    LOG_AE32X2("p24_ops", "AE_ROUND32F48SASYM", r32sa);
}

// -------------------------------------------------------------------------
// CLASS: arith16 (16-bit arithmetic)
// -------------------------------------------------------------------------
static void test_arith16(void)
{
    ae_int16x4 a = AE_MOVINT16X4_FROMINT32X2(AE_MOVDA32X2(0x0001000Au, 0x000B000Cu));
    ae_int16x4 b = AE_MOVINT16X4_FROMINT32X2(AE_MOVDA32X2(0x0002000Bu, 0x000C000Du));

    ae_int16x4 add = AE_ADD16S(a, b);
    ae_int16x4 neg = AE_NEG16S(a);

    ae_int32x2 radd = (ae_int32x2)add;
    ae_int32x2 rneg = (ae_int32x2)neg;
    LOG_AE32X2("arith16", "AE_ADD16S", radd);
    LOG_AE32X2("arith16", "AE_NEG16S", rneg);

    /* AE_SLAI16S / AE_SRAA16RS */
    ae_int16x4 sl = AE_SLAI16S(a, 4);
    ae_int16x4 sr = AE_SRAA16RS(a, 4);
    LOG_AE32X2("arith16", "AE_SLAI16S(4)", (ae_int32x2)(sl));
    LOG_AE32X2("arith16", "AE_SRAA16RS(4)", (ae_int32x2)(sr));

    /* AE_SLAI24S / AE_SLAI16S */
    ae_p24x2s pa24 = AE_MOVINT24X2_FROMF32X2(AE_MOVDA32X2(0x00010000u, 0x00020000u));
    ae_p24x2s sl24 = AE_SLAI24S(pa24, 2);
    LOG_AE32X2("arith16", "AE_SLAI24S(2)", (ae_int32x2)(sl24));

    /* AE_SLAA16S */
    ae_int16x4 dyn_cnt = AE_MOVINT16X4_FROMINT32X2(AE_MOVDA32(4));
    ae_int16x4 slaa = AE_SLAA16S(a, dyn_cnt);
    LOG_AE32X2("arith16", "AE_SLAA16S(4)", (ae_int32x2)(slaa));
}

// -------------------------------------------------------------------------
// CLASS: bitwise  (AND64/OR64/XOR64 on ae_int64 — HiFi3 has no AND32/OR32/XOR32)
// -------------------------------------------------------------------------
static void test_bitwise(void)
{
    ae_int64 a = AE_MOVINT64_FROMINT32X2(AE_MOVDA32X2(0x0F0F0F0Fu, 0xAAAAAAAAu));
    ae_int64 b = AE_MOVINT64_FROMINT32X2(AE_MOVDA32X2(0x00FF00FFu, 0x55555555u));
    ae_int64 ones = AE_MOVINT64_FROMINT32X2(AE_MOVDA32X2(0xFFFFFFFFu, 0xFFFFFFFFu));

    LOG64("bitwise", "AE_AND64",         AE_AND64(a, b));    /* 0x000F000F00000000 */
    LOG64("bitwise", "AE_OR64",          AE_OR64(a, b));     /* 0x0FFF0FFFFFFFFFFF */
    LOG64("bitwise", "AE_XOR64",         AE_XOR64(a, b));    /* 0x0FF00FF0FFFFFFFF */
    LOG64("bitwise", "AE_XOR64(NOT a)",  AE_XOR64(a, ones)); /* 0xF0F0F0F055555555 */
}

// -------------------------------------------------------------------------
// CLASS: load_store_64  (64-bit loads/stores)
// -------------------------------------------------------------------------
static void test_load_store_64(void)
{
    ae_int64 buf[4];
    ae_int64 *p64;
    ae_int64 v = AE_MOVINT64_FROMINT32X2(AE_MOVDA32X2(0xDEADBEEFu, 0xCAFEBABEu));

    /* AE_S64_I / AE_L64_I  — immediate offset */
    p64 = buf;
    AE_S64_I(v, p64, 0);
    LOG64("load_store_64", "S64_I/L64_I",    AE_L64_I(p64, 0)); /* 0xDEADBEEFCAFEBABE */

    /* AE_S64_IP / AE_L64_IP  — immediate offset with post-increment */
    p64 = buf;
    AE_S64_IP(v, p64, 8);   /* store at buf[0], p64 → buf[1] */
    p64 = buf;
    ae_int64 rip;
    AE_L64_IP(rip, p64, 8); /* load from buf[0], p64 → buf[1] */
    LOG64("load_store_64", "S64_IP/L64_IP",  rip);               /* 0xDEADBEEFCAFEBABE */

    /* AE_L64_XP  — register offset with post-update */
    p64 = &buf[1];
    AE_S64_I(v, p64, 0);     /* store v at buf[1] */
    p64 = &buf[1];
    ae_int64 rxp;
    AE_L64_XP(rxp, p64, -8); /* load from buf[1], p64 → buf[0] */
    LOG64("load_store_64", "L64_XP(ofs=-8)", rxp);               /* 0xDEADBEEFCAFEBABE */
}

// -------------------------------------------------------------------------
// CLASS: arith64_ext  (AE_ADD64 non-sat wrap, AE_MAXABS32S)
// -------------------------------------------------------------------------
static void test_arith64_ext(void)
{
    ae_int64 a = AE_MOVINT64_FROMINT32X2(AE_MOVDA32X2(0x00000001u, 0x00000000u)); /* +2^32 */
    ae_int64 b = AE_MOVINT64_FROMINT32X2(AE_MOVDA32X2(0xFFFFFFFFu, 0xFFFFFFFEu)); /* -2 */
    ae_int64 max64 = AE_MOVINT64_FROMINT32X2(AE_MOVDA32X2(0x7FFFFFFFu, 0xFFFFFFFFu));
    ae_int64 one   = AE_MOVINT64_FROMINT32X2(AE_MOVDA32X2(0x00000000u, 0x00000001u));

    /* AE_ADD64: non-saturating, wraps on overflow (contrast with ADD64S in test_arith64) */
    LOG64("arith64_ext", "AE_ADD64(a+b)",       AE_ADD64(a, b));       /* 0x00000000FFFFFFFE */
    LOG64("arith64_ext", "AE_ADD64(MAX64+1)",   AE_ADD64(max64, one)); /* 0x8000000000000000 */

    /* AE_MAXABS32S: max(|a_lane|, |b_lane|) saturating */
    ae_int32x2 ma = AE_MOVDA32X2(-5, 3);   /* H=-5, L=3  */
    ae_int32x2 mb = AE_MOVDA32X2(4, -7);   /* H=4,  L=-7 */
    LOG_AE32X2("arith64_ext", "AE_MAXABS32S",   AE_MAXABS32S(ma, mb)); /* H=5, L=7 */
}

// -------------------------------------------------------------------------
// CLASS: arith16_ext  (SUB16S, ABS16S — extend arith16 with remaining ops)
// -------------------------------------------------------------------------
static void test_arith16_ext(void)
{
    /* a: lanes {-10, 5, -3, 8}, b: lanes {2, 3, 1, 2} */
    ae_int16x4 a = AE_MOVINT16X4_FROMINT32X2(AE_MOVDA32X2(0xFFF60005u, 0xFFFD0008u));
    ae_int16x4 b = AE_MOVINT16X4_FROMINT32X2(AE_MOVDA32X2(0x00020003u, 0x00010002u));

    /* AE_SUB16S: a - b lane-wise, saturating */
    ae_int16x4 sub = AE_SUB16S(a, b);
    LOG_AE32X2("arith16_ext", "AE_SUB16S", (ae_int32x2)sub); /* {-12,2,-4,6}→0xFFF40002_FFFC0006 */

    /* AE_ABS16S: |a| lane-wise, saturating (no |INT16_MIN| overflow) */
    ae_int16x4 abs16 = AE_ABS16S(a);
    LOG_AE32X2("arith16_ext", "AE_ABS16S", (ae_int32x2)abs16); /* {10,5,3,8}→0x000A0005_00030008 */
}

// -------------------------------------------------------------------------
// CLASS: mulsf32_ext  (MULSF32S_LH and MULSF32S_HH — cross-product subtract-MACs)
// -------------------------------------------------------------------------
static void test_mulsf32_ext(void)
{
    ae_int32x2 a = AE_MOVDA32X2(0x00010000u, 0x00020000u);
    ae_int32x2 b = AE_MOVDA32X2(0x00030000u, 0x00040000u);

    /* MULSF32S_LH: acc -= frac_sat(L(a) × H(b) × 2) */
    ae_int64 acc = AE_MUL32_HH(a, b);  /* start: 0x0000000300000000 */
    AE_MULSF32S_LH(acc, a, b);
    LOG64("mulsf32_ext", "MULSF32S_LH(HH-LH)", acc);

    /* MULSF32S_HH: acc -= frac_sat(H(a) × H(b) × 2) */
    acc = AE_MUL32_LL(a, b);           /* start: 0x0000000800000000 */
    AE_MULSF32S_HH(acc, a, b);
    LOG64("mulsf32_ext", "MULSF32S_HH(LL-HH)", acc);
}

// -------------------------------------------------------------------------
// CLASS: mul32_ext  (MUL32_LH cross-lane, MULA32_HH non-sat acc, MULS32_LL non-sat sub)
// -------------------------------------------------------------------------
static void test_mul32_ext(void)
{
    ae_int32x2 a = AE_MOVDA32X2(0x00010000u, 0x00020000u);
    ae_int32x2 b = AE_MOVDA32X2(0x00030000u, 0x00040000u);

    /* AE_MUL32_LH: L(a) × H(b) = 0x00020000 × 0x00030000 */
    LOG64("mul32_ext", "AE_MUL32_LH(L*H)", AE_MUL32_LH(a, b)); /* 0x0000000600000000 */

    /* AE_MULA32_HH: acc += H(a) × H(b) = 0 + 1×3 ×(2^16)² */
    ae_int64 acc = AE_ZERO64();
    AE_MULA32_HH(acc, a, b);
    LOG64("mul32_ext", "MULA32_HH(0+1*3)", acc); /* 0x0000000300000000 */

    /* AE_MULS32_LL: acc -= L(a) × L(b) = 3×2^32 - 2×4×2^32 = -5×2^32 */
    AE_MULS32_LL(acc, a, b);
    LOG64("mul32_ext", "MULS32_LL(3-2*4)", acc); /* 0xFFFFFFFB00000000 */
}

// -------------------------------------------------------------------------
// CLASS: sat16_conv  (AE_SAT16X4, AE_CLAMPS16)
// -------------------------------------------------------------------------
static void test_sat16_convert(void)
{
    /* AE_SAT16X4: clip two ae_int32x2 lanes to int16 range and pack */
    ae_int32x2 hi = AE_MOVDA32X2(0x00010000u, 0xFFFE0000u); /* +65536→+32767, -131072→-32768 */
    ae_int32x2 lo = AE_MOVDA32(100);                        /* both lanes = 100 */
    ae_int16x4 sat = AE_SAT16X4(hi, lo);
    LOG_AE32X2("sat16_conv", "AE_SAT16X4", (ae_int32x2)sat); /* 0x7FFF8000_00640064 */

    /* AE_TRUNC16X4F32: take high 16 bits of each Q31 lane → ae_int16x4 */
    ae_int32x2 t_hi = AE_MOVDA32X2(0x00010000u, 0x00020000u);
    ae_int32x2 t_lo = AE_MOVDA32X2(0x00030000u, 0x00040000u);
    LOG_AE32X2("sat16_conv", "AE_TRUNC16X4F32", (ae_int32x2)AE_TRUNC16X4F32(t_hi, t_lo)); /* 0x00010002_00030004 */
}

// -------------------------------------------------------------------------
// CLASS: bitwise16  (AND16/OR16/XOR16 and non-saturating ADD16/SUB16)
// -------------------------------------------------------------------------
static void test_bitwise16(void)
{
    ae_int16x4 a = AE_MOVINT16X4_FROMINT32X2(AE_MOVDA32X2(0x0F0F1234u, 0xAAAA5678u));
    ae_int16x4 b = AE_MOVINT16X4_FROMINT32X2(AE_MOVDA32X2(0xF0F05678u, 0x5555AAAAu));
    LOG_AE32X2("bitwise16", "AE_AND16", (ae_int32x2)AE_AND16(a, b)); /* 0x00001230_00000228 */
    LOG_AE32X2("bitwise16", "AE_OR16",  (ae_int32x2)AE_OR16(a, b));  /* 0xFFFF567C_FFFFfefa */
    LOG_AE32X2("bitwise16", "AE_XOR16", (ae_int32x2)AE_XOR16(a, b)); /* 0xFFFF444C_FFFFfcd2 */

    /* Non-saturating: wraps on overflow (contrast with ADD16S/SUB16S) */
    ae_int16x4 c = AE_MOVINT16X4_FROMINT32X2(AE_MOVDA32X2(0x7FFE8001u, 0x00020003u));
    ae_int16x4 d = AE_MOVINT16X4_FROMINT32X2(AE_MOVDA32X2(0x00020002u, 0x00010001u));
    LOG_AE32X2("bitwise16", "AE_ADD16", (ae_int32x2)AE_ADD16(c, d)); /* 0x80008003_00030004 */
    LOG_AE32X2("bitwise16", "AE_SUB16", (ae_int32x2)AE_SUB16(c, d)); /* 0x7FFC7FFF_00010002 */
}

// -------------------------------------------------------------------------
// CLASS: fir_remain  (FIR_LH and FIR_LL — complete the 32x16 FIR family)
// -------------------------------------------------------------------------
static void test_fir_remain(void)
{
    ae_int32x2 d0 = AE_MOVDA32X2(0x00010000u, 0x00020000u);
    ae_int32x2 d1 = AE_MOVDA32X2(0x00030000u, 0x00040000u);
    ae_int16x4 c  = AE_MOVINT16X4_FROMINT32X2(AE_MOVDA32X2(0x00030004u, 0x00050006u));

    ae_int64 q0 = AE_ZERO64(), q1 = AE_ZERO64();
    AE_MULAFD32X16X2_FIR_LH(q0, q1, d0, d1, c);
    LOG64("fir_remain", "FIR_LH q0", q0);
    LOG64("fir_remain", "FIR_LH q1", q1);

    q0 = AE_ZERO64(); q1 = AE_ZERO64();
    AE_MULAFD32X16X2_FIR_LL(q0, q1, d0, d1, c);
    LOG64("fir_remain", "FIR_LL q0", q0);
    LOG64("fir_remain", "FIR_LL q1", q1);
}

// -------------------------------------------------------------------------
// CLASS: circ_buf  (AE_SETCBEGIN0/SETCEND0, AE_L32_XC, AE_S32_L_XC)
// -------------------------------------------------------------------------
static void test_circ_buf(void)
{
    /* 4-element circular buffer aligned to its own size (16 bytes) */
    ae_int32 cbuf[4] __attribute__((aligned(16)));
    int i;

    for (i = 0; i < 4; i++)
        cbuf[i] = (ae_int32)(0x10000000 * (i + 1));
    /* cbuf = { 0x10000000, 0x20000000, 0x30000000, 0x40000000 } */

    ae_int32 *p = &cbuf[3];
    AE_SETCBEGIN0(cbuf);
    AE_SETCEND0(cbuf + 4);

    ae_int32x2 v0, v1;
    AE_L32_XC(v0, p, 4); /* load cbuf[3]=0x40000000, wrap → cbuf[0] */
    AE_L32_XC(v1, p, 4); /* load cbuf[0]=0x10000000, advance → cbuf[1] */
    LOG_AE32X2("circ_buf", "L32_XC cbuf[3]", v0); /* L=0x40000000 */
    LOG_AE32X2("circ_buf", "L32_XC cbuf[0]", v1); /* L=0x10000000 */

    /* AE_S32_L_XC: store L-lane at current pos (cbuf[1]), advance */
    ae_int32x2 sv = AE_MOVDA32X2(0, 0xCAFEBABEu);
    AE_S32_L_XC(sv, p, 4); /* store 0xCAFEBABE at cbuf[1], → cbuf[2] */
    ae_int32x2 chk = AE_L32_I((ae_int32 *)cbuf, 4); /* verify cbuf[1] */
    LOG_AE32X2("circ_buf", "S32_L_XC cbuf[1]", chk); /* L=0xCAFEBABE */

    /* Restore circular buffer regs */
    AE_SETCBEGIN0((ae_int32 *)0);
    AE_SETCEND0((ae_int32 *)0);
}

// -------------------------------------------------------------------------
// CLASS: circ_64  (AE_L32X2_XC / AE_S32X2_XC — 64-bit circular load/store)
// Two 32-bit samples per access; typical delay-line inner loop pattern
// -------------------------------------------------------------------------
static void test_circ_64(void)
{
    ae_int32x2 cbuf[4] __attribute__((aligned(32)));
    cbuf[0] = AE_MOVDA32X2(0x11111111u, 0x22222222u);
    cbuf[1] = AE_MOVDA32X2(0x33333333u, 0x44444444u);
    cbuf[2] = AE_MOVDA32X2(0x55555555u, 0x66666666u);
    cbuf[3] = AE_MOVDA32X2(0x77777777u, 0x88888888u);

    ae_int32x2 *p = &cbuf[3];
    AE_SETCBEGIN0((ae_int32 *)cbuf);
    AE_SETCEND0((ae_int32 *)(cbuf + 4));

    int stride = (int)sizeof(ae_int32x2); /* 8 — must be a register operand */
    ae_int32x2 v0, v1;
    AE_L32X2_XC(v0, p, stride); /* load cbuf[3], wrap → cbuf[0] */
    AE_L32X2_XC(v1, p, stride); /* load cbuf[0], → cbuf[1] */
    LOG_AE32X2("circ_64", "L32X2_XC cbuf[3]", v0); /* 0x77777777_88888888 */
    LOG_AE32X2("circ_64", "L32X2_XC cbuf[0]", v1); /* 0x11111111_22222222 */

    ae_int32x2 sv = AE_MOVDA32X2(0xDEADBEEFu, 0xCAFEBABEu);
    AE_S32X2_XC(sv, p, stride); /* store at cbuf[1], → cbuf[2] */
    ae_int32x2 chk = AE_L32X2_I(cbuf, 8); /* verify cbuf[1] */
    LOG_AE32X2("circ_64", "S32X2_XC cbuf[1]", chk); /* 0xDEADBEEF_CAFEBABE */

    AE_SETCBEGIN0((ae_int32 *)0);
    AE_SETCEND0((ae_int32 *)0);
}

// -------------------------------------------------------------------------
// CLASS: circ_16  (AE_L16_XC — 16-bit sample circular load)
// CB0-backed; 16-bit signed sample loaded and sign-extended to ae_int32x2
// -------------------------------------------------------------------------
static void test_circ_16(void)
{
    short sbuf[4] __attribute__((aligned(8)));
    sbuf[0] = 0x1234; sbuf[1] = 0x5678;
    sbuf[2] = (short)0xABCD; sbuf[3] = (short)0xFFFF;

    ae_int16 *sp = (ae_int16 *)&sbuf[3];
    AE_SETCBEGIN0(sbuf);
    AE_SETCEND0(sbuf + 4);

    ae_int32x2 s0, s1;
    AE_L16_XC(s0, sp, 2); /* load sbuf[3]=0xFFFF=-1, wrap → sbuf[0] */
    AE_L16_XC(s1, sp, 2); /* load sbuf[0]=0x1234,  → sbuf[1] */
    LOG_AE32X2("circ_16", "L16_XC sbuf[3]", s0); /* sign-ext -1 */
    LOG_AE32X2("circ_16", "L16_XC sbuf[0]", s1); /* sign-ext 0x1234 */

    AE_SETCBEGIN0((ae_int32 *)0);
    AE_SETCEND0((ae_int32 *)0);
}

// -------------------------------------------------------------------------
// CLASS: movad16  (AE_MOVAD16_0/1/2/3 — direct 16-bit lane extraction)
// Faster than the memory-round-trip ae16x4_lane helper
// -------------------------------------------------------------------------
static void test_movad16(void)
{
    /* lanes: {0=0x0001, 1=0x7FFF, 2=0x8000, 3=0xFFFF} */
    ae_int16x4 v = AE_MOVINT16X4_FROMINT32X2(AE_MOVDA32X2(0x00017FFFu, 0x8000FFFFu));
    LOG32("movad16", "MOVAD16_0", (unsigned int)AE_MOVAD16_0(v)); /* 0x00000001 */
    LOG32("movad16", "MOVAD16_1", (unsigned int)AE_MOVAD16_1(v)); /* 0x00007FFF */
    LOG32("movad16", "MOVAD16_2", (unsigned int)AE_MOVAD16_2(v)); /* 0xFFFF8000 (signed -32768) */
    LOG32("movad16", "MOVAD16_3", (unsigned int)AE_MOVAD16_3(v)); /* 0xFFFFFFFF (signed -1) */
}

// -------------------------------------------------------------------------
// CLASS: mul16x4  (AE_MUL16X4, AE_SLAA64, AE_SRAA32RS)
// -------------------------------------------------------------------------
static void test_mul16x4(void)
{
    /* AE_MUL16X4: SIMD 16×16→32 multiply, 4 products into 2 ae_int32x2 */
    ae_int16x4 data  = AE_MOVINT16X4_FROMINT32X2(AE_MOVDA32X2(0x00010002u, 0x00030004u));
    ae_int16x4 gains = AE_MOVINT16X4_FROMINT32X2(AE_MOVDA32X2(0x00050006u, 0x00070008u));
    /* lanes: data={1,2,3,4}, gains={5,6,7,8} */
    ae_int32x2 p_hi, p_lo;
    AE_MUL16X4(p_hi, p_lo, data, gains);
    LOG_AE32X2("mul16x4", "AE_MUL16X4 hi", p_hi); /* {1*5=5, 2*6=12} 0x00000005_0000000c */
    LOG_AE32X2("mul16x4", "AE_MUL16X4 lo", p_lo); /* {3*7=21,4*8=32} 0x00000015_00000020 */

    /* AE_SLAA64: non-saturating 64-bit dynamic left shift */
    ae_int64 v64 = AE_MOVINT64_FROMINT32X2(AE_MOVDA32X2(0x00000001u, 0x00000000u));
    ae_int32x2 cnt = AE_MOVDA32(4);
    LOG64("mul16x4", "AE_SLAA64(2^32,4)", AE_SLAA64(v64, cnt)); /* 0x0000001000000000 */

    /* AE_SRAA32RS: dynamic right shift with round + saturation */
    ae_int32x2 x = AE_MOVDA32X2(0x00000100u, 0xFFFFFF00u); /* +256, -256 */
    ae_int32x2 sh = AE_MOVDA32(4);
    LOG_AE32X2("mul16x4", "AE_SRAA32RS(x,4)", AE_SRAA32RS(x, sh)); /* 0x00000010_fffffff0 */
}

// -------------------------------------------------------------------------
// Test runner — called from both main() and the LLEXT entry point
// -------------------------------------------------------------------------
static void run_all_tests(void)
{
    TEST_PRINTF("=== HiFi Intrinsic Tests: intel_ace15_adsp (HiFi4) ===\n");

    test_zero_init();
    test_move_convert();
    test_arith32();
    test_arith64();
    test_shift32();
    test_shift64();
    test_multiply32();
    test_multiply16();
    test_round_sat();
    test_select();
    test_sign_extend();
    test_load_store_imm();
    test_load_store_16();
    test_align_load_store();
    test_mac_fir();
    test_p24_ops();
    test_arith16();
    test_bitwise();
    test_load_store_64();
    test_arith64_ext();
    test_arith16_ext();
    test_mulsf32_ext();
    test_mul32_ext();
    test_sat16_convert();
    test_bitwise16();
    test_fir_remain();
    test_circ_buf();
    test_circ_64();
    test_circ_16();
    test_movad16();
    test_mul16x4();

    TEST_PRINTF("=== TOTAL FAILURES: %d ===\n", g_fail);
}

#ifdef LL_EXTENSION_BUILD
// -------------------------------------------------------------------------
// LLEXT entry point — called via llext_call_fn(ext, "hifi_intrin_test_run")
// -------------------------------------------------------------------------
void hifi_intrin_test_run(void *arg)
{
    (void)arg;
    g_fail = 0;
    run_all_tests();
}

// -------------------------------------------------------------------------
// LLEXT constructor — runs tests automatically via sof llext ctor
// -------------------------------------------------------------------------
static void __attribute__((constructor)) hifi_intrin_test_ctor(void)
{
    g_fail = 0;
    run_all_tests();
}

// -------------------------------------------------------------------------
// LLEXT module manifest — required by rimage for signing
// -------------------------------------------------------------------------
#include <module/module/api_ver.h>
#include <module/module/llext.h>
#include <rimage/sof/user/manifest.h>
#include <sof/lib/uuid.h>

SOF_DEFINE_REG_UUID(hifi_intrin_test);

static const struct sof_man_module_manifest mod_manifest __section(".module") __used =
	SOF_LLEXT_MODULE_MANIFEST("HIFITEST", hifi_intrin_test_run, 1,
				  SOF_REG_UUID(hifi_intrin_test), 1);

SOF_LLEXT_BUILDINFO;

#elif defined(SOF_HIFI_INTRIN_BUILTIN)
// -------------------------------------------------------------------------
// Built-in firmware mode — SYS_INIT runs all tests at APPLICATION level
// -------------------------------------------------------------------------
static int hifi_intrin_test_builtin_init(void)
{
    g_fail = 0;
    run_all_tests();
    return 0;
}
SYS_INIT(hifi_intrin_test_builtin_init, APPLICATION, 90);

#else
// -------------------------------------------------------------------------
// Bare-metal entry point
// -------------------------------------------------------------------------
int main(void)
{
    run_all_tests();
    return g_fail ? 1 : 0;
}
#endif /* LL_EXTENSION_BUILD */
