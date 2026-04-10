#!/usr/bin/env python3
"""
Chitu E1 Dryer Firmware — M105 Temperature/Humidity Patch

Patches the firmware so M105 returns sensor readings via UART.

Root cause: The UART RX handler intercepts "M105" commands and dispatches
them to a NOP callback (BX LR at flash 0x0801C894). M105 is never queued
for the normal command dispatcher and produces zero output.

Fix approach (dual patch for robustness):
  Patch 1: Replace the NOP callback pointer with our handler
  Patch 2: Change "M105" literal so the interception no longer triggers
           (M105 goes through normal queue → dispatch function)
  Patch 3: Replace the M105 "goto exit" in dispatch function with a
           trampoline to our handler (in case M105 reaches dispatch)
  Handler: Reads both SHT sensors, sends "ok T0:xx H0:xx T1:xx H1:xx"

Address mapping:
  Ghidra addr + 0x8000 = real flash addr
  File offset = flash addr - 0x08000000
"""
import struct
import shutil
import sys

# ── File paths ──
CLEAN_FW  = "chitu_e1_bootloader_plus_firmware.bin"
PATCHED   = "chitu_e1_m105_patched.bin"

# ── Flash addresses (for BL offset calculations) ──
FLASH_HANDLER     = 0x08040000  # Handler location in flash
FLASH_FUN_01594   = 0x08009594  # FUN_08001594 (sensor 0 read) — Ghidra+0x8000
FLASH_FUN_015DA   = 0x080095DA  # FUN_080015da (sensor 1 read)
FLASH_FUN_1C3A8   = 0x080243A8  # FUN_0801c3a8 (UART send/printf)
FLASH_LAB_FA06    = 0x08017A06  # LAB_0800fa06 (dispatch exit label)

# ── File offsets ──
OFF_CALLBACK_PTR  = 0x25020     # Literal pool: callback fn pointer
OFF_M105_LITERAL  = 0x1B89C     # "M105" string in UART RX handler
OFF_M105_BEQ      = 0x1712C     # BEQ instruction for M105 in dispatch
OFF_M105_TARGET   = 0x17172     # Original M105 handler (B to exit)
OFF_HANDLER       = 0x40000     # Free flash for our handler code


def encode_thumb_bl(from_flash, to_flash):
    """Encode a Thumb BL (branch-and-link) instruction.
    from_flash: address of the BL instruction itself
    to_flash: target function address (without Thumb bit)
    Returns 4 bytes (little-endian pair of halfwords).
    """
    offset = to_flash - (from_flash + 4)  # PC is BL_addr + 4 in Thumb
    if not (-16777216 <= offset <= 16777214):
        raise ValueError(f"BL offset {offset} out of range (±16MB)")
    
    # Encode as Thumb-2 BL (encoding T1)
    S  = 1 if offset < 0 else 0
    abs_off = offset if offset >= 0 else offset + (1 << 25)
    imm11 = (abs_off >> 1) & 0x7FF
    imm10 = (abs_off >> 12) & 0x3FF
    J1 = ((~(abs_off >> 23)) ^ S) & 1  # J1 = NOT(bit23 XOR S)
    J2 = ((~(abs_off >> 22)) ^ S) & 1  # J2 = NOT(bit22 XOR S)
    
    # Actually use the simpler method:
    # For BL: offset = SignExtend(S:I1:I2:imm10:imm11:0, 25)
    # I1 = NOT(J1 XOR S), I2 = NOT(J2 XOR S)
    # So J1 = NOT(I1 XOR S), J2 = NOT(I2 XOR S)
    
    # Re-derive from scratch
    if offset < 0:
        val = offset + (1 << 25)
    else:
        val = offset
    
    S = (offset >> 24) & 1
    I1 = (val >> 23) & 1
    I2 = (val >> 22) & 1
    imm10 = (val >> 12) & 0x3FF
    imm11 = (val >> 1) & 0x7FF
    J1 = (~(I1 ^ S)) & 1
    J2 = (~(I2 ^ S)) & 1
    
    hw1 = 0xF000 | (S << 10) | imm10
    hw2 = 0xD000 | (J1 << 13) | (J2 << 11) | imm11
    
    return struct.pack('<HH', hw1, hw2)


