# Chitu Dryer Firmware — Project Status Summary

**Date:** 2026-04-10
**Purpose:** Full UART control of Chitu E1 resin dryer — sensor readings, drying control, status queries
**Firmware version:** V 1.2.0 (patched from V 1.1.8)

---

## UART Command Reference (for ESP32 / ESPHome integration)

**UART settings:** 115200 baud, 8N1, line ending `\n`
All commands return `ok ...\r\n` followed by `ok N:<seq>\r\n`.

### M105 — Read temperature & humidity sensors
```
Send:  M105\n
Reply: ok T0:25 H0:23 T1:24 H1:24\r\n
       ok N:1\r\n
```
- **T0/T1**: temperature in °C (integer) for zone 0 / zone 1
- **H0/H1**: relative humidity in % (integer)
- Zone 0 = sensor closer to top/lid, Zone 1 = sensor closer to heater
- 100% reliable (built-in I2C retry, 5 attempts per sensor)
- Error value `T:124 H:99` = sensor not connected or I2C failure
- **Poll interval:** safe to query every 1–10 seconds

### M6050 — Start drying
```
Send:  M6050 I<temp_celsius> T<time_minutes>\n
Reply: ok Drying T:<temp> I:<time>\r\n
       ok N:2\r\n
```
- **I parameter**: target temperature in °C (integer, e.g., `I40` for 40°C)
- **T parameter**: drying time in minutes (integer, e.g., `T20` for 20 min)
- Sets heater PWM to 100%, enables heating flag, stores timer
- The firmware’s PID loop controls the actual heating based on the setpoint
- Example: `M6050 I55 T30\n` → dry at 55°C for 30 minutes

### M6051 — Stop drying
```
Send:  M6051\n
Reply: ok Drying stopped\r\n
       ok N:3\r\n
```
- Clears heating enable flag, sets heater PWM to 0 for both zones
- Calls firmware cleanup function for proper shutdown
- Safe to call even if not currently drying

### M6052 — Query status (box presence + heating state)
```
Send:  M6052\n
Reply: ok B0:1 B1:0 S0:0 S1:0\r\n
       ok N:4\r\n
```
- **B0/B1**: box/tray presence per zone
  - `1` = OK (box in place or detection disabled)
  - `0` = interlock active (box missing / lid open)
- **S0/S1**: heating state per zone
  - `0` = idle (not heating)
  - `1` = actively heating
  - `2` = cooldown / done

### M115 — Firmware version
```
Send:  M115\n
Reply: ok CBD make it.Date:May  7 2025 Time:11:14:52\r\n
```
- Patched firmware shows version `V 1.2.0` on the LCD display

### M8500 — Factory reset settings
```
Send:  M8500\n
Reply: ok N:5\r\n
```
- Restores all settings to factory defaults (from SPI flash)
- Use if drying parameters get corrupted

### ESPHome UART example
```yaml
uart:
  tx_pin: GPIO17
  rx_pin: GPIO16
  baud_rate: 115200

interval:
  - interval: 10s
    then:
      - uart.write: "M105\n"
      # Parse response: "ok T0:XX H0:XX T1:XX H1:XX"

button:
  - platform: template
    name: "Start Drying 40°C 30min"
    on_press:
      - uart.write: "M6050 I40 T30\n"
  - platform: template
    name: "Stop Drying"
    on_press:
      - uart.write: "M6051\n"
```

### Wiring (ESP32 → Chitu dryer board)
```
ESP32 TX (GPIO17) → Dryer RX
ESP32 RX (GPIO16) → Dryer TX
ESP32 GND         → Dryer GND
```
**Important:** The dryer runs at 3.3V logic levels (STM32F4). Do NOT connect 5V signals directly.

---

## Hardware

