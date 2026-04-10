#!/usr/bin/env python3
"""
Create a Chitu-format update.GZH from a patched combined firmware image.

Encryption: per-byte XOR with keystream derived from position + key.
Algorithm (reversed from fcn_08028114 in firmware):
  For each byte at position `i` within a 256-byte block at block_offset:
    shift = i % 24
    val   = (i*i + block_offset * 0x4BAD) >> shift   (32-bit unsigned)
    ks    = (val ^ key) & 0xFF
    encrypted[i] = plaintext[i] ^ ks

GZH file format:
  [0:4]   magic = 0xA9B0C0D0
  [4:8]   key   = 0x4BDE6541
  [8:12]  version/flag (preserved from original)
  [12:]   encrypted firmware data (variable length)
"""
import struct, sys, ctypes

MAGIC   = 0xA9B0C0D0
KEY     = 0x4BDE6541
BLOCK   = 0x800   # 2048 bytes per block (verified against original GZH)
CONST   = 0x4BAD
FW_BASE = 0x8000  # Firmware starts at this offset in the combined image


def chitu_crypt(data, key=KEY):
    """Encrypt or decrypt firmware data (symmetric XOR).
    Uses signed 32-bit arithmetic matching ARM ASR instruction."""
    out = bytearray(len(data))
    for blk_start in range(0, len(data), BLOCK):
        blk_off = blk_start // BLOCK
        scramble = ctypes.c_int32(blk_off * CONST).value
        blk_end = min(blk_start + BLOCK, len(data))
        for i in range(blk_end - blk_start):
            shift = i % 24
            val = ctypes.c_int32(i * i + scramble).value  # signed 32-bit
            val = val >> shift  # Python arithmetic shift right for negative values
            ks = (val ^ key) & 0xFF
            out[blk_start + i] = data[blk_start + i] ^ ks
    return bytes(out)


def main():
    combined_file = sys.argv[1] if len(sys.argv) > 1 else "chitu_e1_dryer_patched.bin"
    output_file   = sys.argv[2] if len(sys.argv) > 2 else "update.GZH"
    original_gzh  = "update.GZH.orig"  # Need original for version field

    print(f"Reading combined image: {combined_file}")
    with open(combined_file, 'rb') as f:
        combined = f.read()

    # Extract firmware portion (skip bootloader)
    fw = combined[FW_BASE:]

    # Trim trailing 0xFF to find actual end
    end = len(fw)
    while end > 0 and fw[end - 1] == 0xFF:
        end -= 1
    # Round up to next block boundary
    end = ((end + BLOCK - 1) // BLOCK) * BLOCK
    fw = fw[:end]
    print(f"Firmware size: {len(fw)} bytes (0x{len(fw):X})")

    # Read version field from original GZH
    try:
        with open(original_gzh, 'rb') as f:
            orig = f.read(12)
        version = struct.unpack_from('<I', orig, 8)[0]
        print(f"Version from {original_gzh}: 0x{version:08X}")
    except FileNotFoundError:
        # Try reading from update.GZH if .orig doesn't exist
        try:
            with open("update.GZH", 'rb') as f:
                orig = f.read(12)
            version = struct.unpack_from('<I', orig, 8)[0]
            print(f"Version from update.GZH: 0x{version:08X}")
        except FileNotFoundError:
            version = 0x00000000  # default
            print(f"No original GZH found, using version: 0x{version:08X}")

    # Verify decryption works by round-tripping first block
    test = chitu_crypt(fw[:256])
    test2 = chitu_crypt(test)
    assert test2 == fw[:256], "Round-trip encryption failed!"
    print("Encryption round-trip verified ✓")

    # Encrypt
    print("Encrypting...")
    encrypted = chitu_crypt(fw)

    # Build GZH
    header = struct.pack('<III', MAGIC, KEY, version)
    gzh = header + encrypted

    with open(output_file, 'wb') as f:
        f.write(gzh)
    print(f"\nWritten: {output_file} ({len(gzh)} bytes)")
    print(f"Copy to SD card root as 'update.GZH' and insert into the dryer.")


if __name__ == '__main__':
    main()
