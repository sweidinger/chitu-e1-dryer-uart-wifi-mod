#!/usr/bin/env python3
"""
Build a self-contained update.GZH with all patches relocated into the
original firmware footprint.

All handler code is placed at flash 0x08024434 (FUN_0801c434 = unused
G-code file crypto handler, 966 bytes available).

Uses XOR-difference on the original GZH for bulletproof encryption.
Output is same-size as original → bootloader guaranteed to accept it.
"""
import struct, sys, ctypes

# ── Input/output files ──
ORIG_GZH   = "update.GZH.orig"
ORIG_PLAIN = "chitu_e1_update_decrypted.bin"
OUTPUT_GZH = sys.argv[1] if len(sys.argv) > 1 else "update_patched.GZH"

# ── Handler relocation target ──
# FUN_08013b84: print progress display page (896B). Never shown on a dryer.
# Referenced via callback table only. BX LR stub at entry = harmless no-op.
STUB_FLASH    = 0x0801BB84   # BX LR stub at function entry
HANDLER_FLASH = 0x0801BB88   # Our code starts 4 bytes after stub (aligned)
HANDLER_FW    = 0x13B88      # Offset in firmware file
HANDLER_MAX   = 896 - 4      # 892 bytes available

# ── Flash addresses of firmware functions we call ──
FLASH_EXIT       = 0x08017A06
FLASH_FUN_01594  = 0x08009594   # sensor 0 read
FLASH_FUN_015DA  = 0x080095DA   # sensor 1 read
FLASH_FUN_C3A8   = 0x080243A8   # UART printf
FLASH_FUN_A968   = 0x08021968   # set thermal zone target
FLASH_FUN_F480   = 0x08027480   # stop drying / cleanup
FLASH_FUN_F2E8   = 0x080272E8   # FUN_0801f2e8 — drying cycle init (PID, timer, state)
FLASH_FUN_01B0   = 0x080281B0   # box presence check
FLASH_FUN_01E8   = 0x080281E8   # heating state check
FLASH_M6501_ORIG = 0x080175EE   # original M6501 handler

# ── SRAM pointers ──
SRAM_MAIN_STATE = 0x200002C8
SRAM_SETTINGS   = 0x20000324

# ── Patch locations (firmware file offsets) ──
# These are the small patches (trampolines/injections) that point to our handlers
PATCH_M105_CALLBACK   = 0x1D020   # literal pool: callback fn ptr → M105 handler
PATCH_M105_LITERAL    = 0x1389C   # "M105" → "MXXX" (disable RX interception)
PATCH_M105_TRAMPOLINE = 0x0F172   # M105 dispatch → B.W to M105 handler
PATCH_DRYER_INJECT    = 0x0F4FE   # M6xxx fallthrough → B.W to dryer router

# ── Firmware version ──
PATCH_VERSION = 0x16166    # "1.1.8" in "V 1.1.8" string
VERSION_OLD   = b'1.1.8'
VERSION_NEW   = b'1.3.0'

# ── Offsets ──
OFF_HEAT_ENABLE = 0xD4
OFF_SETPOINT_Z0 = 0x380
OFF_HEATER_PWM0 = 0x1374
OFF_HEATER_PWM1 = 0x137C


def encode_branch(from_addr, to_addr, link=False):
    offset = to_addr - (from_addr + 4)
    assert -16777216 <= offset <= 16777214, f"Branch out of range: {offset}"
    val = offset if offset >= 0 else offset + (1 << 25)
    S = (offset >> 24) & 1
    I1, I2 = (val >> 23) & 1, (val >> 22) & 1
    imm10, imm11 = (val >> 12) & 0x3FF, (val >> 1) & 0x7FF
    J1, J2 = (~(I1 ^ S)) & 1, (~(I2 ^ S)) & 1
    hw1 = 0xF000 | (S << 10) | imm10
    hw2 = (0xD000 if link else 0x9000) | (J1 << 13) | (J2 << 11) | imm11
    return struct.pack('<HH', hw1, hw2)


