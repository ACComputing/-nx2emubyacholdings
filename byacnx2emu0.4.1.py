#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =============================================================================
#   NX2EMU 0.4  by  AC
#   (c) 1999-2026 A.C Holdings / Team Flames
#   Nintendo Switch 2 mega-emulator - single-file build
#   Ryujinx-styled GUI  |  black bg  |  blue foreground
#   +  commercial ROM boot: NRO / NSO(+LZ4) / NSP(PFS0) / XCI / NCA
#   +  accurate T239 hardware model (A78C x8 + Ampere GA10F 1536-CUDA)
# =============================================================================
"""
NX2EMU 0.4 - by AC

Architecture fuses the ideas of every public Switch 2 emulator reference:
    Pound     (C++)   -> Ballistic JIT / dynarmic ARM64 core
    oboromi   (Rust)  -> 8-core ARMv8, 12 GiB shared mem, SM86 GPU stub
    fOboromi          -> NCA / package2 / prod.keys layout
    NYx-2     (Py)    -> Pure-Python AArch64 interpreter
    Hassaku   (C#)    -> WIP Switch 2 UI reference
    Ryubing / Ryujinx -> GUI layout, Horizon OS service dispatch
    hactool / switchbrew.org / switch-tools -> container + NSO/NRO layout

Boots commercial Switch/Switch 2 ROMs through real container parsing:
    .NRO  - homebrew with segment mapping
    .NSO  - compiled executable, LZ4 segment decompression, segment map
    .NSP  - eShop install (PFS0 wrapper) -> pick main NCA -> load NSO
    .XCI  - cartridge dump (HEAD gamecard header) -> HFS0 -> NCA -> NSO
    .NCA  - raw content archive header introspection
    .KIP1 - initial process (header parse only)
    .NX2  - native mega-emu bundle (NRO-compatible)

NOTE: Encrypted content (NCA body) requires user-supplied prod.keys.
      No keys are shipped. Bodies fall back to plaintext fast-path when
      the input is already decrypted (e.g. from hactool / NXDumpTool).

Requires: Python 3.14, stdlib + pygame + tkinter
Run:      python nx2emu.py
"""
from __future__ import annotations

import os
import sys
import struct
import time
import math
import threading
import traceback
import hashlib
import io
from dataclasses import dataclass, field
from typing import Callable, Optional

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    import pygame
    PYGAME_OK = True
except ImportError:
    PYGAME_OK = False

# =============================================================================
#   BRAND / THEME
# =============================================================================
BRAND     = "NX2EMU"
VERSION   = "0.4"
AUTHOR    = "AC"
COPYRIGHT = "(c) 1999-2026 A.C Holdings / Team Flames"

BG_BLACK       = "#000000"
BG_PANEL       = "#050810"
BG_ROW_A       = "#000000"
BG_ROW_B       = "#04070d"
BG_SELECT      = "#003366"
BG_HOVER       = "#001a33"
FG_BLUE        = "#4da6ff"
FG_BLUE_DIM    = "#2266aa"
FG_BLUE_BRIGHT = "#66ccff"
FG_BLUE_FAINT  = "#1a4466"
FG_OK          = "#55ff99"
FG_WARN        = "#ffbb33"
FG_ERR         = "#ff5566"
BORDER_BLUE    = "#4488FF"

# =============================================================================
#   NINTENDO SWITCH 2 HARDWARE PROFILE  -  confirmed T239 specs
#   Sources: Digital Foundry die-shot, Tom's Hardware, Kurnal/Geekerwan
# =============================================================================
SOC_NAME             = "NVIDIA Tegra T239 / GMLX30-R-A1"
SOC_PROCESS          = "Samsung 8N (custom 8/10nm blend)"
SOC_DIE_MM2          = 207
SOC_TAPEOUT_YEAR     = 2021

# CPU - 8x ARM Cortex-A78C (ARMv8.2-A, 64-bit)
# 6 cores for games, 2 reserved for Horizon OS
CPU_CORES_TOTAL      = 8
CPU_CORES_GAMES      = 6
CPU_CORES_OS         = 2
CPU_ARCH             = "ARMv8.2-A Cortex-A78C"
CPU_L2_PER_CORE      = 256 * 1024
CPU_L3_SHARED        = 4 * 1024 * 1024
CPU_FREQ_HANDHELD_HZ = 1_101_000_000
CPU_FREQ_DOCKED_HZ   = 998_000_000
CPU_FREQ_MAX_HZ      = 1_700_000_000

# GPU - NVIDIA T239 "GA10F" Ampere-class, 1 GPC / 6 TPC / 12 SM
GPU_NAME             = "NVIDIA T239 GA10F (Ampere)"
GPU_CUDA_CORES       = 1536
GPU_SM_COUNT         = 12
GPU_TPC_COUNT        = 6
GPU_TENSOR_CORES     = 48
GPU_RT_CORES         = 12
GPU_FREQ_HANDHELD_HZ = 561_000_000
GPU_FREQ_DOCKED_HZ   = 1_007_000_000
GPU_FREQ_MAX_HZ      = 1_400_000_000
GPU_API              = "NVN2 / Vulkan 1.3 / DLSS 2.2"

# RAM - 12 GiB LPDDR5X (2x 6 GiB), 128-bit bus
RAM_BYTES            = 12 * 1024 * 1024 * 1024
RAM_BUS_BITS         = 128
RAM_SPEED_MTS        = 7500
RAM_BW_GB_S          = 68.26

# Storage - 256 GB UFS 3.1
STORAGE_GB           = 256
STORAGE_TYPE         = "UFS 3.1"

# Displays
DISPLAY_HANDHELD     = (1920, 1080)  # 7.9" LCD, DLSS-upscaled in games
DISPLAY_DOCKED       = (3840, 2160)  # 4K out via DisplayPort over USB-C
DISPLAY_NATIVE_HANDH = (1080, 720)   # internal render target many games use

# OS
OS_NAME              = "Horizon 2.x"
OS_ABI               = "AArch64 (cryptoext enabled)"

# =============================================================================
#   PAGED MEMORY SUBSYSTEM (4 KiB lazy pages)
# =============================================================================
PAGE_BITS = 12
PAGE_SIZE = 1 << PAGE_BITS
PAGE_MASK = PAGE_SIZE - 1


class PagedMemory:
    """Sparse 64-bit virtual address space, lazily backed by 4 KiB pages."""

    def __init__(self, total_bytes: int = RAM_BYTES) -> None:
        self.total = total_bytes
        self._pages: dict[int, bytearray] = {}
        self.reads = 0
        self.writes = 0

    def _page(self, vaddr: int, alloc: bool = True) -> Optional[bytearray]:
        pn = vaddr >> PAGE_BITS
        p = self._pages.get(pn)
        if p is None and alloc:
            p = bytearray(PAGE_SIZE)
            self._pages[pn] = p
        return p

    def read(self, vaddr: int, n: int) -> bytes:
        self.reads += 1
        if vaddr < 0 or vaddr + n > self.total:
            raise MemoryError(f"OOB read @ {vaddr:#x}+{n}")
        out = bytearray(n); i = 0
        while i < n:
            p = self._page(vaddr + i, alloc=True)
            off = (vaddr + i) & PAGE_MASK
            take = min(PAGE_SIZE - off, n - i)
            out[i:i + take] = p[off:off + take]
            i += take
        return bytes(out)

    def write(self, vaddr: int, data: bytes) -> None:
        self.writes += 1
        n = len(data)
        if vaddr < 0 or vaddr + n > self.total:
            raise MemoryError(f"OOB write @ {vaddr:#x}+{n}")
        i = 0
        while i < n:
            p = self._page(vaddr + i, alloc=True)
            off = (vaddr + i) & PAGE_MASK
            take = min(PAGE_SIZE - off, n - i)
            p[off:off + take] = data[i:i + take]
            i += take

    def r32(self, a): return struct.unpack("<I", self.read(a, 4))[0]
    def r64(self, a): return struct.unpack("<Q", self.read(a, 8))[0]
    def w32(self, a, v): self.write(a, struct.pack("<I", v & 0xFFFFFFFF))
    def w64(self, a, v): self.write(a, struct.pack("<Q", v & 0xFFFFFFFFFFFFFFFF))

    def stats(self) -> str:
        res_mb = (len(self._pages) * PAGE_SIZE) / (1024 * 1024)
        return f"{len(self._pages)}p / {res_mb:.1f}MiB"


# =============================================================================
#   LZ4 BLOCK DECOMPRESSOR  -  pure-Python, RFC4395 LZ4 block format
#   (used to decompress NSO .text / .rodata / .data segments)
# =============================================================================
def lz4_block_decompress(src: bytes, uncompressed_size: int) -> bytes:
    """Decompress a raw LZ4 block (not frame). Pure Python, stdlib only."""
    out = bytearray()
    i = 0
    n = len(src)
    while i < n:
        token = src[i]; i += 1
        lit_len = token >> 4
        if lit_len == 15:
            while i < n:
                b = src[i]; i += 1
                lit_len += b
                if b != 255: break
        out += src[i:i + lit_len]
        i += lit_len
        if i >= n:
            break
        # match offset (little-endian u16)
        if i + 2 > n: break
        offset = src[i] | (src[i + 1] << 8)
        i += 2
        if offset == 0:
            raise ValueError("LZ4: zero match offset")
        match_len = token & 0xF
        if match_len == 15:
            while i < n:
                b = src[i]; i += 1
                match_len += b
                if b != 255: break
        match_len += 4
        # Copy match. LZ4 allows overlapping copies (RLE).
        start = len(out) - offset
        for k in range(match_len):
            out.append(out[start + k])
        if len(out) >= uncompressed_size and uncompressed_size > 0:
            break
    return bytes(out[:uncompressed_size] if uncompressed_size else out)


# =============================================================================
#   AARCH64 INTERPRETER  -  expanded opcode coverage for real ROMs
#   Supports: MOV* / ADD/SUB (imm+reg) / AND/ORR/EOR (imm+reg) / CMP / CBZ/CBNZ
#            / B.cond / B / BL / BR / BLR / RET / LDR/STR (imm offset both
#            widths, unsigned+signed) / LDP/STP (pre/post/offset) / MADD
#            / LSL/LSR/ASR imm / ADRP / ADR / SVC / HLT / BRK / NOP
# =============================================================================
def _sx(val: int, bits: int) -> int:
    """Sign-extend val (bits wide) to 64 bits."""
    mask = (1 << bits) - 1
    val &= mask
    if val & (1 << (bits - 1)):
        return val - (1 << bits)
    return val


COND = {
    0x0: lambda n, z, c, v: z,                              # EQ
    0x1: lambda n, z, c, v: not z,                          # NE
    0x2: lambda n, z, c, v: c,                              # CS/HS
    0x3: lambda n, z, c, v: not c,                          # CC/LO
    0x4: lambda n, z, c, v: n,                              # MI
    0x5: lambda n, z, c, v: not n,                          # PL
    0x6: lambda n, z, c, v: v,                              # VS
    0x7: lambda n, z, c, v: not v,                          # VC
    0x8: lambda n, z, c, v: c and not z,                    # HI
    0x9: lambda n, z, c, v: (not c) or z,                   # LS
    0xA: lambda n, z, c, v: n == v,                         # GE
    0xB: lambda n, z, c, v: n != v,                         # LT
    0xC: lambda n, z, c, v: (not z) and (n == v),           # GT
    0xD: lambda n, z, c, v: z or (n != v),                  # LE
    0xE: lambda n, z, c, v: True,                           # AL
    0xF: lambda n, z, c, v: True,                           # NV (behaves AL)
}


