/*
 * Chitu E1 Dryer Firmware – Reverse Engineering Report
 * =====================================================
 * Firmware base:  0x08008000  (application, from update.GZH decrypted)
 * Bootloader:     0x08000000  (chitu_e1_bootloader.bin, 24 KB)
 * MCU:            ARM Cortex-M4 + FPU  (STM32F4-family)
 * Build date:     May 7 2025  (string found in firmware)
 * GUI library:    emWin / STemWin  (GUI_ALLOC_Alloc found)
 * GUI assets:     Stored on EXTERNAL SPI flash (see GUI_DrawPicFromSPIFlash @ 0x0802ae13)
 *                 NOT in the MCU flash image.
 *
 * Analysis methodology:
 *   1. update.GZH decrypted with rolling-XOR key 0x4BDE6541 (block 0x800 bytes)
 *   2. Ghidra headless + r2+r2ghidra produced 1,114 functions
 *   3. String cross-references used to name all subsystem entry points
 *   4. Field offsets mapped by tracing *DAT_0801c800 (main device context pointer)
 *
 * ─────────────────────────────────────────────────────────────────────────────
 * SECTION 1: MAIN DEVICE STATE STRUCT
 * ─────────────────────────────────────────────────────────────────────────────
 *
 * The global pointer   DAT_0801c800  → points to this struct (singleton).
 * Total size: > 0x13F4 bytes (~5 KB).
 */

#ifndef CHITU_E1_RE_H
#define CHITU_E1_RE_H

#include <stdint.h>
#include <stdbool.h>

/* ── PID controller state (per temperature zone) ── */
typedef struct {
    float  Kp;           /* +0x08  proportional gain */
    float  Ki_accum;     /* +0x0C  integral accumulator (clamped) */
    float  output;       /* +0x10  current PID output (PWM duty, clamped) */
    float  prev_error;   /* +0x14  previous error (for derivative) */
    float  setpoint;     /* +0x18  target temperature (°C, float) */
    /* +0x1C … +0x4C: PID config: Kd, output_min/max, dt, etc. */
    float  Kd;           /* estimated */
    float  output_min;
    float  output_max;
    float  dt;
    code_t *on_setpoint_change_cb; /* +0x4C callback when target changes */
    /* +0x50: current ADC reading (raw * 10) */
    int32_t adc_raw_x10; /* +0x50 */
    float  error;        /* +0x54 last error × Kp */
    float  filtered_err; /* +0x58 derivative term input */
    /* +0x7C..0x89: NTC look-up table pointers / calibration flags */
    uint16_t ntc_lut[2]; /* +0x7C  NTC correction table (2 channels) */
    uint8_t  enable_ch0; /* +0x88  channel 0 heating enabled */
    uint8_t  enable_ch1; /* +0x89  channel 1 heating enabled */
    /* +0x0B: per-channel heating-active flags (3 bytes) */
    uint8_t  heat_active[3]; /* +0x0B */
    /* +0x10..+0x2C: target temp per channel (int32, up to 3 channels) */
    int32_t  target_temp[3]; /* +0x10, +0x14, +0x18 */
    /* +0x1C..0x28: actual / setpoint (int32) */
    int32_t  setpoint_int[3];/* +0x1C */
    int32_t  pwm_output[3];  /* +0x28 PID output per channel */
} PidZone_t;  /* approximately 0x94 bytes */