def encode_thumb_bw(from_flash, to_flash):
    """Encode a Thumb B.W (unconditional wide branch) instruction.
    from_flash: address of the B.W instruction
    to_flash: target address
    Returns 4 bytes.
    """
    offset = to_flash - (from_flash + 4)
    if not (-16777216 <= offset <= 16777214):
        raise ValueError(f"B.W offset {offset} out of range")
    
    if offset < 0:
        val = offset + (1 << 25)
    else:
        val = offset
    
    S = (offset >> 24) & 1
    I1 = (val >> 23) & 1
    I2 = (val >> 22) & 1
    imm10 = (val >> 12) & 0x3FF
    imm11 = (val >> 1) & 0x7FF
    J1 = (~(I1 ^ S)) & 1
    J2 = (~(I2 ^ S)) & 1
    
    hw1 = 0xF000 | (S << 10) | imm10
    hw2 = 0x9000 | (J1 << 13) | (J2 << 11) | imm11  # B.W uses 0x9000 not 0xD000
    
    return struct.pack('<HH', hw1, hw2)


RETRY_COUNT = 5  # retries per sensor — (0.2)^5 = 0.003% failure
ERROR_TEMP  = 124  # I2C NACK returns raw 0xFFFF → 124°C after conversion


def _build_handler_common(code, flash_pc_start, uart_reg, emit16, emit_bl, flash_base):
    """Shared handler body: retry-loop sensor reads + printf.
    Expects R<uart_reg> = UART context, R5 free for retry counter.
    Returns (code, ldr_fixup_pos, lit_pool_pos).
    """
    flash_pc = [flash_pc_start]  # mutable for closures

    def _emit16(hw):
        code.extend(struct.pack('<H', hw))
        flash_pc[0] += 2

    def _emit_bl(target):
        bl = encode_thumb_bl(flash_pc[0], target)
        code.extend(bl)
        flash_pc[0] += 4

    # ── Sensor 0 with retry ──
    _emit16(0x2505)              # MOV R5, #5 (RETRY_COUNT)
    retry0_pos = len(code)       # remember loop top
    _emit16(0x4668)              # MOV R0, SP          (&temp0)
    _emit16(0xA901)              # ADD R1, SP, #4      (&humid0)
    _emit_bl(FLASH_FUN_01594)    # BL sensor_0_read
    _emit16(0x9800)              # LDR R0, [SP, #0]    (temp0)
    _emit16(0x287C)              # CMP R0, #124        (error?)
    _emit16(0xD101)              # BNE +2              (skip 2 insns: SUBS+BNE)
    _emit16(0x3D01)              # SUBS R5, #1
    # BNE back to retry0
    back_offset = (retry0_pos - (len(code) + 2 + 2)) // 2
    _emit16(0xD100 | (back_offset & 0xFF))  # BNE retry0

    # ── Sensor 1 with retry ──
    _emit16(0x2505)              # MOV R5, #5
    retry1_pos = len(code)
    _emit16(0xA802)              # ADD R0, SP, #8      (&temp1)
    _emit16(0xA903)              # ADD R1, SP, #12     (&humid1)
    _emit_bl(FLASH_FUN_015DA)    # BL sensor_1_read
    _emit16(0x9802)              # LDR R0, [SP, #8]    (temp1)
    _emit16(0x287C)              # CMP R0, #124
    _emit16(0xD101)              # BNE +2              (skip 2 insns)
    _emit16(0x3D01)              # SUBS R5, #1
    back_offset = (retry1_pos - (len(code) + 2 + 2)) // 2
    _emit16(0xD100 | (back_offset & 0xFF))  # BNE retry1

    # ── Prepare printf args ──
    _emit16(0x9A00)              # LDR R2, [SP, #0]    temp0
    _emit16(0x9B01)              # LDR R3, [SP, #4]    humid0
    _emit16(0x9802)              # LDR R0, [SP, #8]    temp1
    _emit16(0x9000)              # STR R0, [SP, #0]    → stack arg 5
    _emit16(0x9803)              # LDR R0, [SP, #12]   humid1
    _emit16(0x9001)              # STR R0, [SP, #4]    → stack arg 6

    # MOV R0, R<uart_reg>  — UART context
    _emit16(0x4600 | (uart_reg << 3))  # MOV R0, Rn

    # LDR R1, [PC, #offset]  — format string (fixup later)
    ldr_r1_pos = len(code)
    _emit16(0x0000)              # placeholder

    _emit_bl(FLASH_FUN_1C3A8)   # BL printf

    return flash_pc[0], ldr_r1_pos


