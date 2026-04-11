# Chitu E1 Resin Dryer — Custom Firmware (V 1.3.0)

Firmware modification for the Chitu Systems E1 resin curing/drying station that adds full UART control — temperature/humidity sensors, drying start/stop, and status queries. Designed for integration with ESP32 + ESPHome for remote control via Home Assistant.

## What's added

| Command | Function | Example |
|---------|----------|---------|
| `M105` | Read temperature & humidity (2 sensors) | `ok T0:25 H0:23 T1:24 H1:24` |
| `M6050 I<°C> T<min>` | Start drying at target temp for N minutes | `M6050 I40 T30` |
| `M6051` | Stop drying | `ok Drying stopped` |
| `M6052` | Query box presence + heating state | `ok B0:1 B1:0 S0:0 S1:0` |
| `M6053 I<°C> T<min>` | Start drying **zone 1** (bottom chamber) | `M6053 I55 T60` |
| `M6054` | Stop drying **zone 1** | `ok Drying stopped` |

The original firmware returned **zero bytes** for M105 (the feature was never implemented). M6050/M6051/M6052 are entirely new commands.

---

## Flashing

### Method 1: USB update (recommended for stock boards)

No disassembly required. Works with the stock bootloader.

```bash
# Build the update file
python3 build_gzh_update.py

# Copy to USB stick (use cp, NOT Finder drag-and-drop)
cp update_patched.GZH /Volumes/YOURUSBDRIVE/update.GZH
```

1. Eject the USB stick safely from your Mac
2. Insert into the dryer's USB port
3. Power cycle the dryer
4. The display will go dark briefly during the update (~5 seconds)
5. After reboot, the display shows **V 1.3.0**

### Method 2: UART bootloader (for dev boards with BOOT0 access)

```bash
# Build the full combined image
python3 patch_m105.py          # Step 1: M105 sensor fix
python3 patch_dryer_cmds.py    # Step 2: add M6050/M6051/M6052

# Flash (hold BOOT0 while resetting the board first)
stm32flash -b 115200 -m 8e1 -w chitu_e1_dryer_patched.bin -v -S 0x08000000 /dev/cu.usbserial-2110
```

### Restoring stock firmware

```bash
# USB method: copy the original update file
cp update.GZH.orig /Volumes/YOURUSBDRIVE/update.GZH

# UART method:
stm32flash -b 115200 -m 8e1 -w chitu_e1_bootloader_plus_firmware.bin -v -S 0x08000000 /dev/cu.usbserial-2110
```

---

## UART Command Reference

**Settings:** 115200 baud, 8N1, line ending `\n`

Every command returns one or more lines ending with `\r\n`. The last line is always `ok N:<sequence>\r\n`.

### M105 — Read sensors

```
→ M105\n
← ok T0:25 H0:23 T1:24 H1:24\r\n
← ok N:1\r\n
```

| Field | Meaning | Range |
|-------|---------|-------|
| T0 | Temperature zone 0 (°C) | -40 to 80 |
| H0 | Humidity zone 0 (%) | 0 to 99 |
| T1 | Temperature zone 1 (°C) | -40 to 80 |
| H1 | Humidity zone 1 (%) | 0 to 99 |

- Zone 0 = sensor near top/lid, Zone 1 = sensor near heater
- 100% reliable (5 I2C retries per sensor)
- `T:124 H:99` = sensor not connected
- Safe to poll every 1–10 seconds

### M6050 / M6053 — Start drying (zone 0 / zone 1)

```
→ M6050 I55 T30\n       (zone 0 — top chamber)
← ok Drying T:55 I:30\r\n
← ok N:2\r\n
```

| Parameter | Meaning | Example |
|-----------|---------|---------|
| I | Target temperature in °C | `I40` = 40°C |
| T | Drying time in minutes | `T30` = 30 minutes |

- Each zone is controlled independently
- Enables heater at 100% PWM for the specified zone
- Firmware PID loop regulates to the target temperature
- Timer counts down from the specified duration
- Both zones can run simultaneously with different settings

**M6053** works identically but targets **zone 1** (bottom chamber).

### M6051 / M6054 — Stop drying (zone 0 / zone 1)

```
→ M6051\n
← ok Drying stopped\r\n
← ok N:3\r\n
```

- `M6051` stops zone 0, `M6054` stops zone 1
- Stops the specified zone only; the other zone keeps running
- Safe to call even when not drying

### M6052 — Query status

```
→ M6052\n
← ok B0:1 B1:0 S0:0 S1:0\r\n
← ok N:4\r\n
```

| Field | Meaning | Values |
|-------|---------|--------|
| B0 | Box/tray zone 0 | `1` = OK, `0` = missing/interlock |
| B1 | Box/tray zone 1 | `1` = OK, `0` = missing/interlock |
| S0 | Heating state zone 0 | `0` = idle, `1` = heating, `2` = cooldown |
| S1 | Heating state zone 1 | `0` = idle, `1` = heating, `2` = cooldown |

### M115 — Firmware version

```
→ M115\n
← ok CBD make it.Date:May  7 2025 Time:11:14:52\r\n
```

