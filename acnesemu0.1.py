#!/usr/bin/env python3
"""
AC'S NES EMU 0.1.1A

A pre-baked black-and-blue Tkinter NES emulator scaffold.

What is included:
- iNES ROM loading and header parsing.
- Mapper 0 / NROM CPU ROM mapping.
- A practical 6502/2A03 CPU core for official opcodes.
- A CHR-ROM preview renderer using blue hues.
- A warm, simple Tkinter interface with black background and blue text/buttons.
- Cython-friendly structure and optional cython decorators/shim.

What is not included yet:
- Cycle-accurate PPU rendering.
- APU audio.
- Full mapper support beyond NROM-style PRG mapping.

Run:
    python acnesemu0.1.py

Optional Cython note:
    The core is written with small, typed-ish methods to make it easy to move CPU hot paths
    into a .pyx file later. This .py file remains normal Python for one-click use.
"""

import tkinter
from tkinter import filedialog, messagebox
import os
import sys
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

try:
    import cython  # type: ignore
except Exception:  # pragma: no cover - fallback for normal Python installs
    class _CythonShim:
        int = int
        bint = bool

        def locals(self, **_kwargs):
            def decorate(func):
                return func
            return decorate

        def cfunc(self, func=None, **_kwargs):
            if func is None:
                def decorate(inner):
                    return inner
                return decorate
            return func

    cython = _CythonShim()  # type: ignore


APP_TITLE = "AC'S NES EMU 0.1.1A"
APP_VERSION = "0.1.1A"

BLACK = "#000000"
BLUE_TEXT = "#00A8FF"
BLUE_BUTTON = "#001C3A"
BLUE_BUTTON_HOVER = "#003D79"
BLUE_EDGE = "#006BFF"
BLUE_BRIGHT = "#46D9FF"
BLUE_DIM = "#00345F"

NES_WIDTH = 256
NES_HEIGHT = 240


# ---------------------------------------------------------------------------
# NES ROM parsing
# ---------------------------------------------------------------------------


@dataclass
class NESROM:
    path: str
    name: str
    prg_rom: bytes
    chr_rom: bytes
    mapper: int
    mirroring: str
    prg_banks: int
    chr_banks: int
    has_trainer: bool
    battery: bool
    ines2: bool

    @classmethod
    def load(cls, path: str) -> "NESROM":
        with open(path, "rb") as rom_file:
            data = rom_file.read()

        if len(data) < 16:
            raise ValueError("File is too small to be an iNES ROM.")
        if data[0:4] != b"NES\x1A":
            raise ValueError("Missing iNES magic header: expected NES<EOF>.")

        prg_banks = data[4]
        chr_banks = data[5]
        flags6 = data[6]
        flags7 = data[7]
        has_trainer = bool(flags6 & 0x04)
        battery = bool(flags6 & 0x02)
        ines2 = (flags7 & 0x0C) == 0x08
        mapper = ((flags7 & 0xF0) | (flags6 >> 4)) & 0xFF

        if flags6 & 0x08:
            mirroring = "four-screen"
        elif flags6 & 0x01:
            mirroring = "vertical"
        else:
            mirroring = "horizontal"

        offset = 16 + (512 if has_trainer else 0)
        prg_size = prg_banks * 16 * 1024
        chr_size = chr_banks * 8 * 1024

        if len(data) < offset + prg_size:
            raise ValueError("ROM ended before declared PRG-ROM data was complete.")

        prg_rom = data[offset:offset + prg_size]
        offset += prg_size

        if chr_size:
            chr_rom = data[offset:offset + chr_size]
            if len(chr_rom) < chr_size:
                raise ValueError("ROM ended before declared CHR-ROM data was complete.")
        else:
            # CHR-RAM cartridges have no CHR-ROM; allocate an empty pattern table.
            chr_rom = bytes(8 * 1024)

        return cls(
            path=path,
            name=os.path.basename(path),
            prg_rom=bytes(prg_rom),
            chr_rom=bytes(chr_rom),
            mapper=mapper,
            mirroring=mirroring,
            prg_banks=prg_banks,
            chr_banks=chr_banks,
            has_trainer=has_trainer,
            battery=battery,
            ines2=ines2,
        )

    def summary(self) -> str:
        chr_label = f"{self.chr_banks} x 8 KiB" if self.chr_banks else "CHR-RAM / blank"
        return (
            f"{self.name}\n"
            f"Mapper: {self.mapper}\n"
            f"PRG-ROM: {self.prg_banks} x 16 KiB\n"
            f"CHR: {chr_label}\n"
            f"Mirroring: {self.mirroring}\n"
            f"Battery: {'yes' if self.battery else 'no'}\n"
            f"Trainer: {'yes' if self.has_trainer else 'no'}\n"
            f"Header: {'NES 2.0-ish' if self.ines2 else 'iNES'}"
        )


# ---------------------------------------------------------------------------
# CPU bus: enough for NROM CPU fetch/execute and educational stepping.
# ---------------------------------------------------------------------------