class Thumb:
    def __init__(self, base):
        self.code = bytearray()
        self.base = base
        self._ldr_fixups = []

    @property
    def pc(self): return self.base + len(self.code)

    def e16(self, hw): self.code += struct.pack('<H', hw)
    def e32(self, h1, h2): self.code += struct.pack('<HH', h1, h2)
    def ebl(self, t): self.code += encode_branch(self.pc, t, True)
    def ebw(self, t): self.code += encode_branch(self.pc, t, False)

    def emovw(self, rd, imm):
        i4, i1, i3, i8 = (imm>>12)&0xF, (imm>>11)&1, (imm>>8)&7, imm&0xFF
        self.e32(0xF240|(i1<<10)|i4, (i3<<12)|(rd<<8)|i8)

    def eldr_lit(self, rd, label):
        self._ldr_fixups.append((len(self.code), rd, label))
        self.e16(0x4800|(rd<<8))

    def align4(self):
        if len(self.code) % 4: self.e16(0xBF00)

    def emit_litpool(self, entries):
        """Emit literal pool and fix up all pending LDR references."""
        self.align4()
        pool = {}
        for name, val in entries:
            pool[name] = len(self.code)
            self.code += struct.pack('<I', val)
        for off, rd, label in self._ldr_fixups:
            poff = pool[label]
            ldr_flash = self.base + off
            pc_al = (ldr_flash + 4) & ~3
            imm = (self.base + poff) - pc_al
            assert 0 <= imm <= 1020 and imm % 4 == 0, f"LDR {label}: {imm}"
            struct.pack_into('<H', self.code, off, 0x4800|(rd<<8)|(imm//4))


def build_handlers():
    t = Thumb(HANDLER_FLASH)

    # ════════════════════════════════════════
    # M105 DISPATCH HANDLER (with retry)
    # Entered via B.W from dispatch trampoline
    # R0=cmd, R4=parsed_cmd, R7=ctx, SP+104=uart_ctx
    # ════════════════════════════════════════
    m105_handler = t.pc

    t.e16(0xB430)         # PUSH {R4, R5}
    t.e16(0xB086)         # SUB SP, #24
    # Stack: 8+24=32 → local_30 at SP+136

    t.e16(0x9C22)         # LDR R4, [SP, #136] → UART ctx

    # Sensor 0 with retry
    t.e16(0x2505)         # MOV R5, #5
    r0_pos = len(t.code)
    t.e16(0x4668); t.e16(0xA901)  # MOV R0,SP; ADD R1,SP,#4
    t.ebl(FLASH_FUN_01594)
    t.e16(0x9800); t.e16(0x287C); t.e16(0xD101)  # LDR R0,[SP]; CMP #124; BNE+2
    t.e16(0x3D01)         # SUBS R5, #1
    back = (r0_pos - (len(t.code)+2)) // 2
    t.e16(0xD100|(back & 0xFF))  # BNE retry

    # Sensor 1 with retry
    t.e16(0x2505)
    r1_pos = len(t.code)
    t.e16(0xA802); t.e16(0xA903)
    t.ebl(FLASH_FUN_015DA)
    t.e16(0x9802); t.e16(0x287C); t.e16(0xD101)
    t.e16(0x3D01)
    back = (r1_pos - (len(t.code)+2)) // 2
    t.e16(0xD100|(back & 0xFF))

    # Printf args
    t.e16(0x9A00); t.e16(0x9B01)  # R2=temp0, R3=humid0
    t.e16(0x9802); t.e16(0x9000)  # temp1 → [SP]
    t.e16(0x9803); t.e16(0x9001)  # humid1 → [SP+4]
    t.e16(0x4620)         # MOV R0, R4
    t.eldr_lit(1, 'fmt_m105')
    t.ebl(FLASH_FUN_C3A8)

    t.e16(0xB006); t.e16(0xBC30)  # ADD SP,#24; POP {R4,R5}
    t.ebw(FLASH_EXIT)

    # ════════════════════════════════════════
    # M105 CALLBACK HANDLER (from RX path)
    # R0=UART ctx, R1=UART ctx
    # ════════════════════════════════════════
    t.align4()
    m105_callback = t.pc

    t.e16(0xB5F0)         # PUSH {R4-R7, LR}
    t.e16(0xB086)         # SUB SP, #24
    t.e16(0x4604)         # MOV R4, R0

    # Sensor 0 with retry
    t.e16(0x2505)
    r0c = len(t.code)
    t.e16(0x4668); t.e16(0xA901)
    t.ebl(FLASH_FUN_01594)
    t.e16(0x9800); t.e16(0x287C); t.e16(0xD101)
    t.e16(0x3D01)
    back = (r0c - (len(t.code)+2)) // 2
    t.e16(0xD100|(back & 0xFF))

    # Sensor 1 with retry
    t.e16(0x2505)
    r1c = len(t.code)
    t.e16(0xA802); t.e16(0xA903)
    t.ebl(FLASH_FUN_015DA)
    t.e16(0x9802); t.e16(0x287C); t.e16(0xD101)
    t.e16(0x3D01)
    back = (r1c - (len(t.code)+2)) // 2
    t.e16(0xD100|(back & 0xFF))

    t.e16(0x9A00); t.e16(0x9B01)
    t.e16(0x9802); t.e16(0x9000)
    t.e16(0x9803); t.e16(0x9001)
    t.e16(0x4620)
    t.eldr_lit(1, 'fmt_m105')
    t.ebl(FLASH_FUN_C3A8)

    t.e16(0xB006); t.e16(0xBDF0)  # ADD SP,#24; POP {R4-R7, PC}

    # ════════════════════════════════════════
    # DRYER COMMAND ROUTER
    # ════════════════════════════════════════
    t.align4()
    dryer_router = t.pc

    # M6501 relay
    t.emovw(1, 0x1965); t.e16(0x4288); t.e16(0xD101)
    t.ebw(FLASH_M6501_ORIG)

    # M6050 check
    t.emovw(1, 0x17A2); t.e16(0x4288); t.e16(0xD101)
    m6050_bw = len(t.code); t.e32(0,0)  # placeholder

    # M6051 check
    t.emovw(1, 0x17A3); t.e16(0x4288); t.e16(0xD101)
    m6051_bw = len(t.code); t.e32(0,0)

    # M6052 check
    t.emovw(1, 0x17A4); t.e16(0x4288); t.e16(0xD101)
    m6052_bw = len(t.code); t.e32(0,0)

    # M6053 check (start zone 1)
    t.emovw(1, 0x17A5); t.e16(0x4288); t.e16(0xD101)
    m6053_bw = len(t.code); t.e32(0,0)

    # M6054 check (stop zone 1)
    t.emovw(1, 0x17A6); t.e16(0x4288); t.e16(0xD101)
    m6054_bw = len(t.code); t.e32(0,0)

    t.ebw(FLASH_EXIT)  # default

    # ════════════ START DRYING (shared logic) ════════════
    # M6050 enters with zone=0, M6053 enters with zone=1
    # Zone is passed in R3 (preserved across the handler)

    def emit_start_entry(bw_off, zone):
        """Emit entry stub that sets zone and falls through to shared code."""
        t.align4()
        addr = t.pc
        struct.pack_into('<4s', t.code, bw_off, encode_branch(HANDLER_FLASH+bw_off, addr))
        t.e16(0x2300 | zone)   # MOVS R3, #zone
        return addr

    m6050 = emit_start_entry(m6050_bw, 0)  # M6050 → zone 0
    # M6053 entry merges here after setting R3=1 (emitted below)
    m6050_shared = len(t.code)  # remember for M6053 branch target

    t.e16(0xB4F8); t.e16(0xB082)  # PUSH{R3,R4,R5,R6,R7}; SUB SP,#8
    # Stack: 20+8=28 → local_30 at SP+132
    # R3 (zone) is saved on stack at SP+8 (first pushed reg)

    t.e32(0xF8D4, 0x5030)   # LDR.W R5, [R4, #0x30] (I=temp)
    t.e32(0xF8D4, 0x6038)   # LDR.W R6, [R4, #0x38] (T=time)
    t.e16(0x461F)            # MOV R7, R3  (save zone in R7)

    # Write target temp as float to settings+0x30 (zone 0) or +0x40 (zone 1)
    t.eldr_lit(0, 'settings'); t.e16(0x6800)  # R0 = settings
    t.e32(0xEE00, 0x5A10)   # VMOV S0, R5
    t.e32(0xEEB8, 0x0AC0)   # VCVT.F32.S32 S0, S0
    # Offset = 0x30 + zone*0x10: compute R1 = 0x30 + R7*16
    t.e16(0x2130)            # MOVS R1, #0x30
    t.e32(0xEB01, 0x1107)    # ADD.W R1, R1, R7, LSL #4  (R1 = 0x30 or 0x40)
    # VSTR S0, [R0, R1] — no direct encoding, use ADD then VSTR [R0, #0]
    t.e16(0x1840)            # ADD R0, R0, R1
    t.e32(0xED80, 0x0A00)   # VSTR S0, [R0, #0]

    # Restore R0 = settings for timer writes
    t.eldr_lit(0, 'settings'); t.e16(0x6800)

    # Write timer: hours and minutes
    t.e16(0x213C)            # R1 = 60
    t.e32(0xFBB6, 0xF2F1)   # UDIV R2, R6, R1
    t.e32(0xF8C0, 0x2038)   # STR.W R2, [R0, #0x38]
    t.e32(0xFB02, 0x6611)   # MLS R6, R2, R1, R6
    t.e32(0xF8C0, 0x603C)   # STR.W R6, [R0, #0x3C]

    # Set heating enable flag
    t.eldr_lit(0, 'main_st'); t.e16(0x6800)
    t.e16(0x2101); t.e32(0xF8C0, 0x10D4)  # *(R0+0xD4) = 1

    # Call FUN_0801f2e8(zone)
    t.e16(0x4638)            # MOV R0, R7  (zone)
    t.ebl(FLASH_FUN_F2E8)

    # Response
    t.e32(0xF8D4, 0x5030)   # reload R5
    t.e16(0x9800|(132//4))   # LDR R0, [SP, #132] → UART ctx
    t.eldr_lit(1, 'fmt_6050')
    t.e16(0x462A)            # MOV R2, R5
    t.e32(0xF8D4, 0x3038)   # LDR.W R3, [R4, #0x38]
    t.ebl(FLASH_FUN_C3A8)

    t.e16(0xB002); t.e16(0xBCF8)  # ADD SP,#8; POP{R3,R4,R5,R6,R7}
    t.ebw(FLASH_EXIT)

    # M6053 entry → zone 1, then jump to shared code
    m6053 = emit_start_entry(m6053_bw, 1)
    t.ebw(HANDLER_FLASH + m6050_shared)  # B.W to shared start handler

    # ════════════ STOP DRYING (shared logic) ════════════
    # M6051 enters with zone=0, M6054 enters with zone=1

    def emit_stop_entry(bw_off, zone):
        t.align4()
        addr = t.pc
        struct.pack_into('<4s', t.code, bw_off, encode_branch(HANDLER_FLASH+bw_off, addr))
        t.e16(0x2300 | zone)  # MOVS R3, #zone
        return addr

    m6051 = emit_stop_entry(m6051_bw, 0)
    m6051_shared = len(t.code)

    t.e16(0xB418); t.e16(0xB082)  # PUSH{R3,R4}; SUB SP,#8
    # Stack: 8+8=16 → local_30 at SP+120

    # Clear heating enable + PWMs
    t.eldr_lit(0, 'main_st'); t.e16(0x6800)
    t.e16(0x2100)
    t.e32(0xF8C0, 0x10D4)
    t.e32(0xF8C0, 0x1000|OFF_HEATER_PWM0)
    t.e32(0xF8C0, 0x1000|OFF_HEATER_PWM1)

    # Call FUN_0801f480(zone)
    t.e16(0x4618)            # MOV R0, R3  (zone)
    t.ebl(FLASH_FUN_F480)

    t.e16(0x9800|(120//4))   # LDR R0, [SP, #120]
    t.eldr_lit(1, 'fmt_6051')
    t.ebl(FLASH_FUN_C3A8)

    t.e16(0xB002); t.e16(0xBC18)  # ADD SP,#8; POP{R3,R4}
    t.ebw(FLASH_EXIT)

    # M6054 entry → zone 1
    m6054 = emit_stop_entry(m6054_bw, 1)
    t.ebw(HANDLER_FLASH + m6051_shared)

    # ════════════ M6052 — STATUS QUERY ════════════
    t.align4()
    m6052 = t.pc
    struct.pack_into('<4s', t.code, m6052_bw, encode_branch(HANDLER_FLASH+m6052_bw, m6052))

    t.e16(0xB4F0); t.e16(0xB084)  # PUSH{R4-R7}; SUB SP,#16
    # Stack: 16+16=32 → local_30 at SP+136

    t.e16(0x2000); t.ebl(FLASH_FUN_01B0); t.e16(0x4604)
    t.e16(0x2001); t.ebl(FLASH_FUN_01B0); t.e16(0x4605)
    t.e16(0x2000); t.ebl(FLASH_FUN_01E8); t.e16(0x4606)
    t.e16(0x2001); t.ebl(FLASH_FUN_01E8); t.e16(0x4607)

    t.e16(0x9600); t.e16(0x9701)
    t.e16(0x9800|(136//4))
    t.eldr_lit(1, 'fmt_6052')
    t.e16(0x4622); t.e16(0x462B)
    t.ebl(FLASH_FUN_C3A8)

    t.e16(0xB004); t.e16(0xBCF0)
    t.ebw(FLASH_EXIT)

    # ════════════ LITERAL POOL + STRINGS ════════════
    fmt_m105_addr  = HANDLER_FLASH + len(t.code) + 6*4  # after 6 pool words + alignment
    # Pre-calculate string addresses
    strs = [
        ('fmt_m105',  b"ok T0:%d H0:%d T1:%d H1:%d\r\n\0"),
        ('fmt_6050',  b"ok Drying T:%d I:%d\r\n\0"),
        ('fmt_6051',  b"ok Drying stopped\r\n\0"),
        ('fmt_6052',  b"ok B0:%d B1:%d S0:%d S1:%d\r\n\0"),
    ]

    pool_entries = [
        ('main_st',  SRAM_MAIN_STATE),
        ('settings', SRAM_SETTINGS),
    ]
    # Calculate string positions
    t.align4()
    pool_start = len(t.code)
    n_pool = len(pool_entries) + len(strs)  # pointer entries
    str_base = t.base + pool_start + n_pool * 4
    pos = str_base
    for name, data in strs:
        while len(data) % 4: data += b'\0'
        pool_entries.append((name, pos))
        pos += len(data)

    t.emit_litpool(pool_entries)

    # Emit strings
    for _, data in strs:
        while len(data) % 4: data += b'\0'
        t.code += data

    print(f"Handler code: {len(t.code)} / {HANDLER_MAX} bytes ({100*len(t.code)/HANDLER_MAX:.0f}%)")
    assert len(t.code) <= HANDLER_MAX, f"Handlers too large! {len(t.code)} > {HANDLER_MAX}"

    return t.code, m105_handler, m105_callback, dryer_router


def main():
    print("Building relocatable GZH update...")

    with open(ORIG_GZH, 'rb') as f:
        gzh = bytearray(f.read())
    with open(ORIG_PLAIN, 'rb') as f:
        orig = f.read()

    # Build handlers
    handler_code, m105_dispatch, m105_callback, dryer_router = build_handlers()

    # Start with clean firmware
    fw = bytearray(orig)

    # 1. Write BX LR stub at FUN_08017450 entry (so callback table callers get a no-op)
    stub_fw = STUB_FLASH - 0x08008000  # = 0x17450
    struct.pack_into('<H', fw, stub_fw, 0x4770)     # BX LR
    struct.pack_into('<H', fw, stub_fw + 2, 0xBF00)  # NOP (alignment pad)

    # 2. Write handler code right after the stub+pad (4-byte aligned)
    fw[HANDLER_FW:HANDLER_FW+len(handler_code)] = handler_code

    # 2. Bump firmware version
    assert fw[PATCH_VERSION:PATCH_VERSION+5] == VERSION_OLD
    fw[PATCH_VERSION:PATCH_VERSION+5] = VERSION_NEW

    # 3. Patch M105 literal "M105" → "MXXX"
    assert fw[PATCH_M105_LITERAL:PATCH_M105_LITERAL+4] == b'M105'
    fw[PATCH_M105_LITERAL:PATCH_M105_LITERAL+4] = b'MXXX'

    # 3. Patch M105 callback pointer → m105_callback handler
    old_cb = struct.pack('<I', 0x0801C895)
    assert fw[PATCH_M105_CALLBACK:PATCH_M105_CALLBACK+4] == old_cb
    fw[PATCH_M105_CALLBACK:PATCH_M105_CALLBACK+4] = struct.pack('<I', m105_callback | 1)

    # 4. Patch M105 dispatch trampoline → m105_dispatch handler
    fw[PATCH_M105_TRAMPOLINE:PATCH_M105_TRAMPOLINE+4] = encode_branch(
        0x08008000 + PATCH_M105_TRAMPOLINE, m105_dispatch)

    # 5. Patch dryer command injection → dryer_router
    fw[PATCH_DRYER_INJECT:PATCH_DRYER_INJECT+4] = encode_branch(
        0x08008000 + PATCH_DRYER_INJECT, dryer_router)

    # Count total changes
    changes = sum(1 for a, b in zip(orig, fw) if a != b)
    print(f"Total firmware bytes changed: {changes}")

    # Apply XOR-difference to original GZH
    for i in range(len(orig)):
        if orig[i] != fw[i]:
            gzh[12 + i] = gzh[12 + i] ^ orig[i] ^ fw[i]

    with open(OUTPUT_GZH, 'wb') as f:
        f.write(gzh)

    print(f"Written: {OUTPUT_GZH} ({len(gzh)} bytes = original size)")
    print(f"Copy as 'update.GZH' to SD card → power cycle to update")


if __name__ == '__main__':
    main()