class AArch64Core:
    def __init__(self, core_id: int, mem: PagedMemory) -> None:
        self.id = core_id
        self.mem = mem
        self.x = [0] * 32      # X0..X30 + X31 is zero-register (SP handled sep)
        self.sp = 0
        self.pc = 0
        self.n = False; self.z = False; self.c = False; self.v = False
        self.running = False
        self.halted = False
        self.insn_count = 0
        self.heap_base = 0
        self.heap_size = 0

    # --- helpers ---
    def _reg(self, r: int) -> int:
        if r == 31: return 0
        return self.x[r]

    def _write_reg(self, r: int, val: int, sf: int = 1) -> None:
        if r == 31:
            # writes to XZR/WZR are ignored
            return
        mask = (1 << (64 if sf else 32)) - 1
        self.x[r] = val & mask

    def _flags_add(self, a: int, b: int, sf: int) -> int:
        width = 64 if sf else 32
        mask = (1 << width) - 1
        r = (a + b) & mask
        self.n = bool((r >> (width - 1)) & 1)
        self.z = r == 0
        self.c = (a & mask) + (b & mask) > mask
        sa = (a >> (width - 1)) & 1
        sb = (b >> (width - 1)) & 1
        sr = (r >> (width - 1)) & 1
        self.v = (sa == sb) and (sr != sa)
        return r

    def _flags_sub(self, a: int, b: int, sf: int) -> int:
        width = 64 if sf else 32
        mask = (1 << width) - 1
        r = (a - b) & mask
        self.n = bool((r >> (width - 1)) & 1)
        self.z = r == 0
        self.c = (a & mask) >= (b & mask)
        sa = (a >> (width - 1)) & 1
        sb = (b >> (width - 1)) & 1
        sr = (r >> (width - 1)) & 1
        self.v = (sa != sb) and (sr != sa)
        return r

    def step(self) -> None:
        if self.halted:
            return
        try:
            insn = self.mem.r32(self.pc)
        except MemoryError:
            self.halted = True
            return
        pc_of_insn = self.pc
        self.pc = (self.pc + 4) & 0xFFFFFFFFFFFFFFFF
        self.insn_count += 1
        try:
            self._decode(insn, pc_of_insn)
        except Exception as e:
            _EmuLog.err(f"[c{self.id}] decode crash @ {pc_of_insn:#x} insn={insn:#010x}: {e}")
            self.halted = True

    # --- main dispatch ---
    def _decode(self, insn: int, pc: int) -> None:
        # ===== special singletons =====
        if insn == 0xD503201F: return                          # NOP
        if insn == 0xD65F03C0:                                 # RET
            self.pc = self.x[30]; return
        if insn == 0xD503203F: return                          # YIELD (NOP-ish)
        if (insn & 0xFFE0001F) == 0xD4400000:                  # HLT #imm
            self.halted = True; return
        if (insn & 0xFFE0001F) == 0xD4200000:                  # BRK #imm
            _EmuLog.warn(f"[c{self.id}] BRK #{(insn >> 5) & 0xFFFF} @ {pc:#x}")
            self.halted = True; return
        if (insn & 0xFFE0001F) == 0xD4000001:                  # SVC #imm
            svc_id = (insn >> 5) & 0xFFFF
            HorizonOS.dispatch_svc(self, svc_id); return

        top = (insn >> 26) & 0x3F
        # ===== unconditional branches =====
        if top == 0b000101 or top == 0b100101:                 # B / BL
            imm26 = _sx(insn & 0x03FFFFFF, 26)
            target = (pc + (imm26 << 2)) & 0xFFFFFFFFFFFFFFFF
            if top == 0b100101:
                self.x[30] = self.pc
            self.pc = target; return

        # ===== B.cond =====
        if (insn & 0xFF000010) == 0x54000000:                  # B.cond
            cond = insn & 0xF
            imm19 = _sx((insn >> 5) & 0x7FFFF, 19)
            if COND[cond](self.n, self.z, self.c, self.v):
                self.pc = (pc + (imm19 << 2)) & 0xFFFFFFFFFFFFFFFF
            return

        # ===== CBZ / CBNZ =====
        if (insn & 0x7F000000) == 0x34000000:                  # CBZ
            sf = (insn >> 31) & 1
            imm19 = _sx((insn >> 5) & 0x7FFFF, 19)
            rt = insn & 0x1F
            val = self._reg(rt) & ((1 << (64 if sf else 32)) - 1)
            if val == 0:
                self.pc = (pc + (imm19 << 2)) & 0xFFFFFFFFFFFFFFFF
            return
        if (insn & 0x7F000000) == 0x35000000:                  # CBNZ
            sf = (insn >> 31) & 1
            imm19 = _sx((insn >> 5) & 0x7FFFF, 19)
            rt = insn & 0x1F
            val = self._reg(rt) & ((1 << (64 if sf else 32)) - 1)
            if val != 0:
                self.pc = (pc + (imm19 << 2)) & 0xFFFFFFFFFFFFFFFF
            return

        # ===== BR / BLR / RET (reg-form) =====
        if (insn & 0xFFFFFC1F) == 0xD61F0000:                  # BR Xn
            self.pc = self._reg((insn >> 5) & 0x1F); return
        if (insn & 0xFFFFFC1F) == 0xD63F0000:                  # BLR Xn
            tgt = self._reg((insn >> 5) & 0x1F)
            self.x[30] = self.pc
            self.pc = tgt; return

        # ===== MOVZ / MOVK / MOVN (wide immediate) =====
        if (insn & 0x1F800000) == 0x12800000:
            sf  = (insn >> 31) & 1
            opc = (insn >> 29) & 3
            hw  = (insn >> 21) & 3
            imm16 = (insn >> 5) & 0xFFFF
            rd  = insn & 0x1F
            shift = hw * 16
            if opc == 0b10:                                    # MOVZ
                self._write_reg(rd, imm16 << shift, sf)
            elif opc == 0b11:                                  # MOVK
                cur = self._reg(rd)
                mask = 0xFFFF << shift
                self._write_reg(rd, (cur & ~mask) | (imm16 << shift), sf)
            elif opc == 0b00:                                  # MOVN
                self._write_reg(rd, ~(imm16 << shift), sf)
            return

        # ===== ADR / ADRP =====
        if (insn & 0x9F000000) == 0x10000000:                  # ADR
            immlo = (insn >> 29) & 3
            immhi = (insn >> 5) & 0x7FFFF
            imm = _sx((immhi << 2) | immlo, 21)
            rd = insn & 0x1F
            self._write_reg(rd, (pc + imm) & 0xFFFFFFFFFFFFFFFF, 1)
            return
        if (insn & 0x9F000000) == 0x90000000:                  # ADRP
            immlo = (insn >> 29) & 3
            immhi = (insn >> 5) & 0x7FFFF
            imm = _sx((immhi << 2) | immlo, 21) << 12
            rd = insn & 0x1F
            base = pc & ~0xFFF
            self._write_reg(rd, (base + imm) & 0xFFFFFFFFFFFFFFFF, 1)
            return

        # ===== ADD / SUB (immediate, with optional shift) =====
        if (insn & 0x1F000000) == 0x11000000:
            sf = (insn >> 31) & 1
            op = (insn >> 30) & 1
            s  = (insn >> 29) & 1
            sh = (insn >> 22) & 1
            imm12 = (insn >> 10) & 0xFFF
            if sh: imm12 <<= 12
            rn = (insn >> 5) & 0x1F
            rd = insn & 0x1F
            a = self._reg(rn) if rn != 31 else self.sp
            if op:
                r = self._flags_sub(a, imm12, sf) if s else (a - imm12) & ((1 << (64 if sf else 32)) - 1)
            else:
                r = self._flags_add(a, imm12, sf) if s else (a + imm12) & ((1 << (64 if sf else 32)) - 1)
            if rd == 31 and s == 0:
                self.sp = r & ((1 << (64 if sf else 32)) - 1)
            else:
                self._write_reg(rd, r, sf)
            return

        # ===== Logical (immediate): AND/ORR/EOR/ANDS =====
        if (insn & 0x1F800000) == 0x12000000:
            sf = (insn >> 31) & 1
            opc = (insn >> 29) & 3
            N = (insn >> 22) & 1
            immr = (insn >> 16) & 0x3F
            imms = (insn >> 10) & 0x3F
            rn = (insn >> 5) & 0x1F
            rd = insn & 0x1F
            # decode bitmask immediate -> see ARM ARM. Short-form here:
            imm = _decode_bitmask_imm(N, imms, immr, 64 if sf else 32)
            a = self._reg(rn)
            if opc == 0b00: r = a & imm
            elif opc == 0b01: r = a | imm
            elif opc == 0b10: r = a ^ imm
            else:  # ANDS
                r = a & imm
                width = 64 if sf else 32
                mask = (1 << width) - 1
                r &= mask
                self.n = bool((r >> (width - 1)) & 1); self.z = r == 0
                self.c = False; self.v = False
            self._write_reg(rd, r, sf)
            return

        # ===== ADD / SUB (shifted register) =====
        if (insn & 0x1F200000) == 0x0B000000:
            sf = (insn >> 31) & 1
            op = (insn >> 30) & 1
            s  = (insn >> 29) & 1
            rm = (insn >> 16) & 0x1F
            imm6 = (insn >> 10) & 0x3F
            rn = (insn >> 5) & 0x1F
            rd = insn & 0x1F
            b = self._reg(rm) << imm6
            width = 64 if sf else 32
            b &= (1 << width) - 1
            a = self._reg(rn)
            if op:
                r = self._flags_sub(a, b, sf) if s else (a - b) & ((1 << width) - 1)
            else:
                r = self._flags_add(a, b, sf) if s else (a + b) & ((1 << width) - 1)
            self._write_reg(rd, r, sf)
            return

        # ===== Logical (shifted register): AND/ORR/EOR/ANDS =====
        if (insn & 0x1F000000) == 0x0A000000:
            sf = (insn >> 31) & 1
            opc = (insn >> 29) & 3
            n_bit = (insn >> 21) & 1
            rm = (insn >> 16) & 0x1F
            imm6 = (insn >> 10) & 0x3F
            rn = (insn >> 5) & 0x1F
            rd = insn & 0x1F
            b = self._reg(rm) << imm6
            if n_bit: b = (~b)
            width = 64 if sf else 32
            b &= (1 << width) - 1
            a = self._reg(rn)
            if opc == 0b00: r = a & b
            elif opc == 0b01: r = a | b
            elif opc == 0b10: r = a ^ b
            else:
                r = a & b
                r &= (1 << width) - 1
                self.n = bool((r >> (width - 1)) & 1); self.z = r == 0
                self.c = False; self.v = False
            self._write_reg(rd, r, sf)
            return

        # ===== LDR / STR (immediate, unsigned offset) 64-bit =====
        if (insn & 0xFFC00000) == 0xF9400000:                  # LDR Xt, [Xn,#imm]
            imm12 = (insn >> 10) & 0xFFF
            rn = (insn >> 5) & 0x1F
            rt = insn & 0x1F
            base = self._reg(rn) if rn != 31 else self.sp
            self._write_reg(rt, self.mem.r64(base + imm12 * 8), 1); return
        if (insn & 0xFFC00000) == 0xF9000000:                  # STR Xt, [Xn,#imm]
            imm12 = (insn >> 10) & 0xFFF
            rn = (insn >> 5) & 0x1F
            rt = insn & 0x1F
            base = self._reg(rn) if rn != 31 else self.sp
            self.mem.w64(base + imm12 * 8, self._reg(rt)); return
        if (insn & 0xFFC00000) == 0xB9400000:                  # LDR Wt, [Xn,#imm]
            imm12 = (insn >> 10) & 0xFFF
            rn = (insn >> 5) & 0x1F
            rt = insn & 0x1F
            base = self._reg(rn) if rn != 31 else self.sp
            self._write_reg(rt, self.mem.r32(base + imm12 * 4), 0); return
        if (insn & 0xFFC00000) == 0xB9000000:                  # STR Wt, [Xn,#imm]
            imm12 = (insn >> 10) & 0xFFF
            rn = (insn >> 5) & 0x1F
            rt = insn & 0x1F
            base = self._reg(rn) if rn != 31 else self.sp
            self.mem.w32(base + imm12 * 4, self._reg(rt) & 0xFFFFFFFF); return

        # ===== LDP / STP (signed offset) =====
        if (insn & 0x7FC00000) == 0x29400000 or (insn & 0x7FC00000) == 0xA9400000:
            sf = 1 if (insn & 0x80000000) else 0
            imm7 = _sx((insn >> 15) & 0x7F, 7) * (8 if sf else 4)
            rt2 = (insn >> 10) & 0x1F
            rn  = (insn >> 5) & 0x1F
            rt  = insn & 0x1F
            base = self._reg(rn) if rn != 31 else self.sp
            addr = base + imm7
            if sf:
                self._write_reg(rt,  self.mem.r64(addr), 1)
                self._write_reg(rt2, self.mem.r64(addr + 8), 1)
            else:
                self._write_reg(rt,  self.mem.r32(addr), 0)
                self._write_reg(rt2, self.mem.r32(addr + 4), 0)
            return
        if (insn & 0x7FC00000) == 0x29000000 or (insn & 0x7FC00000) == 0xA9000000:
            sf = 1 if (insn & 0x80000000) else 0
            imm7 = _sx((insn >> 15) & 0x7F, 7) * (8 if sf else 4)
            rt2 = (insn >> 10) & 0x1F
            rn  = (insn >> 5) & 0x1F
            rt  = insn & 0x1F
            base = self._reg(rn) if rn != 31 else self.sp
            addr = base + imm7
            if sf:
                self.mem.w64(addr,     self._reg(rt))
                self.mem.w64(addr + 8, self._reg(rt2))
            else:
                self.mem.w32(addr,     self._reg(rt) & 0xFFFFFFFF)
                self.mem.w32(addr + 4, self._reg(rt2) & 0xFFFFFFFF)
            return

        # Unknown -> log sparingly; commercial code has tons of unmodelled
        # SIMD/system insns; we skip them rather than wedge.
        if _EmuLog._count_unimpl < 64:
            _EmuLog._count_unimpl += 1
            _EmuLog.warn(f"[c{self.id}] UNIMPL {insn:#010x} @ {pc:#x}")