### M8500 — Factory reset

```
→ M8500\n
← ok N:5\r\n
```

Restores all settings to factory defaults from SPI flash.

---

## ESP32 / ESPHome Integration

### Wiring

```
ESP32 TX (GPIO17) ──→ Dryer RX
ESP32 RX (GPIO16) ←── Dryer TX
ESP32 GND         ──→ Dryer GND
```

The dryer uses **3.3V logic** (STM32F4). ESP32 GPIOs are 3.3V — direct connection is safe. Do **not** use a 5V Arduino without a level shifter.

### ESPHome configuration

```yaml
uart:
  id: dryer_uart
  tx_pin: GPIO17
  rx_pin: GPIO16
  baud_rate: 115200

sensor:
  - platform: custom
    lambda: |-
      // Custom sensor that polls M105 every 10s
      // Parse "ok T0:XX H0:XX T1:XX H1:XX"
    sensors:
      - name: "Dryer Temperature Zone 0"
        unit_of_measurement: "°C"
      - name: "Dryer Humidity Zone 0"
        unit_of_measurement: "%"
      - name: "Dryer Temperature Zone 1"
        unit_of_measurement: "°C"
      - name: "Dryer Humidity Zone 1"
        unit_of_measurement: "%"

button:
  - platform: template
    name: "Start Zone 0 (40°C 30min)"
    on_press:
      - uart.write: "M6050 I40 T30\n"

  - platform: template
    name: "Start Zone 1 (55°C 60min)"
    on_press:
      - uart.write: "M6053 I55 T60\n"

  - platform: template
    name: "Stop Zone 0"
    on_press:
      - uart.write: "M6051\n"

  - platform: template
    name: "Stop Zone 1"
    on_press:
      - uart.write: "M6054\n"

interval:
  - interval: 10s
    then:
      - uart.write: "M105\n"
```

### Parsing M105 response

The response format is fixed: `ok T0:<int> H0:<int> T1:<int> H1:<int>\r\n`

Example parser (pseudo-code):
```
line = read_until("\n")
if line.startswith("ok T0:"):
    parts = line.split()        # ["ok", "T0:25", "H0:23", "T1:24", "H1:24"]
    temp0  = int(parts[1][3:])  # 25
    humid0 = int(parts[2][3:])  # 23
    temp1  = int(parts[3][3:])  # 24
    humid1 = int(parts[4][3:])  # 24
```

---

## Build System

All patches are applied by Python scripts — no cross-compiler needed.

| Script | Input | Output | Purpose |
|--------|-------|--------|---------|
| `build_gzh_update.py` | `update.GZH.orig` + `chitu_e1_update_decrypted.bin` | `update_patched.GZH` | **Main build** — GZH for USB update |
| `patch_m105.py` | `chitu_e1_bootloader_plus_firmware.bin` | `chitu_e1_m105_patched.bin` | M105 sensor fix (stm32flash) |
| `patch_dryer_cmds.py` | `chitu_e1_m105_patched.bin` | `chitu_e1_dryer_patched.bin` | Add dryer commands (stm32flash) |

### Prerequisites

- Python 3 with `pyserial` (`pip install pyserial`)
- `stm32flash` (only for UART method): `brew install stm32flash`

### File inventory

| File | Description |
|------|-------------|
| `update_patched.GZH` | Ready-to-flash USB update (V 1.3.0) |
| `update.GZH.orig` | Original stock firmware update (backup) |
| `chitu_e1_dryer_patched.bin` | Full 512KB image for stm32flash |
| `chitu_e1_bootloader_plus_firmware.bin` | Clean unpatched combined image |
| `chitu_e1_update_decrypted.bin` | Decrypted stock firmware (for patching) |
| `chitu_e1_decompiled_full.c` | Full Ghidra decompilation (941 functions) |
| `PROJECT_STATUS.md` | Detailed technical notes and firmware internals |

---

## Technical Details

### How it works

The stock firmware has a G-code command dispatcher that handles M-codes via UART at 115200 baud. The M105 handler was never implemented (it jumps to a NOP stub). The M6050-M6052 codes are new additions in the unused 6000-range.

All patches are binary ARM Thumb instruction modifications:
- **4 injection points** (trampolines/branches redirected to our handler code)
- **504 bytes of handler code** placed inside an unused display page function (print progress page — never shown on a dryer)
- **BX LR stub** at the overwritten function's entry ensures safe no-op if called

### GZH encryption

The Chitu GZH update format uses a position-dependent XOR cipher with 2048-byte blocks. The `build_gzh_update.py` script uses an XOR-difference approach: it patches the original encrypted GZH byte-by-byte, which is guaranteed correct without needing to re-derive the keystream.

### Limitations

- M6050/M6051 control zone 0, M6053/M6054 control zone 1 independently
- Box detection depends on hardware GPIO switches — if damaged, `M6052 B` values may be incorrect
- The overwritten print progress page will show blank if somehow navigated to (impossible on a dryer)
- Settings written by M6050 may persist to SPI flash; use `M8500` to reset if needed