class CPUBus:
    def __init__(self) -> None:
        self.ram = bytearray(2 * 1024)
        self.prg_ram = bytearray(8 * 1024)
        self.prg_rom = bytes()
        self.mapper = 0

    def load_rom(self, rom: NESROM) -> None:
        self.ram[:] = b"\x00" * len(self.ram)
        self.prg_ram[:] = b"\x00" * len(self.prg_ram)
        self.prg_rom = rom.prg_rom
        self.mapper = rom.mapper

    @cython.locals(addr=cython.int)
    def cpu_read(self, addr: int) -> int:
        addr &= 0xFFFF
        if addr < 0x2000:
            return self.ram[addr & 0x07FF]
        if addr < 0x4000:
            # PPU registers are mirrored here. PPU is not implemented in this scaffold.
            return 0
        if addr < 0x4020:
            # APU and controller ports. Controller support is intentionally stubbed for now.
            return 0
        if 0x6000 <= addr < 0x8000:
            return self.prg_ram[addr - 0x6000]
        if addr >= 0x8000:
            if not self.prg_rom:
                return 0
            if len(self.prg_rom) == 0x4000:
                return self.prg_rom[(addr - 0x8000) & 0x3FFF]
            return self.prg_rom[(addr - 0x8000) % len(self.prg_rom)]
        return 0

    @cython.locals(addr=cython.int, value=cython.int)
    def cpu_write(self, addr: int, value: int) -> None:
        addr &= 0xFFFF
        value &= 0xFF
        if addr < 0x2000:
            self.ram[addr & 0x07FF] = value
        elif 0x6000 <= addr < 0x8000:
            self.prg_ram[addr - 0x6000] = value
        # Mapper/MMC writes and PPU/APU writes are intentionally ignored for now.


# ---------------------------------------------------------------------------
# 6502 / Ricoh 2A03 CPU core
# ---------------------------------------------------------------------------


class UnsupportedOpcode(RuntimeError):
    pass


# status flags
C_FLAG = 0x01
Z_FLAG = 0x02
I_FLAG = 0x04
D_FLAG = 0x08
B_FLAG = 0x10
U_FLAG = 0x20
V_FLAG = 0x40
N_FLAG = 0x80

MODE_LEN = {
    "imp": 1,
    "acc": 1,
    "imm": 2,
    "zp": 2,
    "zpx": 2,
    "zpy": 2,
    "rel": 2,
    "abs": 3,
    "absx": 3,
    "absy": 3,
    "ind": 3,
    "indx": 2,
    "indy": 2,
}

READ_PAGE_EXTRA = {"LDA", "LDX", "LDY", "ADC", "SBC", "CMP", "AND", "ORA", "EOR"}
BRANCHES = {"BPL", "BMI", "BVC", "BVS", "BCC", "BCS", "BNE", "BEQ"}

