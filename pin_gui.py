import json
import re
import shutil
import threading
import subprocess
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox
from tkinter.scrolledtext import ScrolledText

ROOT_DIR       = Path(__file__).resolve().parent
INDEX_V        = ROOT_DIR / "index.v"
UCF            = ROOT_DIR / "blink.ucf"
GUI_STATE_JSON = ROOT_DIR / "gui_state.json"
BURN_CMD       = ROOT_DIR / "burn.cmd"

COLS = 8

# Default Nexys3 I/O pin list (194 pins, recovered from git history commit 4b4eef0)
NEXYS3_DEFAULT_PINS = [
    "C4",  "B2",  "A2",  "D6",  "C6",  "B3",  "A3",  "B4",  "A4",  "C5",
    "A5",  "F7",  "E6",  "B6",  "A6",  "E7",  "E8",  "C7",  "A7",  "D8",
    "C8",  "G8",  "F8",  "B8",  "A8",  "D9",  "C9",  "B9",  "A9",  "C10",
    "A10", "G9",  "F9",  "B11", "A11", "G11", "F10", "B12", "A12", "F11",
    "E11", "D12", "C12", "C13", "A13", "F12", "E12", "B14", "A14", "F13",
    "E13", "C15", "A15", "D14", "C14", "B16", "A16", "F16", "C17", "C18",
    "F14", "G14", "D17", "D18", "H12", "G13", "E16", "E18", "K12", "K13",
    "F17", "F18", "H13", "H14", "H15", "H16", "G16", "G18", "J13", "K14",
    "L12", "L13", "K15", "K16", "L15", "L16", "H17", "H18", "J16", "J18",
    "K17", "K18", "L17", "L18", "M16", "M18", "N17", "N18", "P17", "P18",
    "N15", "N16", "T17", "T18", "U17", "U18", "M14", "N14", "L14", "M13",
    "P15", "P16", "T15", "U16", "V16", "R13", "T13", "U15", "V15", "T14",
    "V14", "N12", "P12", "U13", "V13", "M11", "N11", "R11", "T11", "T12",
    "V12", "N10", "P11", "M10", "N9",  "U11", "V11", "R10", "T10", "V10",
    "R8",  "T8",  "T9",  "V9",  "M8",  "N8",  "U8",  "V8",  "U7",  "V7",
    "N7",  "P8",  "T6",  "V6",  "R7",  "T7",  "N6",  "P7",  "R5",  "T5",
    "U5",  "V5",  "R3",  "T3",  "T4",  "V4",  "N5",  "P6",  "U3",  "V3",
    "N3",  "P4",  "P3",  "L6",  "M5",  "U2",  "U1",  "T2",  "T1",  "P2",
    "P1",  "N2",  "N1",  "M3",  "M1",  "L2",  "L1",  "K2",  "K1",  "L4",
    "L3",  "J3",  "J1",  "H2",
]

DEFAULT_CLK  = 'NET "clk_in" LOC = "U10" | IOSTANDARD = LVCMOS33;'
DEFAULT_LED0 = 'NET "led<0>" LOC = "D11" | IOSTANDARD = LVCMOS33 | DRIVE = 2 | SLEW = SLOW;'
DEFAULT_LED1 = 'NET "led<1>" LOC = "C11" | IOSTANDARD = LVCMOS33 | DRIVE = 2 | SLEW = SLOW;'


# ── GUI state persistence ─────────────────────────────────────────────────────

def _default_state() -> dict:
    return {
        "pin_list":     list(NEXYS3_DEFAULT_PINS),
        "checked_pins": [],
        "pin_levels":   {},
        "known_pins":   {},
        "led1":         False,
    }


def load_gui_state() -> dict:
    """Load GUI state from gui_state.json, falling back to defaults.
    Migrates old known_pins.json if present."""
    if GUI_STATE_JSON.exists():
        try:
            raw = json.loads(GUI_STATE_JSON.read_text())
            if isinstance(raw, dict):
                state = _default_state()
                for key in state:
                    if key in raw:
                        state[key] = raw[key]
                return state
        except Exception:
            pass
    state = _default_state()
    old_kp = ROOT_DIR / "known_pins.json"
    if old_kp.exists():
        try:
            state["known_pins"] = json.loads(old_kp.read_text())
        except Exception:
            pass
    return state