/* ── Temperature zone config (read from flash/EEPROM settings) ── */
typedef struct {
    float  max_temp_ch0; /* +0x14  max allowed temp channel 0 (default 80°C) */
    float  max_temp_ch1; /* same for channel 1 */
    float  target_ch0;   /* +0x30  drying target temperature ch0 (float, °C) */
    float  target_ch1;   /* +0x40  drying target temperature ch1 */
    int32_t t_threshold1;/* +0xDC  lower temp threshold for fan speed zone 1 */
    int32_t t_threshold2;/* +0xE0  upper temp threshold for fan speed zone 2 */
    int32_t t_threshold3;/* +0xE4  upper temp threshold for fan speed zone 3 */
    int32_t t_threshold4;/* +0xE8  upper temp threshold for fan speed zone 4 */
    int32_t t_threshold5;/* +0xEC  upper temp threshold for fan speed zone 5 */
    int32_t fan_speed_z1;/* +0xF0  fan speed (%) for zone 1 (cool) */
    int32_t fan_speed_z2;/* +0xF4  fan speed (%) for zone 2 */
    int32_t fan_speed_z3;/* +0xF8  fan speed (%) for zone 3 */
    int32_t fan_speed_z4;/* +0xFC  fan speed (%) for zone 4 */
    int32_t fan_speed_max;/*+0x100  fan speed (%) at/above full target temp */
    int32_t slope_below; /* +0x104  fan speed slope (below threshold) */
    int32_t slope_above; /* +0x108  fan speed slope (above threshold) */
} TempZoneConfig_t;