# Official 6502 opcode table. NES decimal mode flag exists, but decimal arithmetic is disabled on 2A03.
OPCODES: Dict[int, Tuple[str, str, int]] = {
    # Load/store
    0xA9: ("LDA", "imm", 2), 0xA5: ("LDA", "zp", 3), 0xB5: ("LDA", "zpx", 4),
    0xAD: ("LDA", "abs", 4), 0xBD: ("LDA", "absx", 4), 0xB9: ("LDA", "absy", 4),
    0xA1: ("LDA", "indx", 6), 0xB1: ("LDA", "indy", 5),

    0xA2: ("LDX", "imm", 2), 0xA6: ("LDX", "zp", 3), 0xB6: ("LDX", "zpy", 4),
    0xAE: ("LDX", "abs", 4), 0xBE: ("LDX", "absy", 4),

    0xA0: ("LDY", "imm", 2), 0xA4: ("LDY", "zp", 3), 0xB4: ("LDY", "zpx", 4),
    0xAC: ("LDY", "abs", 4), 0xBC: ("LDY", "absx", 4),

    0x85: ("STA", "zp", 3), 0x95: ("STA", "zpx", 4), 0x8D: ("STA", "abs", 4),
    0x9D: ("STA", "absx", 5), 0x99: ("STA", "absy", 5), 0x81: ("STA", "indx", 6),
    0x91: ("STA", "indy", 6),

    0x86: ("STX", "zp", 3), 0x96: ("STX", "zpy", 4), 0x8E: ("STX", "abs", 4),
    0x84: ("STY", "zp", 3), 0x94: ("STY", "zpx", 4), 0x8C: ("STY", "abs", 4),

    # Register transfers
    0xAA: ("TAX", "imp", 2), 0xA8: ("TAY", "imp", 2), 0x8A: ("TXA", "imp", 2),
    0x98: ("TYA", "imp", 2), 0xBA: ("TSX", "imp", 2), 0x9A: ("TXS", "imp", 2),

    # Stack
    0x48: ("PHA", "imp", 3), 0x68: ("PLA", "imp", 4), 0x08: ("PHP", "imp", 3),
    0x28: ("PLP", "imp", 4),

    # Arithmetic
    0x69: ("ADC", "imm", 2), 0x65: ("ADC", "zp", 3), 0x75: ("ADC", "zpx", 4),
    0x6D: ("ADC", "abs", 4), 0x7D: ("ADC", "absx", 4), 0x79: ("ADC", "absy", 4),
    0x61: ("ADC", "indx", 6), 0x71: ("ADC", "indy", 5),

    0xE9: ("SBC", "imm", 2), 0xE5: ("SBC", "zp", 3), 0xF5: ("SBC", "zpx", 4),
    0xED: ("SBC", "abs", 4), 0xFD: ("SBC", "absx", 4), 0xF9: ("SBC", "absy", 4),
    0xE1: ("SBC", "indx", 6), 0xF1: ("SBC", "indy", 5),

    0xC9: ("CMP", "imm", 2), 0xC5: ("CMP", "zp", 3), 0xD5: ("CMP", "zpx", 4),
    0xCD: ("CMP", "abs", 4), 0xDD: ("CMP", "absx", 4), 0xD9: ("CMP", "absy", 4),
    0xC1: ("CMP", "indx", 6), 0xD1: ("CMP", "indy", 5),

    0xE0: ("CPX", "imm", 2), 0xE4: ("CPX", "zp", 3), 0xEC: ("CPX", "abs", 4),
    0xC0: ("CPY", "imm", 2), 0xC4: ("CPY", "zp", 3), 0xCC: ("CPY", "abs", 4),

    # Increment/decrement
    0xE6: ("INC", "zp", 5), 0xF6: ("INC", "zpx", 6), 0xEE: ("INC", "abs", 6),
    0xFE: ("INC", "absx", 7), 0xE8: ("INX", "imp", 2), 0xC8: ("INY", "imp", 2),
    0xC6: ("DEC", "zp", 5), 0xD6: ("DEC", "zpx", 6), 0xCE: ("DEC", "abs", 6),
    0xDE: ("DEC", "absx", 7), 0xCA: ("DEX", "imp", 2), 0x88: ("DEY", "imp", 2),

    # Shifts/rotates
    0x0A: ("ASL", "acc", 2), 0x06: ("ASL", "zp", 5), 0x16: ("ASL", "zpx", 6),
    0x0E: ("ASL", "abs", 6), 0x1E: ("ASL", "absx", 7),
    0x4A: ("LSR", "acc", 2), 0x46: ("LSR", "zp", 5), 0x56: ("LSR", "zpx", 6),
    0x4E: ("LSR", "abs", 6), 0x5E: ("LSR", "absx", 7),
    0x2A: ("ROL", "acc", 2), 0x26: ("ROL", "zp", 5), 0x36: ("ROL", "zpx", 6),
    0x2E: ("ROL", "abs", 6), 0x3E: ("ROL", "absx", 7),
    0x6A: ("ROR", "acc", 2), 0x66: ("ROR", "zp", 5), 0x76: ("ROR", "zpx", 6),
    0x6E: ("ROR", "abs", 6), 0x7E: ("ROR", "absx", 7),

    # Logic
    0x29: ("AND", "imm", 2), 0x25: ("AND", "zp", 3), 0x35: ("AND", "zpx", 4),
    0x2D: ("AND", "abs", 4), 0x3D: ("AND", "absx", 4), 0x39: ("AND", "absy", 4),
    0x21: ("AND", "indx", 6), 0x31: ("AND", "indy", 5),

    0x09: ("ORA", "imm", 2), 0x05: ("ORA", "zp", 3), 0x15: ("ORA", "zpx", 4),
    0x0D: ("ORA", "abs", 4), 0x1D: ("ORA", "absx", 4), 0x19: ("ORA", "absy", 4),
    0x01: ("ORA", "indx", 6), 0x11: ("ORA", "indy", 5),

    0x49: ("EOR", "imm", 2), 0x45: ("EOR", "zp", 3), 0x55: ("EOR", "zpx", 4),
    0x4D: ("EOR", "abs", 4), 0x5D: ("EOR", "absx", 4), 0x59: ("EOR", "absy", 4),
    0x41: ("EOR", "indx", 6), 0x51: ("EOR", "indy", 5),

    0x24: ("BIT", "zp", 3), 0x2C: ("BIT", "abs", 4),

    # Control flow
    0x4C: ("JMP", "abs", 3), 0x6C: ("JMP", "ind", 5), 0x20: ("JSR", "abs", 6),
    0x60: ("RTS", "imp", 6), 0x00: ("BRK", "imp", 7), 0x40: ("RTI", "imp", 6),

    0x10: ("BPL", "rel", 2), 0x30: ("BMI", "rel", 2), 0x50: ("BVC", "rel", 2),
    0x70: ("BVS", "rel", 2), 0x90: ("BCC", "rel", 2), 0xB0: ("BCS", "rel", 2),
    0xD0: ("BNE", "rel", 2), 0xF0: ("BEQ", "rel", 2),

    # Flags and NOP
    0x18: ("CLC", "imp", 2), 0x38: ("SEC", "imp", 2), 0x58: ("CLI", "imp", 2),
    0x78: ("SEI", "imp", 2), 0xB8: ("CLV", "imp", 2), 0xD8: ("CLD", "imp", 2),
    0xF8: ("SED", "imp", 2), 0xEA: ("NOP", "imp", 2),
}