- **MCU:** STM32F401/F415 (Cortex-M4), 512 KB flash
- **Board:** Chitu Systems resin curing/drying station (CBD platform)
- **UART:** 115200 baud, 8N1 — bidirectional (confirmed via stm32flash at 8E1 in boot mode)
- **Boot modes:**
  - BOOT0 high: ROM bootloader (SWD works, UART bootloader works, firmware doesn't run)
  - BOOT0 low: firmware runs from flash (SWD disabled by firmware, display works, UART command interface active)
- **ST-Link V2** connected via SWD (but firmware disables SWD pins during init, so debug only works in boot mode)
- **USB-UART adapter** at `/dev/cu.usbserial-2110`

## Firmware Versions

### Board 1 (original, working)
- **Dump:** `stm32f4-rdp-workaround/flash_dump.bin` (512 KB, extracted via FPB exploit)
- SP: `0x2000FE18`, Reset: `0x08000101`
- No bootloader — firmware starts directly at 0x08000000
- Build: `ok CBD make it.Date:May  7 2025 Time:11:14:52`
- Boots, display works, M115 responds, M105 returns empty (the bug)

### Stock board (boot-looping)
- **Dump:** `stm32f4-rdp-workaround/flash_dump2.bin` (512 KB, extracted via FPB exploit)
- SP: `0x20002DD0`, Reset: `0x080001D9`
- Has bootloader with real exception handlers + IWDG watchdog
- Boot-loops: sends "start" repeatedly, HardFault during peripheral init, watchdog resets
- Different firmware variant — NOT compatible with Board 1

### Decrypted update firmware
- **File:** `chitu_e1_update_decrypted.bin` (180 KB — previously decrypted from `update.GZH`)
- SP: `0x2000FE18`, Reset: `0x080081A9`
- Built for boards WITH bootloader (firmware starts at 0x08008000)
- Same build date as Board 1 but different code layout
- **Full decompilation:** `chitu_e1_decompiled_full.c` (941 functions, 38,616 lines)

### Working combined image
- **File:** `chitu_e1_bootloader_plus_firmware.bin` (512 KB)
- Stock board's bootloader (0x00000-0x07FFF) + decrypted update firmware (0x08000+)
- **This is the current working firmware on the board**
- Boots, display on, M115 responds at 115200 baud, M105 returns empty

## GZH Encryption — FULLY REVERSED

- Magic: `0xA9B0C0D0` (newer Chitu format, NOT the old `0x443D2D3F` CBD format)
- **File format:** 12-byte header + encrypted firmware data
  - `[0:4]` magic `0xA9B0C0D0`
  - `[4:8]` XOR key `0x4BDE6541`
  - `[8:12]` version/flag `0xD83073F5`
  - `[12:]` encrypted firmware (variable length)

- **Encryption algorithm** (reversed from bootloader at `0x08005C44`, verified 88/88 blocks):
  ```python
  BLOCK = 0x800  # 2048-byte blocks
  CONST = 0x4BAD
  for each 0x800-byte block at block_offset:
      scramble = int32(block_offset * 0x4BAD)
      for i in 0..block_size:
          shift = i % 24
          val = int32(i*i + scramble) >> shift   # signed arithmetic shift right
          ks = (val ^ key) & 0xFF
          output[i] = input[i] ^ ks              # symmetric: encrypt = decrypt
  ```

- **Tools:**
  - `build_gzh_update.py` — **recommended:** builds self-contained GZH using XOR-difference
  - `create_gzh.py` — full re-encryption (for reference; XOR-difference is more reliable)
  - Usage: `python3 build_gzh_update.py [output.GZH]`
  - Copy as `update.GZH` to USB stick root → bootloader flashes on power-up
  - **Note:** use `cp` from terminal, NOT Finder drag-and-drop (macOS creates .textClipping files)
  - **Note:** bootloader only writes the original firmware size (180KB). Handlers must be
    relocated within this range (currently placed in FUN_08013b84 — unused print progress page).

- Bootloader decrypt function at flash `0x08005C44` (identical to app's `fcn_08028114`)
- App's `FUN_0801c434` handles G-code file decryption (different use case, same algorithm)

## The M105 Bug — Root Cause (CONFIRMED) and FIX (APPLIED)

### Root cause (two-layer)

**Layer 1: UART RX handler interception**
The UART RX handler `FUN_080136a8` (line 24345) compares incoming data against the literal "M105" at `DAT_0801389c` (flash `0x0801B89C`). When the received data matches:
- All OTHER commands take Branch A → set received flag `0x224=1` → queued for main task
- M105 ONLY takes Branch B → calls callback at `[uart_ctx + 0x22c]`

The callback at `+0x22c` is registered during system init at flash `0x08024FDA`:
```asm
STRD R0, R4, [R4, #0x22C]  ; [ctx+0x22C] = fn_ptr, [ctx+0x230] = ctx
```
The function pointer (loaded from literal pool at `0x08025020`) = `0x0801C895`, pointing to flash `0x0801C894` which is... **BX LR** — a NOP that just returns!

So M105 is intercepted, dispatched to a NOP stub, and silently consumed.

**Layer 2: Dispatch function also empty**
Even if M105 reaches the dispatch function `FUN_0800eb9c`, the handler at command 0x69 just does `goto LAB_0800fa06` (skip to exit with no output). The firmware never implemented M105.

### Fix applied — `chitu_e1_m105_patched.bin`

Three patches applied by `patch_m105.py`:

| Patch | File offset | What | Effect |
|-------|-----------|------|--------|
| 1 | 0x25020 | Callback ptr: `0x0801C895` → `0x08040001` | RX callback → handler (safety net) |
| 2 | 0x1B89C | Literal: `"M105"` → `"MXXX"` | Disables RX interception, M105 goes through queue |
| 3 | 0x17172 | `B exit` → `B.W 0x08040054` | Dispatch M105 handler → sensor read + response |

**Handler code at flash 0x08040054** (dispatch path, 88 bytes):
1. PUSH {R4}, SUB SP, #24
2. LDR R4, [SP, #132] — reads `local_30` (UART context) from dispatch stack
3. BL FUN_08001594 — reads sensor 0 (temp + humidity via I2C)
4. BL FUN_080015da — reads sensor 1
5. BL FUN_0801c3a8(R4, "ok T0:%d H0:%d T1:%d H1:%d\r\n", t0, h0, t1, h1)
6. POP {R4}, B.W LAB_0800fa06 — return to dispatch exit

**Result:** `M105` → `ok T0:25 H0:23 T1:24 H1:24\r\nok N:1\r\n`

With retry logic (5 attempts per sensor), **100% reliability** — verified over 40+ consecutive reads
and an 8-minute drying cycle monitoring session.

### Sensor functions
- `FUN_08001594` (Ghidra) / flash `0x08009594` — reads sensor 0 via `FUN_080074b4` (I2C)
- `FUN_080015da` (Ghidra) / flash `0x080095DA` — reads sensor 1 via `FUN_08007516` (I2C)
- Both return: `*param_1` = temperature (°C, integer), `*param_2` = humidity (%, integer)
- Conversion: SHT-type sensor formula: `T = (raw*16500>>16 - 4000) / 100`, `H = (raw*100) >> 16`
- 0xFFFF raw = 124°C / 99% = sensor not responding (I2C NACK or bus contention)

## Key Functions

All addresses below are Ghidra addresses (base 0x08000000). **Add 0x8000 to get real flash address.** File offset = flash address - 0x08000000.

| Ghidra addr | Flash addr | File offset | Function |
|---|---|---|---|
| 0x0800EB9C | 0x08016B9C | 0x16B9C | M-code dispatch (3042B) — contains M105 bug |
| 0x0800F12A | 0x0801712A | 0x1712A | CMP R0, #105 (M105 check) |
| 0x0800F12C | 0x0801712C | 0x1712C | BEQ 0x08017172 (original: 0xD021) |
| 0x0800F172 | 0x08017172 | 0x17172 | M105 handler: B 0x0801761A (send ok shortcut) |
| 0x0800F222 | 0x08017222 | 0x17222 | Dead-code slot (trampoline location) |
| 0x0800FA06 | 0x08017A06 | 0x17A06 | LAB exit — "ok N:%d" + response flag check |
| 0x0800FA1E | 0x08017A1E | 0x17A1E | LDR R0, [SP, #104] — loads local_30 (UART ctx) |
| 0x0800FB88 | 0x08017B88 | 0x17B88 | start_task — sends "start_task" via UART |
| 0x0801C3A8 | 0x080243A8 | 0x243A8 | FUN_0801c3a8 — response send function |
| 0x0801C310 | 0x08024310 | 0x24310 | FUN_0801c310 — low-level UART send |
| 0x0801C454 | 0x08024454 | 0x24454 | UART init — stores contexts to global array |
| 0x0801A8D8 | 0x080228D8 | 0x228D8 | Temperature monitor (over-temp check) |

## Response Function: FUN_0801c3a8

```c
void FUN_0801c3a8(int param_1, char* fmt, ...) {
    // param_1 = UART context object (struct in SRAM)
    // param_1 + 0x228 = channel number
    // param_1 + 0x249 = mode flag
    // param_1 + 0x24b = output buffer (255 bytes)
    
    if (*(int*)(param_1 + 0x228) != 3) {
        FUN_0800b904(channel, mode);  // configure UART
    }
    int len = vsnprintf(param_1 + 0x24b, 0xff, fmt, va_args);
    if (len > 0) {
        FUN_0801c310(param_1, param_1 + 0x24b, len);  // send
    }
}
```

## SRAM Pointers (CRITICAL — CORRECTED)

**0x20000090 and 0x200000AC are NOT temperature/humidity sensor values.** They are **pointers to UART context objects**, used by `start_task` to send debug output:

From `start_task` at file 0x17B88:
```asm
LDR R0, =0x20000090   ; SRAM location holding UART context pointer
LDR R0, [R0]          ; dereference → UART context object
; check R0 != 0
LDR R1, =0x200000AC   ; second pointer (enable check)
LDR R1, [R1]          ; check != 0
ADR R1, "start_task"
BL FUN_0801c3a8        ; send via UART
```

**The actual temperature/humidity sensor variable addresses are UNKNOWN.** The decompiled code shows 30+ references to 0x20000090 and 24+ to 0x200000AC, but these are the UART context pointers, not sensor data. The real sensor addresses need to be found by tracing the ADC/I2C reading functions.

## Dispatch Function Stack Frame

```
Function: FUN_0800eb9c (starts at flash 0x08016B9C, file 0x16B9C)
Prologue: PUSH.W {R4-R11, LR}  (36 bytes)
          SUB SP, #116           (0x74)
Total frame: 152 bytes

local_30 (= param_2 = UART context for command responses):
  Offset from SP (after PUSH+SUB): SP + 0x68 = SP + 104
  Confirmed by: LDR R0, [SP, #104] at flash 0x08017A1E (file 0x17A1E)
```

## Dryer Control Commands — M6050/M6051/M6052 (IMPLEMENTED)

Three custom M-codes added by `patch_dryer_cmds.py` (applied on top of M105 patch):

| Command | Response | Function |
|---------|----------|----------|
| `M6050 I<temp> T<min>` | `ok Drying T:40 I:2` | Start drying zone 0: sets target temp, PWM, timer |
| `M6051` | `ok Drying stopped` | Stop drying: clears heating enable + PWM, calls cleanup |
| `M6052` | `ok B0:0 B1:0 S0:0 S1:0` | Query box presence (B) and heating state (S) per zone |

**Injection point:** 4-byte B.W at file `0x174FE` replaces the M6xxx dispatch fallthrough
(original `BEQ M6501 + B exit`). Routes to a dispatch router at flash `0x08040100` (324 bytes).

The router checks R0 (M-code number) for:
- `0x1965` (M6501) → original handler (relocated)
- `0x17A2` (M6050) → start drying handler
- `0x17A3` (M6051) → stop drying handler
- `0x17A4` (M6052) → status query handler
- Default → dispatch exit (LAB_0800fa06)

### M6050 internals
Writes directly to SRAM via `*0x200002C8` (main_state) and `*0x20000324` (settings):
- `main_state + 0xD4` = 1 (heating enable flag)
- `main_state + 0x1374` = 100 (heater PWM zone 0)
- `main_state + 0x380` = target temp (thermal zone 0 setpoint)
- `settings + 0x34` = time_min × 60 (total timer in seconds)

### M6052 internals
Calls firmware functions:
- `FUN_080201b0(zone)` — box presence: 0=interlock active, 1=OK
- `FUN_080201e8(zone)` — heating state: 0=idle, 1=heating, 2=cooldown

## Drying Architecture (from firmware analysis)

- **Heating enable:** `main_state + 0xD4` (toggled by display button 0x40DA)
- **Power mode:** `main_state + 0xD8` (100% vs 70% power)
- **UV lamp:** `main_state + 0x10C` (also controls box detection enable)
- **Target temp zone 0:** `*(float*)(settings + 0x30)`
- **Target temp zone 1:** `*(float*)(settings + 0x40)`
- **Timer:** `settings + 0x38` (hours) × 3600 + `settings + 0x3C` (minutes) × 60 → `settings + 0x34` (total secs)
- **Heater PWM:** `main_state + 0x1374` (zone 0), `+0x137C` (zone 1), values 0-100
- **Box detection GPIO:** channels 4 (zone 0) and 5 (zone 1) via `FUN_0801c240`

### Existing M-codes (from firmware)
| Code | Hex | Function |
|------|-----|----------|
| M6010 | 0x177A | Start file execution (requires I + T params) |
| M6011 | 0x177B | Stop file execution |
| M6030 | 0x178E | Execute file from SD |
| M6040 | 0x1798 | System reset (writes SCB→AIRCR) |
| M6045 | 0x179D | Abort/restart |
| M8000-M8998 | — | Settings via `FUN_0801adec`, T param selects sub-command |
| M8500 | 0x2134 | Factory reset (restores defaults from flash) |

## Hardware Issue — Box Detection GPIO Damaged

Dev board box detection inputs are non-functional (all GPIO readings identical with box in/out).
Likely caused by 24V overvoltage on 12V system. GPIO channels 4+5 do not respond to switches.

**Workaround:** Patch `FUN_080201b0` at file `0x281B0` to always return 1 (OK):
```
0x281B0: 01 20 70 47  →  MOVS R0, #1; BX LR
```
This bypasses box detection for both zones. Not yet applied to the patched firmware.

## Files

### Firmware images
| File | Description |
|---|---|
| `chitu_e1_bootloader_plus_firmware.bin` | Clean combined image (bootloader + update firmware, no patches) |
| `chitu_e1_m105_patched.bin` | M105 sensor fix only (no dryer commands) |
| `chitu_e1_dryer_patched.bin` | **Full patch: M105 + M6050/M6051/M6052** — flash via stm32flash |
| `update_patched.GZH` | **GZH update file** — copy as `update.GZH` to SD card for stock board |
| `chitu_e1_update_decrypted.bin` | Clean decrypted update firmware (180 KB, unpatched) |

### Build scripts
| File | Description |
|---|---|
| `build_gzh_update.py` | **Main build script** — generates GZH with all patches (version bump, handlers, commands) |
| `patch_m105.py` | Step 1: M105 sensor patch (input: clean combined image → stm32flash) |
| `patch_dryer_cmds.py` | Step 2: adds M6050-M6052 (input: M105 patched → stm32flash) |
| `create_gzh.py` | Full re-encryption tool (reference; `build_gzh_update.py` is preferred) |

### Flashing methods
| Method | Use case | Command |
|--------|----------|--------|
| **USB GZH update** | Stock boards, no BOOT0 access | `cp update_patched.GZH /Volumes/USBDRIVE/update.GZH` → power cycle |
| **stm32flash** | Dev board, BOOT0 available | `stm32flash -b 115200 -m 8e1 -w chitu_e1_dryer_patched.bin -v -S 0x08000000 /dev/cu.usbserial-2110` |

### Analysis / reference
| File | Description |
|---|---|
| `chitu_e1_decompiled_full.c` | Complete Ghidra decompilation (941 functions, 38K lines) |
| `chitu_e1_re_report.h` | Reverse engineering report — struct layouts, function map |
| `update.GZH.orig` | Original encrypted firmware update (backup) |
| `update_disasm.asm` | Full disassembly of decrypted update firmware |
| `stm32f4-rdp-workaround/` | FPB exploit, firmware dumps, extraction notes |

## Ghidra Projects
- `/Users/stefan/ghidra_projects/ChituDryer_Update` — decrypted update firmware analysis
- `/Users/stefan/ghidra_projects/ChituDryer_Stock` — stock board firmware analysis
- `/Users/stefan/ghidra_projects/ChituDryer_Full` — Board 1 firmware analysis
- Annotation script: `/Users/stefan/ghidra_scripts/ChituDryerAnnotate.java`

## Tools
- `stm32flash` at `/opt/homebrew/bin/stm32flash` — UART flashing (boot mode, 115200 8E1)
- `openocd` at `/opt/homebrew/bin/openocd` — SWD (only works in boot mode)
- `arm-none-eabi-gcc` at `/opt/homebrew/bin/` — cross-compiler for handler code
- Ghidra 12.0.4 at `/Users/stefan/ghidra/ghidra_12.0.4_PUBLIC/`
- Python 3 with pyserial — serial communication and firmware patching