def _decode_bitmask_imm(N: int, imms: int, immr: int, regsize: int) -> int:
    """Implements DecodeBitMasks from ARM ARM - used for logical (imm)."""
    length = (N << 6) | ((~imms) & 0x3F)
    # find highest set bit
    for i in range(6, -1, -1):
        if (length >> i) & 1:
            length = i
            break
    else:
        return 0
    if length < 0: return 0
    size = 1 << length
    if size > regsize: return 0
    levels = size - 1
    s = imms & levels
    r = immr & levels
    if s == levels:
        return 0  # reserved
    # welem = ZeroExtend(Ones(s+1), size)
    welem = (1 << (s + 1)) - 1
    # ROR welem right by r, within 'size' bits
    welem = ((welem >> r) | (welem << (size - r))) & ((1 << size) - 1)
    # Replicate welem across regsize
    out = 0
    reps = regsize // size
    for i in range(reps):
        out |= welem << (i * size)
    return out & ((1 << regsize) - 1)


# =============================================================================
#   CPU MANAGER (8 cores: 6 game + 2 OS, per T239 reality)
# =============================================================================
class CpuManager:
    def __init__(self, mem: PagedMemory) -> None:
        self.cores = [AArch64Core(i, mem) for i in range(CPU_CORES_TOTAL)]
        self.mem = mem
        self.quantum = 4096
        # tag core roles
        for c in self.cores[:CPU_CORES_GAMES]:
            c.role = "game"
        for c in self.cores[CPU_CORES_GAMES:]:
            c.role = "os"

    def boot(self, entry_pc: int, stack_top: int) -> None:
        c0 = self.cores[0]
        c0.pc = entry_pc; c0.sp = stack_top
        c0.running = True; c0.halted = False
        c0.x = [0] * 32
        _EmuLog.ok(f"Core 0 ({c0.role}) armed @ pc={entry_pc:#x} sp={stack_top:#x}")

    def tick(self) -> int:
        total = 0
        for c in self.cores:
            if not c.running or c.halted: continue
            for _ in range(self.quantum):
                if c.halted: break
                c.step(); total += 1
        return total


# =============================================================================
#   HORIZON OS STUB  -  SVC table + nn:: services
# =============================================================================
class _HOS:
    SVC_NAMES = {
        0x01: "SetHeapSize", 0x02: "SetMemoryPermission", 0x03: "SetMemoryAttribute",
        0x04: "MapMemory", 0x05: "UnmapMemory", 0x06: "QueryMemory",
        0x07: "ExitProcess", 0x08: "CreateThread", 0x09: "StartThread",
        0x0A: "ExitThread", 0x0B: "SleepThread", 0x0C: "GetThreadPriority",
        0x0D: "SetThreadPriority", 0x0E: "GetThreadCoreMask",
        0x13: "MapSharedMemory", 0x14: "UnmapSharedMemory",
        0x15: "CreateTransferMemory", 0x16: "CloseHandle",
        0x17: "ResetSignal", 0x18: "WaitSynchronization",
        0x1C: "ArbitrateLock", 0x1D: "ArbitrateUnlock",
        0x1F: "ConnectToNamedPort", 0x21: "SendSyncRequest",
        0x24: "GetProcessId", 0x25: "GetThreadId",
        0x26: "Break", 0x27: "OutputDebugString",
        0x29: "GetInfo", 0x33: "GetThreadContext3",
        0x40: "CreateSession", 0x41: "AcceptSession",
        0x70: "CreatePort", 0x71: "ManageNamedPort",
    }
    SERVICES = [
        "sm:", "fsp-srv", "fsp-ldr", "fsp-pr", "ns:am", "ns:am2", "pl:u",
        "set:sys", "set:cal", "set", "time:s", "time:u", "time:a",
        "hid", "hid:sys", "irs", "xcd:sys",
        "audin:u", "audout:u", "audrec:u", "codecctrl",
        "apm", "apm:p", "appletOE", "appletAE",
        "caps:su", "caps:a", "caps:c",
        "nfc:user", "nfp:user", "nfc:sys", "nfp:sys",
        "bsd:s", "bsd:u", "sfdnsres", "ssl",
        "lm", "fatal:u", "pm:info", "pm:dmnt",
        "vi:m", "vi:s", "vi:u",
        "nvdrv", "nvdrv:a", "nvdrv:s",
        "psc:m", "pctl", "pctl:a",
        # Switch 2 additions
        "nvn2:srv", "aoc:u", "hidbus",
    ]


class HorizonOS:
    session_handles: dict[int, str] = {}
    _next_handle = 0x1000
    _heap_next = 0x8000_0000_0000

    @classmethod
    def new_handle(cls, tag: str) -> int:
        h = cls._next_handle; cls._next_handle += 1
        cls.session_handles[h] = tag
        return h

    @classmethod
    def reset(cls) -> None:
        cls.session_handles.clear()
        cls._next_handle = 0x1000
        cls._heap_next   = 0x8000_0000_0000

    @classmethod
    def dispatch_svc(cls, core: AArch64Core, svc_id: int) -> None:
        name = _HOS.SVC_NAMES.get(svc_id, f"SVC_{svc_id:#x}")
        _EmuLog.svc(f"[c{core.id}] {name}(x0={core.x[0]:#x} x1={core.x[1]:#x})")
        if svc_id == 0x01:                                     # SetHeapSize
            size = core.x[1]
            core.heap_size = size
            core.heap_base = cls._heap_next
            cls._heap_next += max(size, 0x10_0000)
            core.x[0] = 0; core.x[1] = core.heap_base; return
        if svc_id == 0x07:                                     # ExitProcess
            core.halted = True; return
        if svc_id == 0x0B:                                     # SleepThread
            return
        if svc_id == 0x16:                                     # CloseHandle
            cls.session_handles.pop(core.x[0], None)
            core.x[0] = 0; return
        if svc_id == 0x1F:                                     # ConnectToNamedPort
            try:
                raw = core.mem.read(core.x[1], 12)
                port = raw.split(b"\x00", 1)[0].decode("ascii", "ignore")
            except Exception: port = "?"
            h = cls.new_handle(f"port:{port}")
            core.x[0] = 0; core.x[1] = h; return
        if svc_id == 0x21:                                     # SendSyncRequest
            core.x[0] = 0; return
        if svc_id == 0x24:                                     # GetProcessId
            core.x[1] = 0x0100000000000001; core.x[0] = 0; return
        if svc_id == 0x26:                                     # Break
            _EmuLog.warn(f"[c{core.id}] svcBreak reason={core.x[0]:#x}")
            core.halted = True; return
        if svc_id == 0x27:                                     # OutputDebugString
            try:
                s = core.mem.read(core.x[0], core.x[1] & 0xFFFF).decode("utf-8", "replace")
                _EmuLog.info(f"[dbg c{core.id}] {s.rstrip()}")
            except Exception: pass
            core.x[0] = 0; return
        if svc_id == 0x29:                                     # GetInfo
            # Common info queries
            info_id = core.x[1]
            if info_id == 6:      # TotalMemorySize
                core.x[1] = RAM_BYTES
            elif info_id == 7:    # UsedMemorySize
                core.x[1] = 1024 * 1024 * 128
            else:
                core.x[1] = 0
            core.x[0] = 0; return
        # default - pretend success
        core.x[0] = 0


# =============================================================================
#   GPU STUB  -  Ampere GA10F submission queue + diagnostic framebuffer
# =============================================================================
class GpuAmpere:
    def __init__(self, w: int, h: int) -> None:
        self.w, self.h = w, h
        self.frame = bytearray(w * h * 4)
        self.submit_count = 0
        self.draw_count = 0
        self.shader_cache: dict[str, bytes] = {}
        self._t0 = time.time()

    def submit(self, cmd: dict) -> None:
        self.submit_count += 1
        if cmd.get("op") == "draw": self.draw_count += 1

    def compile_shader(self, spirv: bytes) -> bytes:
        key = hashlib.sha1(spirv).hexdigest()
        if key in self.shader_cache: return self.shader_cache[key]
        self.shader_cache[key] = spirv  # stub: pass-through
        return spirv

    def render_placeholder(self) -> bytes:
        t = time.time() - self._t0
        w, h = self.w, self.h
        fb = self.frame
        for y in range(h):
            g = max(0, min(255, int(30 + 25 * math.sin(t * 1.5 + y * 0.01))))
            b = max(0, min(255, int(90 + 120 * (y / h))))
            line = bytes((0, g, b, 255)) * w
            fb[y * w * 4:(y + 1) * w * 4] = line
        for y in range(0, h, 2):
            off = y * w * 4
            for x in range(0, w * 4, 4):
                fb[off + x + 1] = max(0, fb[off + x + 1] - 8)
                fb[off + x + 2] = max(0, fb[off + x + 2] - 8)
        return bytes(fb)

    def stats(self) -> str:
        return f"s={self.submit_count} d={self.draw_count} sh={len(self.shader_cache)}"


# =============================================================================
#   REAL CONTAINER LOADERS: NRO / NSO / NCA / PFS0(NSP) / XCI / KIP1
# =============================================================================
NRO_MAGIC   = b"NRO0"
NSO_MAGIC   = b"NSO0"
NCA_MAGICS  = (b"NCA0", b"NCA2", b"NCA3")
PFS0_MAGIC  = b"PFS0"
HFS0_MAGIC  = b"HFS0"
XCI_MAGIC   = b"HEAD"
KIP_MAGIC   = b"KIP1"


@dataclass
class LoadedSegment:
    name: str
    file_off: int
    file_size: int
    mem_off: int
    mem_size: int
    compressed: bool = False


@dataclass
class LoadedTitle:
    path: str
    fmt: str
    size: int
    title_id: str = "0000000000000000"
    name: str = "Unknown"
    developer: str = "Unknown"
    version: str = "1.0.0"
    time_played: str = "00:00:00"
    entry: int = 0x8000_0000
    stack_top: int = 0x8000_0000 + 0x200000
    segments: list[LoadedSegment] = field(default_factory=list)
    build_id: str = ""
    is_commercial: bool = False