/* ── Main device state struct ─ accessed via *DAT_0801c800 ── */
typedef struct ChituE1State {

    /* --- 0x0000..0x00FF: SD/USB file system state ------------------- */
    /* (varies – file path buffers, open file handles, SD mount status) */
    uint8_t  _pad_fs[0x100];

    /* --- 0x0100..0x01FF: G-code / command parser state -------------- */
    uint8_t  tool_count;         /* +0x100  number of extruder tools */
    /* +0x101..0x11F: per-tool active/error flags */
    uint8_t  tool_flags[0x1F];
    uint8_t  _pad_gcode[0xE0];   /* +0x120..0x1FF */

    /* --- 0x0200..0x02FF: network / WiFi state ----------------------- */
    uint8_t  _pad_wifi[0x100];

    /* --- 0x0300..0x035F: motion / print job counters ---------------- */
    uint8_t  _pad_motion[0x60];

    /* --- 0x0364..0x03BF: temperature zone array -------------------- */
    /*   param + 0x364  =  start of ThermalZone_t array
     *   passed to FUN_08022734 (PID), FUN_08022968 (set target),
     *             FUN_080229cc (read temp), FUN_080229f4 (filtered read)  */
    PidZone_t thermal[2];        /* +0x364  zone 0=heater, zone 1=secondary */

    /* --- 0x03C4..0x03FF: per-zone schedule / profile -------------- */
    uint8_t  zone_schedule[0x3C];

    /* --- 0x0400..0x049F: serial / UART command buffers ------------ */
    uint8_t  _pad_uart[0xA0];

    /* --- 0x04A0..0x04FF: display / UI state ----------------------- */
    uint8_t  _pad_ui[0x60];

    /* --- 0x0500..0x053F: file-list / directory cache --------------- */
    uint8_t  _pad_filelist[0x40];

    /* --- 0x0538: misc flags ---------------------------------------- */
    uint8_t  misc_flag_538;      /* +0x538  (cleared by update handler) */
    uint8_t  _pad53[0x1];

    /* --- 0x053A..0x05BF: ------------------------------------------ */
    uint8_t  _pad_53a[0x86];

    /* --- 0x05C4..0x05FF: current SD/USB file context --------------- */
    /*   chitu_initial_file_autorun, update.GZH detection live here   */
    char     current_file[0x28]; /* +0x5C4  active file name (UTF-8) */
    uint8_t  file_flag_c8;       /* +0x5C8  1 = CNC mode (no auto-run)  */
    uint8_t  file_flag_c9;       /* +0x5C9  1 = autorun inhibited        */
    uint8_t  file_flag_ca;       /* +0x5CA  1 = filament-change pending  */
    uint8_t  file_flag_cb;       /* +0x5CB  1 = pause requested          */
    uint8_t  file_flag_cc;       /* +0x5CC  used by print start check    */
    uint8_t  _pad5d[3];
    uint32_t gcode_bytes_remaining; /* +0x5E8 */
    uint32_t gcode_total_bytes;     /* +0x5EC */
    uint32_t gcode_offset;          /* +0x5F0 */
    uint8_t  _pad5f[0x0C];

    /* --- 0x0600..0x061B: ------------------------------------------ */
    uint8_t  _pad600[0x1C];

    /* --- 0x061C..0x06FF: G-code line buffer (secondary) ----------- */
    uint8_t  gcode_line_buf[0xE4]; /* +0x61C  256-byte line buffer */

    /* --- 0x0700..0x09FF: print thumbnail / preview buffer --------- */
    uint8_t  _pad_preview[0x300];

    /* --- 0x0A00..0x0A3F: FAT long-name buffer ---------------------- */
    uint8_t  longname_buf[0x40];  /* +0xA00 */

    /* --- 0x0A40..0x10AB: SD file entry list / directory entries ---- */
    uint8_t  _pad_entries[0x66C]; /* +0xA40 */

    /* --- 0x10AC..0x10AF: pending-command flag ---------------------- */
    uint32_t pending_cmd;        /* +0x10AC 0 = idle */

    /* --- 0x10B0: print-in-progress flag ---------------------------- */
    uint8_t  printing;           /* +0x10B0 */
    uint8_t  _pad_10b[3];

    /* --- 0x1114..0x1137: loop tick counters ----------------------- */
    uint32_t loop_tick;          /* +0x1134  monotonic tick counter (100 ms steps) */
    uint32_t last_gcode_tick;    /* +0x1138 */
    uint32_t loop_tick_1140;     /* +0x113C  callback every 2nd tick */
    uint32_t loop_tick_1144;     /* +0x1140  callback every 9th tick */
    uint32_t loop_tick_114c;     /* +0x114C  callback every 27th tick */
    uint8_t  _pad114[0x14];

    /* --- 0x1172..0x12FF: SPI flash write staging buffer ----------- */
    uint8_t  spi_flash_buf[0x18E]; /* +0x1172  256-byte page write buffer */
    uint8_t  _pad12[0x72];

    /* --- 0x1374..0x1383: heater PWM duty cycle (4 zones × uint32) - */
    /*   written by dryer_tick (thunk_FUN_0801b92c)                    */
    /*   applied to hardware by FUN_0801c3b4 → FUN_080241f0            */
    uint32_t heater_pwm[4];      /* +0x1374  PWM channels 7..10 (0–255) */

    /* --- 0x1384..0x138B: fan PWM duty cycle (2 fans × uint32) ----- */
    uint32_t fan_pwm[2];         /* +0x1384  PWM channels 0..1 (0–255)  */

    /* --- 0x138C: update progress percentage ------------------------ */
    uint32_t update_progress_pct; /* +0x138C  0..100 during firmware flash */

    /* --- 0x139E: misc update/error flag ----------------------------- */
    uint8_t  update_flag_139e;   /* +0x139E */
    uint8_t  _pad139[1];

    /* --- 0x13A0..0x13B3: ------------------------------------------ */
    uint8_t  _pad13a[0x14];

    /* --- 0x13B4: UART echo flag ------------------------------------ */
    uint8_t  uart_echo_enabled;  /* +0x13B4 */
    uint8_t  _pad13b[0x1D];

    /* --- 0x13D0..0x13D7: ------------------------------------------ */
    uint8_t  state_13d0;         /* +0x13D0 */
    uint8_t  _pad13d[7];

    /* --- 0x13D8: return code of last G-code operation -------------- */
    int32_t  last_gcode_result;  /* +0x13D8  -1 = none pending */

    /* --- 0x13DC..0x13EB: ------------------------------------------ */
    uint8_t  _pad13dc[0x10];

    /* --- 0x13EC: update extra data (from GZH header sub-field) ----- */
    uint32_t update_extra;       /* +0x13EC */

    /* --- 0x13F4: G-code string pointer (multi-line buffer) --------- */
    char    *gcode_multiline_ptr; /* +0x13F4  pointer into G-code buffer */

} ChituE1State_t;  /* minimum ~0x13F8 bytes */

/* Global singleton pointer (held in RAM, written at boot) */
extern ChituE1State_t *g_device;  /* ≈ *DAT_0801c800 */


