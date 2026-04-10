#!/usr/bin/env python3
"""
Chitu E1 Dryer — M6050/M6051/M6052 UART command patch.

Adds three new M-codes to the firmware:
  M6050 I<temp_C> T<time_min>  — Start drying (zone 0)
  M6051                         — Stop drying
  M6052                         — Query box presence + heating state

Applied on top of chitu_e1_m105_patched.bin (which already has the M105 fix).

Injection: replaces the 4-byte BEQ+B fallthrough at file 0x174FE in the
M6xxx dispatch chain with a B.W to our router in free flash.
"""
import struct

# ── Files ──
INPUT  = "chitu_e1_m105_patched.bin"
OUTPUT = "chitu_e1_dryer_patched.bin"

# ── Flash addresses ──
FLASH_ROUTER      = 0x08040100   # Our router + handlers in free flash
FLASH_EXIT        = 0x08017A06   # LAB_0800fa06 — dispatch exit
FLASH_M6501_ORIG  = 0x080175EE   # Original M6501 handler target
FLASH_FUN_A968    = 0x08021968   # FUN_0801a968 — set thermal zone target temp
FLASH_FUN_F480    = 0x08027480   # FUN_0801f480 — stop drying / cleanup
FLASH_FUN_01B0    = 0x080281B0   # FUN_080201b0 — check box presence
FLASH_FUN_01E8    = 0x080281E8   # FUN_080201e8 — get heating state
FLASH_FUN_C3A8    = 0x080243A8   # FUN_0801c3a8 — UART printf

# ── SRAM pointers (literal pool values in flash → SRAM addresses) ──
SRAM_MAIN_STATE_PTR = 0x200002C8  # *this = main_state struct address
SRAM_SETTINGS_PTR   = 0x20000324  # *this = settings struct address

# ── Offsets within main_state struct ──
OFF_HEAT_ENABLE  = 0xD4    # int: 1=heating on, 0=off
OFF_THERMAL_ZONE = 0x364   # base of thermal zone array
OFF_SETPOINT_Z0  = 0x364 + 0x1C  # = 0x380, int: zone 0 target temp
OFF_HEATER_PWM0  = 0x1374  # int: zone 0 heater PWM (100=full)
OFF_HEATER_PWM1  = 0x137C  # int: zone 1 heater PWM

# ── Offsets within settings struct ──
OFF_TARGET_TEMP  = 0x30    # float: zone 0 target temp (°C)
OFF_TIMER_HOURS  = 0x38    # int: drying hours
OFF_TIMER_MINS   = 0x3C    # int: drying minutes
OFF_TIMER_TOTAL  = 0x34    # int: total timer in seconds

# ── Parsed command struct (R4 at injection point) ──
# param[0x0C] = *(R4+0x30) = I parameter (int)
# param[0x0E] = *(R4+0x38) = T parameter (int)
# Sentinel for "not provided" = 0x80000000

# ── Injection point ──
OFF_INJECT = 0x174FE  # File offset of BEQ+B (4 bytes) to replace
FLASH_INJECT = 0x080174FE


# ═══════════════════════════════════════════════════════════════
# Thumb-2 encoding helpers (reuse from patch_m105.py)
# ═══════════════════════════════════════════════════════════════

def _encode_branch(from_addr, to_addr, link=False):
    """Encode Thumb-2 B.W or BL instruction."""
    offset = to_addr - (from_addr + 4)
    assert -16777216 <= offset <= 16777214, f"Branch offset {offset} out of range"
    val = offset if offset >= 0 else offset + (1 << 25)
    S   = (offset >> 24) & 1
    I1  = (val >> 23) & 1
    I2  = (val >> 22) & 1
    imm10 = (val >> 12) & 0x3FF
    imm11 = (val >> 1) & 0x7FF
    J1  = (~(I1 ^ S)) & 1
    J2  = (~(I2 ^ S)) & 1
    hw1 = 0xF000 | (S << 10) | imm10
    hw2 = (0xD000 if link else 0x9000) | (J1 << 13) | (J2 << 11) | imm11
    return struct.pack('<HH', hw1, hw2)