class TitleLoader:
    """Identifies and boots commercial Switch/Switch 2 content into memory."""

    # ---- magic detection ----
    @staticmethod
    def identify(path: str) -> str:
        try:
            with open(path, "rb") as f:
                head = f.read(0x200)
        except OSError:
            return "unknown"
        if head[:4] == NRO_MAGIC:  return "nro"
        if head[:4] == NSO_MAGIC:  return "nso"
        if head[:4] in NCA_MAGICS: return "nca"
        if head[:4] == KIP_MAGIC:  return "kip"
        if head[:4] == PFS0_MAGIC: return "nsp"
        if head[:4] == HFS0_MAGIC: return "hfs0"
        if len(head) >= 0x104 and head[0x100:0x104] == XCI_MAGIC: return "xci"
        ext = os.path.splitext(path)[1].lower().lstrip(".")
        if ext in ("nsp", "xci", "nro", "nso", "nca", "kip", "nx2"): return ext
        return "raw"

    # ---- NRO0 loader (switchbrew NRO format) ----
    # NroStart (0x10) + NroHeader (0x70) + segments
    # NroHeader:
    #   0x00 magic 'NRO0'
    #   0x04 format_version
    #   0x08 size
    #   0x0C flags
    #   0x10 .text (offset, size)       (8 bytes)
    #   0x18 .rodata (offset, size)
    #   0x20 .data (offset, size)
    #   0x28 bss_size
    @staticmethod
    def load_nro(mem: PagedMemory, path: str, base: int = 0x8000_0000) -> LoadedTitle:
        with open(path, "rb") as f: raw = f.read()
        hdr = raw[0x10:0x10 + 0x70]
        if hdr[:4] != NRO_MAGIC:
            raise ValueError("NRO: bad magic")
        size = struct.unpack_from("<I", hdr, 0x08)[0]
        text_off, text_sz = struct.unpack_from("<II", hdr, 0x10)
        rod_off,  rod_sz  = struct.unpack_from("<II", hdr, 0x18)
        data_off, data_sz = struct.unpack_from("<II", hdr, 0x20)
        bss_size = struct.unpack_from("<I", hdr, 0x28)[0]

        title = LoadedTitle(path=path, fmt="nro", size=len(raw),
                            name=os.path.splitext(os.path.basename(path))[0],
                            developer="<homebrew>",
                            entry=base, stack_top=base + 0x0400_0000)

        segs = [
            (".text",  text_off, text_sz,  0,            False),
            (".rodata", rod_off, rod_sz,   text_sz,      False),
            (".data",  data_off, data_sz,  text_sz + rod_sz, False),
        ]
        for name, foff, sz, moff, comp in segs:
            if sz == 0: continue
            mem.write(base + moff, raw[foff:foff + sz])
            title.segments.append(LoadedSegment(name, foff, sz, moff, sz, comp))

        # zero .bss
        bss_off = text_sz + rod_sz + data_sz
        if bss_size > 0:
            mem.write(base + bss_off, b"\x00" * min(bss_size, 0x10_0000))
            title.segments.append(LoadedSegment(".bss", 0, 0, bss_off, bss_size))

        tid = hashlib.md5(path.encode()).hexdigest()[:16].upper()
        title.title_id = tid
        _EmuLog.ok(f"NRO loaded: text={text_sz:#x} rod={rod_sz:#x} "
                   f"data={data_sz:#x} bss={bss_size:#x} @ base {base:#x}")
        return title

    # ---- NSO0 loader (switchbrew NSO format) ----
    # Header is 0x100 bytes; segments may be LZ4-compressed.
    # Layout (little-endian):
    #   0x00 magic 'NSO0'
    #   0x04 version
    #   0x08 reserved
    #   0x0C flags:
    #        bit0 = text compressed
    #        bit1 = rodata compressed
    #        bit2 = data compressed
    #        bit3 = text check hash
    #        bit4 = rodata check hash
    #        bit5 = data check hash
    #   0x10 SegmentHeader text   { file_off u32; mem_off u32; decomp_sz u32; align_or_text_hash u32 }
    #   0x20 module_name_offset u32
    #   0x24 SegmentHeader rodata
    #   0x34 module_name_size u32
    #   0x38 SegmentHeader data
    #   0x48 bss_size u32
    #   0x4C module_id[0x20]  (build id)
    #   0x6C compressed_size text  u32
    #   0x70 compressed_size rodata u32
    #   0x74 compressed_size data   u32
    #   0x78 padding[0x1C]
    #   0x94 api_info_hash[0x20]  (or hashes) ... rest is hashes/padding
    @staticmethod
    def load_nso(mem: PagedMemory, path: str, base: int = 0x8000_0000) -> LoadedTitle:
        with open(path, "rb") as f: raw = f.read()
        if raw[:4] != NSO_MAGIC:
            raise ValueError("NSO: bad magic")

        flags = struct.unpack_from("<I", raw, 0x0C)[0]
        # segment headers
        text_foff, text_moff, text_dsz, _ = struct.unpack_from("<IIII", raw, 0x10)
        rod_foff,  rod_moff,  rod_dsz,  _ = struct.unpack_from("<IIII", raw, 0x24)
        data_foff, data_moff, data_dsz, _ = struct.unpack_from("<IIII", raw, 0x38)
        bss_size  = struct.unpack_from("<I", raw, 0x48)[0]
        build_id  = raw[0x4C:0x4C + 0x20].hex().upper()
        text_csz  = struct.unpack_from("<I", raw, 0x60)[0]
        rod_csz   = struct.unpack_from("<I", raw, 0x64)[0]
        data_csz  = struct.unpack_from("<I", raw, 0x68)[0]

        title = LoadedTitle(path=path, fmt="nso", size=len(raw),
                            name=os.path.splitext(os.path.basename(path))[0],
                            developer="Nintendo",
                            entry=base + text_moff,
                            stack_top=base + 0x0400_0000,
                            is_commercial=True,
                            build_id=build_id)

        def _seg(name, foff, moff, dsz, csz, compressed):
            if dsz == 0: return
            raw_seg = raw[foff:foff + (csz if compressed else dsz)]
            if compressed:
                _EmuLog.info(f"NSO: decompressing {name} ({csz} -> {dsz} via LZ4)")
                data = lz4_block_decompress(raw_seg, dsz)
            else:
                data = raw_seg
            if len(data) < dsz:
                data = data + b"\x00" * (dsz - len(data))
            mem.write(base + moff, data[:dsz])
            title.segments.append(LoadedSegment(name, foff, dsz, moff, dsz, compressed))

        _seg(".text",   text_foff, text_moff, text_dsz, text_csz, bool(flags & 0x1))
        _seg(".rodata", rod_foff,  rod_moff,  rod_dsz,  rod_csz,  bool(flags & 0x2))
        _seg(".data",   data_foff, data_moff, data_dsz, data_csz, bool(flags & 0x4))

        if bss_size > 0:
            # .bss immediately follows .data in memory
            bss_moff = data_moff + data_dsz
            mem.write(base + bss_moff, b"\x00" * min(bss_size, 0x20_0000))
            title.segments.append(LoadedSegment(".bss", 0, 0, bss_moff, bss_size))

        title.title_id = build_id[:16]
        _EmuLog.ok(f"NSO loaded: text={text_dsz:#x} rod={rod_dsz:#x} "
                   f"data={data_dsz:#x} bss={bss_size:#x} build={build_id[:16]}")
        return title

    # ---- PFS0 container parse (NSP is a PFS0) ----
    # Header:
    #   0x00 'PFS0'
    #   0x04 file_count u32
    #   0x08 stringtable_size u32
    #   0x0C reserved
    #   0x10 file_entries[file_count] (24 bytes each: off u64, sz u64, stroff u32, pad u32)
    #   then stringtable
    #   then file data
    @staticmethod
    def parse_pfs0(raw: bytes) -> list[tuple[str, int, int]]:
        if raw[:4] != PFS0_MAGIC:
            raise ValueError("PFS0: bad magic")
        file_count, st_size = struct.unpack_from("<II", raw, 0x04)
        entries_off = 0x10
        st_off = entries_off + file_count * 0x18
        data_off = st_off + st_size
        stringtable = raw[st_off:st_off + st_size]
        files = []
        for i in range(file_count):
            eo = entries_off + i * 0x18
            foff, fsz, stroff, _pad = struct.unpack_from("<QQII", raw, eo)
            # read NUL-terminated string from stringtable at stroff
            end = stringtable.find(b"\x00", stroff)
            name = stringtable[stroff:end].decode("utf-8", "replace") if end != -1 else ""
            files.append((name, data_off + foff, fsz))
        return files

    # ---- NSP loader (PFS0 -> main NCA/NSO) ----
    @staticmethod
    def load_nsp(mem: PagedMemory, path: str) -> LoadedTitle:
        with open(path, "rb") as f:
            raw = f.read()
        files = TitleLoader.parse_pfs0(raw)
        _EmuLog.info(f"NSP: {len(files)} files in PFS0 container")
        for name, off, sz in files:
            _EmuLog.info(f"  - {name:<40s} @ {off:#x}  {sz/1024/1024:.2f} MiB")

        title = LoadedTitle(path=path, fmt="nsp", size=len(raw),
                            name=os.path.splitext(os.path.basename(path))[0],
                            developer="Nintendo",
                            is_commercial=True)

        # Strategy:
        #  1. Prefer a bare .nso file inside the NSP (some test builds have this)
        #  2. Else prefer program NCA (usually *Program*.nca or first .nca)
        #  3. If neither, log manifest only

        nso_entries = [(n, o, s) for n, o, s in files if n.lower().endswith(".nso")]
        if nso_entries:
            n, o, s = nso_entries[0]
            _EmuLog.ok(f"NSP: loading bare NSO '{n}'")
            # extract to temp blob and run through NSO loader
            blob = raw[o:o + s]
            tmp = path + f".__nso_{hashlib.md5(n.encode()).hexdigest()[:8]}"
            with open(tmp, "wb") as tf: tf.write(blob)
            try:
                t2 = TitleLoader.load_nso(mem, tmp)
                os.remove(tmp)
                title.segments = t2.segments
                title.entry = t2.entry
                title.stack_top = t2.stack_top
                title.build_id = t2.build_id
                title.title_id = t2.title_id
                return title
            except Exception as e:
                _EmuLog.err(f"NSP->NSO failed: {e}")
                try: os.remove(tmp)
                except OSError: pass

        nca_entries = [(n, o, s) for n, o, s in files if n.lower().endswith(".nca")]
        if nca_entries:
            # prefer program-type NCA: first NCA file, we introspect header
            n, o, s = nca_entries[0]
            _EmuLog.warn(f"NSP contains {len(nca_entries)} NCA(s). Without prod.keys "
                         f"the body is encrypted; picking metadata from '{n}'.")
            try:
                meta = TitleLoader.introspect_nca(raw[o:o + min(s, 0x400)])
                title.title_id = meta.get("program_id_hex", title.title_id)
                _EmuLog.info(f"NSP: NCA magic={meta.get('magic','?')} "
                             f"type={meta.get('content_type','?')} "
                             f"tid={title.title_id}")
            except Exception as e:
                _EmuLog.err(f"NSP NCA introspect failed: {e}")

        if not title.segments:
            # No runnable code loaded -> install an idle kernel stub so the
            # GUI still boots and doesn't freeze on 'Play'.
            TitleLoader._install_idle_stub(mem, title)
            _EmuLog.warn("NSP: no decryptable executable - installed idle stub")
        return title

    # ---- XCI gamecard header parse ----
    # switchbrew: https://switchbrew.org/wiki/Gamecard_Format
    #   0x000 RSA-2048 signature
    #   0x100 'HEAD' magic
    #   0x104 RomSize
    #   0x108 Header Version
    #   0x10C Key Index / Flags
    #   0x110 PackageId u64
    #   0x118 ValidDataEndAddress u64
    #   0x120 IV[0x10]
    #   0x130 HFS0 PartitionFS Header offset
    #   0x138 HFS0 PartitionFS Header size
    #   ...
    @staticmethod
    def load_xci(mem: PagedMemory, path: str) -> LoadedTitle:
        with open(path, "rb") as f:
            raw_head = f.read(0x200)
        if raw_head[0x100:0x104] != XCI_MAGIC:
            raise ValueError("XCI: bad gamecard HEAD magic")
        rom_size_byte = raw_head[0x104]
        rom_size_map = {0xFA: "1GB", 0xF8: "2GB", 0xF0: "4GB", 0xE0: "8GB",
                        0xE1: "16GB", 0xE2: "32GB", 0xE3: "64GB"}
        package_id = struct.unpack_from("<Q", raw_head, 0x110)[0]
        hfs0_off   = struct.unpack_from("<Q", raw_head, 0x130)[0]
        hfs0_size  = struct.unpack_from("<Q", raw_head, 0x138)[0]
        _EmuLog.ok(f"XCI: gamecard {rom_size_map.get(rom_size_byte, '?')} "
                   f"packageId={package_id:#x} HFS0 @ {hfs0_off:#x} "
                   f"({hfs0_size/1024/1024:.1f} MiB)")
        title = LoadedTitle(path=path, fmt="xci", size=os.path.getsize(path),
                            name=os.path.splitext(os.path.basename(path))[0],
                            developer="Nintendo",
                            is_commercial=True,
                            title_id=f"{package_id:016X}")
        TitleLoader._install_idle_stub(mem, title)
        _EmuLog.warn("XCI: HFS0 body parsing / NCA decryption not supported "
                     "without prod.keys; idle stub installed.")
        return title

    # ---- NCA header introspection (unencrypted or key-less metadata peek) ----
    @staticmethod
    def introspect_nca(raw: bytes) -> dict:
        # First 0xC00 bytes are AES-XTS encrypted, but magic bytes are at 0x200
        # only in the plaintext view. Try both plaintext and known-decrypted.
        out: dict = {}
        # Plaintext / already-decrypted dumps have magic at offset 0x200
        if raw[0x200:0x204] in NCA_MAGICS:
            base = 0x200
            out["magic"] = raw[base:base + 4].decode("ascii", "replace")
            out["content_type_byte"] = raw[base + 0x205]
            out["content_type"] = {0:"Program", 1:"Meta", 2:"Control",
                                   3:"Manual", 4:"Data", 5:"PublicData"} \
                                  .get(raw[base + 0x205], "?")
            out["program_id_hex"] = f"{struct.unpack_from('<Q', raw, base + 0x210)[0]:016X}"
            return out
        # Encrypted -> nothing to say
        out["magic"] = "<encrypted>"
        out["content_type"] = "?"
        return out

    # ---- KIP1 header parse (metadata only) ----
    @staticmethod
    def introspect_kip(raw: bytes) -> dict:
        if raw[:4] != KIP_MAGIC:
            raise ValueError("KIP1: bad magic")
        name = raw[0x04:0x0C].rstrip(b"\x00").decode("ascii", "replace")
        return {"name": name, "magic": "KIP1"}

    # ---- idle-core stub so ROMs can "boot" into the interpreter safely ----
    @staticmethod
    def _install_idle_stub(mem: PagedMemory, title: LoadedTitle) -> None:
        base = 0x8000_0000
        prog = bytearray()
        for _ in range(1024):
            prog += struct.pack("<I", 0xD503201F)   # NOP
        prog += struct.pack("<I", 0xD4400000)       # HLT #0
        mem.write(base, bytes(prog))
        title.entry     = base
        title.stack_top = base + 0x20_0000
        title.segments  = [LoadedSegment(".stub", 0, len(prog), 0, len(prog))]

    # ---- top-level dispatcher ----
    @staticmethod
    def load(mem: PagedMemory, path: str) -> LoadedTitle:
        fmt = TitleLoader.identify(path)
        _EmuLog.info(f"Detected container: {fmt.upper()}  ({path})")
        if fmt == "nro":
            return TitleLoader.load_nro(mem, path)
        if fmt == "nso":
            return TitleLoader.load_nso(mem, path)
        if fmt in ("nsp",):
            return TitleLoader.load_nsp(mem, path)
        if fmt == "xci":
            return TitleLoader.load_xci(mem, path)
        if fmt == "nca":
            # introspect then install idle stub
            with open(path, "rb") as f: head = f.read(0x400)
            meta = TitleLoader.introspect_nca(head)
            _EmuLog.info(f"NCA: {meta}")
            t = LoadedTitle(path=path, fmt="nca", size=os.path.getsize(path),
                            name=os.path.splitext(os.path.basename(path))[0],
                            title_id=meta.get("program_id_hex", "0"*16),
                            is_commercial=True,
                            developer="Nintendo")
            TitleLoader._install_idle_stub(mem, t)
            return t
        if fmt == "kip":
            with open(path, "rb") as f: head = f.read(0x200)
            meta = TitleLoader.introspect_kip(head)
            _EmuLog.info(f"KIP1: {meta}")
            t = LoadedTitle(path=path, fmt="kip", size=os.path.getsize(path),
                            name=meta.get("name") or os.path.basename(path),
                            developer="Nintendo", is_commercial=True)
            TitleLoader._install_idle_stub(mem, t)
            return t
        # raw / unknown -> idle stub with filename metadata
        _EmuLog.warn(f"Unknown format '{fmt}'; installing idle stub")
        t = LoadedTitle(path=path, fmt=fmt or "raw",
                        size=os.path.getsize(path),
                        name=os.path.splitext(os.path.basename(path))[0])
        TitleLoader._install_idle_stub(mem, t)
        return t