def save_gui_state(state: dict):
    GUI_STATE_JSON.write_text(json.dumps(state, indent=2, ensure_ascii=False))


# ── ISE output generators ─────────────────────────────────────────────────────

def write_verilog(checked_pins: list, pin_levels: dict, led1: bool = False):
    """Write index.v.
    checked_pins: list of pin LOCs to include in pin_out bus (preserves order).
    pin_levels: {LOC: True=HIGH / False=LOW}; unlisted pins default to HIGH.
    """
    pin_count = len(checked_pins)
    led1_bit  = "1" if led1 else "0"
    if pin_count == 0:
        text = (
            "module clk_probe (\n"
            "    input  wire clk_in,\n"
            "    output wire [1:0] led\n"
            ");\n\n"
            "reg [31:0] cnt = 32'd0;\n\n"
            "always @(posedge clk_in) begin\n"
            "    cnt <= cnt + 1'b1;\n"
            "end\n\n"
            "assign led[0] = cnt[27];\n"
            f"assign led[1] = 1'b{led1_bit};\n\n"
            "endmodule\n"
        )
    else:
        # MSB first: index pin_count-1 down to 0
        bits = "".join(
            "1" if pin_levels.get(pin, True) else "0"
            for pin in reversed(checked_pins)
        )
        text = (
            "module clk_probe #(\n"
            f"    parameter integer PIN_COUNT = {pin_count}\n"
            ")\n"
            "(\n"
            "    input  wire clk_in,\n"
            "    output wire [PIN_COUNT-1:0] pin_out,\n"
            "    output wire [1:0] led\n"
            ");\n\n"
            "reg [31:0] cnt = 32'd0;\n\n"
            "always @(posedge clk_in) begin\n"
            "    cnt <= cnt + 1'b1;\n"
            "end\n\n"
            f"localparam [PIN_COUNT-1:0] PIN_MASK = {pin_count}'b{bits};\n\n"
            "assign pin_out = PIN_MASK;\n"
            "assign led[0] = cnt[27];\n"
            f"assign led[1] = 1'b{led1_bit};\n\n"
            "endmodule\n"
        )
    INDEX_V.write_text(text)


def write_ucf(checked_pins: list):
    """Write blink.ucf containing ONLY the checked pins."""
    lines = [DEFAULT_CLK, "", DEFAULT_LED0, DEFAULT_LED1, ""]
    for idx, pin in enumerate(checked_pins):
        lines.append(f'NET "pin_out[{idx}]" LOC = "{pin}" | IOSTANDARD = LVCMOS33;')
    UCF.write_text("\n".join(lines) + "\n")


def write_burn_cmd(path: Path = None):
    target = path or BURN_CMD
    target.write_text(
        "setMode -bs\n"
        "setCable -p auto\n"
        "Identify\n"
        "assignFile -p 1 -file clk_probe.bit\n"
        "Program -p 1\n"
        "quit\n"
    )


def run_bash(cmd: str, log_fn):
    proc = subprocess.run(
        ["/bin/bash", "-lc", cmd],
        cwd=str(ROOT_DIR),
        capture_output=True,
        text=True,
    )
    output = proc.stdout + proc.stderr
    if proc.returncode != 0:
        output += f"\n[exit {proc.returncode}]\n"
    log_fn(output)


_XISE_SOURCES = [
    "index.v",
    "uart_tx.v",
    "can_simple_controller.v",
    "obd_can_node.v",
    "blink.ucf",
    "assume_board_is_nexys3.xise",
    "assume_board_is_nexys3.gise",
    "clk_probe.prj",
    "clk_probe.xst",
    "clk_probe.lso",
    "clk_probe.ut",
]


def make_run_dir() -> Path:
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    run_dir = ROOT_DIR / "every_time_burn" / ts
    run_dir.mkdir(parents=True, exist_ok=True)
    for name in _XISE_SOURCES:
        src = ROOT_DIR / name
        if src.exists():
            shutil.copy2(src, run_dir / name)
    write_burn_cmd(run_dir / "burn.cmd")
    return run_dir


