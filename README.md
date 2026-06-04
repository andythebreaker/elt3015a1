# FPGA Pin GND Test — `pin_gui.py`

A Tkinter GUI for driving Nexys3 (Spartan-6) I/O pins HIGH one batch at a time,
so you can probe which physical connector pins are shorted to ground.

---

## Overview

The tool generates a minimal `clk_probe` Verilog module (`index.v`) whose
`pin_out` bus is driven by a fixed bitmask.  Each checkbox in the GUI controls
one bit: checked = HIGH, unchecked = LOW.  After setting the desired pattern you
click **Apply** to regenerate the source files, then **Compile** or
**Compile + Burn** to build and program the FPGA.

---

## Requirements

| Requirement | Notes |
|---|---|
| Python 3.8+ | Standard library only — no extra packages |
| Xilinx ISE 14.7 | Installed at `/opt/Xilinx/14.7/ISE_DS/` |
| Digilent Nexys3 (Spartan-6 XC6SLX16) | Or any board with the same UCF pin names |

---

## Running

```bash
cd assume_board_is_nexys3
python3 pin_gui.py
```

---

## GUI walkthrough

### Toolbar (top)

| Button | What it does |
|---|---|
| **Init UCF (Nexys3)** | Overwrites `blink.ucf` with all 194 default I/O pins.  Use this whenever the UCF has been repurposed for another design and no longer contains `pin_out[N]` entries. |
| **Apply (.v + .ucf)** | Regenerates `index.v` (Verilog bitmask) and `blink.ucf` (pin constraints) from the current checkbox state. |
| **Compile** | Runs Apply then invokes Xilinx ISE (`xtclsh`) to synthesise, implement, and generate the bitstream. |
| **Burn** | Runs Apply then programs the FPGA via `impact`. |
| **Compile + Burn** | Combines both steps in one click. |

The status label on the left shows how many `pin_out` pins were found in the
current `blink.ucf`.  It turns **red** when the UCF has no `pin_out` entries
(e.g. after switching to a different design) — click **Init UCF** to fix it.

---

### Pin Control tab

The scrollable grid lists every pin in the UCF as a checkbox.

**Bulk-select bar (above the grid)**

| Control | Effect |
|---|---|
| **All** | Check every pin |
| **None** | Uncheck every pin |
| **Invert** | Toggle every pin |

**Row controls (left side of each row)**

Each row has two small buttons:
- **✓** — check all pins in that row
- **✗** — uncheck all pins in that row

**Known pins** appear in blue with their label appended, e.g.
`[100] N15  (uart_tx_can)`.  They behave like any other checkbox but the colour
serves as a reminder that the pin identity is already known.

---

### Known Pins tab

Use this tab to record pins whose function is already established so you do not
accidentally test them.

| Field / Control | Description |
|---|---|
| **LOC** entry | The FPGA pin name as it appears in the UCF (e.g. `N15`) |
| **Label** entry | A free-form description (e.g. `uart_tx_can`) |
| **Add** (or press Enter in Label) | Saves the entry to `known_pins.json` |
| **Remove Selected** | Deletes highlighted rows from the list |
| **Refresh Pin Grid** | Reloads `known_pins.json` and redraws the Pin Control tab |

Known-pin data is persisted to `known_pins.json` in the project directory.

---

## Typical workflow

1. **First run / broken UCF**: Click **Init UCF (Nexys3)**.  The GUI loads
   194 I/O pins from the embedded Nexys3 default list and regenerates
   `blink.ucf`.

2. **Mark already-identified pins**: Switch to the **Known Pins** tab and add
   entries for every pin you have already traced (e.g. power, UART, CAN).
   Click **Refresh Pin Grid** — those pins turn blue in the grid.

3. **Select pins to test**: Use **None** to clear everything, then tick the
   rows or individual pins you want to drive HIGH.  The row **✓** button lets
   you select a whole group at once.

4. **Deploy**: Click **Compile + Burn** (or **Compile** then **Burn**
   separately).  Progress and errors stream into the log panel at the bottom.

5. **Iterate**: Observe which physical pins go HIGH with a multimeter or
   oscilloscope, add the newly identified ones to Known Pins, then repeat.

---

## Generated files

| File | Description |
|---|---|
| `index.v` | Verilog module — rewritten by **Apply** |
| `blink.ucf` | Pin constraints — rewritten by **Apply** / **Init UCF** |
| `known_pins.json` | Persistent known-pin database (LOC → label) |
| `burn.cmd` | iMPACT batch script — regenerated before each burn |

---

## Manual compile / burn (no GUI)

```bash
source /opt/Xilinx/14.7/ISE_DS/settings64.sh

# Compile
xtclsh <<'EOF'
project open assume_board_is_nexys3.xise
process run "Synthesize - XST"
process run "Implement Design"
process run "Generate Programming File"
project close
exit
EOF

# Burn
impact -batch burn.cmd
```