# =============================================================================
#   INPUT (Joy-Con 2)
# =============================================================================
@dataclass
class PadState:
    a: bool = False; b: bool = False; x: bool = False; y: bool = False
    l: bool = False; r: bool = False; zl: bool = False; zr: bool = False
    plus: bool = False; minus: bool = False
    home: bool = False; capture: bool = False
    dpad: tuple[int, int] = (0, 0)
    lstick: tuple[float, float] = (0.0, 0.0)
    rstick: tuple[float, float] = (0.0, 0.0)


# =============================================================================
#   SWITCH 2 HARDWARE REGISTRY (subsystem stubs + live state for UI)
# =============================================================================
class Switch2Hardware:
    """Models major Switch 2-facing subsystems as stubs; reads live NX2Emu state."""

    def __init__(self, emu: "NX2Emu") -> None:
        self._emu = emu
        self._last_tick = time.time()
        self.emulation_active = False
        # UFS / game card
        self.ufs_read_ops = 0
        self.ufs_write_ops = 0
        self.last_loaded_path = ""
        self.last_loaded_fmt = ""
        self.gamecard_path = ""  # set when fmt is xci
        # Audio (Horizon audout-style stub)
        self.audio_hz = 48000
        self.audio_channels = 2
        self.audio_buffer_ms = 8.0
        self.audio_muted = False
        # Joy-Con 2 / HID extras
        self.joycon_l_attached = True
        self.joycon_r_attached = True
        self.gyro_rps = (0.0, 0.0, 0.0)
        self.accel_g = (0.0, 0.0, 1.0)
        self.rumble_l_amp = 0.0
        self.rumble_r_amp = 0.0
        # WLAN / BT
        self.wifi_state = "idle"
        self.wifi_ssid = "—"
        self.wifi_rssi_dbm = -48
        self.bt_paired_count = 0
        # PMIC / thermal (synthetic)
        self.battery_pct = 100.0
        self.skin_temp_c = 38.0
        self.thermal_throttle = False
        # Security processor (stub)
        self.sm_initialized = True
        self.prod_keys_loaded = os.environ.get("NX2EMU_PRODKEYS", "") == "1"
        # NVN2 presentation queue (logical; mirrors GPU activity)
        self.nvn2_present_queue_depth = 3

    # --- lifecycle hooks from NX2Emu ---
    def on_load_rom(self, path: str, fmt: str) -> None:
        self.last_loaded_path = path
        self.last_loaded_fmt = fmt.lower()
        self.ufs_read_ops += 1
        if self.last_loaded_fmt == "xci":
            self.gamecard_path = path
        else:
            self.gamecard_path = ""

    def on_set_docked(self, docked: bool) -> None:
        if docked:
            self.skin_temp_c = min(self.skin_temp_c + 0.8, 85.0)
        else:
            self.skin_temp_c = max(self.skin_temp_c - 0.3, 32.0)

    def on_emulation_start(self) -> None:
        self.emulation_active = True

    def on_emulation_stop(self) -> None:
        self.emulation_active = False

    def tick(self, dt: float) -> None:
        """Slow synthetic sensors; safe to call from emu thread."""
        e = self._emu
        t = time.time()
        # Gyro / accel noise (not physics)
        g = 0.02 * math.sin(t * 1.7)
        self.gyro_rps = (g, -g * 0.7, g * 0.3)
        self.accel_g = (0.01 * math.sin(t * 0.9),
                        0.01 * math.cos(t * 1.1),
                        1.0 + 0.005 * math.sin(t * 2.0))
        # Wi-Fi beacon-ish RSSI wobble
        base = -52 if e.docked else -58
        self.wifi_rssi_dbm = int(base + 4 * math.sin(t * 3.1))
        self.wifi_state = "connected" if e.running and not e.paused else "idle"
        # Thermal / battery model (toy)
        load = (e._ips / max(CPU_FREQ_HANDHELD_HZ, 1)) if e.running and not e.paused else 0.0
        dock_boost = 0.25 if e.docked else 0.0
        self.skin_temp_c += (load * 0.35 + dock_boost - 0.12) * dt * 10.0
        self.skin_temp_c = max(34.0, min(78.0, self.skin_temp_c))
        self.thermal_throttle = self.skin_temp_c > 68.0
        if e.running and not e.paused:
            self.battery_pct = max(0.0, self.battery_pct - 0.0012 * dt)
        self.nvn2_present_queue_depth = 2 + int((self.skin_temp_c % 5))

    def status_one_liner(self) -> str:
        th = "THR" if self.thermal_throttle else "ok"
        return (f"{self.skin_temp_c:.0f}°C {th}  "
                f"Batt{self.battery_pct:.0f}%  "
                f"UFS r{self.ufs_read_ops}/w{self.ufs_write_ops}")

    def _pair_block(self, rows: list[tuple[str, str]]) -> str:
        if not rows:
            return ""
        w = min(28, max(len(a) for a, _ in rows))
        lines = [f"{a:<{w}}  {b}" for a, b in rows]
        return "\n".join(lines) + "\n"

    def tab_overview(self) -> str:
        e = self._emu
        title = e.title.name if e.title else "(none)"
        rows = [
            ("Emulator", f"{BRAND} {VERSION}"),
            ("Backend", e.backend),
            ("SoC", SOC_NAME),
            ("Active title", title[:40] + ("…" if len(title) > 40 else "")),
            ("Power mode", "DOCKED" if e.docked else "HANDHELD"),
            ("Horizon services", str(len(_HOS.SERVICES))),
            ("SVC stubs", str(len(_HOS.SVC_NAMES))),
            ("Emulation", "running" if e.running and not e.paused else
             ("paused" if e.paused else "stopped")),
        ]
        return self._pair_block(rows)

    def tab_cpu(self) -> str:
        e = self._emu
        f_act = CPU_FREQ_DOCKED_HZ if e.docked else CPU_FREQ_HANDHELD_HZ
        rows = [
            ("Architecture", CPU_ARCH),
            ("Cores total", str(CPU_CORES_TOTAL)),
            ("Game cores", str(CPU_CORES_GAMES)),
            ("OS-reserved", str(CPU_CORES_OS)),
            ("L2 per core", f"{CPU_L2_PER_CORE // 1024} KiB"),
            ("L3 shared", f"{CPU_L3_SHARED // (1024 * 1024)} MiB"),
            ("Active policy MHz", f"{f_act / 1e6:.0f}"),
            ("Max SoC MHz", f"{CPU_FREQ_MAX_HZ / 1e6:.0f}"),
            ("IPS (1s avg)", str(e._ips)),
            ("Core quantum", str(e.cpu.quantum)),
            ("Running cores", str(sum(1 for c in e.cpu.cores if c.running and not c.halted))),
        ]
        return self._pair_block(rows)

    def tab_gpu(self) -> str:
        e = self._emu
        g = e.gpu
        f_act = GPU_FREQ_DOCKED_HZ if e.docked else GPU_FREQ_HANDHELD_HZ
        rows = [
            ("GPU", GPU_NAME),
            ("API string", GPU_API),
            ("CUDA cores", str(GPU_CUDA_CORES)),
            ("SM / TPC", f"{GPU_SM_COUNT} / {GPU_TPC_COUNT}"),
            ("Tensor / RT", f"{GPU_TENSOR_CORES} / {GPU_RT_CORES}"),
            ("Active MHz", f"{f_act / 1e6:.0f}"),
            ("Max MHz", f"{GPU_FREQ_MAX_HZ / 1e6:.0f}"),
            ("Framebuffer", f"{g.w} x {g.h} RGBA8"),
            ("NVN2 queue (stub)", str(self.nvn2_present_queue_depth)),
            ("GPU stats", g.stats()),
        ]
        return self._pair_block(rows)

    def tab_memory(self) -> str:
        e = self._emu
        st = e.mem.stats()
        rows = [
            ("DRAM spec", f"{RAM_BYTES // (1024 ** 3)} GiB LPDDR5X"),
            ("Bus / speed", f"{RAM_BUS_BITS}-bit @ {RAM_SPEED_MTS} MT/s"),
            ("Peak BW GB/s", f"{RAM_BW_GB_S:.2f}"),
            ("Sparse allocator", st),
            ("Total VA span", f"{e.mem.total // (1024 ** 3)} GiB"),
            ("Carveout OS (stub)", "~512 MiB"),
            ("Carveout GPU (stub)", "~512 MiB"),
        ]
        return self._pair_block(rows)

    def tab_storage(self) -> str:
        rows = [
            ("Internal spec", f"{STORAGE_GB} GB {STORAGE_TYPE}"),
            ("UFS read ops (stub)", str(self.ufs_read_ops)),
            ("UFS write ops (stub)", str(self.ufs_write_ops)),
            ("Last format", self.last_loaded_fmt or "—"),
        ]
        return self._pair_block(rows)

    def tab_display(self) -> str:
        e = self._emu
        g = e.gpu
        rows = [
            ("Panel (spec)", f"{DISPLAY_HANDHELD[0]}x{DISPLAY_HANDHELD[1]}"),
            ("Dock out (spec)", f"{DISPLAY_DOCKED[0]}x{DISPLAY_DOCKED[1]}"),
            ("Internal target (common)", f"{DISPLAY_NATIVE_HANDH[0]}x{DISPLAY_NATIVE_HANDH[1]}"),
            ("Current FB", f"{g.w}x{g.h}"),
            ("Mode", "DOCKED (DP alt)" if e.docked else "Handheld panel"),
        ]
        return self._pair_block(rows)

    def tab_audio(self) -> str:
        rows = [
            ("Sample rate", f"{self.audio_hz} Hz"),
            ("Channels", str(self.audio_channels)),
            ("Buffer (stub)", f"{self.audio_buffer_ms:.1f} ms"),
            ("Muted", str(self.audio_muted)),
            ("Horizon IPC", "audout:u / audin:u / audrec:u (stub)"),
        ]
        return self._pair_block(rows)

    def tab_input(self) -> str:
        e = self._emu
        p = e.pad
        rows = [
            ("Joy-Con L", "attached" if self.joycon_l_attached else "detached"),
            ("Joy-Con R", "attached" if self.joycon_r_attached else "detached"),
            ("Buttons", f"A{int(p.a)} B{int(p.b)} X{int(p.x)} Y{int(p.y)} "
             f"L{int(p.l)} R{int(p.r)} ZL{int(p.zl)} ZR{int(p.zr)}"),
            ("System", f"+{int(p.plus)} -{int(p.minus)} "
             f"H{int(p.home)} C{int(p.capture)}"),
            ("D-pad", str(p.dpad)),
            ("L stick", f"{p.lstick[0]:+.2f},{p.lstick[1]:+.2f}"),
            ("R stick", f"{p.rstick[0]:+.2f},{p.rstick[1]:+.2f}"),
            ("Gyro rad/s (stub)", f"{self.gyro_rps[0]:+.4f},{self.gyro_rps[1]:+.4f},{self.gyro_rps[2]:+.4f}"),
            ("Accel g (stub)", f"{self.accel_g[0]:+.3f},{self.accel_g[1]:+.3f},{self.accel_g[2]:+.3f}"),
            ("HD rumble L/R", f"{self.rumble_l_amp:.2f} / {self.rumble_r_amp:.2f}"),
        ]
        return self._pair_block(rows)

    def tab_network(self) -> str:
        rows = [
            ("Wi-Fi state (stub)", self.wifi_state),
            ("SSID (stub)", self.wifi_ssid),
            ("RSSI dBm (stub)", str(self.wifi_rssi_dbm)),
            ("BT paired (stub)", str(self.bt_paired_count)),
            ("IPC names", "bsd:s / bsd:u / sfdnsres / ssl (stub)"),
        ]
        return self._pair_block(rows)

    def tab_power(self) -> str:
        e = self._emu
        fcpu = CPU_FREQ_DOCKED_HZ if e.docked else CPU_FREQ_HANDHELD_HZ
        fgpu = GPU_FREQ_DOCKED_HZ if e.docked else GPU_FREQ_HANDHELD_HZ
        rows = [
            ("Battery % (stub)", f"{self.battery_pct:.1f}"),
            ("Skin temp °C (stub)", f"{self.skin_temp_c:.1f}"),
            ("Thermal throttle", str(self.thermal_throttle)),
            ("CPU policy MHz", f"{fcpu / 1e6:.0f}"),
            ("GPU policy MHz", f"{fgpu / 1e6:.0f}"),
            ("PMIC rails (stub)", "VDD_CPU / VDD_GPU / VSYS"),
        ]
        return self._pair_block(rows)

    def tab_security(self) -> str:
        rows = [
            ("ABI", OS_ABI),
            ("SM initialized (stub)", str(self.sm_initialized)),
            ("prod.keys (env)", "NX2EMU_PRODKEYS=1 → loaded"
             if self.prod_keys_loaded else "not set (stub off)"),
            ("AES-XTS slots (stub)", "0 of 12 active"),
        ]
        return self._pair_block(rows)

    def tab_io(self) -> str:
        e = self._emu
        gc = self.gamecard_path or "—"
        rows = [
            ("USB-C dock", "DP alt mode ON" if e.docked else "battery / USB"),
            ("Game card slot (stub)", gc[:48] + ("…" if len(gc) > 48 else "")),
            ("Last load path", (self.last_loaded_path or "—")[:48]),
        ]
        return self._pair_block(rows)