class CPU6502:
    def __init__(self, bus: CPUBus) -> None:
        self.bus = bus
        self.a = 0
        self.x = 0
        self.y = 0
        self.sp = 0xFD
        self.pc = 0x8000
        self.status = U_FLAG | I_FLAG
        self.cycles = 0
        self.last_trace = ""
        self.halted = False
        self._extra_cycles = 0

    def read(self, addr: int) -> int:
        return self.bus.cpu_read(addr)

    def write(self, addr: int, value: int) -> None:
        self.bus.cpu_write(addr, value)

    def flag(self, flag: int) -> int:
        return 1 if (self.status & flag) else 0

    def set_flag(self, flag: int, condition: bool) -> None:
        if condition:
            self.status |= flag
        else:
            self.status &= (~flag) & 0xFF
        self.status |= U_FLAG

    def set_zn(self, value: int) -> None:
        value &= 0xFF
        self.set_flag(Z_FLAG, value == 0)
        self.set_flag(N_FLAG, bool(value & 0x80))

    def push(self, value: int) -> None:
        self.write(0x0100 | self.sp, value)
        self.sp = (self.sp - 1) & 0xFF

    def pop(self) -> int:
        self.sp = (self.sp + 1) & 0xFF
        return self.read(0x0100 | self.sp)

    def read_word(self, addr: int) -> int:
        lo = self.read(addr)
        hi = self.read((addr + 1) & 0xFFFF)
        return lo | (hi << 8)

    def read_word_bug(self, addr: int) -> int:
        # Emulate the 6502 JMP ($xxFF) page-wrap bug.
        lo = self.read(addr)
        hi_addr = (addr & 0xFF00) | ((addr + 1) & 0x00FF)
        hi = self.read(hi_addr)
        return lo | (hi << 8)

    def reset(self) -> None:
        self.a = 0
        self.x = 0
        self.y = 0
        self.sp = 0xFD
        self.status = U_FLAG | I_FLAG
        self.cycles = 7
        self.halted = False
        vector = self.read_word(0xFFFC)
        self.pc = vector if vector not in (0x0000, 0xFFFF) else 0x8000
        self.last_trace = f"RESET -> PC=${self.pc:04X}"

    def irq(self) -> None:
        if not self.flag(I_FLAG):
            self.push((self.pc >> 8) & 0xFF)
            self.push(self.pc & 0xFF)
            self.set_flag(B_FLAG, False)
            self.set_flag(U_FLAG, True)
            self.set_flag(I_FLAG, True)
            self.push(self.status)
            self.pc = self.read_word(0xFFFE)
            self.cycles += 7

    def nmi(self) -> None:
        self.push((self.pc >> 8) & 0xFF)
        self.push(self.pc & 0xFF)
        self.set_flag(B_FLAG, False)
        self.set_flag(U_FLAG, True)
        self.set_flag(I_FLAG, True)
        self.push(self.status)
        self.pc = self.read_word(0xFFFA)
        self.cycles += 8

    def address(self, mode: str) -> Tuple[Optional[int], bool]:
        if mode in ("imp", "acc"):
            return None, False
        if mode == "imm":
            addr = self.pc
            self.pc = (self.pc + 1) & 0xFFFF
            return addr, False
        if mode == "zp":
            addr = self.read(self.pc)
            self.pc = (self.pc + 1) & 0xFFFF
            return addr, False
        if mode == "zpx":
            addr = (self.read(self.pc) + self.x) & 0xFF
            self.pc = (self.pc + 1) & 0xFFFF
            return addr, False
        if mode == "zpy":
            addr = (self.read(self.pc) + self.y) & 0xFF
            self.pc = (self.pc + 1) & 0xFFFF
            return addr, False
        if mode == "abs":
            lo = self.read(self.pc)
            hi = self.read((self.pc + 1) & 0xFFFF)
            self.pc = (self.pc + 2) & 0xFFFF
            return lo | (hi << 8), False
        if mode == "absx":
            lo = self.read(self.pc)
            hi = self.read((self.pc + 1) & 0xFFFF)
            base = lo | (hi << 8)
            self.pc = (self.pc + 2) & 0xFFFF
            addr = (base + self.x) & 0xFFFF
            return addr, (base & 0xFF00) != (addr & 0xFF00)
        if mode == "absy":
            lo = self.read(self.pc)
            hi = self.read((self.pc + 1) & 0xFFFF)
            base = lo | (hi << 8)
            self.pc = (self.pc + 2) & 0xFFFF
            addr = (base + self.y) & 0xFFFF
            return addr, (base & 0xFF00) != (addr & 0xFF00)
        if mode == "ind":
            ptr_lo = self.read(self.pc)
            ptr_hi = self.read((self.pc + 1) & 0xFFFF)
            self.pc = (self.pc + 2) & 0xFFFF
            return self.read_word_bug(ptr_lo | (ptr_hi << 8)), False
        if mode == "indx":
            zp_ptr = (self.read(self.pc) + self.x) & 0xFF
            self.pc = (self.pc + 1) & 0xFFFF
            lo = self.read(zp_ptr)
            hi = self.read((zp_ptr + 1) & 0xFF)
            return lo | (hi << 8), False
        if mode == "indy":
            zp_ptr = self.read(self.pc)
            self.pc = (self.pc + 1) & 0xFFFF
            lo = self.read(zp_ptr)
            hi = self.read((zp_ptr + 1) & 0xFF)
            base = lo | (hi << 8)
            addr = (base + self.y) & 0xFFFF
            return addr, (base & 0xFF00) != (addr & 0xFF00)
        if mode == "rel":
            raw = self.read(self.pc)
            self.pc = (self.pc + 1) & 0xFFFF
            offset = raw if raw < 0x80 else raw - 0x100
            return offset, False
        raise ValueError(f"Unknown addressing mode: {mode}")

    def format_trace(self, pc: int, opcode: int, mnemonic: str, mode: str, operands: List[int]) -> str:
        raw = " ".join([f"{opcode:02X}"] + [f"{b:02X}" for b in operands]).ljust(8)
        op = ""
        if mode == "imm":
            op = f"#${operands[0]:02X}"
        elif mode == "zp":
            op = f"${operands[0]:02X}"
        elif mode == "zpx":
            op = f"${operands[0]:02X},X"
        elif mode == "zpy":
            op = f"${operands[0]:02X},Y"
        elif mode == "abs":
            op = f"${operands[0] | (operands[1] << 8):04X}"
        elif mode == "absx":
            op = f"${operands[0] | (operands[1] << 8):04X},X"
        elif mode == "absy":
            op = f"${operands[0] | (operands[1] << 8):04X},Y"
        elif mode == "ind":
            op = f"(${operands[0] | (operands[1] << 8):04X})"
        elif mode == "indx":
            op = f"(${operands[0]:02X},X)"
        elif mode == "indy":
            op = f"(${operands[0]:02X}),Y"
        elif mode == "rel":
            offset = operands[0] if operands[0] < 0x80 else operands[0] - 0x100
            target = (pc + 2 + offset) & 0xFFFF
            op = f"${target:04X}"
        elif mode == "acc":
            op = "A"
        return f"{pc:04X}: {raw} {mnemonic} {op}".rstrip()

    def step(self) -> int:
        if self.halted:
            return 0

        pc_before = self.pc
        opcode = self.read(self.pc)
        self.pc = (self.pc + 1) & 0xFFFF

        if opcode not in OPCODES:
            self.last_trace = f"{pc_before:04X}: {opcode:02X}       ???"
            self.halted = True
            raise UnsupportedOpcode(f"Unsupported opcode ${opcode:02X} at ${pc_before:04X}")

        mnemonic, mode, base_cycles = OPCODES[opcode]
        operand_count = MODE_LEN[mode] - 1
        operands = [self.read((pc_before + 1 + i) & 0xFFFF) for i in range(operand_count)]
        self.last_trace = self.format_trace(pc_before, opcode, mnemonic, mode, operands)

        self._extra_cycles = 0
        addr, page_cross = self.address(mode)
        self.execute(mnemonic, mode, addr)

        cycles = base_cycles + self._extra_cycles
        if page_cross and mnemonic in READ_PAGE_EXTRA:
            cycles += 1
        self.cycles += cycles
        self.status |= U_FLAG
        return cycles

    def value_at(self, addr: Optional[int]) -> int:
        if addr is None:
            return 0
        return self.read(addr)

    def compare(self, register: int, value: int) -> None:
        result = (register - value) & 0x1FF
        self.set_flag(C_FLAG, register >= value)
        self.set_zn(result & 0xFF)

    def branch(self, condition: bool, offset: Optional[int]) -> None:
        if not condition or offset is None:
            return
        old_pc = self.pc
        self.pc = (self.pc + offset) & 0xFFFF
        self._extra_cycles += 1
        if (old_pc & 0xFF00) != (self.pc & 0xFF00):
            self._extra_cycles += 1

    def execute(self, mnemonic: str, mode: str, addr: Optional[int]) -> None:
        if mnemonic == "LDA":
            self.a = self.value_at(addr)
            self.set_zn(self.a)
        elif mnemonic == "LDX":
            self.x = self.value_at(addr)
            self.set_zn(self.x)
        elif mnemonic == "LDY":
            self.y = self.value_at(addr)
            self.set_zn(self.y)
        elif mnemonic == "STA":
            if addr is not None:
                self.write(addr, self.a)
        elif mnemonic == "STX":
            if addr is not None:
                self.write(addr, self.x)
        elif mnemonic == "STY":
            if addr is not None:
                self.write(addr, self.y)

        elif mnemonic == "TAX":
            self.x = self.a
            self.set_zn(self.x)
        elif mnemonic == "TAY":
            self.y = self.a
            self.set_zn(self.y)
        elif mnemonic == "TXA":
            self.a = self.x
            self.set_zn(self.a)
        elif mnemonic == "TYA":
            self.a = self.y
            self.set_zn(self.a)
        elif mnemonic == "TSX":
            self.x = self.sp
            self.set_zn(self.x)
        elif mnemonic == "TXS":
            self.sp = self.x

        elif mnemonic == "PHA":
            self.push(self.a)
        elif mnemonic == "PLA":
            self.a = self.pop()
            self.set_zn(self.a)
        elif mnemonic == "PHP":
            self.push(self.status | B_FLAG | U_FLAG)
        elif mnemonic == "PLP":
            self.status = (self.pop() | U_FLAG) & ~B_FLAG

        elif mnemonic == "ADC":
            value = self.value_at(addr)
            total = self.a + value + self.flag(C_FLAG)
            result = total & 0xFF
            self.set_flag(C_FLAG, total > 0xFF)
            self.set_flag(V_FLAG, bool((~(self.a ^ value) & (self.a ^ result) & 0x80)))
            self.a = result
            self.set_zn(self.a)
        elif mnemonic == "SBC":
            value = self.value_at(addr) ^ 0xFF
            total = self.a + value + self.flag(C_FLAG)
            result = total & 0xFF
            self.set_flag(C_FLAG, total > 0xFF)
            self.set_flag(V_FLAG, bool((~(self.a ^ value) & (self.a ^ result) & 0x80)))
            self.a = result
            self.set_zn(self.a)
        elif mnemonic == "CMP":
            self.compare(self.a, self.value_at(addr))
        elif mnemonic == "CPX":
            self.compare(self.x, self.value_at(addr))
        elif mnemonic == "CPY":
            self.compare(self.y, self.value_at(addr))

        elif mnemonic == "INC":
            if addr is not None:
                value = (self.read(addr) + 1) & 0xFF
                self.write(addr, value)
                self.set_zn(value)
        elif mnemonic == "INX":
            self.x = (self.x + 1) & 0xFF
            self.set_zn(self.x)
        elif mnemonic == "INY":
            self.y = (self.y + 1) & 0xFF
            self.set_zn(self.y)
        elif mnemonic == "DEC":
            if addr is not None:
                value = (self.read(addr) - 1) & 0xFF
                self.write(addr, value)
                self.set_zn(value)
        elif mnemonic == "DEX":
            self.x = (self.x - 1) & 0xFF
            self.set_zn(self.x)
        elif mnemonic == "DEY":
            self.y = (self.y - 1) & 0xFF
            self.set_zn(self.y)

        elif mnemonic in ("ASL", "LSR", "ROL", "ROR"):
            if mode == "acc":
                value = self.a
            else:
                value = self.value_at(addr)

            if mnemonic == "ASL":
                self.set_flag(C_FLAG, bool(value & 0x80))
                value = (value << 1) & 0xFF
            elif mnemonic == "LSR":
                self.set_flag(C_FLAG, bool(value & 0x01))
                value = (value >> 1) & 0xFF
            elif mnemonic == "ROL":
                old_carry = self.flag(C_FLAG)
                self.set_flag(C_FLAG, bool(value & 0x80))
                value = ((value << 1) | old_carry) & 0xFF
            elif mnemonic == "ROR":
                old_carry = self.flag(C_FLAG)
                self.set_flag(C_FLAG, bool(value & 0x01))
                value = ((old_carry << 7) | (value >> 1)) & 0xFF

            if mode == "acc":
                self.a = value
            elif addr is not None:
                self.write(addr, value)
            self.set_zn(value)

        elif mnemonic == "AND":
            self.a &= self.value_at(addr)
            self.a &= 0xFF
            self.set_zn(self.a)
        elif mnemonic == "ORA":
            self.a |= self.value_at(addr)
            self.a &= 0xFF
            self.set_zn(self.a)
        elif mnemonic == "EOR":
            self.a ^= self.value_at(addr)
            self.a &= 0xFF
            self.set_zn(self.a)
        elif mnemonic == "BIT":
            value = self.value_at(addr)
            self.set_flag(Z_FLAG, (self.a & value) == 0)
            self.set_flag(V_FLAG, bool(value & 0x40))
            self.set_flag(N_FLAG, bool(value & 0x80))

        elif mnemonic == "JMP":
            if addr is not None:
                self.pc = addr
        elif mnemonic == "JSR":
            if addr is not None:
                return_addr = (self.pc - 1) & 0xFFFF
                self.push((return_addr >> 8) & 0xFF)
                self.push(return_addr & 0xFF)
                self.pc = addr
        elif mnemonic == "RTS":
            lo = self.pop()
            hi = self.pop()
            self.pc = ((hi << 8) | lo) + 1
            self.pc &= 0xFFFF
        elif mnemonic == "BRK":
            self.pc = (self.pc + 1) & 0xFFFF
            self.push((self.pc >> 8) & 0xFF)
            self.push(self.pc & 0xFF)
            self.push(self.status | B_FLAG | U_FLAG)
            self.set_flag(I_FLAG, True)
            vector = self.read_word(0xFFFE)
            self.pc = vector if vector not in (0x0000, 0xFFFF) else self.pc
        elif mnemonic == "RTI":
            self.status = (self.pop() | U_FLAG) & ~B_FLAG
            lo = self.pop()
            hi = self.pop()
            self.pc = (hi << 8) | lo

        elif mnemonic in BRANCHES:
            if mnemonic == "BPL":
                self.branch(not self.flag(N_FLAG), addr)
            elif mnemonic == "BMI":
                self.branch(bool(self.flag(N_FLAG)), addr)
            elif mnemonic == "BVC":
                self.branch(not self.flag(V_FLAG), addr)
            elif mnemonic == "BVS":
                self.branch(bool(self.flag(V_FLAG)), addr)
            elif mnemonic == "BCC":
                self.branch(not self.flag(C_FLAG), addr)
            elif mnemonic == "BCS":
                self.branch(bool(self.flag(C_FLAG)), addr)
            elif mnemonic == "BNE":
                self.branch(not self.flag(Z_FLAG), addr)
            elif mnemonic == "BEQ":
                self.branch(bool(self.flag(Z_FLAG)), addr)

        elif mnemonic == "CLC":
            self.set_flag(C_FLAG, False)
        elif mnemonic == "SEC":
            self.set_flag(C_FLAG, True)
        elif mnemonic == "CLI":
            self.set_flag(I_FLAG, False)
        elif mnemonic == "SEI":
            self.set_flag(I_FLAG, True)
        elif mnemonic == "CLV":
            self.set_flag(V_FLAG, False)
        elif mnemonic == "CLD":
            self.set_flag(D_FLAG, False)
        elif mnemonic == "SED":
            self.set_flag(D_FLAG, True)
        elif mnemonic == "NOP":
            pass
        else:
            raise UnsupportedOpcode(f"Unimplemented mnemonic {mnemonic}")

    def status_string(self) -> str:
        flags = [
            ("N", N_FLAG), ("V", V_FLAG), ("U", U_FLAG), ("B", B_FLAG),
            ("D", D_FLAG), ("I", I_FLAG), ("Z", Z_FLAG), ("C", C_FLAG),
        ]
        return "".join(letter if (self.status & flag) else "." for letter, flag in flags)