/* ─────────────────────────────────────────────────────────────────────────────
 * SECTION 2: TEMPERATURE / PID SUBSYSTEM
 * ─────────────────────────────────────────────────────────────────────────────
 *
 * Sensor hardware:  TWO I2C temperature+humidity sensors (SHT30/SHT31 family)
 *   Channel 0: I2C bus 1 – read by FUN_0800f4b4() / FUN_08009594() / FUN_08009620()
 *   Channel 1: I2C bus 2 – read by FUN_0800f516() / FUN_080095da() / FUN_08009670()
 *
 * ADC → Temperature formula (integer version):
 *   raw_16bit = byte[0]*256 + byte[1]           (big-endian from sensor)
 *   T_celsius_x10 = (raw_16bit * 0x4074) >> 16) − 4000
 *   T_celsius     = T_celsius_x10 / 10.0         (= −40 to ~165°C range)
 *   Note: 0x4074/65536 ≈ 175/65535 → SHT3x standard conversion
 *
 * ADC → Humidity formula:
 *   raw_rh = byte[2]*256 + byte[3]
 *   RH_pct = (raw_rh * 100) >> 16               (0–100 %)
 */

/* Read raw temperature+humidity, channel 0 (I2C1) */
/* FUN_08009594 @ 0x08009594 */
void ntc_read_ch0(int *out_temp_celsius, int *out_rh_pct);

/* Read raw temperature+humidity, channel 1 (I2C2) */
/* FUN_080095da @ 0x080095da */
void ntc_read_ch1(int *out_temp_celsius, int *out_rh_pct);

/* Read temperature with zone-calibration offset applied (float, °C) */
/* FUN_08022aa8 @ 0x08022aa8 */
float temp_read_calibrated(void *zone_state, int channel /* 0 or 1 */);

/* Read temperature with predictive correction filter */
/* FUN_080229f4 @ 0x080229f4 */
int   temp_read_filtered(void *zone_state, int channel);

/* Get current temperature (dispatcher, returns integer °C) */
/* FUN_080229cc @ 0x080229cc */
int   temp_get_current(void *zone_state, int channel);

/* Set drying target temperature
 *   channel 0: heater element (max 80°C = 0x50)
 *   channel 1: secondary zone (max from config)
 *   enable_heat: 1 = start heating, 0 = setpoint only (no heat)
 */
/* FUN_08022968 @ 0x08022968 */
void  temp_set_target(void *zone_state, int channel, int target_celsius, int enable_heat);

/* PID controller tick – call every 100 ms
 *   Reads sensor, computes Kp/Ki/Kd terms, writes PID output to zone_state
 *   Kp/Ki/Kd stored in *DAT_080228a8 (separate config struct)
 *   Output clamped to [DAT_080228bc, DAT_080228c4]
 */
/* FUN_08022734 @ 0x08022734 */
void  pid_update(void *zone_state);

/* NTC look-up table index from raw ADC (integer, returns 16-bit table value) */
/* FUN_080228d8 @ 0x080228d8  (channel 0) */
/* FUN_080228da @ 0x080228da  (channel 1) */
uint16_t ntc_lut_lookup_ch0(void *zone_state, int channel, int adc_offset);
uint16_t ntc_lut_lookup_ch1(void *zone_state, int channel, int adc_offset);
/* NOTE: if (adc_offset + 30) > 127 → logs "temperature T:%d exceed the range!" */


/* ─────────────────────────────────────────────────────────────────────────────
 * SECTION 3: FAN SPEED CONTROL
 * ─────────────────────────────────────────────────────────────────────────────
 *
 * Fan speed is computed in the main dryer tick (thunk_FUN_0801b92c / dryer_tick)
 * based on the ratio of current temperature to setpoint, with 5 speed zones
 * defined in TempZoneConfig_t:
 *
 *   actual < t_threshold1  →  fan_speed_z1 (lowest)
 *   actual < t_threshold2  →  fan_speed_z2 or proportional
 *   actual < t_threshold3  →  fan_speed_z3 or proportional
 *   actual >= t_threshold5 →  fan_speed_max (full speed)
 *
 * The computed percentage is stored at state->fan_pwm[channel] (0x1384).
 * It is applied to hardware by FUN_0801c3b4 → FUN_080241f0 → FUN_0800f02c.
 *
 * PWM channels:
 *   Channel 0: fan 1  (mapped to PWM timer channel, table @ DAT_08024230)
 *   Channel 1: fan 2
 *   Channels 7–10: heater elements (4 heater zones max)
 *
 * Fan speed UI labels seen:
 *   "Fan Speed(%):"  @ 0x080320e8
 *   "PWM(%):"        @ 0x080320e0
 */