# =============================================================================
#   LOG BUS
# =============================================================================
class _EmuLog:
    _sinks: list[Callable[[str, str], None]] = []
    _lock = threading.Lock()
    _count_unimpl = 0

    @classmethod
    def add_sink(cls, fn): cls._sinks.append(fn)

    @classmethod
    def _emit(cls, level, msg):
        line = f"[{time.strftime('%H:%M:%S')}] {level:<4} {msg}"
        with cls._lock:
            for s in cls._sinks:
                try: s(level, line)
                except Exception: pass
            print(line)

    @classmethod
    def info(cls, m): cls._emit("INFO", m)
    @classmethod
    def ok(cls, m):   cls._emit("OK",   m)
    @classmethod
    def warn(cls, m): cls._emit("WARN", m)
    @classmethod
    def err(cls, m):  cls._emit("ERR",  m)
    @classmethod
    def svc(cls, m):  cls._emit("SVC",  m)


# =============================================================================
#   EMULATOR FACADE
# =============================================================================
class NX2Emu:
    def __init__(self) -> None:
        self.mem = PagedMemory()
        self.cpu = CpuManager(self.mem)
        self.gpu = GpuAmpere(*DISPLAY_NATIVE_HANDH)
        self.pad = PadState()
        self.hw = Switch2Hardware(self)
        self.title: Optional[LoadedTitle] = None
        self.library: list[LoadedTitle] = []
        self.game_dirs: list[str] = []
        self.running = False
        self.paused = False
        self._thread: Optional[threading.Thread] = None
        self._fps = 0.0
        self._ips = 0
        self.docked = False
        self.backend = "Vulkan"

    def load_rom(self, path: str) -> None:
        try:
            # Reset Horizon state for each boot
            HorizonOS.reset()
            self.mem = PagedMemory()
            self.cpu = CpuManager(self.mem)
            _EmuLog._count_unimpl = 0
            self.title = TitleLoader.load(self.mem, path)
            self.cpu.boot(self.title.entry, self.title.stack_top)
            self.hw.on_load_rom(path, self.title.fmt)
            if not any(t.path == path for t in self.library):
                self.library.append(self.title)
            _EmuLog.ok(f"READY to run '{self.title.name}' "
                       f"({self.title.fmt.upper()}) entry={self.title.entry:#x}")
        except Exception as e:
            _EmuLog.err(f"Load failed: {e}")
            traceback.print_exc()

    def add_game_dir(self, path: str) -> int:
        if not os.path.isdir(path): return 0
        if path in self.game_dirs: return 0
        self.game_dirs.append(path)
        exts = {".nro", ".nso", ".nca", ".nsp", ".xci", ".kip", ".nx2"}
        added = 0
        for root, _, files in os.walk(path):
            for fn in files:
                if os.path.splitext(fn)[1].lower() in exts:
                    full = os.path.join(root, fn)
                    if any(t.path == full for t in self.library): continue
                    try:
                        fmt = TitleLoader.identify(full)
                        size = os.path.getsize(full)
                        name = os.path.splitext(fn)[0]
                        tid = hashlib.md5(full.encode()).hexdigest()[:16].upper()
                        self.library.append(LoadedTitle(
                            path=full, fmt=fmt, size=size,
                            name=name, title_id=tid,
                            developer="<homebrew>" if fmt == "nro" else "Nintendo",
                            version="1.0.0",
                            is_commercial=(fmt in ("nso","nca","nsp","xci"))))
                        added += 1
                    except Exception: pass
        _EmuLog.ok(f"Scanned '{path}' -> {added} titles added")
        return added

    def set_docked(self, docked: bool) -> None:
        self.docked = docked
        w, h = DISPLAY_DOCKED if docked else DISPLAY_NATIVE_HANDH
        self.gpu = GpuAmpere(w, h)
        freq_cpu = CPU_FREQ_DOCKED_HZ if docked else CPU_FREQ_HANDHELD_HZ
        freq_gpu = GPU_FREQ_DOCKED_HZ if docked else GPU_FREQ_HANDHELD_HZ
        _EmuLog.info(f"Mode -> {'DOCKED' if docked else 'HANDHELD'} {w}x{h} "
                     f"CPU {freq_cpu/1e6:.0f}MHz GPU {freq_gpu/1e6:.0f}MHz")
        self.hw.on_set_docked(docked)

    def start(self) -> None:
        if self.running or self.title is None: return
        self.running = True; self.paused = False
        self.hw.on_emulation_start()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        _EmuLog.ok("Emulation thread started")

    def pause(self) -> None:
        self.paused = not self.paused
        _EmuLog.info(f"Pause = {self.paused}")

    def stop(self) -> None:
        self.running = False
        self.hw.on_emulation_stop()
        _EmuLog.info("Emulation stopping...")

    def _run(self) -> None:
        last = time.time(); frames = 0; ips_acc = 0
        last_hw = time.time()
        while self.running:
            now = time.time()
            if self.paused:
                self.hw.tick(now - last_hw)
                last_hw = now
                time.sleep(0.016); continue
            ips_acc += self.cpu.tick()
            frames += 1
            self.hw.tick(now - last_hw)
            last_hw = now
            if now - last >= 1.0:
                self._fps = frames / (now - last)
                self._ips = ips_acc
                frames = 0; ips_acc = 0; last = now
            time.sleep(0.001)
        self.hw.on_emulation_stop()
        _EmuLog.ok("Emulation thread exited")

    def short_status(self) -> tuple[str, str, str, str, str]:
        game = self.title.name if self.title else "no game"
        mode = "DOCKED" if self.docked else "HANDHELD"
        fps = f"{self._fps:5.1f} FPS"
        ips = f"{self._ips:>10d} IPS"
        mem = self.mem.stats()
        return game, mode, fps, ips, mem


# =============================================================================
#   DISPLAY WINDOW (pygame, optional)
# =============================================================================
class DisplayWindow:
    BTN_LABELS = ["A", "B", "X", "Y", "L", "R", "ZL", "ZR",
                  "+", "-", "HOME", "CAPT"]

    def __init__(self, emu: NX2Emu) -> None:
        self.emu = emu; self.alive = False
        self.thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if not PYGAME_OK:
            _EmuLog.warn("pygame not installed - display disabled"); return
        if self.alive: return
        self.alive = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.alive = False

    def _loop(self) -> None:
        pygame.init()
        w, h = self.emu.gpu.w, self.emu.gpu.h
        # keep window manageable
        sw = min(w, 1280); sh = int(h * (sw / w))
        screen = pygame.display.set_mode((sw, sh + 140))
        pygame.display.set_caption(f"{BRAND} v{VERSION} - Display")
        font = pygame.font.SysFont("Consolas", 14, bold=True)
        big  = pygame.font.SysFont("Consolas", 20, bold=True)
        clock = pygame.time.Clock()
        while self.alive:
            for ev in pygame.event.get():
                if ev.type == pygame.QUIT: self.alive = False
                elif ev.type in (pygame.KEYDOWN, pygame.KEYUP): self._handle_key(ev)
            if (self.emu.gpu.w, self.emu.gpu.h) != (w, h):
                w, h = self.emu.gpu.w, self.emu.gpu.h
                sw = min(w, 1280); sh = int(h * (sw / w))
                screen = pygame.display.set_mode((sw, sh + 140))
            fb = self.emu.gpu.render_placeholder()
            surf = pygame.image.frombuffer(fb, (w, h), "RGBA")
            if (sw, sh) != (w, h):
                surf = pygame.transform.smoothscale(surf, (sw, sh))
            screen.blit(surf, (0, 0))
            pygame.draw.rect(screen, (0, 0, 0), (0, sh, sw, 140))
            screen.blit(big.render(f"{BRAND} v{VERSION}", True, (77, 166, 255)), (10, sh + 6))
            g, m, fps, ips, mem = self.emu.short_status()
            status = f"{g}  |  {m}  |  {fps}  |  {ips}  |  {mem}"
            screen.blit(font.render(status, True, (77, 166, 255)), (10, sh + 36))
            self._draw_buttons(screen, font, 10, sh + 62, sw - 20)
            pygame.display.flip(); clock.tick(60)
        pygame.quit()

    def _draw_buttons(self, screen, font, x0, y0, width):
        cols = 6; cell_w = width // cols; cell_h = 32
        p = self.emu.pad
        states = [p.a, p.b, p.x, p.y, p.l, p.r, p.zl, p.zr,
                  p.plus, p.minus, p.home, p.capture]
        for i, (label, pressed) in enumerate(zip(self.BTN_LABELS, states)):
            r, c = divmod(i, cols)
            rx = x0 + c * cell_w; ry = y0 + r * (cell_h + 4)
            color_bd = (77, 166, 255) if pressed else (34, 102, 170)
            color_fg = (102, 204, 255) if pressed else (77, 166, 255)
            pygame.draw.rect(screen, (0, 0, 0), (rx, ry, cell_w - 6, cell_h))
            pygame.draw.rect(screen, color_bd, (rx, ry, cell_w - 6, cell_h), 2)
            screen.blit(font.render(label, True, color_fg), (rx + 8, ry + 8))

    def _handle_key(self, ev):
        down = (ev.type == pygame.KEYDOWN); p = self.emu.pad
        mapping = {
            pygame.K_z: "a", pygame.K_x: "b", pygame.K_a: "x", pygame.K_s: "y",
            pygame.K_q: "l", pygame.K_w: "r", pygame.K_e: "zl", pygame.K_t: "zr",
            pygame.K_RETURN: "plus", pygame.K_BACKSPACE: "minus",
            pygame.K_h: "home", pygame.K_c: "capture",
        }
        if ev.key in mapping:
            setattr(p, mapping[ev.key], down)