_COMPILE_STEPS = (
    "mkdir -p xst/projnav.tmp\n"
    "xst -intstyle ise -ifn clk_probe.xst -ofn clk_probe.syr\n"
    "ngdbuild -intstyle ise -dd _ngo -nt timestamp "
    "-uc blink.ucf -p xc6slx16-csg324-2 clk_probe.ngc clk_probe.ngd\n"
    "map -intstyle ise -p xc6slx16-csg324-2 -w -logic_opt off "
    "-ol high -t 1 -o clk_probe_map.ncd clk_probe.ngd clk_probe.pcf\n"
    "par -w -intstyle ise -ol high clk_probe_map.ncd clk_probe.ncd clk_probe.pcf\n"
    "bitgen -intstyle ise -f clk_probe.ut clk_probe.ncd clk_probe.bit clk_probe.pcf\n"
)


# ── Main GUI ─────────────────────────────────────────────────────────────────

class PinGui(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("FPGA Pin GND Test GUI")
        self.geometry("1200x860")

        state = load_gui_state()
        self.pin_list   = state["pin_list"]
        self.known_pins = dict(state["known_pins"])
        self.led1_var   = tk.BooleanVar(value=bool(state.get("led1", False)))

        checked_set = set(state["checked_pins"])
        pin_levels  = state.get("pin_levels", {})

        self.vars: dict = {
            pin: tk.BooleanVar(value=(pin in checked_set))
            for pin in self.pin_list
        }
        self.level_vars: dict = {
            pin: tk.BooleanVar(value=bool(pin_levels.get(pin, True)))
            for pin in self.pin_list
        }

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── state helpers ─────────────────────────────────────────────────────────

    def _get_checked_pins(self) -> list:
        return [p for p in self.pin_list if self.vars[p].get()]

    def _get_state(self) -> dict:
        return {
            "pin_list":     self.pin_list,
            "checked_pins": self._get_checked_pins(),
            "pin_levels":   {pin: self.level_vars[pin].get() for pin in self.pin_list},
            "known_pins":   self.known_pins,
            "led1":         self.led1_var.get(),
        }

    def _save_state(self):
        save_gui_state(self._get_state())

    def _on_close(self):
        self._save_state()
        self.destroy()

    def _status_text(self) -> str:
        n = len(self._get_checked_pins())
        return f"{n} pin(s) selected"

    def _update_status(self):
        self._status_lbl.config(text=self._status_text())

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        top = ttk.Frame(self)
        top.pack(fill="x", padx=8, pady=6)

        self._status_lbl = ttk.Label(top, text=self._status_text())
        self._status_lbl.pack(side="left", padx=4)

        ttk.Checkbutton(
            top, text="LED1 HIGH",
            variable=self.led1_var,
            command=self._on_led1_toggle,
        ).pack(side="left", padx=10)

        btn_bar = ttk.Frame(top)
        btn_bar.pack(side="right")
        ttk.Button(btn_bar, text="Reset pin list (Nexys3)", command=self._do_reset_pins).pack(side="left", padx=3)
        ttk.Button(btn_bar, text="Apply (.v + .ucf)",       command=self.apply_files).pack(side="left", padx=3)
        ttk.Button(btn_bar, text="Compile",                 command=self.compile_only).pack(side="left", padx=3)
        ttk.Button(btn_bar, text="Burn",                    command=self.burn_only).pack(side="left", padx=3)
        ttk.Button(btn_bar, text="Compile + Burn",          command=self.compile_and_burn).pack(side="left", padx=3)

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=4)

        self._pin_tab      = ttk.Frame(nb)
        self._selected_tab = ttk.Frame(nb)
        self._known_tab    = ttk.Frame(nb)
        nb.add(self._pin_tab,      text="Pin Control")
        nb.add(self._selected_tab, text="Selected Pins")
        nb.add(self._known_tab,    text="Known Pins")

        self._build_pin_tab()
        self._build_selected_tab()
        self._build_known_tab()

        self.log = ScrolledText(self, height=10, state="disabled")
        self.log.pack(fill="both", expand=False, padx=8, pady=6)

    # ── Tab 1: Pin Control ────────────────────────────────────────────────────

    def _build_pin_tab(self):
        sel_bar = ttk.Frame(self._pin_tab)
        sel_bar.pack(fill="x", padx=6, pady=4)

        ttk.Label(sel_bar, text="Bulk:").pack(side="left")
        ttk.Button(sel_bar, text="All",    command=self._sel_all).pack(side="left", padx=2)
        ttk.Button(sel_bar, text="None",   command=self._desel_all).pack(side="left", padx=2)
        ttk.Button(sel_bar, text="Invert", command=self._invert).pack(side="left", padx=2)
        ttk.Separator(sel_bar, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Label(
            sel_bar,
            text="Checked = pin included in design.  Blue = known pin.",
            foreground="gray",
        ).pack(side="left")

        container = ttk.Frame(self._pin_tab)
        container.pack(fill="both", expand=True)

        self._canvas = tk.Canvas(container, highlightthickness=0)
        vsb = ttk.Scrollbar(container, orient="vertical",   command=self._canvas.yview)
        hsb = ttk.Scrollbar(container, orient="horizontal", command=self._canvas.xview)
        self._grid_frame = ttk.Frame(self._canvas)
        self._grid_frame.bind(
            "<Configure>",
            lambda e: self._canvas.configure(scrollregion=self._canvas.bbox("all")),
        )
        self._canvas.create_window((0, 0), window=self._grid_frame, anchor="nw")
        self._canvas.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self._canvas.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        container.rowconfigure(0, weight=1)
        container.columnconfigure(0, weight=1)
        self._canvas.bind(
            "<Enter>",
            lambda e: self._canvas.bind_all(
                "<MouseWheel>",
                lambda ev: self._canvas.yview_scroll(-1 * (ev.delta // 120), "units"),
            ),
        )
        self._canvas.bind("<Leave>", lambda e: self._canvas.unbind_all("<MouseWheel>"))
        self._build_checkboxes()

    def _build_checkboxes(self):
        for w in self._grid_frame.winfo_children():
            w.destroy()
        known_locs = set(self.known_pins.keys())
        for txt, col in [("Row", 0), ("✓", 1), ("✗", 2)]:
            ttk.Label(self._grid_frame, text=txt, foreground="gray").grid(
                row=0, column=col, padx=2, pady=2
            )
        for c in range(COLS):
            ttk.Label(self._grid_frame, text=f"col {c}", foreground="gray").grid(
                row=0, column=c + 3, padx=4, pady=2
            )
        ttk.Separator(self._grid_frame, orient="horizontal").grid(
            row=1, column=0, columnspan=COLS + 3, sticky="ew", pady=2
        )
        for i, pin in enumerate(self.pin_list):
            row      = i // COLS
            col      = i % COLS
            grid_row = row + 2
            if col == 0:
                ttk.Label(self._grid_frame, text=f"R{row:02d}", width=4, anchor="e").grid(
                    row=grid_row, column=0, padx=(4, 1), pady=1
                )
                ttk.Button(
                    self._grid_frame, text="✓", width=2,
                    command=lambda r=row: self._sel_row(r),
                ).grid(row=grid_row, column=1, padx=1, pady=1)
                ttk.Button(
                    self._grid_frame, text="✗", width=2,
                    command=lambda r=row: self._desel_row(r),
                ).grid(row=grid_row, column=2, padx=1, pady=1)
            is_known = pin in known_locs
            lbl = f"[{i}] {pin}"
            if is_known:
                lbl += f"  ({self.known_pins[pin]})"
            cb = tk.Checkbutton(
                self._grid_frame, text=lbl, variable=self.vars[pin],
                fg="royalblue" if is_known else "black",
                selectcolor="white", anchor="w", padx=2,
                command=self._on_checkbox_change,
            )
            cb.grid(row=grid_row, column=col + 3, sticky="w", padx=4, pady=1)

    def _on_checkbox_change(self):
        self._update_status()
        self._build_selected_grid()
        self._save_state()

    # ── Tab 2: Selected Pins (level control) ──────────────────────────────────

    def _build_selected_tab(self):
        sel_bar = ttk.Frame(self._selected_tab)
        sel_bar.pack(fill="x", padx=6, pady=4)
        ttk.Label(sel_bar, text="Bulk:").pack(side="left")
        ttk.Button(sel_bar, text="All HIGH", command=self._lvl_all_high).pack(side="left", padx=2)
        ttk.Button(sel_bar, text="All LOW",  command=self._lvl_all_low).pack(side="left", padx=2)
        ttk.Button(sel_bar, text="Invert",   command=self._lvl_invert).pack(side="left", padx=2)
        ttk.Separator(sel_bar, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Label(
            sel_bar,
            text="Checked = HIGH, Unchecked = LOW.  Only pins selected in Pin Control appear here.",
            foreground="gray",
        ).pack(side="left")

        container = ttk.Frame(self._selected_tab)
        container.pack(fill="both", expand=True)

        self._sel_canvas = tk.Canvas(container, highlightthickness=0)
        vsb2 = ttk.Scrollbar(container, orient="vertical",   command=self._sel_canvas.yview)
        hsb2 = ttk.Scrollbar(container, orient="horizontal", command=self._sel_canvas.xview)
        self._sel_grid = ttk.Frame(self._sel_canvas)
        self._sel_grid.bind(
            "<Configure>",
            lambda e: self._sel_canvas.configure(scrollregion=self._sel_canvas.bbox("all")),
        )
        self._sel_canvas.create_window((0, 0), window=self._sel_grid, anchor="nw")
        self._sel_canvas.configure(yscrollcommand=vsb2.set, xscrollcommand=hsb2.set)
        self._sel_canvas.grid(row=0, column=0, sticky="nsew")
        vsb2.grid(row=0, column=1, sticky="ns")
        hsb2.grid(row=1, column=0, sticky="ew")
        container.rowconfigure(0, weight=1)
        container.columnconfigure(0, weight=1)
        self._sel_canvas.bind(
            "<Enter>",
            lambda e: self._sel_canvas.bind_all(
                "<MouseWheel>",
                lambda ev: self._sel_canvas.yview_scroll(-1 * (ev.delta // 120), "units"),
            ),
        )
        self._sel_canvas.bind("<Leave>", lambda e: self._sel_canvas.unbind_all("<MouseWheel>"))
        self._build_selected_grid()

    def _build_selected_grid(self):
        for w in self._sel_grid.winfo_children():
            w.destroy()
        checked = self._get_checked_pins()
        if not checked:
            ttk.Label(
                self._sel_grid,
                text='No pins selected. Go to "Pin Control" tab and check some pins first.',
                foreground="gray",
            ).grid(row=0, column=0, padx=20, pady=20)
            return
        known_locs = set(self.known_pins.keys())
        for txt, col in [("Row", 0), ("✓", 1), ("✗", 2)]:
            ttk.Label(self._sel_grid, text=txt, foreground="gray").grid(
                row=0, column=col, padx=2, pady=2
            )
        for c in range(COLS):
            ttk.Label(self._sel_grid, text=f"col {c}", foreground="gray").grid(
                row=0, column=c + 3, padx=4, pady=2
            )
        ttk.Separator(self._sel_grid, orient="horizontal").grid(
            row=1, column=0, columnspan=COLS + 3, sticky="ew", pady=2
        )
        for i, pin in enumerate(checked):
            row      = i // COLS
            col      = i % COLS
            grid_row = row + 2
            if col == 0:
                ttk.Label(self._sel_grid, text=f"R{row:02d}", width=4, anchor="e").grid(
                    row=grid_row, column=0, padx=(4, 1), pady=1
                )
                ttk.Button(
                    self._sel_grid, text="✓", width=2,
                    command=lambda r=row: self._lvl_sel_row(r),
                ).grid(row=grid_row, column=1, padx=1, pady=1)
                ttk.Button(
                    self._sel_grid, text="✗", width=2,
                    command=lambda r=row: self._lvl_desel_row(r),
                ).grid(row=grid_row, column=2, padx=1, pady=1)
            is_known = pin in known_locs
            lbl = f"[{i}] {pin}"
            if is_known:
                lbl += f"  ({self.known_pins[pin]})"
            cb = tk.Checkbutton(
                self._sel_grid, text=lbl, variable=self.level_vars[pin],
                fg="royalblue" if is_known else "black",
                selectcolor="white", anchor="w", padx=2,
                command=self._on_level_change,
            )
            cb.grid(row=grid_row, column=col + 3, sticky="w", padx=4, pady=1)

    def _on_level_change(self):
        self._save_state()

    def _lvl_all_high(self):
        for pin in self._get_checked_pins():
            self.level_vars[pin].set(True)
        self._save_state()

    def _lvl_all_low(self):
        for pin in self._get_checked_pins():
            self.level_vars[pin].set(False)
        self._save_state()

    def _lvl_invert(self):
        for pin in self._get_checked_pins():
            self.level_vars[pin].set(not self.level_vars[pin].get())
        self._save_state()

    def _lvl_sel_row(self, row: int):
        checked = self._get_checked_pins()
        for pin in checked[row * COLS : (row + 1) * COLS]:
            self.level_vars[pin].set(True)
        self._save_state()

    def _lvl_desel_row(self, row: int):
        checked = self._get_checked_pins()
        for pin in checked[row * COLS : (row + 1) * COLS]:
            self.level_vars[pin].set(False)
        self._save_state()

    # ── Tab 3: Known Pins ─────────────────────────────────────────────────────

    def _build_known_tab(self):
        ttk.Label(
            self._known_tab,
            text="Record pins whose identity is already established — they appear highlighted in blue.",
        ).pack(anchor="w", padx=8, pady=(6, 2))
        tree_frame = ttk.Frame(self._known_tab)
        tree_frame.pack(fill="both", expand=True, padx=8, pady=4)
        self._tree = ttk.Treeview(
            tree_frame, columns=("loc", "label"), show="headings",
            height=14, selectmode="extended",
        )
        self._tree.heading("loc",   text="LOC (FPGA pin name)")
        self._tree.heading("label", text="Label / signal name")
        self._tree.column("loc",   width=130, anchor="w")
        self._tree.column("label", width=450, anchor="w")
        tsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=tsb.set)
        self._tree.pack(side="left", fill="both", expand=True)
        tsb.pack(side="right", fill="y")
        self._populate_tree()
        add_bar = ttk.Frame(self._known_tab)
        add_bar.pack(fill="x", padx=8, pady=4)
        ttk.Label(add_bar, text="LOC:").pack(side="left")
        self._new_loc = ttk.Entry(add_bar, width=10)
        self._new_loc.pack(side="left", padx=4)
        ttk.Label(add_bar, text="Label:").pack(side="left")
        self._new_label = ttk.Entry(add_bar, width=35)
        self._new_label.pack(side="left", padx=4)
        ttk.Button(add_bar, text="Add",             command=self._add_known).pack(side="left", padx=4)
        ttk.Button(add_bar, text="Remove Selected", command=self._remove_known).pack(side="left", padx=4)
        ttk.Separator(add_bar, orient="vertical").pack(side="left", fill="y", padx=10)
        ttk.Button(add_bar, text="Refresh Grids", command=self._refresh_grids).pack(side="left", padx=4)
        self._new_label.bind("<Return>", lambda e: self._add_known())

    def _populate_tree(self):
        self._tree.delete(*self._tree.get_children())
        for loc, lbl in sorted(self.known_pins.items()):
            self._tree.insert("", "end", iid=loc, values=(loc, lbl))

    def _add_known(self):
        loc   = self._new_loc.get().strip().upper()
        label = self._new_label.get().strip()
        if not loc:
            messagebox.showwarning("Input error", "LOC cannot be empty.")
            return
        self.known_pins[loc] = label
        self._save_state()
        self._populate_tree()
        self._new_loc.delete(0, "end")
        self._new_label.delete(0, "end")
        self._new_loc.focus_set()

    def _remove_known(self):
        sel = self._tree.selection()
        if not sel:
            return
        for item in sel:
            loc = self._tree.item(item, "values")[0]
            self.known_pins.pop(loc, None)
        self._save_state()
        self._populate_tree()

    def _refresh_grids(self):
        self._build_checkboxes()
        self._build_selected_grid()
        self._append_log("Grids refreshed.")

    # ── Pin Control bulk selection ────────────────────────────────────────────

    def _sel_all(self):
        for v in self.vars.values():
            v.set(True)
        self._update_status()
        self._build_selected_grid()
        self._save_state()

    def _desel_all(self):
        for v in self.vars.values():
            v.set(False)
        self._update_status()
        self._build_selected_grid()
        self._save_state()

    def _invert(self):
        for v in self.vars.values():
            v.set(not v.get())
        self._update_status()
        self._build_selected_grid()
        self._save_state()

    def _sel_row(self, row: int):
        for pin in self.pin_list[row * COLS : (row + 1) * COLS]:
            self.vars[pin].set(True)
        self._update_status()
        self._build_selected_grid()
        self._save_state()

    def _desel_row(self, row: int):
        for pin in self.pin_list[row * COLS : (row + 1) * COLS]:
            self.vars[pin].set(False)
        self._update_status()
        self._build_selected_grid()
        self._save_state()

    # ── Reset pin list ────────────────────────────────────────────────────────

    def _do_reset_pins(self):
        if not messagebox.askyesno(
            "Reset pin list",
            f"Reset to the Nexys3 default pin list ({len(NEXYS3_DEFAULT_PINS)} pins)?\n"
            "All checkbox states will be cleared.",
        ):
            return
        self.pin_list   = list(NEXYS3_DEFAULT_PINS)
        self.vars       = {pin: tk.BooleanVar(value=False) for pin in self.pin_list}
        self.level_vars = {pin: tk.BooleanVar(value=True)  for pin in self.pin_list}
        self._build_checkboxes()
        self._build_selected_grid()
        self._update_status()
        self._save_state()
        self._append_log(f"Pin list reset to {len(self.pin_list)} Nexys3 defaults.")

    # ── LED1 toggle ───────────────────────────────────────────────────────────

    def _on_led1_toggle(self):
        self._save_state()
        val = self.led1_var.get()
        self._append_log(f"LED1 → {'HIGH' if val else 'LOW'} (will apply on next Apply/Compile).")

    # ── File write ────────────────────────────────────────────────────────────

    def apply_files(self) -> bool:
        checked = self._get_checked_pins()
        if not checked:
            messagebox.showwarning("No pins selected", "Check at least one pin in Pin Control.")
            return False
        pin_levels = {pin: self.level_vars[pin].get() for pin in checked}
        write_verilog(checked, pin_levels, led1=self.led1_var.get())
        write_ucf(checked)
        self._save_state()
        high_n = sum(1 for v in pin_levels.values() if v)
        low_n  = len(checked) - high_n
        self._append_log(
            f"Wrote index.v ({len(checked)} pins: {high_n} HIGH, {low_n} LOW, "
            f"LED1={'HIGH' if self.led1_var.get() else 'LOW'}) and blink.ucf."
        )
        return True

    def _run_threaded(self, cmd: str):
        def worker():
            run_bash(cmd, lambda out: self.after(0, self._append_log, out))
        threading.Thread(target=worker, daemon=True).start()

    def compile_only(self):
        if not self.apply_files():
            return
        run_dir = make_run_dir()
        self._append_log(f"Snapshot → {run_dir}")
        cmd = (f"cd '{run_dir}'\n" "source /opt/Xilinx/14.7/ISE_DS/settings64.sh\n") + _COMPILE_STEPS
        self._append_log("Starting compile...")
        self._run_threaded(cmd)

    def burn_only(self):
        burn_root = ROOT_DIR / "every_time_burn"
        run_dirs  = sorted(burn_root.glob("*/clk_probe.bit"), reverse=True) if burn_root.exists() else []
        if run_dirs:
            burn_dir = run_dirs[0].parent
        elif (ROOT_DIR / "clk_probe.bit").exists():
            burn_dir = ROOT_DIR
        else:
            messagebox.showwarning("No bitfile", "No clk_probe.bit found. Compile first.")
            return
        self._append_log(f"Burning from {burn_dir}")
        cmd = (
            f"cd '{burn_dir}'\n"
            "source /opt/Xilinx/14.7/ISE_DS/settings64.sh\n"
            "impact -batch burn.cmd\n"
        )
        self._append_log("Starting burn...")
        self._run_threaded(cmd)

    def compile_and_burn(self):
        if not self.apply_files():
            return
        run_dir = make_run_dir()
        self._append_log(f"Snapshot → {run_dir}")
        cmd = (f"cd '{run_dir}'\n" "source /opt/Xilinx/14.7/ISE_DS/settings64.sh\n") + _COMPILE_STEPS + "impact -batch burn.cmd\n"
        self._append_log("Starting compile + burn...")
        self._run_threaded(cmd)

    # ── Log ───────────────────────────────────────────────────────────────────

    def _append_log(self, msg: str):
        self.log.configure(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")


if __name__ == "__main__":
    app = PinGui()
    app.mainloop()