/* Apply PWM duty cycle to hardware channel (0–10)
 *   Uses DAT_08024230 + channel*12 = {GPIO, mask, cached_duty}
 *   Calls FUN_0800f02c(channel_desc, duty_0_to_255)
 */
/* FUN_080241f0 @ 0x080241f0 */
void pwm_set_channel(uint32_t timer_base_plus_3fc, uint32_t channel, uint32_t duty_0_255, int force);

/* Output tick for all fan and heater PWM channels (called periodically) */
/* FUN_0801c3b4 @ 0x0801c3b4 */
void pwm_output_tick(void *ctx, void *device_state);


/* ─────────────────────────────────────────────────────────────────────────────
 * SECTION 4: MAIN DRYER CONTROL LOOP  (dryer_tick)
 * ─────────────────────────────────────────────────────────────────────────────
 *
 * Called by SysTick / timer ISR at ~100 ms intervals.
 * Symbol: thunk_FUN_0801b92c  (jumps to FUN_0801b92c).
 * Entry conditions: *DAT_0801b998 != NULL (device initialized).
 *
 * Execution sequence every tick:
 *   1. FUN_0800e4e4(0,0,0,10)       – clear watchdog / timer flag
 *   2. Increment *DAT_0801b99c      – monotonic tick counter
 *   3. Every 2nd tick:  callback @ *state + 0x113C  (sensor fast poll)
 *   4. Every 9th tick:  callback @ *state + 0x1140  (mid-rate task)
 *   5. Every 27th tick: callback @ *state + 0x114C  (slow task)
 *   6. If 100 ms elapsed since last PID run (checked via DAT_08024c34+0x4C):
 *        pid_update(state + 0x364)  – run PID, update heater PWM
 *   7. Compute fan_pwm[0] based on temp vs setpoint zones
 *   8. Compute fan_pwm[1] for secondary fan
 *   9. Increment state->loop_tick  (state + 0x1134)
 *
 * Note: DAT_08024c34 is a separate timer-state struct with field +0x44 = last_tick
 *       and +0x48 = reset-after-PID-run flag.
 */

/* dryer_tick @ 0x0801b92c (via thunk at 0x0800c1fc) */
void dryer_tick(void);


/* ─────────────────────────────────────────────────────────────────────────────
 * SECTION 5: FILAMENT SENSOR
 * ─────────────────────────────────────────────────────────────────────────────
 *
 * Error strings found in firmware:
 *   "filament 1 error,please replace filament 1!" @ 0x08031160
 *   "filament 2 error,please replace filament 2!" @ 0x0803118C
 *   "Filament 1 is exhausted,please replace it!"  @ 0x080313D8
 *   "Filament 2 is exhausted,please replace it!"  @ 0x08031404
 *   "Filament is exhausted,please replace it!"    @ 0x08031430
 *   "Do you want to replace filament?"            @ 0x080323F4
 *
 * Filament state is tracked via state->file_flag_ca (+0x5CA):
 *   bit set → filament-change pending, dryer halts current job.
 *
 * Sensor inputs appear to be GPIO interrupt-driven (not polled),
 * with debouncing via the 100 ms tick callbacks at +0x113C / +0x1140.
 */