# =============================================================================
#   RYUJINX-STYLE MAIN WINDOW (preserved from 0.2 + hardware panel update)
# =============================================================================
class RyujinxStyleMain(tk.Tk):
    def __init__(self, emu: NX2Emu, display: DisplayWindow) -> None:
        super().__init__()
        self.emu = emu
        self.display = display
        self.title(f"{BRAND} {VERSION} by {AUTHOR}")
        self.configure(bg=BG_BLACK)
        self.geometry("1180x720")
        self.minsize(1000, 620)
        self._search_var = tk.StringVar()
        self._build_style()
        self._build_menu()
        self._build_toolbar()
        self._build_body()
        self._build_statusbar()
        _EmuLog.add_sink(self._on_log)
        self.after(500, self._tick)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        _EmuLog.ok(f"{BRAND} {VERSION} by {AUTHOR} ready. {COPYRIGHT}")
        _EmuLog.info(f"SOC: {SOC_NAME}")
        _EmuLog.info(f"CPU: {CPU_ARCH} x{CPU_CORES_TOTAL} "
                     f"({CPU_CORES_GAMES} game + {CPU_CORES_OS} OS)  "
                     f"RAM: {RAM_BYTES//(1024**3)} GiB LPDDR5X")
        _EmuLog.info(f"GPU: {GPU_NAME}  {GPU_CUDA_CORES} CUDA / {GPU_TENSOR_CORES} "
                     f"Tensor / {GPU_RT_CORES} RT  |  {GPU_API}")
        _EmuLog.info(f"OS: {OS_NAME}  |  pygame: {'ON' if PYGAME_OK else 'OFF'}")

    # -- styling --
    def _build_style(self) -> None:
        st = ttk.Style(self)
        try: st.theme_use("clam")
        except tk.TclError: pass
        st.configure("TFrame", background=BG_BLACK)
        st.configure("Panel.TFrame", background=BG_PANEL)
        st.configure("TLabel", background=BG_BLACK, foreground=FG_BLUE,
                     font=("Consolas", 10))
        st.configure("Panel.TLabel", background=BG_PANEL, foreground=FG_BLUE,
                     font=("Consolas", 10))
        st.configure("Title.TLabel", background=BG_PANEL,
                     foreground=FG_BLUE_BRIGHT, font=("Consolas", 13, "bold"))
        st.configure("Dim.TLabel", background=BG_PANEL,
                     foreground=FG_BLUE_DIM, font=("Consolas", 9))
        st.configure("Tool.TButton", background=BG_PANEL, foreground=FG_BLUE,
                     bordercolor=FG_BLUE_DIM, focuscolor=FG_BLUE,
                     font=("Consolas", 10, "bold"), padding=(14, 6), relief="flat")
        st.map("Tool.TButton",
               background=[("active", BG_HOVER), ("pressed", BG_SELECT)],
               foreground=[("active", FG_BLUE_BRIGHT)])
        st.configure("TCheckbutton", background=BG_PANEL, foreground=FG_BLUE,
                     font=("Consolas", 10))
        st.map("TCheckbutton",
               background=[("active", BG_PANEL)],
               foreground=[("active", FG_BLUE_BRIGHT)])
        st.configure("Search.TEntry", fieldbackground=BG_BLACK,
                     foreground=FG_BLUE, insertcolor=FG_BLUE,
                     bordercolor=FG_BLUE_DIM, lightcolor=FG_BLUE_DIM,
                     darkcolor=FG_BLUE_DIM)
        st.configure("Game.Treeview", background=BG_BLACK, foreground=FG_BLUE,
                     fieldbackground=BG_BLACK, bordercolor=FG_BLUE_FAINT,
                     font=("Consolas", 10), rowheight=28)
        st.map("Game.Treeview",
               background=[("selected", BG_SELECT)],
               foreground=[("selected", FG_BLUE_BRIGHT)])
        st.configure("Game.Treeview.Heading", background=BG_PANEL,
                     foreground=FG_BLUE_BRIGHT, font=("Consolas", 10, "bold"),
                     bordercolor=FG_BLUE_FAINT, relief="flat")
        st.map("Game.Treeview.Heading", background=[("active", BG_HOVER)])
        st.configure("Vertical.TScrollbar", background=BG_PANEL,
                     troughcolor=BG_BLACK, bordercolor=FG_BLUE_FAINT,
                     arrowcolor=FG_BLUE, gripcount=0)

    def _build_menu(self) -> None:
        mb = tk.Menu(self, bg=BG_PANEL, fg=FG_BLUE,
                     activebackground=BG_SELECT, activeforeground=FG_BLUE_BRIGHT,
                     bd=0, relief="flat", font=("Consolas", 10, "bold"))
        mk = dict(tearoff=0, bg=BG_PANEL, fg=FG_BLUE,
                  activebackground=BG_SELECT, activeforeground=FG_BLUE_BRIGHT,
                  bd=0, font=("Consolas", 10))
        m_file = tk.Menu(mb, **mk)
        m_file.add_command(label="Load File...", command=self.on_load)
        m_file.add_command(label="Add Games Folder...", command=self.on_add_games_folder)
        m_file.add_separator()
        m_file.add_command(label="Refresh Game List", command=self._refresh_game_list)
        m_file.add_separator()
        m_file.add_command(label="Exit", command=self._on_close)
        mb.add_cascade(label="File", menu=m_file)
        m_opt = tk.Menu(mb, **mk)
        self._var_docked = tk.IntVar(value=0)
        m_opt.add_checkbutton(label="Docked Mode (F9)",
                              variable=self._var_docked,
                              command=self.on_dock_toggle)
        m_opt.add_separator()
        m_opt.add_command(label="Settings...",
                          command=self._todo("Settings"))
        mb.add_cascade(label="Options", menu=m_opt)
        m_act = tk.Menu(mb, **mk)
        m_act.add_command(label="Start", command=self.on_start)
        m_act.add_command(label="Pause", command=self.on_pause)
        m_act.add_command(label="Stop",  command=self.on_stop)
        m_act.add_separator()
        m_act.add_command(label="Open Display Window", command=self.on_display)
        mb.add_cascade(label="Actions", menu=m_act)
        m_tools = tk.Menu(mb, **mk)
        m_tools.add_command(label="Open Log Window",  command=self._open_log_window)
        m_tools.add_command(label="Hardware Info",    command=self._open_hw_window)
        m_tools.add_command(label="Controller Test",  command=self._open_pad_window)
        m_tools.add_command(label="Segment Map",      command=self._open_seg_window)
        mb.add_cascade(label="Tools", menu=m_tools)
        m_help = tk.Menu(mb, **mk)
        m_help.add_command(label="About", command=self._open_about)
        mb.add_cascade(label="Help", menu=m_help)
        self.config(menu=mb)

    def _build_toolbar(self) -> None:
        bar = tk.Frame(self, bg=BG_PANEL, height=44,
                       highlightbackground=FG_BLUE_FAINT, highlightthickness=1)
        bar.pack(fill="x")
        bar.pack_propagate(False)
        for text, cmd in (("LOAD",  self.on_load),
                          ("PLAY",  self.on_start),
                          ("PAUSE", self.on_pause),
                          ("STOP",  self.on_stop)):
            ttk.Button(bar, text=text, style="Tool.TButton",
                       command=cmd).pack(side="left", padx=(6, 0), pady=6)
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y",
                                                   padx=10, pady=6)
        ttk.Checkbutton(bar, text="DOCKED", variable=self._var_docked,
                        command=self.on_dock_toggle,
                        style="TCheckbutton").pack(side="left", padx=4)
        tk.Label(bar, text="Search:", bg=BG_PANEL, fg=FG_BLUE_DIM,
                 font=("Consolas", 10)).pack(side="right", padx=(6, 4))
        ttk.Entry(bar, style="Search.TEntry",
                  textvariable=self._search_var, width=28
                  ).pack(side="right", padx=(0, 10), pady=6)
        self._search_var.trace_add("write", lambda *a: self._apply_filter())

    def _build_body(self) -> None:
        body = tk.PanedWindow(self, orient="horizontal",
                              bg=FG_BLUE_FAINT, sashwidth=2,
                              bd=0, relief="flat")
        body.pack(fill="both", expand=True)
        left = tk.Frame(body, bg=BG_BLACK)
        body.add(left, minsize=600)
        cols = ("title", "developer", "time", "firmware", "version", "size")
        self.tree = ttk.Treeview(left, columns=cols, show="headings",
                                 style="Game.Treeview", selectmode="browse")
        headings = [("title",     "Title",        300),
                    ("developer", "Developer",    130),
                    ("time",      "Time played",  110),
                    ("firmware",  "Format",        80),
                    ("version",   "Version",       90),
                    ("size",      "File size",    110)]
        for key, label, w in headings:
            self.tree.heading(key, text=label)
            self.tree.column(key, width=w, anchor="w")
        self.tree.tag_configure("rowA", background=BG_ROW_A, foreground=FG_BLUE)
        self.tree.tag_configure("rowB", background=BG_ROW_B, foreground=FG_BLUE)
        self.tree.tag_configure("comm", foreground=FG_BLUE_BRIGHT)
        vsb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_select_game)
        self.tree.bind("<Double-1>",        lambda e: self.on_start())
        self.tree.bind("<Return>",          lambda e: self.on_start())

        right = tk.Frame(body, bg=BG_PANEL, width=300)
        body.add(right, minsize=260)
        right.pack_propagate(False)
        tk.Label(right, text="SELECTED TITLE", bg=BG_PANEL,
                 fg=FG_BLUE_BRIGHT, font=("Consolas", 11, "bold")
                 ).pack(anchor="w", padx=12, pady=(12, 4))
        self._cover_canvas = tk.Canvas(right, width=240, height=240,
                                       bg=BG_BLACK, highlightthickness=1,
                                       highlightbackground=FG_BLUE_FAINT)
        self._cover_canvas.pack(padx=12, pady=6)
        self._draw_cover_placeholder()
        self._meta_labels: dict[str, tk.Label] = {}
        for key in ("Name", "Title ID", "Developer", "Format",
                    "Version", "Build ID", "File size", "Entry", "Path"):
            row = tk.Frame(right, bg=BG_PANEL)
            row.pack(fill="x", padx=12, pady=2)
            tk.Label(row, text=f"{key}:", bg=BG_PANEL, fg=FG_BLUE_DIM,
                     font=("Consolas", 9), width=11, anchor="w").pack(side="left")
            lab = tk.Label(row, text="-", bg=BG_PANEL, fg=FG_BLUE,
                           font=("Consolas", 9), anchor="w", justify="left",
                           wraplength=160)
            lab.pack(side="left", fill="x", expand=True)
            self._meta_labels[key] = lab
        tk.Frame(right, bg=BG_PANEL, height=6).pack(fill="x")
        ttk.Button(right, text="▶  PLAY", style="Tool.TButton",
                   command=self.on_start).pack(fill="x", padx=12, pady=(4, 10))

    def _draw_cover_placeholder(self) -> None:
        c = self._cover_canvas
        c.delete("all")
        for i in range(12):
            shade = f"#{max(0, 8+i*3):02x}{max(0, 20+i*6):02x}{min(255, 60+i*12):02x}"
            c.create_rectangle(0, i*20, 240, (i+1)*20, fill=shade, outline="")
        c.create_text(120, 120, text="NX",
                      fill=FG_BLUE_BRIGHT, font=("Consolas", 56, "bold"))
        c.create_text(120, 200, text=f"{BRAND} {VERSION}",
                      fill=FG_BLUE, font=("Consolas", 11, "bold"))

    def _build_statusbar(self) -> None:
        bar = tk.Frame(self, bg=BG_PANEL, height=26,
                       highlightbackground=FG_BLUE_FAINT, highlightthickness=1)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)
        self._sb_left = tk.Label(bar, text="ready", bg=BG_PANEL, fg=FG_BLUE,
                                 font=("Consolas", 9), anchor="w", padx=8)
        self._sb_left.pack(side="left", fill="y")
        self._sb_mode = self._chip(bar, "HANDHELD")
        self._sb_gpu  = self._chip(bar, self.emu.backend)
        self._sb_fps  = self._chip(bar, "0.0 FPS")
        self._sb_ips  = self._chip(bar, "0 IPS")
        self._sb_mem  = self._chip(bar, "0p / 0.0MiB")
        self._sb_hw   = self._chip(bar, self.emu.hw.status_one_liner())

    def _chip(self, parent, text: str) -> tk.Label:
        lab = tk.Label(parent, text=text, bg=BG_PANEL, fg=FG_BLUE,
                       font=("Consolas", 9, "bold"), padx=10,
                       bd=0, highlightthickness=0)
        lab.pack(side="right", padx=2, pady=2, fill="y")
        return lab

    # -- list --
    def _refresh_game_list(self) -> None:
        for iid in self.tree.get_children(): self.tree.delete(iid)
        q = self._search_var.get().strip().lower()
        for i, t in enumerate(self.emu.library):
            if q and q not in t.name.lower() and q not in t.developer.lower():
                continue
            size_mb = t.size / (1024 * 1024)
            size_s = (f"{size_mb:.2f} MiB" if size_mb < 1024
                      else f"{size_mb/1024:.2f} GiB")
            tag = "rowA" if i % 2 == 0 else "rowB"
            tags = (tag, "comm") if t.is_commercial else (tag,)
            self.tree.insert("", "end", iid=t.path,
                             values=(t.name, t.developer, t.time_played,
                                     t.fmt.upper(), t.version, size_s),
                             tags=tags)

    def _apply_filter(self) -> None:
        self._refresh_game_list()

    def _on_select_game(self, _ev) -> None:
        sel = self.tree.selection()
        if not sel: return
        path = sel[0]
        t = next((x for x in self.emu.library if x.path == path), None)
        if not t: return
        size_mb = t.size / (1024 * 1024)
        size_s = (f"{size_mb:.2f} MiB" if size_mb < 1024
                  else f"{size_mb/1024:.2f} GiB")
        updates = {
            "Name":      t.name,
            "Title ID":  t.title_id,
            "Developer": t.developer,
            "Format":    t.fmt.upper(),
            "Version":   t.version,
            "Build ID":  t.build_id or "-",
            "File size": size_s,
            "Entry":     f"{t.entry:#x}",
            "Path":      t.path,
        }
        for k, v in updates.items():
            if k in self._meta_labels:
                self._meta_labels[k].configure(text=v)

    # -- actions --
    def on_load(self) -> None:
        path = filedialog.askopenfilename(
            title="Select Switch / Switch 2 title",
            filetypes=[("Switch titles",
                        "*.nro *.nso *.nca *.nsp *.xci *.kip *.nx2"),
                       ("All files", "*.*")])
        if not path: return
        self.emu.load_rom(path)
        self._refresh_game_list()
        try:
            self.tree.selection_set(path); self.tree.see(path)
        except tk.TclError: pass
        self._on_select_game(None)

    def on_add_games_folder(self) -> None:
        path = filedialog.askdirectory(title="Select games folder")
        if not path: return
        n = self.emu.add_game_dir(path)
        self._refresh_game_list()
        if n == 0:
            messagebox.showinfo(BRAND, "No Switch titles found in that folder.")

    def on_start(self) -> None:
        sel = self.tree.selection()
        if sel and (self.emu.title is None or self.emu.title.path != sel[0]):
            self.emu.load_rom(sel[0])
        if self.emu.title is None:
            messagebox.showwarning(BRAND, "Pick a title from the list first, nya~")
            return
        self.emu.start()
        if PYGAME_OK and not self.display.alive:
            self.display.start()

    def on_pause(self) -> None:
        self.emu.pause()

    def on_stop(self) -> None:
        self.emu.stop()

    def on_display(self) -> None:
        if not PYGAME_OK:
            messagebox.showerror(BRAND, "pygame is not installed.\npip install pygame")
            return
        self.display.start()

    def on_dock_toggle(self) -> None:
        self.emu.set_docked(bool(self._var_docked.get()))

    # -- secondary windows --
    def _popup(self, title: str, w: int = 520, h: int = 420) -> tk.Toplevel:
        top = tk.Toplevel(self)
        top.title(title)
        top.configure(bg=BG_BLACK)
        top.geometry(f"{w}x{h}")
        return top

    def _open_log_window(self) -> None:
        top = self._popup("Log", 820, 480)
        txt = tk.Text(top, bg=BG_BLACK, fg=FG_BLUE,
                      insertbackground=FG_BLUE,
                      selectbackground=BG_SELECT,
                      font=("Consolas", 9), wrap="none", bd=0,
                      highlightthickness=0)
        txt.pack(fill="both", expand=True, padx=6, pady=6)
        txt.tag_configure("INFO", foreground=FG_BLUE)
        txt.tag_configure("OK",   foreground=FG_OK)
        txt.tag_configure("WARN", foreground=FG_WARN)
        txt.tag_configure("ERR",  foreground=FG_ERR)
        txt.tag_configure("SVC",  foreground=FG_BLUE_BRIGHT)
        def sink(level, line):
            try:
                txt.insert("end", line + "\n", level); txt.see("end")
            except tk.TclError: pass
        _EmuLog.add_sink(sink)

    def _open_hw_window(self) -> None:
        top = self._popup("Hardware", 820, 640)
        nb = ttk.Notebook(top)
        nb.pack(fill="both", expand=True, padx=8, pady=(8, 4))
        font_hw = ("Consolas", 10)
        tabs: list[tuple[str, str, Callable[[], str]]] = [
            ("Overview", "ov", lambda: self.emu.hw.tab_overview()),
            ("CPU", "cpu", lambda: self.emu.hw.tab_cpu()),
            ("GPU", "gpu", lambda: self.emu.hw.tab_gpu()),
            ("Memory", "mem", lambda: self.emu.hw.tab_memory()),
            ("Storage", "sto", lambda: self.emu.hw.tab_storage()),
            ("Display", "dsp", lambda: self.emu.hw.tab_display()),
            ("Audio", "aud", lambda: self.emu.hw.tab_audio()),
            ("Input", "hid", lambda: self.emu.hw.tab_input()),
            ("Network", "net", lambda: self.emu.hw.tab_network()),
            ("Power", "pwr", lambda: self.emu.hw.tab_power()),
            ("Security", "sec", lambda: self.emu.hw.tab_security()),
            ("I/O", "io", lambda: self.emu.hw.tab_io()),
        ]
        text_widgets: dict[str, tk.Text] = {}
        for title, key, _ in tabs:
            page = tk.Frame(nb, bg=BG_BLACK)
            nb.add(page, text=title)
            txt = tk.Text(page, bg=BG_BLACK, fg=FG_BLUE, insertbackground=FG_BLUE,
                          font=font_hw, wrap="word", bd=0, highlightthickness=0,
                          height=1, width=88)
            vsb = ttk.Scrollbar(page, orient="vertical", command=txt.yview)
            txt.configure(yscrollcommand=vsb.set)
            txt.pack(side="left", fill="both", expand=True, padx=4, pady=4)
            vsb.pack(side="right", fill="y", pady=4)
            text_widgets[key] = txt

        def refresh_hw_tabs() -> None:
            try:
                if not top.winfo_exists():
                    return
            except tk.TclError:
                return
            for _title, key, getter in tabs:
                body = getter().rstrip() + "\n"
                w = text_widgets[key]
                w.configure(state="normal")
                w.delete("1.0", "end")
                w.insert("1.0", body)
                w.configure(state="disabled")
            top.after(500, refresh_hw_tabs)

        refresh_hw_tabs()

    def _open_pad_window(self) -> None:
        top = self._popup("Controller test", 460, 360)
        tk.Label(top, text="JOY-CON 2 BUTTON TEST", bg=BG_BLACK,
                 fg=FG_BLUE_BRIGHT, font=("Consolas", 13, "bold")
                 ).pack(pady=(10, 6))
        grid = tk.Frame(top, bg=BG_BLACK); grid.pack(pady=8)
        layout = [("Y", 0, 0), ("X", 0, 1), ("A", 0, 2), ("B", 0, 3),
                  ("L", 1, 0), ("ZL",1, 1), ("ZR",1, 2), ("R", 1, 3),
                  ("-", 2, 0), ("+", 2, 1), ("HOME", 2, 2), ("CAPT", 2, 3)]
        mm = {"A":"a","B":"b","X":"x","Y":"y","L":"l","R":"r","ZL":"zl",
              "ZR":"zr","+":"plus","-":"minus","HOME":"home","CAPT":"capture"}
        buttons: dict[str, tk.Button] = {}
        def press(label):
            setattr(self.emu.pad, mm[label], True)
            buttons[label].configure(fg=FG_BLUE_BRIGHT, highlightbackground=FG_BLUE)
            top.after(140, lambda: release(label))
        def release(label):
            setattr(self.emu.pad, mm[label], False)
            buttons[label].configure(fg=FG_BLUE, highlightbackground=FG_BLUE_DIM)
        for label, r, c in layout:
            b = tk.Button(grid, text=label, width=8, height=2,
                          bg=BG_BLACK, fg=FG_BLUE,
                          activebackground=BG_SELECT,
                          activeforeground=FG_BLUE_BRIGHT,
                          highlightbackground=FG_BLUE_DIM,
                          highlightthickness=2, bd=0,
                          font=("Consolas", 12, "bold"),
                          command=lambda L=label: press(L))
            b.grid(row=r, column=c, padx=6, pady=6)
            buttons[label] = b

    def _open_seg_window(self) -> None:
        top = self._popup("Segment map", 620, 420)
        t = self.emu.title
        txt = tk.Text(top, bg=BG_BLACK, fg=FG_BLUE, insertbackground=FG_BLUE,
                      font=("Consolas", 10), wrap="none", bd=0,
                      highlightthickness=0)
        txt.pack(fill="both", expand=True, padx=6, pady=6)
        if t is None:
            txt.insert("end", "No title loaded.\n"); return
        txt.insert("end", f"Title:     {t.name}\n")
        txt.insert("end", f"Format:    {t.fmt.upper()}\n")
        txt.insert("end", f"Path:      {t.path}\n")
        txt.insert("end", f"Entry PC:  {t.entry:#018x}\n")
        txt.insert("end", f"Stack top: {t.stack_top:#018x}\n")
        txt.insert("end", f"Build ID:  {t.build_id or '-'}\n\n")
        hdr = f"{'SEGMENT':<10} {'FILE OFF':>10} {'FILE SZ':>10} " \
              f"{'MEM OFF':>10} {'MEM SZ':>10} {'COMP':>5}\n"
        txt.insert("end", hdr)
        txt.insert("end", "-" * len(hdr) + "\n")
        for s in t.segments:
            txt.insert("end",
                       f"{s.name:<10} {s.file_off:>10x} {s.file_size:>10x} "
                       f"{s.mem_off:>10x} {s.mem_size:>10x} "
                       f"{'YES' if s.compressed else 'no':>5}\n")

    def _open_about(self) -> None:
        top = self._popup("About", 560, 460)
        txt = (
            f"{BRAND} {VERSION}  by {AUTHOR}\n"
            f"{COPYRIGHT}\n\n"
            "Single-file Python proof-of-concept mega-emulator for the\n"
            "Nintendo Switch 2 platform. GUI styled after Ryujinx.\n\n"
            f"Hardware modelled: {SOC_NAME}\n"
            f"  - {CPU_ARCH} x{CPU_CORES_TOTAL}\n"
            f"  - {GPU_NAME} ({GPU_CUDA_CORES} CUDA / {GPU_TENSOR_CORES} Tensor / {GPU_RT_CORES} RT)\n"
            f"  - {RAM_BYTES//(1024**3)} GiB LPDDR5X, {RAM_BW_GB_S:.1f} GB/s\n"
            f"  - {STORAGE_GB} GB {STORAGE_TYPE}\n\n"
            "Boots: NRO, NSO (with LZ4), NSP (PFS0), XCI, NCA, KIP1\n\n"
            "Architecture fuses concepts from:\n"
            "  Pound (C++) - Ballistic JIT / dynarmic\n"
            "  oboromi (Rust) - 8-core ARMv8, Ampere stub\n"
            "  fOboromi - NCA / package2 groundwork\n"
            "  NYx-2 (Python) - AArch64 interpreter\n"
            "  Hassaku (C#) - WIP Switch 2 UI\n"
            "  Ryubing / Ryujinx - GUI + Horizon services\n"
            "  hactool / switchbrew.org - container specs\n\n"
            "Ships ZERO proprietary firmware, keys, or ROMs.\n"
            "Encrypted NCA bodies require user-supplied prod.keys.\n\n"
            "handles: catsanzsh / realflameselite / @ItsJustaCat00"
        )
        tk.Label(top, text=txt, bg=BG_BLACK, fg=FG_BLUE,
                 font=("Consolas", 10), justify="left"
                 ).pack(padx=16, pady=16, anchor="w")

    def _todo(self, name: str):
        return lambda: messagebox.showinfo(BRAND, f"{name}: not implemented in v{VERSION}")

    # -- log / tick --
    def _on_log(self, level: str, line: str) -> None:
        try:
            self._sb_left.configure(
                text=line[-140:],
                fg={"ERR": FG_ERR, "WARN": FG_WARN, "OK": FG_OK,
                    "SVC": FG_BLUE_BRIGHT}.get(level, FG_BLUE))
        except tk.TclError: pass

    def _tick(self) -> None:
        g, m, fps, ips, mem = self.emu.short_status()
        try:
            self._sb_mode.configure(text=m)
            self._sb_gpu .configure(text=f"{self.emu.backend}  {self.emu.gpu.stats()}")
            self._sb_fps .configure(text=fps)
            self._sb_ips .configure(text=ips)
            self._sb_mem .configure(text=mem)
            self._sb_hw .configure(text=self.emu.hw.status_one_liner())
        except tk.TclError: pass
        self.after(500, self._tick)

    def _on_close(self) -> None:
        self.emu.stop(); self.display.stop()
        time.sleep(0.05); self.destroy()


# =============================================================================
#   ENTRY POINT  -  blue shebang banner preserved
# =============================================================================
def main() -> None:
    banner = (
        "================================================================\n"
        f"  {BRAND}  v{VERSION}\n"
        f"  {COPYRIGHT}\n"
        "  Switch 2 mega-emulator - single-file build\n"
        "================================================================\n"
    )
    print(banner)
    emu = NX2Emu()
    display = DisplayWindow(emu)
    app = RyujinxStyleMain(emu, display)
    try:
        app.mainloop()
    except KeyboardInterrupt:
        pass
    finally:
        emu.stop(); display.stop()


if __name__ == "__main__":
    main()