def _add_literal_pool_and_string(code, flash_pc, flash_base, ldr_r1_pos):
    """Append literal pool entry + format string. Fix up LDR R1."""
    # Align to 4 bytes
    if len(code) % 4 != 0:
        code.extend(struct.pack('<H', 0xBF00))  # NOP
        flash_pc += 2

    fmt_flash_addr = flash_base + len(code) + 4
    lit_pool_pos = len(code)
    code.extend(struct.pack('<I', fmt_flash_addr))
    flash_pc += 4

    fmt_string = b"ok T0:%d H0:%d T1:%d H1:%d\r\n\0"
    while len(fmt_string) % 4 != 0:
        fmt_string += b'\0'
    code.extend(fmt_string)

    # Fix up LDR R1, [PC, #offset]
    ldr_flash = flash_base + ldr_r1_pos
    pc_aligned = (ldr_flash + 4) & ~3
    lit_pool_flash = flash_base + lit_pool_pos
    imm_offset = lit_pool_flash - pc_aligned
    assert 0 <= imm_offset <= 1020 and imm_offset % 4 == 0, \
        f"LDR offset {imm_offset} invalid"
    struct.pack_into('<H', code, ldr_r1_pos, 0x4900 | (imm_offset // 4))


def build_handler():
    """Build the callback-path M105 handler (called from UART RX handler).
    R0 = UART context.  Uses R4=ctx, R5=retry counter.
    """
    code = bytearray()
    flash_pc = FLASH_HANDLER

    def emit16(hw):
        nonlocal flash_pc
        code.extend(struct.pack('<H', hw))
        flash_pc += 2

    def emit_bl(target):
        nonlocal flash_pc
        code.extend(encode_thumb_bl(flash_pc, target))
        flash_pc += 4

    emit16(0xB5F0)               # PUSH {R4-R7, LR}
    emit16(0xB086)               # SUB SP, #24
    emit16(0x4604)               # MOV R4, R0  (save UART ctx)

    flash_pc, ldr_pos = _build_handler_common(
        code, flash_pc, uart_reg=4, emit16=emit16, emit_bl=emit_bl,
        flash_base=FLASH_HANDLER)

    code.extend(struct.pack('<H', 0xB006))   # ADD SP, #24
    code.extend(struct.pack('<H', 0xBDF0))   # POP {R4-R7, PC}
    flash_pc += 4

    _add_literal_pool_and_string(code, flash_pc, FLASH_HANDLER, ldr_pos)
    return bytes(code)


def main():
    print(f"Reading clean firmware: {CLEAN_FW}")
    with open(CLEAN_FW, 'rb') as f:
        fw = bytearray(f.read())
    
    print(f"Firmware size: {len(fw)} bytes ({len(fw)//1024} KB)")
    
    # ── Verify preconditions ──
    # Check "M105" literal
    assert fw[OFF_M105_LITERAL:OFF_M105_LITERAL+4] == b'M105', \
        f"Expected 'M105' at 0x{OFF_M105_LITERAL:X}, got {fw[OFF_M105_LITERAL:OFF_M105_LITERAL+4]}"
    
    # Check callback pointer (should be 0x0801C895 = NOP stub)
    cb_val = struct.unpack_from('<I', fw, OFF_CALLBACK_PTR)[0]
    assert cb_val == 0x0801C895, \
        f"Expected callback 0x0801C895 at 0x{OFF_CALLBACK_PTR:X}, got 0x{cb_val:08X}"
    
    # Check free flash at handler location
    for i in range(64):
        assert fw[OFF_HANDLER + i] == 0xFF, \
            f"Flash at 0x{OFF_HANDLER+i:X} not free (0x{fw[OFF_HANDLER+i]:02X})"
    
    print("All preconditions verified ✓")
    
    # ── Build handler ──
    handler = build_handler()
    print(f"Handler size: {len(handler)} bytes")
    
    # ── Apply patches ──
    
    # Patch 1: Replace callback pointer (NOP → handler)
    old_cb = struct.pack('<I', 0x0801C895)
    new_cb = struct.pack('<I', FLASH_HANDLER | 1)  # Thumb bit set
    assert fw[OFF_CALLBACK_PTR:OFF_CALLBACK_PTR+4] == old_cb
    fw[OFF_CALLBACK_PTR:OFF_CALLBACK_PTR+4] = new_cb
    print(f"Patch 1: Callback pointer 0x0801C895 → 0x{FLASH_HANDLER|1:08X} at file 0x{OFF_CALLBACK_PTR:X}")
    
    # Patch 2: Change "M105" literal to "MXXX" (disable RX interception)
    # This ensures M105 also goes through normal queue as a fallback
    fw[OFF_M105_LITERAL:OFF_M105_LITERAL+4] = b'MXXX'
    print(f"Patch 2: 'M105' → 'MXXX' at file 0x{OFF_M105_LITERAL:X}")
    
    # Patch 3: In the dispatch function, replace M105's "goto exit" with
    # a trampoline to our handler. The BEQ at 0x1712C points to 0x17172.
    # At 0x17172, replace the branch-to-exit with B.W to our handler.
    # Original at 0x17172: should be a B (branch) instruction
    orig_hw = struct.unpack_from('<H', fw, OFF_M105_TARGET)[0]
    print(f"  Original instruction at 0x{OFF_M105_TARGET:X}: 0x{orig_hw:04X}")
    
    # Encode B.W from flash 0x08017172 to handler at 0x08040000
    # But we need to make this work within the dispatch function context.
    # Since M105 now goes through the queue (Patch 2), the dispatch handler
    # needs to output sensor data too. Let's make the M105 handler at
    # 0x17172 jump to a dispatch-context handler.
    # 
    # Actually, with Patch 2 disabling the RX interception, M105 now goes
    # through the queue. In the dispatch function, M105 (0x69) hits the
    # BEQ at 0x1712C → 0x17172. At 0x17172 we need to call our handler.
    #
    # But the dispatch function context is different from the callback
    # context. local_30 = param_2 = UART context is on the stack.
    # 
    # For simplicity, let's write a SECOND handler for the dispatch path.
    # Or better: make the dispatch M105 handler also call the sensor
    # functions and output via FUN_0801c3a8(local_30, fmt, ...).
    #
    # Since we already have Patches 1+2 handling the callback path,
    # Patch 3 handles the queue/dispatch path as a belt-and-suspenders fix.
    #
    # At the M105 BEQ target (0x17172), write a B.W to a second handler
    # that reads local_30 from the dispatch stack.
    
    # Write the dispatch-path handler right after the callback handler
    dispatch_handler_offset = OFF_HANDLER + len(handler)
    # Align to 4 bytes
    while dispatch_handler_offset % 4 != 0:
        dispatch_handler_offset += 1
    dispatch_handler_flash = 0x08000000 + dispatch_handler_offset
    
    dispatch_handler = build_dispatch_handler(dispatch_handler_flash)
    print(f"Dispatch handler size: {len(dispatch_handler)} bytes at file 0x{dispatch_handler_offset:X}")
    
    # Write B.W at 0x17172 → dispatch handler
    bw_bytes = encode_thumb_bw(0x08017172, dispatch_handler_flash)
    fw[OFF_M105_TARGET:OFF_M105_TARGET+4] = bw_bytes
    print(f"Patch 3: B.W at file 0x{OFF_M105_TARGET:X} → flash 0x{dispatch_handler_flash:08X}")
    
    # ── Write handlers to free flash ──
    fw[OFF_HANDLER:OFF_HANDLER+len(handler)] = handler
    fw[dispatch_handler_offset:dispatch_handler_offset+len(dispatch_handler)] = dispatch_handler
    
    # ── Save ──
    with open(PATCHED, 'wb') as f:
        f.write(fw)
    
    print(f"\nPatched firmware written to: {PATCHED}")
    print(f"Flash with: stm32flash -b 115200 -m 8e1 -w {PATCHED} -v -S 0x08000000 /dev/cu.usbserial-2110")


def build_dispatch_handler(handler_flash):
    """Build the dispatch-path M105 handler with retry logic.
    
    Called from M-code dispatch when M105 (0x69) is detected.
    local_30 (UART context) is on the dispatch stack at SP+104.
    We use PUSH {R4, R5} to save dispatch regs: R4=ctx, R5=retry.
    Stack: PUSH 8 + SUB 24 = 32 → local_30 at SP+136.
    """
    code = bytearray()
    flash_pc = handler_flash

    def emit16(hw):
        nonlocal flash_pc
        code.extend(struct.pack('<H', hw))
        flash_pc += 2

    def emit_bl(target):
        nonlocal flash_pc
        code.extend(encode_thumb_bl(flash_pc, target))
        flash_pc += 4

    # PUSH {R4, R5}  — save dispatch's R4 + our retry counter R5
    emit16(0xB430)               # PUSH {R4, R5}
    emit16(0xB086)               # SUB SP, #24

    # Load local_30: PUSH 8 + SUB 24 = 32, so SP+136 = dispatch SP+104
    emit16(0x9C22)               # LDR R4, [SP, #136]  (136/4=34=0x22)

    flash_pc, ldr_pos = _build_handler_common(
        code, flash_pc, uart_reg=4, emit16=emit16, emit_bl=emit_bl,
        flash_base=handler_flash)

    # Epilogue: restore stack, pop R4+R5, branch to dispatch exit
    code.extend(struct.pack('<H', 0xB006))   # ADD SP, #24
    flash_pc += 2
    code.extend(struct.pack('<H', 0xBC30))   # POP {R4, R5}
    flash_pc += 2
    bw = encode_thumb_bw(flash_pc, FLASH_LAB_FA06)
    code.extend(bw)
    flash_pc += 4

    _add_literal_pool_and_string(code, flash_pc, handler_flash, ldr_pos)
    return bytes(code)


if __name__ == '__main__':
    main()