# ---------------------------------------------------------------------------
# Emulator wrapper
# ---------------------------------------------------------------------------


class NESEmulator:
    def __init__(self) -> None:
        self.bus = CPUBus()
        self.cpu = CPU6502(self.bus)
        self.rom: Optional[NESROM] = None
        self.powered = False

    def load_rom(self, path: str) -> NESROM:
        rom = NESROM.load(path)
        self.rom = rom
        self.bus.load_rom(rom)
        self.cpu.reset()
        self.powered = True
        return rom

    def power(self) -> None:
        if not self.rom:
            raise RuntimeError("Load a .nes ROM before powering the emulator.")
        self.bus.load_rom(self.rom)
        self.cpu.reset()
        self.powered = True

    def reset(self) -> None:
        if not self.rom:
            raise RuntimeError("Load a .nes ROM before reset.")
        self.cpu.reset()
        self.powered = True

    def step(self, count: int = 1) -> int:
        if not self.powered:
            raise RuntimeError("Emulator is not powered. Load/power a ROM first.")
        cycles = 0
        for _ in range(max(1, count)):
            cycles += self.cpu.step()
        return cycles


# ---------------------------------------------------------------------------
# Tkinter interface
# ---------------------------------------------------------------------------


tk = tkinter


class ACNESApp:
    def __init__(self, root: tkinter.Tk) -> None:
        self.root = root
        self.emu = NESEmulator()
        self.running = False
        self.screen_image: Optional[tkinter.PhotoImage] = None
        self.frame_counter = 0
        self.last_run_time = time.time()

        self.root.title(APP_TITLE)
        self.root.configure(bg=BLACK)
        self.root.minsize(860, 620)

        self.build_ui()
        self.draw_splash()
        self.log(f"{APP_TITLE} READY")
        self.log("Load a legally owned .nes ROM to inspect it and step CPU code.")

    def widget_kwargs(self) -> Dict[str, object]:
        return {
            "bg": BLACK,
            "fg": BLUE_TEXT,
            "insertbackground": BLUE_TEXT,
            "highlightbackground": BLUE_EDGE,
            "highlightcolor": BLUE_BRIGHT,
        }

    def make_button(self, parent: tkinter.Misc, text: str, command) -> tkinter.Button:
        button = tk.Button(
            parent,
            text=text,
            command=command,
            bg=BLUE_BUTTON,
            fg=BLUE_TEXT,
            activebackground=BLUE_BUTTON_HOVER,
            activeforeground=BLUE_BRIGHT,
            relief=tk.FLAT,
            bd=0,
            padx=12,
            pady=8,
            font=("Courier New", 10, "bold"),
            cursor="hand2",
        )
        button.bind("<Enter>", lambda _event: button.configure(bg=BLUE_BUTTON_HOVER))
        button.bind("<Leave>", lambda _event: button.configure(bg=BLUE_BUTTON))
        return button

    def build_ui(self) -> None:
        title = tk.Label(
            self.root,
            text=APP_TITLE,
            bg=BLACK,
            fg=BLUE_BRIGHT,
            font=("Courier New", 20, "bold"),
            pady=10,
        )
        title.pack(fill=tk.X)

        main = tk.Frame(self.root, bg=BLACK)
        main.pack(fill=tk.BOTH, expand=True, padx=12, pady=8)

        left = tk.Frame(main, bg=BLACK)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.canvas = tk.Canvas(
            left,
            width=512,
            height=480,
            bg=BLACK,
            highlightthickness=2,
            highlightbackground=BLUE_EDGE,
        )
        self.canvas.pack(fill=tk.BOTH, expand=True)

        status_frame = tk.Frame(left, bg=BLACK)
        status_frame.pack(fill=tk.X, pady=(8, 0))

        self.cpu_status = tk.Label(
            status_frame,
            text="CPU: --",
            bg=BLACK,
            fg=BLUE_TEXT,
            anchor="w",
            font=("Courier New", 10),
        )
        self.cpu_status.pack(fill=tk.X)

        right = tk.Frame(main, bg=BLACK, width=300)
        right.pack(side=tk.RIGHT, fill=tk.Y, padx=(12, 0))
        right.pack_propagate(False)

        controls = tk.Frame(right, bg=BLACK)
        controls.pack(fill=tk.X)

        self.load_button = self.make_button(controls, "LOAD ROM", self.load_rom)
        self.load_button.pack(fill=tk.X, pady=4)

        self.power_button = self.make_button(controls, "POWER", self.power_rom)
        self.power_button.pack(fill=tk.X, pady=4)

        self.reset_button = self.make_button(controls, "RESET", self.reset_rom)
        self.reset_button.pack(fill=tk.X, pady=4)

        self.step_button = self.make_button(controls, "STEP CPU", self.step_cpu)
        self.step_button.pack(fill=tk.X, pady=4)

        self.run_button = self.make_button(controls, "RUN", self.toggle_run)
        self.run_button.pack(fill=tk.X, pady=4)

        self.about_button = self.make_button(controls, "ABOUT", self.show_about)
        self.about_button.pack(fill=tk.X, pady=4)

        rom_label = tk.Label(
            right,
            text="ROM / TRACE",
            bg=BLACK,
            fg=BLUE_BRIGHT,
            font=("Courier New", 11, "bold"),
            anchor="w",
            pady=8,
        )
        rom_label.pack(fill=tk.X)

        self.log_box = tk.Text(
            right,
            height=25,
            bg=BLACK,
            fg=BLUE_TEXT,
            insertbackground=BLUE_TEXT,
            selectbackground=BLUE_DIM,
            selectforeground=BLUE_BRIGHT,
            relief=tk.FLAT,
            bd=0,
            highlightthickness=1,
            highlightbackground=BLUE_EDGE,
            wrap=tk.WORD,
            font=("Courier New", 9),
        )
        self.log_box.pack(fill=tk.BOTH, expand=True)

        footer = tk.Label(
            right,
            text="BLACK BG // BLUE UI // NROM CORE",
            bg=BLACK,
            fg=BLUE_DIM,
            font=("Courier New", 8),
            pady=8,
        )
        footer.pack(fill=tk.X)

        self.root.bind("<F5>", lambda _event: self.toggle_run())
        self.root.bind("<F10>", lambda _event: self.step_cpu())
        self.root.bind("<Control-o>", lambda _event: self.load_rom())

    def draw_splash(self) -> None:
        self.canvas.delete("all")
        width = max(512, self.canvas.winfo_width())
        height = max(480, self.canvas.winfo_height())
        self.canvas.create_rectangle(10, 10, width - 10, height - 10, outline=BLUE_EDGE, width=2)
        self.canvas.create_text(
            width // 2,
            height // 2 - 48,
            text="AC'S NES EMU",
            fill=BLUE_BRIGHT,
            font=("Courier New", 28, "bold"),
        )
        self.canvas.create_text(
            width // 2,
            height // 2,
            text="0.1.1A",
            fill=BLUE_TEXT,
            font=("Courier New", 18, "bold"),
        )
        self.canvas.create_text(
            width // 2,
            height // 2 + 48,
            text="LOAD ROM  //  STEP CPU  //  BLUE HUE",
            fill=BLUE_TEXT,
            font=("Courier New", 12),
        )
        self.update_status()

    def log(self, text: str) -> None:
        self.log_box.insert(tk.END, text + "\n")
        self.log_box.see(tk.END)

    def load_rom(self) -> None:
        path = filedialog.askopenfilename(
            title="Load NES ROM",
            filetypes=[("NES ROM files", "*.nes"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            rom = self.emu.load_rom(path)
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            self.log(f"LOAD ERROR: {exc}")
            return

        self.running = False
        self.run_button.configure(text="RUN")
        self.log("-" * 34)
        self.log(rom.summary())
        if rom.mapper != 0:
            self.log("NOTE: CPU ROM mapping is NROM-focused; this mapper may need MMC support.")
        self.render_chr_preview(rom.chr_rom, rom.chr_banks == 0)
        self.update_status()

    def power_rom(self) -> None:
        try:
            self.emu.power()
        except Exception as exc:
            messagebox.showinfo(APP_TITLE, str(exc))
            return
        self.running = False
        self.run_button.configure(text="RUN")
        self.log("POWER CYCLE")
        self.log(self.emu.cpu.last_trace)
        self.update_status()

    def reset_rom(self) -> None:
        try:
            self.emu.reset()
        except Exception as exc:
            messagebox.showinfo(APP_TITLE, str(exc))
            return
        self.running = False
        self.run_button.configure(text="RUN")
        self.log("RESET")
        self.log(self.emu.cpu.last_trace)
        self.update_status()

    def step_cpu(self) -> None:
        try:
            self.emu.step(1)
        except UnsupportedOpcode as exc:
            self.running = False
            self.run_button.configure(text="RUN")
            self.log(f"HALT: {exc}")
            self.update_status()
            return
        except Exception as exc:
            messagebox.showinfo(APP_TITLE, str(exc))
            return
        self.log(self.emu.cpu.last_trace)
        self.update_status()

    def toggle_run(self) -> None:
        if not self.emu.rom:
            messagebox.showinfo(APP_TITLE, "Load a .nes ROM first.")
            return
        self.running = not self.running
        self.run_button.configure(text="PAUSE" if self.running else "RUN")
        if self.running:
            self.log("RUN START")
            self.last_run_time = time.time()
            self.run_loop()
        else:
            self.log("RUN PAUSE")
            self.update_status()

    def run_loop(self) -> None:
        if not self.running:
            return
        try:
            # Chunked execution keeps Tkinter responsive while still moving quickly.
            for _ in range(800):
                self.emu.step(1)
        except UnsupportedOpcode as exc:
            self.running = False
            self.run_button.configure(text="RUN")
            self.log(f"HALT: {exc}")
            self.update_status()
            return
        except Exception as exc:
            self.running = False
            self.run_button.configure(text="RUN")
            self.log(f"RUN ERROR: {exc}")
            self.update_status()
            return

        self.frame_counter += 1
        if self.frame_counter % 6 == 0:
            self.update_status()
        self.root.after(1, self.run_loop)

    def update_status(self) -> None:
        cpu = self.emu.cpu
        rom_name = self.emu.rom.name if self.emu.rom else "NO ROM"
        text = (
            f"{rom_name} | "
            f"PC={cpu.pc:04X} A={cpu.a:02X} X={cpu.x:02X} Y={cpu.y:02X} "
            f"SP={cpu.sp:02X} P={cpu.status:02X}[{cpu.status_string()}] "
            f"CYC={cpu.cycles}"
        )
        self.cpu_status.configure(text=text)

    def render_chr_preview(self, chr_data: bytes, chr_ram: bool = False) -> None:
        self.canvas.delete("all")
        if chr_ram or not any(chr_data):
            self.canvas.create_rectangle(10, 10, 502, 470, outline=BLUE_EDGE, width=2)
            self.canvas.create_text(
                256,
                210,
                text="CHR-RAM / BLANK PATTERN TABLE",
                fill=BLUE_TEXT,
                font=("Courier New", 16, "bold"),
            )
            self.canvas.create_text(
                256,
                250,
                text="CPU stepping is available. Full PPU render is next-core work.",
                fill=BLUE_TEXT,
                font=("Courier New", 10),
            )
            return

        tiles = min(len(chr_data) // 16, 256)
        tiles_per_row = 16
        rows = 16
        img_w = tiles_per_row * 8
        img_h = rows * 8
        palette = [BLACK, BLUE_DIM, BLUE_EDGE, BLUE_BRIGHT]

        image = tk.PhotoImage(width=img_w, height=img_h)
        for tile_index in range(tiles):
            tile_x = (tile_index % tiles_per_row) * 8
            tile_y = (tile_index // tiles_per_row) * 8
            base = tile_index * 16
            for row in range(8):
                p0 = chr_data[base + row]
                p1 = chr_data[base + row + 8]
                pixels = []
                for col in range(8):
                    bit = 7 - col
                    color_index = ((p0 >> bit) & 1) | (((p1 >> bit) & 1) << 1)
                    pixels.append(palette[color_index])
                image.put("{" + " ".join(pixels) + "}", to=(tile_x, tile_y + row))

        # Scale the preview into the faux NES screen area.
        self.screen_image = image.zoom(3, 3)
        self.canvas.create_rectangle(10, 10, 502, 470, outline=BLUE_EDGE, width=2)
        self.canvas.create_text(
            256,
            36,
            text="CHR-ROM PATTERN TABLE PREVIEW",
            fill=BLUE_BRIGHT,
            font=("Courier New", 14, "bold"),
        )
        self.canvas.create_image(256, 250, image=self.screen_image)
        self.canvas.create_text(
            256,
            450,
            text="BLUE HUE DEBUG VIEW — NOT FULL PPU VIDEO YET",
            fill=BLUE_TEXT,
            font=("Courier New", 10),
        )

    def show_about(self) -> None:
        messagebox.showinfo(
            APP_TITLE,
            "AC'S NES EMU 0.1.1A\n\n"
            "Black background, blue text, blue-hue buttons.\n"
            "Includes iNES parsing, NROM CPU bus mapping, a 6502 CPU core, "
            "and a CHR pattern preview.\n\n"
            "Use legally owned ROMs only.",
        )


def main() -> int:
    root = tk.Tk()
    app = ACNESApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