/* ─────────────────────────────────────────────────────────────────────────────
 * SECTION 6: FIRMWARE UPDATE MECHANISM
 * ─────────────────────────────────────────────────────────────────────────────
 *
 * File: update.GZH
 * Header: 12 bytes = [magic:4][file_key:4][crc_word:4]
 *   magic    = 0xA9B0C0D0, file_key = 0x4BDE6541, crc_word = 0xD83073F5
 *
 * Encryption: Chitu quadratic-XOR stream cipher
 *   For byte `lc` in block `block_num` (block size = 0x800):
 *     bn       = (0x4BAD * block_num) & 0xFFFFFFFF
 *     shift    = lc % 24
 *     xs       = ((lc * lc) + bn) & 0xFFFFFFFF
 *     key_byte = (xs >> shift) ^ file_key
 *     plain    = cipher ^ key_byte
 *   Note: encrypt == decrypt (self-inverse)
 *
 * Decryption tool: chitu_e1_decrypt.py (round-trip verified against original GZH)
 *
 * Detection: FUN_0801c434 / update_file_handler @ 0x0801c434
 *   1. Reads 8 bytes from start of update.GZH via SD/USB
 *   2. Checks magic at +0x00 == DAT_0801c814  (proprietary 4-byte magic)
 *   3. If +0x04 == 0xFFFFFFFF → old-style full-erase update
 *      else                   → block-level incremental update
 *   4. Validates "update.GZH" filename suffix via strcmp
 *   5. Checks "Same firmware would not update!" condition (version field match)
 *   6. Issues G-code M6040 I1200 to confirm update start
 *
 * Flash programming: FUN_08028c1c @ 0x08028c1c  = write_dat_to_flash()
 *   Arguments: (flash_address, source_buffer, byte_count)
 *   - Unlocks STM32 flash with key sequence: FUN_08009954(0xF1)
 *   - Selects and erases sector based on destination address:
 *       0x08000000 → sector 0  (bootloader)
 *       sectors 1–11 mapped to DAT_0802xxxx address constants
 *   - Programs half-words via FUN_08009a34(addr, halfword)
 *   - Re-locks flash via FUN_08009a08()
 *
 * STM32 internal flash sector map (F4 family, assuming 1 MB variant):
 *   Sector 0:  0x08000000 – 0x08003FFF  (16 KB  – bootloader)
 *   Sector 1:  0x08004000 – 0x08007FFF  (16 KB  – bootloader cont.)
 *   Sector 2:  0x08008000 – 0x0800BFFF  (16 KB  – application start)
 *   Sector 3:  0x0800C000 – 0x0800FFFF  (16 KB)
 *   Sector 4:  0x08010000 – 0x0801FFFF  (64 KB)
 *   Sectors 5–11: 0x08020000+ (128 KB each)
 *
 * NOTE: The bootloader (at 0x08000000) validates the application before boot
 *       by checking the application's vector table.  Patched firmware MUST
 *       be re-encrypted with the same rolling-XOR key before placing on SD.
 */

/* SD/USB file-open for update.GZH detection */
/* FUN_0801c434 @ 0x0801c434 */
uint32_t update_file_handler(int drive_ctx, void *start_cb, void *done_cb,
                             uint32_t param4, int silent);

/* Internal flash write (sectors 0–11) */
/* FUN_08028c1c @ 0x08028c1c */
uint32_t write_dat_to_flash(uint32_t flash_addr, const void *src, uint32_t byte_count);

/* Bootloader flash write (via bootloader passthrough) */
/* FUN_08028c1c called with 0x8000000 base → uses sector-erase ID 0 */


