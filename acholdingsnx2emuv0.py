 #!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =============================================================================
#   NX2EMU 0.3  by  AC
#   (c) 1999-2026 A.C Holdings / Team Flames
#   Nintendo Switch 2 mega-emulator - single-file build
#   Ryujinx-styled GUI  |  black bg  |  blue foreground
# =============================================================================
"""
NX2EMU 0.3 - by AC

A single-file Python proof-of-concept Switch 2 emulator that fuses ideas from
every public Switch 2 emulator reference:

    Pound         (C++)   -> Ballistic JIT / dynarmic ARM64 core
    oboromi       (Rust)  -> 8-core ARMv8, 12 GiB shared mem, SM86 GPU stub
    fOboromi              -> NCA / package2 / prod.keys firmware layout
    NYx-2         (Py)    -> Pure-Python AArch64 interpreter
    Hassaku       (C#)    -> WIP Switch 2 UI reference
    Ryubing/Ryujinx(C#)   -> GUI layout, Horizon OS service dispatch
    switchbrew.org        -> SVC table + HBABI conventions

GUI styled after Ryujinx's Avalonia main window:
    [ Menu Bar                                           ]
    [ Toolbar: LOAD | PLAY | PAUSE | STOP | [search]     ]
    [                                     | RIGHT PANEL  ]
    [  GAME LIST (treeview)               |   cover art  ]
    [  icon title dev time_played fw size |   metadata   ]
    [                                     |   quick info ]
    [ Status bar: game | dock | gpu | fps | ips | mem    ]

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
VERSION   = "0.3"
AUTHOR    = "AC"
COPYRIGHT = "(c) 1999-2026 A.C Holdings / Team Flames"

# Ryujinx-inspired dark palette, but with your #4da6ff blue soul
BG_BLACK       = "#000000"
BG_PANEL       = "#050810"   # side panels / menubar
BG_ROW_A       = "#000000"   # zebra stripe A
BG_ROW_B       = "#04070d"   # zebra stripe B
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
#   SWITCH 2 HARDWARE PROFILE
# =============================================================================
S2_CPU_CORES       = 8
S2_CPU_FREQ_HZ     = 1_700_000_000
S2_RAM_BYTES       = 12 * 1024 * 1024 * 1024
S2_GPU_NAME        = "NVIDIA T239 (SM86 Ampere-class)"
S2_GPU_SM_COUNT    = 12
S2_DISPLAY_DOCKED  = (1920, 1080)
S2_DISPLAY_HANDH   = (1080, 720)
S2_OS              = "Horizon 2.x"

# =============================================================================
#   MEMORY SUBSYSTEM (4 KiB lazy pages)
# =============================================================================
PAGE_BITS = 12
PAGE_SIZE = 1 << PAGE_BITS
PAGE_MASK = PAGE_SIZE - 1


class PagedMemory:
    def __init__(self, total_bytes: int = S2_RAM_BYTES) -> None:
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

    def r32(self, a: int) -> int:
        return struct.unpack("<I", self.read(a, 4))[0]

    def r64(self, a: int) -> int:
        return struct.unpack("<Q", self.read(a, 8))[0]

    def w32(self, a: int, v: int) -> None:
        self.write(a, struct.pack("<I", v & 0xFFFFFFFF))

    def w64(self, a: int, v: int) -> None:
        self.write(a, struct.pack("<Q", v & 0xFFFFFFFFFFFFFFFF))

    def stats(self) -> str:
        res_mb = (len(self._pages) * PAGE_SIZE) / (1024 * 1024)
        return f"{len(self._pages)}p / {res_mb:.1f}MiB"


# =============================================================================
#   AARCH64 CPU INTERPRETER
# =============================================================================
class AArch64Core:
    def __init__(self, core_id: int, mem: PagedMemory) -> None:
        self.id = core_id
        self.mem = mem
        self.x = [0] * 32
        self.sp = 0
        self.pc = 0
        self.nzcv = 0
        self.running = False
        self.halted = False
        self.insn_count = 0
        self.heap_base = 0
        self.heap_size = 0

    def step(self) -> None:
        if self.halted: return
        try:
            insn = self.mem.r32(self.pc)
        except MemoryError:
            self.halted = True; return
        self.pc = (self.pc + 4) & 0xFFFFFFFFFFFFFFFF
        self.insn_count += 1
        self._decode(insn)

    def _decode(self, insn: int) -> None:
        if insn == 0xD503201F:                       # NOP
            return
        if insn == 0xD65F03C0:                       # RET
            self.pc = self.x[30]; return
        if (insn & 0xFFE0001F) == 0xD4400000:        # HLT
            self.halted = True; return
        top = (insn >> 26) & 0x3F
        if top == 0b000101 or top == 0b100101:       # B / BL
            imm26 = insn & 0x03FFFFFF
            if imm26 & (1 << 25): imm26 -= (1 << 26)
            target = (self.pc - 4 + (imm26 << 2)) & 0xFFFFFFFFFFFFFFFF
            if top == 0b100101: self.x[30] = self.pc
            self.pc = target; return
        if (insn & 0x1F800000) == 0x12800000:        # MOVZ/MOVK/MOVN
            sf = (insn >> 31) & 1
            opc = (insn >> 29) & 3
            hw = (insn >> 21) & 3
            imm16 = (insn >> 5) & 0xFFFF
            rd = insn & 0x1F
            shift = hw * 16
            if opc == 0b10:
                self.x[rd] = (imm16 << shift) & ((1 << (64 if sf else 32)) - 1)
            elif opc == 0b11:
                mask = 0xFFFF << shift
                self.x[rd] = (self.x[rd] & ~mask) | (imm16 << shift)
            elif opc == 0b00:
                self.x[rd] = (~(imm16 << shift)) & ((1 << (64 if sf else 32)) - 1)
            return
        if (insn & 0x1F000000) == 0x11000000:        # ADD/SUB imm
            sf = (insn >> 31) & 1
            op = (insn >> 30) & 1
            imm12 = (insn >> 10) & 0xFFF
            rn = (insn >> 5) & 0x1F
            rd = insn & 0x1F
            a = self.x[rn] if rn != 31 else self.sp
            r = a - imm12 if op else a + imm12
            mask = (1 << (64 if sf else 32)) - 1
            r &= mask
            if rd == 31: self.sp = r
            else:        self.x[rd] = r
            return
        if (insn & 0xFFE0001F) == 0xD4000001:        # SVC
            svc_id = (insn >> 5) & 0xFFFF
            HorizonOS.dispatch_svc(self, svc_id); return
        if (insn & 0xFFC00000) == 0xF9400000:        # LDR (imm, 64)
            imm12 = (insn >> 10) & 0xFFF
            rn = (insn >> 5) & 0x1F
            rt = insn & 0x1F
            base = self.x[rn] if rn != 31 else self.sp
            addr = base + (imm12 * 8)
            self.x[rt] = self.mem.r64(addr); return
        if (insn & 0xFFC00000) == 0xF9000000:        # STR (imm, 64)
            imm12 = (insn >> 10) & 0xFFF
            rn = (insn >> 5) & 0x1F
            rt = insn & 0x1F
            base = self.x[rn] if rn != 31 else self.sp
            addr = base + (imm12 * 8)
            self.mem.w64(addr, self.x[rt] if rt != 31 else 0); return
        _EmuLog.warn(f"[cpu{self.id}] UNIMPL {insn:#010x} @ {self.pc-4:#x}")


class CpuManager:
    def __init__(self, mem: PagedMemory, n_cores: int = S2_CPU_CORES) -> None:
        self.cores = [AArch64Core(i, mem) for i in range(n_cores)]
        self.quantum = 4096

    def boot(self, entry_pc: int, stack_top: int) -> None:
        c0 = self.cores[0]
        c0.pc = entry_pc; c0.sp = stack_top
        c0.running = True; c0.halted = False

    def tick(self) -> int:
        total = 0
        for c in self.cores:
            if not c.running or c.halted: continue
            for _ in range(self.quantum):
                if c.halted: break
                c.step(); total += 1
        return total


# =============================================================================
#   HORIZON OS STUB
# =============================================================================
class _HOS:
    SVC_NAMES = {
        0x01: "SetHeapSize", 0x03: "SetMemoryAttribute", 0x06: "QueryMemory",
        0x07: "ExitProcess", 0x0B: "SleepThread", 0x13: "MapSharedMemory",
        0x1F: "ConnectToNamedPort", 0x21: "SendSyncRequest",
        0x24: "GetProcessId", 0x26: "Break", 0x27: "OutputDebugString",
        0x29: "GetInfo", 0x33: "GetThreadContext3",
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
        "gpu", "vi:m", "vi:s", "vi:u",
        "nvdrv", "nvdrv:a", "nvdrv:s",
        "psc:m", "pctl", "pctl:a",
    ]


class HorizonOS:
    session_handles: dict[int, str] = {}
    _next_handle = 0x1000

    @classmethod
    def new_handle(cls, tag: str) -> int:
        h = cls._next_handle
        cls._next_handle += 1
        cls.session_handles[h] = tag
        return h

    @classmethod
    def dispatch_svc(cls, core: AArch64Core, svc_id: int) -> None:
        name = _HOS.SVC_NAMES.get(svc_id, f"SVC_{svc_id:#x}")
        _EmuLog.svc(f"[c{core.id}] {name}(x0={core.x[0]:#x} x1={core.x[1]:#x})")
        if svc_id == 0x01:
            core.heap_size = core.x[1]
            core.heap_base = 0x8000_0000_0000
            core.x[0] = 0; core.x[1] = core.heap_base; return
        if svc_id == 0x07:
            core.halted = True; return
        if svc_id == 0x0B: return
        if svc_id == 0x1F:
            try:
                raw = core.mem.read(core.x[1], 12)
                port = raw.split(b"\x00", 1)[0].decode("ascii", "ignore")
            except Exception: port = "?"
            h = cls.new_handle(f"port:{port}")
            core.x[0] = 0; core.x[1] = h; return
        if svc_id == 0x21:
            core.x[0] = 0; return
        if svc_id == 0x26:
            _EmuLog.warn(f"[c{core.id}] svcBreak reason={core.x[0]:#x}")
            core.halted = True; return
        if svc_id == 0x27:
            try:
                s = core.mem.read(core.x[0], core.x[1] & 0xFFFF).decode("utf-8", "replace")
                _EmuLog.info(f"[dbg c{core.id}] {s.rstrip()}")
            except Exception: pass
            core.x[0] = 0; return
        core.x[0] = 0


# =============================================================================
#   GPU STUB (SM86 Ampere-class)
# =============================================================================
class GpuSM86:
    def __init__(self, w: int, h: int) -> None:
        self.w, self.h = w, h
        self.frame = bytearray(w * h * 4)
        self.submit_count = 0
        self.draw_count = 0
        self._t0 = time.time()

    def submit(self, cmd: dict) -> None:
        self.submit_count += 1
        if cmd.get("op") == "draw": self.draw_count += 1

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
        return f"s={self.submit_count} d={self.draw_count}"


# =============================================================================
#   LOADERS (NRO/NSO/NCA/XCI/KIP stubs)
# =============================================================================
NRO_MAGIC, NSO_MAGIC = b"NRO0", b"NSO0"
NCA_MAGIC3, NCA_MAGIC2 = b"NCA3", b"NCA2"
XCI_MAGIC, KIP_MAGIC = b"HEAD", b"KIP1"


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
    entry: int = 0x0000_0000_0010_0000
    stack_top: int = 0x0000_0000_0020_0000


class TitleLoader:
    @staticmethod
    def identify(path: str) -> str:
        try:
            with open(path, "rb") as f:
                head = f.read(0x200)
        except OSError:
            return "unknown"
        if head[:4] == NRO_MAGIC: return "nro"
        if head[:4] == NSO_MAGIC: return "nso"
        if head[:4] in (NCA_MAGIC3, NCA_MAGIC2): return "nca"
        if head[:4] == KIP_MAGIC: return "kip"
        if len(head) >= 0x104 and head[0x100:0x104] == XCI_MAGIC: return "xci"
        ext = os.path.splitext(path)[1].lower().lstrip(".")
        if ext in ("nsp", "xci", "nro", "nso", "nca", "kip", "nx2"): return ext
        return "raw"

    @staticmethod
    def load(mem: PagedMemory, path: str) -> LoadedTitle:
        fmt = TitleLoader.identify(path)
        size = os.path.getsize(path)
        name = os.path.splitext(os.path.basename(path))[0]
        # derive a fake but deterministic title ID from the filename
        import hashlib
        tid = hashlib.md5(path.encode()).hexdigest()[:16].upper()
        title = LoadedTitle(path=path, fmt=fmt, size=size,
                            name=name, title_id=tid,
                            developer="<homebrew>" if fmt == "nro" else "Nintendo",
                            version="1.0.0")
        prog = bytearray()
        for _ in range(1024):
            prog += struct.pack("<I", 0xD503201F)   # NOP
        prog += struct.pack("<I", 0xD4400000)       # HLT
        mem.write(title.entry, bytes(prog))
        _EmuLog.ok(f"LOAD {fmt.upper()} '{title.name}' "
                   f"({size/1024/1024:.2f} MiB) tid={tid}")
        return title


# =============================================================================
#   INPUT (Joy-Con 2 pad state)
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
#   LOG BUS
# =============================================================================
class _EmuLog:
    _sinks: list[Callable[[str, str], None]] = []
    _lock = threading.Lock()

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
        self.gpu = GpuSM86(*S2_DISPLAY_HANDH)
        self.pad = PadState()
        self.title: Optional[LoadedTitle] = None
        self.library: list[LoadedTitle] = []   # Ryujinx-style game list
        self.game_dirs: list[str] = []
        self.running = False
        self.paused = False
        self._thread: Optional[threading.Thread] = None
        self._fps = 0.0
        self._ips = 0
        self.docked = False
        self.backend = "Vulkan"   # cosmetic - matches Ryujinx status bar

    def load_rom(self, path: str) -> None:
        try:
            self.title = TitleLoader.load(self.mem, path)
            self.cpu.boot(self.title.entry, self.title.stack_top)
            if not any(t.path == path for t in self.library):
                self.library.append(self.title)
            _EmuLog.ok(f"Boot prepared pc={self.title.entry:#x} sp={self.title.stack_top:#x}")
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
                        import hashlib
                        tid = hashlib.md5(full.encode()).hexdigest()[:16].upper()
                        self.library.append(LoadedTitle(
                            path=full, fmt=fmt, size=size,
                            name=name, title_id=tid,
                            developer="<homebrew>" if fmt == "nro" else "Nintendo",
                            version="1.0.0"))
                        added += 1
                    except Exception: pass
        _EmuLog.ok(f"Scanned '{path}' -> {added} titles added")
        return added

    def set_docked(self, docked: bool) -> None:
        self.docked = docked
        w, h = S2_DISPLAY_DOCKED if docked else S2_DISPLAY_HANDH
        self.gpu = GpuSM86(w, h)
        _EmuLog.info(f"Mode -> {'DOCKED' if docked else 'HANDHELD'} {w}x{h}")

    def start(self) -> None:
        if self.running or self.title is None: return
        self.running = True; self.paused = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        _EmuLog.ok("Emulation thread started")

    def pause(self) -> None:
        self.paused = not self.paused
        _EmuLog.info(f"Pause = {self.paused}")

    def stop(self) -> None:
        self.running = False
        _EmuLog.info("Emulation stopping...")

    def _run(self) -> None:
        last = time.time(); frames = 0; ips_acc = 0
        while self.running:
            if self.paused:
                time.sleep(0.016); continue
            ips_acc += self.cpu.tick()
            frames += 1
            now = time.time()
            if now - last >= 1.0:
                self._fps = frames / (now - last)
                self._ips = ips_acc
                frames = 0; ips_acc = 0; last = now
            time.sleep(0.001)
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
        screen = pygame.display.set_mode((w, h + 140))
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
                screen = pygame.display.set_mode((w, h + 140))
            fb = self.emu.gpu.render_placeholder()
            surf = pygame.image.frombuffer(fb, (w, h), "RGBA")
            screen.blit(surf, (0, 0))
            pygame.draw.rect(screen, (0, 0, 0), (0, h, w, 140))
            screen.blit(big.render(f"{BRAND} v{VERSION}", True, (77, 166, 255)), (10, h + 6))
            g, m, fps, ips, mem = self.emu.short_status()
            status = f"{g}  |  {m}  |  {fps}  |  {ips}  |  {mem}"
            screen.blit(font.render(status, True, (77, 166, 255)), (10, h + 36))
            self._draw_buttons(screen, font, 10, h + 62, w - 20)
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
            if down: _EmuLog.info(f"PAD {mapping[ev.key].upper()} down")


# =============================================================================
#   RYUJINX-STYLE MAIN WINDOW
#   +-- Menu Bar --------------------------------------------------+
#   |  File  Options  Actions  Tools  Help                         |
#   +-- Toolbar ---------------------------------------------------+
#   | [LOAD][PLAY][PAUSE][STOP]  [dock]  Search: [____________]   |
#   +-- Body -----------------------------------------------------+
#   | GAME LIST                             | SIDE PANEL          |
#   | icon | title | dev | played | fw | sz |   cover             |
#   |                                       |   info              |
#   +-- Status bar -----------------------------------------------+
#   | status | game | dock | gpu | fps | mem                      |
#   +-------------------------------------------------------------+
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
        _EmuLog.info(f"CPU: 8x AArch64  RAM: 12 GiB  GPU: {S2_GPU_NAME}")
        _EmuLog.info(f"OS: {S2_OS}  |  pygame: {'ON' if PYGAME_OK else 'OFF'}")

    # -- Styling -------------------------------------------------------------
    def _build_style(self) -> None:
        st = ttk.Style(self)
        try: st.theme_use("clam")
        except tk.TclError: pass
        # General
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
        # Toolbar buttons
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
        # Entry (search)
        st.configure("Search.TEntry", fieldbackground=BG_BLACK,
                     foreground=FG_BLUE, insertcolor=FG_BLUE,
                     bordercolor=FG_BLUE_DIM, lightcolor=FG_BLUE_DIM,
                     darkcolor=FG_BLUE_DIM)
        # Treeview (game list) - this is the Ryujinx centerpiece
        st.configure("Game.Treeview", background=BG_BLACK, foreground=FG_BLUE,
                     fieldbackground=BG_BLACK, bordercolor=FG_BLUE_FAINT,
                     font=("Consolas", 10), rowheight=28)
        st.map("Game.Treeview",
               background=[("selected", BG_SELECT)],
               foreground=[("selected", FG_BLUE_BRIGHT)])
        st.configure("Game.Treeview.Heading", background=BG_PANEL,
                     foreground=FG_BLUE_BRIGHT, font=("Consolas", 10, "bold"),
                     bordercolor=FG_BLUE_FAINT, relief="flat")
        st.map("Game.Treeview.Heading",
               background=[("active", BG_HOVER)])
        # Scrollbars
        st.configure("Vertical.TScrollbar", background=BG_PANEL,
                     troughcolor=BG_BLACK, bordercolor=FG_BLUE_FAINT,
                     arrowcolor=FG_BLUE, gripcount=0)

    # -- Menu Bar (Ryujinx: File/Options/Actions/Tools/Help) -----------------
    def _build_menu(self) -> None:
        mb = tk.Menu(self, bg=BG_PANEL, fg=FG_BLUE,
                     activebackground=BG_SELECT, activeforeground=FG_BLUE_BRIGHT,
                     bd=0, relief="flat",
                     font=("Consolas", 10, "bold"))
        # File
        m_file = tk.Menu(mb, tearoff=0, bg=BG_PANEL, fg=FG_BLUE,
                         activebackground=BG_SELECT, activeforeground=FG_BLUE_BRIGHT,
                         bd=0, font=("Consolas", 10))
        m_file.add_command(label="Load File...",     command=self.on_load)
        m_file.add_command(label="Load Folder...",   command=self.on_load_folder)
        m_file.add_command(label="Add Games Folder...", command=self.on_add_games_folder)
        m_file.add_separator()
        m_file.add_command(label="Refresh Game List", command=self._refresh_game_list)
        m_file.add_separator()
        m_file.add_command(label="Exit",             command=self._on_close)
        mb.add_cascade(label="File", menu=m_file)
        # Options
        m_opt = tk.Menu(mb, tearoff=0, bg=BG_PANEL, fg=FG_BLUE,
                        activebackground=BG_SELECT, activeforeground=FG_BLUE_BRIGHT,
                        bd=0, font=("Consolas", 10))
        self._var_docked = tk.IntVar(value=0)
        m_opt.add_checkbutton(label="Docked Mode  (F9)",
                              variable=self._var_docked,
                              command=self.on_dock_toggle)
        m_opt.add_separator()
        m_opt.add_command(label="Settings...", command=self._todo("Settings"))
        mb.add_cascade(label="Options", menu=m_opt)
        # Actions
        m_act = tk.Menu(mb, tearoff=0, bg=BG_PANEL, fg=FG_BLUE,
                        activebackground=BG_SELECT, activeforeground=FG_BLUE_BRIGHT,
                        bd=0, font=("Consolas", 10))
        m_act.add_command(label="Start",  command=self.on_start)
        m_act.add_command(label="Pause",  command=self.on_pause)
        m_act.add_command(label="Stop",   command=self.on_stop)
        m_act.add_separator()
        m_act.add_command(label="Open Display Window", command=self.on_display)
        mb.add_cascade(label="Actions", menu=m_act)
        # Tools
        m_tools = tk.Menu(mb, tearoff=0, bg=BG_PANEL, fg=FG_BLUE,
                          activebackground=BG_SELECT, activeforeground=FG_BLUE_BRIGHT,
                          bd=0, font=("Consolas", 10))
        m_tools.add_command(label="Open Log Window", command=self._open_log_window)
        m_tools.add_command(label="Hardware Info",   command=self._open_hw_window)
        m_tools.add_command(label="Controller Test", command=self._open_pad_window)
        mb.add_cascade(label="Tools", menu=m_tools)
        # Help
        m_help = tk.Menu(mb, tearoff=0, bg=BG_PANEL, fg=FG_BLUE,
                         activebackground=BG_SELECT, activeforeground=FG_BLUE_BRIGHT,
                         bd=0, font=("Consolas", 10))
        m_help.add_command(label="About", command=self._open_about)
        mb.add_cascade(label="Help", menu=m_help)
        self.config(menu=mb)

    # -- Toolbar -------------------------------------------------------------
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
        # Search (right side)
        tk.Label(bar, text="Search:", bg=BG_PANEL, fg=FG_BLUE_DIM,
                 font=("Consolas", 10)).pack(side="right", padx=(6, 4))
        ent = ttk.Entry(bar, style="Search.TEntry",
                        textvariable=self._search_var, width=28)
        ent.pack(side="right", padx=(0, 10), pady=6)
        self._search_var.trace_add("write", lambda *a: self._apply_filter())

    # -- Body: paned splitter, game list left, side panel right --------------
    def _build_body(self) -> None:
        body = tk.PanedWindow(self, orient="horizontal",
                              bg=FG_BLUE_FAINT, sashwidth=2,
                              bd=0, relief="flat")
        body.pack(fill="both", expand=True)

        # LEFT: treeview game list
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
        vsb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_select_game)
        self.tree.bind("<Double-1>",        lambda e: self.on_start())
        self.tree.bind("<Return>",          lambda e: self.on_start())

        # RIGHT: side panel (cover + metadata) - Ryujinx-style info pane
        right = tk.Frame(body, bg=BG_PANEL, width=300)
        body.add(right, minsize=260)
        right.pack_propagate(False)

        tk.Label(right, text="SELECTED TITLE", bg=BG_PANEL,
                 fg=FG_BLUE_BRIGHT, font=("Consolas", 11, "bold")
                 ).pack(anchor="w", padx=12, pady=(12, 4))

        # Cover art placeholder - fake Switch cart icon
        self._cover_canvas = tk.Canvas(right, width=240, height=240,
                                       bg=BG_BLACK, highlightthickness=1,
                                       highlightbackground=FG_BLUE_FAINT)
        self._cover_canvas.pack(padx=12, pady=6)
        self._draw_cover_placeholder()

        # Metadata lines
        self._meta_labels: dict[str, tk.Label] = {}
        for key in ("Name", "Title ID", "Developer", "Format",
                    "Version", "File size", "Path"):
            row = tk.Frame(right, bg=BG_PANEL)
            row.pack(fill="x", padx=12, pady=2)
            tk.Label(row, text=f"{key}:", bg=BG_PANEL, fg=FG_BLUE_DIM,
                     font=("Consolas", 9), width=11, anchor="w").pack(side="left")
            lab = tk.Label(row, text="-", bg=BG_PANEL, fg=FG_BLUE,
                           font=("Consolas", 9), anchor="w", justify="left",
                           wraplength=160)
            lab.pack(side="left", fill="x", expand=True)
            self._meta_labels[key] = lab

        # Big play button
        tk.Frame(right, bg=BG_PANEL, height=6).pack(fill="x")
        ttk.Button(right, text="▶  PLAY", style="Tool.TButton",
                   command=self.on_start).pack(fill="x", padx=12, pady=(4, 10))

    def _draw_cover_placeholder(self) -> None:
        c = self._cover_canvas
        c.delete("all")
        # dark blue gradient rectangles
        for i in range(12):
            shade = f"#{max(0, 8+i*3):02x}{max(0, 20+i*6):02x}{min(255, 60+i*12):02x}"
            c.create_rectangle(0, i*20, 240, (i+1)*20, fill=shade, outline="")
        # NX logo stub
        c.create_text(120, 120, text="NX",
                      fill=FG_BLUE_BRIGHT, font=("Consolas", 56, "bold"))
        c.create_text(120, 200, text=f"{BRAND} {VERSION}",
                      fill=FG_BLUE, font=("Consolas", 11, "bold"))

    # -- Status Bar ----------------------------------------------------------
    def _build_statusbar(self) -> None:
        bar = tk.Frame(self, bg=BG_PANEL, height=26,
                       highlightbackground=FG_BLUE_FAINT, highlightthickness=1)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)
        self._sb_left  = tk.Label(bar, text="ready", bg=BG_PANEL, fg=FG_BLUE,
                                   font=("Consolas", 9), anchor="w", padx=8)
        self._sb_left.pack(side="left", fill="y")
        # right-side indicator chips (Ryujinx look: dock | vulkan | fps | mem)
        self._sb_mode = self._make_chip(bar, "HANDHELD")
        self._sb_gpu  = self._make_chip(bar, self.emu.backend)
        self._sb_fps  = self._make_chip(bar, "0.0 FPS")
        self._sb_ips  = self._make_chip(bar, "0 IPS")
        self._sb_mem  = self._make_chip(bar, "0p / 0.0MiB")

    def _make_chip(self, parent, text: str) -> tk.Label:
        lab = tk.Label(parent, text=text, bg=BG_PANEL, fg=FG_BLUE,
                       font=("Consolas", 9, "bold"), padx=10,
                       bd=0, highlightthickness=0)
        lab.pack(side="right", padx=2, pady=2, fill="y")
        return lab

    # -- Game list management ------------------------------------------------
    def _refresh_game_list(self) -> None:
        for iid in self.tree.get_children(): self.tree.delete(iid)
        q = self._search_var.get().strip().lower()
        for i, t in enumerate(self.emu.library):
            if q and q not in t.name.lower() and q not in t.developer.lower():
                continue
            size_mb = t.size / (1024 * 1024)
            size_s = f"{size_mb:.2f} MiB" if size_mb < 1024 else f"{size_mb/1024:.2f} GiB"
            tag = "rowA" if i % 2 == 0 else "rowB"
            self.tree.insert("", "end", iid=t.path,
                             values=(t.name, t.developer, t.time_played,
                                     t.fmt.upper(), t.version, size_s),
                             tags=(tag,))

    def _apply_filter(self) -> None:
        self._refresh_game_list()

    def _on_select_game(self, _ev) -> None:
        sel = self.tree.selection()
        if not sel: return
        path = sel[0]
        t = next((x for x in self.emu.library if x.path == path), None)
        if not t: return
        size_mb = t.size / (1024 * 1024)
        size_s = f"{size_mb:.2f} MiB" if size_mb < 1024 else f"{size_mb/1024:.2f} GiB"
        updates = {
            "Name":      t.name,
            "Title ID":  t.title_id,
            "Developer": t.developer,
            "Format":    t.fmt.upper(),
            "Version":   t.version,
            "File size": size_s,
            "Path":      t.path,
        }
        for k, v in updates.items():
            if k in self._meta_labels:
                self._meta_labels[k].configure(text=v)
        # also set this as the current selected title for quick-play
        self.emu.title = t
        self.emu.cpu.boot(t.entry, t.stack_top)

    # -- Toolbar / Menu actions ---------------------------------------------
    def on_load(self) -> None:
        path = filedialog.askopenfilename(
            title="Select Switch / Switch 2 title",
            filetypes=[("Switch titles",
                        "*.nro *.nso *.nca *.nsp *.xci *.kip *.nx2"),
                       ("All files", "*.*")])
        if not path: return
        self.emu.load_rom(path)
        self._refresh_game_list()
        # select it
        if path in self.tree.get_children():
            self.tree.selection_set(path); self.tree.see(path)

    def on_load_folder(self) -> None:
        self.on_add_games_folder()

    def on_add_games_folder(self) -> None:
        path = filedialog.askdirectory(title="Select games folder")
        if not path: return
        n = self.emu.add_game_dir(path)
        self._refresh_game_list()
        if n == 0:
            messagebox.showinfo(BRAND, "No Switch titles found in that folder.")

    def on_start(self) -> None:
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
            messagebox.showerror(BRAND,
                                 "pygame is not installed.\n"
                                 "Run:  pip install pygame")
            return
        self.display.start()

    def on_dock_toggle(self) -> None:
        self.emu.set_docked(bool(self._var_docked.get()))

    # -- Secondary windows --------------------------------------------------
    def _popup(self, title: str, w: int = 520, h: int = 420) -> tk.Toplevel:
        top = tk.Toplevel(self)
        top.title(title)
        top.configure(bg=BG_BLACK)
        top.geometry(f"{w}x{h}")
        return top

    def _open_log_window(self) -> None:
        top = self._popup("Log", 760, 440)
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
        top = self._popup("Hardware", 520, 360)
        rows = [
            ("BRAND",    f"{BRAND} {VERSION} by {AUTHOR}"),
            ("PLATFORM", "Nintendo Switch 2 (Ounce)"),
            ("CPU",      f"ARMv8.2-A  8 cores @ {S2_CPU_FREQ_HZ/1e9:.2f} GHz"),
            ("RAM",      f"{S2_RAM_BYTES // (1024**3)} GiB LPDDR5X (shared)"),
            ("GPU",      f"{S2_GPU_NAME}  ({S2_GPU_SM_COUNT} SMs)"),
            ("OS",       S2_OS),
            ("HANDHELD", f"{S2_DISPLAY_HANDH[0]}x{S2_DISPLAY_HANDH[1]}"),
            ("DOCKED",   f"{S2_DISPLAY_DOCKED[0]}x{S2_DISPLAY_DOCKED[1]}"),
            ("SERVICES", f"{len(_HOS.SERVICES)} nn:: stubs"),
            ("SVC TBL",  f"{len(_HOS.SVC_NAMES)} Horizon syscalls"),
        ]
        for i, (k, v) in enumerate(rows):
            tk.Label(top, text=k + ":", bg=BG_BLACK, fg=FG_BLUE_BRIGHT,
                     font=("Consolas", 10, "bold")
                     ).grid(row=i, column=0, sticky="w", padx=12, pady=4)
            tk.Label(top, text=v, bg=BG_BLACK, fg=FG_BLUE,
                     font=("Consolas", 10)
                     ).grid(row=i, column=1, sticky="w", padx=12, pady=4)

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
            buttons[label].configure(fg=FG_BLUE_BRIGHT,
                                     highlightbackground=FG_BLUE)
            top.after(140, lambda: release(label))
        def release(label):
            setattr(self.emu.pad, mm[label], False)
            buttons[label].configure(fg=FG_BLUE,
                                     highlightbackground=FG_BLUE_DIM)
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

    def _open_about(self) -> None:
        top = self._popup("About", 520, 420)
        txt = (
            f"{BRAND} {VERSION}  by {AUTHOR}\n"
            f"{COPYRIGHT}\n\n"
            "Single-file Python proof-of-concept mega-emulator for the\n"
            "Nintendo Switch 2 platform. GUI styled after Ryujinx.\n"
            "Architecture fuses concepts from:\n\n"
            "  - Pound      (C++, Ballistic JIT / dynarmic fork)\n"
            "  - oboromi    (Rust, 8-core ARMv8, SM86 Ampere stub)\n"
            "  - fOboromi   (NCA / package2 / prod.keys groundwork)\n"
            "  - NYx-2      (Python AArch64 interpreter)\n"
            "  - Hassaku    (C# WIP UI reference)\n"
            "  - Ryubing    (Ryujinx fork - GUI + Horizon service dispatch)\n"
            "  - switchbrew.org public documentation\n\n"
            "Ships ZERO proprietary firmware, keys, or ROMs.\n"
            "All loaders are stubs. Research and education only.\n\n"
            "handles: catsanzsh / realflameselite / @ItsJustaCat00"
        )
        tk.Label(top, text=txt, bg=BG_BLACK, fg=FG_BLUE,
                 font=("Consolas", 10), justify="left"
                 ).pack(padx=16, pady=16, anchor="w")

    def _todo(self, name: str):
        return lambda: messagebox.showinfo(BRAND, f"{name}: not implemented in v{VERSION}")

    # -- Log / tick ---------------------------------------------------------
    def _on_log(self, level: str, line: str) -> None:
        # main window just reflects the latest line on the left status bar
        try:
            self._sb_left.configure(text=line[-120:],
                fg={"ERR": FG_ERR, "WARN": FG_WARN,
                    "OK": FG_OK, "SVC": FG_BLUE_BRIGHT}.get(level, FG_BLUE))
        except tk.TclError: pass

    def _tick(self) -> None:
        g, m, fps, ips, mem = self.emu.short_status()
        try:
            self._sb_mode.configure(text=m)
            self._sb_gpu .configure(text=f"{self.emu.backend}  {self.emu.gpu.stats()}")
            self._sb_fps .configure(text=fps)
            self._sb_ips .configure(text=ips)
            self._sb_mem .configure(text=mem)
        except tk.TclError: pass
        self.after(500, self._tick)

    def _on_close(self) -> None:
        self.emu.stop(); self.display.stop()
        time.sleep(0.05); self.destroy()


# =============================================================================
#   ENTRY POINT  -  keeping the blue shebang banner ▼
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