class ThumbBuilder:
    """Helper to emit Thumb instructions and track PC."""

    def __init__(self, base_flash):
        self.code = bytearray()
        self.base = base_flash
        self.fixups = {}  # name → code offset of placeholder

    @property
    def pc(self):
        return self.base + len(self.code)

    def emit16(self, hw):
        self.code += struct.pack('<H', hw)

    def emit32(self, hw1, hw2):
        self.code += struct.pack('<HH', hw1, hw2)

    def emit_bl(self, target):
        self.code += _encode_branch(self.pc, target, link=True)

    def emit_bw(self, target):
        self.code += _encode_branch(self.pc, target, link=False)

    def emit_movw(self, rd, imm16):
        """MOVW Rd, #imm16  (Thumb-2, 4 bytes)"""
        assert 0 <= imm16 <= 0xFFFF and 0 <= rd <= 15
        imm4  = (imm16 >> 12) & 0xF
        i     = (imm16 >> 11) & 1
        imm3  = (imm16 >> 8) & 7
        imm8  = imm16 & 0xFF
        hw1 = 0xF240 | (i << 10) | imm4
        hw2 = (imm3 << 12) | (rd << 8) | imm8
        self.emit32(hw1, hw2)

    def emit_ldr_lit(self, rd, label_name):
        """LDR Rd, [PC, #offset] — placeholder, fixed up later."""
        self.fixups[label_name] = len(self.code)
        self.emit16(0x4800 | (rd << 8))  # placeholder

    def emit_word(self, val):
        self.code += struct.pack('<I', val)

    def emit_string(self, s):
        b = s.encode('ascii') + b'\0'
        while len(b) % 4:
            b += b'\0'
        self.code += b

    def align4(self):
        if len(self.code) % 4:
            self.emit16(0xBF00)  # NOP

    def fixup_ldr_lit(self, label_name, pool_code_offset):
        """Fix up a LDR Rd, [PC, #x] to point at a literal pool word."""
        ldr_off = self.fixups[label_name]
        ldr_flash = self.base + ldr_off
        pc_aligned = (ldr_flash + 4) & ~3
        pool_flash = self.base + pool_code_offset
        imm = pool_flash - pc_aligned
        assert 0 <= imm <= 1020 and imm % 4 == 0, \
            f"LDR fixup '{label_name}': offset {imm} invalid"
        rd = (self.code[ldr_off + 1] >> 0) & 7  # extract Rd from placeholder
        # Re-read the Rd we encoded
        rd = (struct.unpack_from('<H', self.code, ldr_off)[0] >> 8) & 7
        struct.pack_into('<H', self.code, ldr_off, 0x4800 | (rd << 8) | (imm // 4))

    def current_offset(self):
        return len(self.code)


def build_all_handlers():
    """Build the router + M6050/M6051/M6052 handlers."""
    t = ThumbBuilder(FLASH_ROUTER)

    # ═══════════════════════════════════════════════════════════
    # ROUTER — reached via B.W from dispatch fallthrough
    # Registers on entry: R0 = M-code number, R4 = parsed cmd,
    #   R7 = main ctx (param_2), SP+104 = local_30 (UART ctx)
    # ═══════════════════════════════════════════════════════════

    # Check for relocated M6501 (0x1965)
    t.emit_movw(1, 0x1965)          # MOVW R1, #0x1965
    t.emit16(0x4288)                # CMP R0, R1
    # BEQ → original M6501 handler (need 4-byte branch for range)
    # Use a trampoline: if equal, B.W to original
    # BEQ +2 (skip the B.W to next check), else fall through
    # Actually: BNE skip_m6501 (2 bytes), then B.W to M6501 orig (4 bytes)
    t.emit16(0xD101)                # BNE +2 (skip next B.W)
    t.emit_bw(FLASH_M6501_ORIG)     # B.W M6501 original handler

    # Check M6050 (0x17A2) — start drying
    t.emit_movw(1, 0x17A2)
    t.emit16(0x4288)                # CMP R0, R1
    t.emit16(0xD101)                # BNE +2
    m6050_bw_off = t.current_offset()
    t.emit32(0, 0)                  # placeholder B.W (fixed up later)

    # Check M6051 (0x17A3) — stop drying
    t.emit_movw(1, 0x17A3)
    t.emit16(0x4288)
    t.emit16(0xD101)
    m6051_bw_off = t.current_offset()
    t.emit32(0, 0)                  # placeholder

    # Check M6052 (0x17A4) — status query
    t.emit_movw(1, 0x17A4)
    t.emit16(0x4288)
    t.emit16(0xD101)
    m6052_bw_off = t.current_offset()
    t.emit32(0, 0)                  # placeholder

    # No match → exit
    t.emit_bw(FLASH_EXIT)

    # ═══════════════════════════════════════════════════════════
    # M6050 — START DRYING: M6050 I<temp_C> T<time_min>
    # ═══════════════════════════════════════════════════════════
    t.align4()
    m6050_flash = t.pc
    # Fix up the B.W placeholder in the router
    struct.pack_into('<4s', t.code, m6050_bw_off,
                     _encode_branch(FLASH_ROUTER + m6050_bw_off, m6050_flash, link=False))

    t.emit16(0xB470)                # PUSH {R4, R5, R6}  (12 bytes, NO LR)
    t.emit16(0xB082)                # SUB SP, #8  (locals)
    # Stack: PUSH 12 + SUB 8 = 20 total.  local_30 at SP+20+104 = SP+124

    # R5 = I param (temp °C): *(R4 + 0x30)
    t.emit32(0xF8D4, 0x5030)       # LDR.W R5, [R4, #0x30]

    # R6 = T param (time in minutes): *(R4 + 0x38)
    t.emit32(0xF8D4, 0x6038)       # LDR.W R6, [R4, #0x38]

    # Load main_state pointer
    t.emit_ldr_lit(0, 'main_state_ptr_addr_m6050')
    t.emit16(0x6800)                # LDR R0, [R0]  → R0 = main_state

    # Set heating enable: *(main_state + 0xD4) = 1
    t.emit16(0x2101)                # MOVS R1, #1
    t.emit32(0xF8C0, 0x10D4)       # STR.W R1, [R0, #0xD4]

    # Set heater PWM: *(main_state + 0x1374) = 100
    t.emit16(0x2164)                # MOVS R1, #100
    t.emit32(0xF8C0, 0x1000 | OFF_HEATER_PWM0)

    # Set thermal zone 0 target temp: *(main_state + 0x380) = R5
    t.emit32(0xF8C0, 0x5000 | OFF_SETPOINT_Z0)

    # Set timer: total_seconds = time_min * 60
    t.emit_ldr_lit(0, 'settings_ptr_addr')
    t.emit16(0x6800)                # LDR R0, [R0]  → R0 = settings
    t.emit16(0x213C)                # MOVS R1, #60
    t.emit32(0xFB06, 0xF101)       # MUL R1, R6, R1  → R1 = time_min * 60
    t.emit32(0xF8C0, 0x1034)       # STR.W R1, [R0, #0x34]  (total timer secs)

    # Send response: "ok Drying T:%d I:%d\r\n"
    # Stack: PUSH 12 + SUB 8 = 20.  local_30 at SP + 124
    t.emit16(0x9800 | (124 // 4))   # LDR R0, [SP, #124]  → UART ctx
    t.emit_ldr_lit(1, 'fmt_m6050')   # LDR R1, =fmt_string
    t.emit16(0x462A)                 # MOV R2, R5  (temp)
    t.emit32(0xF8D4, 0x3038)        # LDR.W R3, [R4, #0x38]  (time_min)
    t.emit_bl(FLASH_FUN_C3A8)

    t.emit16(0xB002)                 # ADD SP, #8
    t.emit16(0xBC70)                 # POP {R4, R5, R6}
    t.emit_bw(FLASH_EXIT)            # B.W dispatch exit

    # ═══════════════════════════════════════════════════════════
    # M6051 — STOP DRYING
    # ═══════════════════════════════════════════════════════════
    t.align4()
    m6051_flash = t.pc
    struct.pack_into('<4s', t.code, m6051_bw_off,
                     _encode_branch(FLASH_ROUTER + m6051_bw_off, m6051_flash, link=False))

    t.emit16(0xB410)                # PUSH {R4}  (4 bytes, NO LR)
    t.emit16(0xB082)                # SUB SP, #8
    # Stack: PUSH 4 + SUB 8 = 12.  local_30 at SP+12+104 = SP+116

    # Clear heating enable: main_state + 0xD4 = 0
    t.emit_ldr_lit(0, 'main_state_ptr_addr')
    t.emit16(0x6800)                # LDR R0, [R0]
    t.emit16(0x2100)                # MOVS R1, #0
    t.emit32(0xF8C0, 0x10D4)       # STR.W R1, [R0, #0xD4]
    # Clear heater PWMs
    t.emit32(0xF8C0, 0x1000 | OFF_HEATER_PWM0)
    t.emit32(0xF8C0, 0x1000 | OFF_HEATER_PWM1)

    # Call FUN_0801f480(0) — proper drying stop
    t.emit16(0x2000)                # MOVS R0, #0
    t.emit_bl(FLASH_FUN_F480)

    # Send response — local_30 at SP+116
    t.emit16(0x9800 | (116 // 4))   # LDR R0, [SP, #116]
    t.emit_ldr_lit(1, 'fmt_m6051')
    t.emit_bl(FLASH_FUN_C3A8)

    t.emit16(0xB002)                # ADD SP, #8
    t.emit16(0xBC10)                # POP {R4}
    t.emit_bw(FLASH_EXIT)            # B.W dispatch exit

    # ═══════════════════════════════════════════════════════════
    # M6052 — STATUS QUERY (box presence + heating state)
    # ═══════════════════════════════════════════════════════════
    t.align4()
    m6052_flash = t.pc
    struct.pack_into('<4s', t.code, m6052_bw_off,
                     _encode_branch(FLASH_ROUTER + m6052_bw_off, m6052_flash, link=False))

    t.emit16(0xB4F0)                # PUSH {R4, R5, R6, R7}  (16 bytes, NO LR)
    t.emit16(0xB084)                # SUB SP, #16
    # Stack: PUSH 16 + SUB 16 = 32.  local_30 at SP+32+104 = SP+136

    # Box 0: R4 = FUN_080201b0(0)
    t.emit16(0x2000)                # MOVS R0, #0
    t.emit_bl(FLASH_FUN_01B0)
    t.emit16(0x4604)                # MOV R4, R0  (box0)

    # Box 1: R5 = FUN_080201b0(1)
    t.emit16(0x2001)                # MOVS R0, #1
    t.emit_bl(FLASH_FUN_01B0)
    t.emit16(0x4605)                # MOV R5, R0  (box1)

    # Heating state 0: R6 = FUN_080201e8(0)
    t.emit16(0x2000)
    t.emit_bl(FLASH_FUN_01E8)
    t.emit16(0x4606)                # MOV R6, R0  (heat0)

    # Heating state 1: R7 = FUN_080201e8(1)
    t.emit16(0x2001)
    t.emit_bl(FLASH_FUN_01E8)
    t.emit16(0x4607)                # MOV R7, R0  (heat1)

    # printf: "ok B0:%d B1:%d S0:%d S1:%d\r\n"
    # args: R0=uart, R1=fmt, R2=box0, R3=box1, [SP]=heat0, [SP+4]=heat1
    t.emit16(0x9600)                # STR R6, [SP, #0]  (heat0 → stack arg)
    t.emit16(0x9701)                # STR R7, [SP, #4]  (heat1 → stack arg)
    # UART ctx: PUSH 16 + SUB 16 = 32 + 104 = SP+136
    t.emit16(0x9800 | (136 // 4))   # LDR R0, [SP, #136]
    t.emit_ldr_lit(1, 'fmt_m6052')
    t.emit16(0x4622)                # MOV R2, R4  (box0)
    t.emit16(0x462B)                # MOV R3, R5  (box1)
    t.emit_bl(FLASH_FUN_C3A8)

    t.emit16(0xB004)                # ADD SP, #16
    t.emit16(0xBCF0)                # POP {R4, R5, R6, R7}
    t.emit_bw(FLASH_EXIT)            # B.W dispatch exit

    # ═══════════════════════════════════════════════════════════
    # LITERAL POOLS + FORMAT STRINGS
    # ═══════════════════════════════════════════════════════════
    t.align4()

    # Literal pool entries
    lp_main_state = t.current_offset()
    t.emit_word(SRAM_MAIN_STATE_PTR)

    lp_main_state_m6050 = t.current_offset()
    t.emit_word(SRAM_MAIN_STATE_PTR)  # duplicate for M6050's separate LDR

    lp_settings = t.current_offset()
    t.emit_word(SRAM_SETTINGS_PTR)

    lp_fmt_m6050 = t.current_offset()
    fmt6050_addr = t.pc + 8  # after this word + 2 more pool entries
    t.emit_word(0)  # placeholder, fixed below

    lp_fmt_m6051 = t.current_offset()
    t.emit_word(0)  # placeholder

    lp_fmt_m6052 = t.current_offset()
    t.emit_word(0)  # placeholder

    # Format strings
    fmt6050_off = t.current_offset()
    t.emit_string("ok Drying T:%d I:%d\r\n")
    struct.pack_into('<I', t.code, lp_fmt_m6050, t.base + fmt6050_off)

    fmt6051_off = t.current_offset()
    t.emit_string("ok Drying stopped\r\n")
    struct.pack_into('<I', t.code, lp_fmt_m6051, t.base + fmt6051_off)

    fmt6052_off = t.current_offset()
    t.emit_string("ok B0:%d B1:%d S0:%d S1:%d\r\n")
    struct.pack_into('<I', t.code, lp_fmt_m6052, t.base + fmt6052_off)

    # Fix up all LDR literals
    t.fixup_ldr_lit('main_state_ptr_addr', lp_main_state)
    if 'main_state_ptr_addr_m6050' in t.fixups:
        t.fixup_ldr_lit('main_state_ptr_addr_m6050', lp_main_state_m6050)
    t.fixup_ldr_lit('settings_ptr_addr', lp_settings)
    t.fixup_ldr_lit('fmt_m6050', lp_fmt_m6050)
    t.fixup_ldr_lit('fmt_m6051', lp_fmt_m6051)
    t.fixup_ldr_lit('fmt_m6052', lp_fmt_m6052)

    return bytes(t.code)


def main():
    print(f"Reading: {INPUT}")
    with open(INPUT, 'rb') as f:
        fw = bytearray(f.read())

    # Verify injection point (should be BEQ+B from M105 patch)
    inject_bytes = fw[OFF_INJECT:OFF_INJECT + 4]
    print(f"Injection point at 0x{OFF_INJECT:X}: {inject_bytes.hex()}")
    # Original was 76D0 81E2, but M105 patch might have changed nearby bytes
    # The bytes should still be 76D0 81E2 since M105 patch was at different offsets
    assert inject_bytes == bytes.fromhex('76d081e2'), \
        f"Expected 76d081e2 at injection point, got {inject_bytes.hex()}"

    # Verify free flash at router location
    router_file_off = FLASH_ROUTER - 0x08000000
    for i in range(512):
        assert fw[router_file_off + i] == 0xFF, \
            f"Flash not free at 0x{router_file_off + i:X}"
    print("Preconditions OK ✓")

    # Build handlers
    handler_code = build_all_handlers()
    print(f"Total handler code: {len(handler_code)} bytes")

    # Patch injection point: B.W from 0x080174FE to FLASH_ROUTER
    bw = _encode_branch(FLASH_INJECT, FLASH_ROUTER, link=False)
    fw[OFF_INJECT:OFF_INJECT + 4] = bw
    print(f"Patch: B.W at 0x{OFF_INJECT:X} → 0x{FLASH_ROUTER:08X}")

    # Write handler code
    fw[router_file_off:router_file_off + len(handler_code)] = handler_code

    with open(OUTPUT, 'wb') as f:
        f.write(fw)
    print(f"\nWritten: {OUTPUT}")
    print(f"Flash: stm32flash -b 115200 -m 8e1 -w {OUTPUT} -v -S 0x08000000 /dev/cu.usbserial-2110")


if __name__ == '__main__':
    main()