/* ─────────────────────────────────────────────────────────────────────────────
 * SECTION 7: GUI / DISPLAY SUBSYSTEM
 * ─────────────────────────────────────────────────────────────────────────────
 *
 * Library: emWin / STemWin (Segger)
 *   - GUI_ALLOC_Alloc identified @ 0x08015154
 *   - GUITASK mutex strings found (GUITASK.c / GUI_Unlock)
 *
 * CRITICAL FINDING: All bitmap / icon assets are stored on EXTERNAL SPI flash.
 *   Function "GUI_DrawPicFromSPIFlash" @ 0x0802ae13 renders pictures by:
 *     1. Reading pixel data from SPI flash at a given offset
 *     2. Passing to emWin's bitmap draw routine
 *   This means the MCU firmware image contains NO embedded bitmaps.
 *   To access / extract GUI assets you need to DUMP THE EXTERNAL SPI FLASH
 *   (typically W25Q64 or similar, 4–8 MB, on the main board SPI bus).
 *
 * How to dump external SPI flash:
 *   Option A) UART backdoor: The firmware exposes an SPI flash read via
 *             FUN_0800f59c (read page), callable indirectly through the
 *             "M6030" G-code command (0x08025048).
 *   Option B) Clip directly to the SPI flash IC on the PCB with a clip
 *             adapter and read with a CH341A programmer.
 *   Option C) Add JTAG/SWD debug firmware that forwards SPI flash reads.
 *
 * Known UI screen messages (strings in MCU flash):
 *   "Preheating"               @ 0x08030F5C
 *   "Sensor error or power is not enough!"  @ 0x0803127C
 *   "Machine parameter error!" @ 0x0803137C
 *   "firmware update now!"     @ 0x080314CC
 *   "Please 111preheat first!" @ 0x080314B0   (typo in firmware)
 *   "Same firmware would not update!" @ 0x0803123C
 *   "PWM(%):"                  @ 0x080320E0
 *   "Fan Speed(%):"            @ 0x080320E8
 *   "UI Error: UI"             @ 0x080326C4
 *   "Firmware Error: Firmware" @ 0x0803274C
 *   "If automatic adjustment error," @ 0x08031A26
 */

/* Draw picture from external SPI flash at given page offset */
/* FUN_0802ae13 @ 0x0802ae13 – symbol: GUI_DrawPicFromSPIFlash */
void GUI_DrawPicFromSPIFlash(uint32_t spi_page_addr, int x, int y);

/* Read one page (256 bytes) from external SPI flash */
/* FUN_0800f59c @ 0x0800f59c */
void spi_flash_read_page(uint8_t *dst, uint32_t page_addr_24bit, int count);

/* Write one page (256 bytes) to external SPI flash */
/* FUN_0800f604 @ 0x0800f604 */
void spi_flash_write_page(uint32_t page_addr_24bit, uint32_t data_word);


/* ─────────────────────────────────────────────────────────────────────────────
 * SECTION 8: HOW TO PATCH / MODIFY THE FIRMWARE
 * ─────────────────────────────────────────────────────────────────────────────
 *
 * 1. DECRYPT:
 *      python3 chitu_e1_decrypt.py --decrypt update.GZH decrypted.bin
 *
 * 2. MODIFY:
 *      Open decrypted.bin in Ghidra (base 0x08008000, ARM Cortex-M Thumb-2)
 *      or use a hex editor for simple constant patches.
 *
 *      Key patch targets:
 *        a) Max temperature cap (heater ch0):
 *           FUN_08022968 @ 0x08022968: constant 0x50 (80°C) at offset +0x10
 *           → Change to e.g. 0x5A (90°C) for higher drying.
 *        b) PID gains (Kp, Ki, Kd):
 *           Located in the config struct pointed to by DAT_080228a8.
 *           Offsets: +0x08=Kp, +0x0C=Ki, +0x10=Kd (float32 little-endian).
 *        c) Fan speed zone thresholds (TempZoneConfig_t):
 *           Located in struct at DAT_08024c38 (+0xDC, +0xE0, +0xE4, +0xE8, +0xEC).
 *        d) Drying time / schedule:
 *           G-code M6040 parameters, handled in update_file_handler.
 *        e) Firmware version check bypass (skip "Same firmware" check):
 *           Patch the branch at ~0x0801c540 to always proceed.
 *
 * 3. RE-ENCRYPT:
 *      python3 chitu_e1_decrypt.py --encrypt decrypted.bin patched.GZH
 *      (The script uses the same rolling-XOR key 0x4BDE6541)
 *
 * 4. DEPLOY:
 *      Copy patched.GZH as "update.GZH" to the root of an SD card.
 *      Power on the dryer with SD inserted → update starts automatically.
 *
 * WARNING: Flashing a corrupt firmware will require JTAG/SWD recovery or
 *          a pre-programmed bootloader-rescue SD card.  The bootloader at
 *          0x08000000 must remain intact.
 */

#endif /* CHITU_E1_RE_H */
