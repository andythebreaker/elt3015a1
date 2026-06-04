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

ROOT_DIR        = Path(__file__).resolve().parent
INDEX_V         = ROOT_DIR / "index.v"
UCF             = ROOT_DIR / "blink.ucf"
KNOWN_PINS_JSON = ROOT_DIR / "known_pins.json"
XISE            = ROOT_DIR / "assume_board_is_nexys3.xise"
BURN_CMD        = ROOT_DIR / "burn.cmd"

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


# ── Data helpers ─────────────────────────────────────────────────────────────

def parse_ucf():
    """Return (clk_line, led0_line, led1_line, pin_list).

    pin_list is empty when the UCF is missing or has no pin_out entries
    (e.g. after the UCF was repurposed for another design). The caller
    should offer UCF initialisation instead of crashing.
    """
    if not UCF.exists():
        return None, None, None, []

    lines_ = UCF.read_text().splitlines()
    clk_line  = next((l for l in lines_ if l.startswith('NET "clk_in"')),  None)
    led0_line = next((l for l in lines_ if l.startswith('NET "led<0>"')), None)
    led1_line = next((l for l in lines_ if l.startswith('NET "led<1>"')), None)

    pin_re = re.compile(r'NET "pin_out\[(\d+)\]"\s+LOC\s*=\s*"([^"]+)"')
    pins = {}
    for line in lines_:
        m = pin_re.search(line)
        if m:
            pins[int(m.group(1))] = m.group(2)

    pin_list = [pins[i] for i in sorted(pins)]
    return clk_line, led0_line, led1_line, pin_list


def infer_pin_states(pin_count):
    if not INDEX_V.exists():
        return [False] * pin_count

    text = INDEX_V.read_text()
    m = re.search(r"PIN_MASK\s*=\s*(\d+)'b([01_]+)", text)
    if not m:
        m = re.search(r"assign\s+pin_out\s*=\s*(\d+)'b([01_]+)", text)
    if m:
        bits = m.group(2).replace("_", "")
        if len(bits) == pin_count:
            return [bits[-1 - i] == "1" for i in range(pin_count)]

    if "{PIN_COUNT{1'b1}}" in text:
        return [True] * pin_count
    return [False] * pin_count


def infer_led1_state() -> bool:
    """Read the current led[1] value from index.v."""
    if not INDEX_V.exists():
        return False
    m = re.search(r"assign\s+led\[1\]\s*=\s*1'b([01])", INDEX_V.read_text())
    return m.group(1) == "1" if m else False


def patch_led1(led1: bool):
    """Patch only the 'assign led[1]' line in index.v without regenerating the file."""
    if not INDEX_V.exists():
        return
    bit = "1" if led1 else "0"
    text = INDEX_V.read_text()
    new_text = re.sub(
        r"assign\s+led\[1\]\s*=\s*1'b[01];",
        f"assign led[1] = 1'b{bit};",
        text,
    )
    INDEX_V.write_text(new_text)


def write_verilog(pin_states, led1: bool = False):
    pin_count = len(pin_states)
    bits = "".join("1" if pin_states[i] else "0" for i in range(pin_count - 1, -1, -1))
    led1_bit = "1" if led1 else "0"
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


def write_ucf(clk_line, led0_line, led1_line, pin_list):
    out = []
    out.append(clk_line  or DEFAULT_CLK)
    out.append("")
    out.append(led0_line or DEFAULT_LED0)
    out.append(led1_line or DEFAULT_LED1)
    out.append("")
    for idx, pin in enumerate(pin_list):
        out.append(f'NET "pin_out[{idx}]" LOC = "{pin}" | IOSTANDARD = LVCMOS33;')
    UCF.write_text("\n".join(out) + "\n")


def init_ucf_defaults():
    """Overwrite blink.ucf with the full Nexys3 default pin list."""
    write_ucf(DEFAULT_CLK, DEFAULT_LED0, DEFAULT_LED1, NEXYS3_DEFAULT_PINS)


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


# Source files the .xise project references (relative names)
_XISE_SOURCES = [
    "index.v",
    "uart_tx.v",
    "can_simple_controller.v",
    "obd_can_node.v",
    "blink.ucf",
    "assume_board_is_nexys3.xise",
    "assume_board_is_nexys3.gise",
    # clk_probe implementation support files
    "clk_probe.prj",
    "clk_probe.xst",
    "clk_probe.lso",
    "clk_probe.ut",
]


def make_run_dir() -> Path:
    """Snapshot the current source files into every_time_burn/YYYYMMDDHHMMSS/
    and return that directory.  The actual compile/burn runs from there so
    manual edits to index.v / blink.ucf are never overwritten."""
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    run_dir = ROOT_DIR / "every_time_burn" / ts
    run_dir.mkdir(parents=True, exist_ok=True)
    for name in _XISE_SOURCES:
        src = ROOT_DIR / name
        if src.exists():
            shutil.copy2(src, run_dir / name)
    write_burn_cmd(run_dir / "burn.cmd")
    return run_dir


def load_known_pins():
    """Load known-pin map from known_pins.json.  Returns {LOC: label}."""
    if KNOWN_PINS_JSON.exists():
        try:
            data = json.loads(KNOWN_PINS_JSON.read_text())
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def save_known_pins(data: dict):
    KNOWN_PINS_JSON.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def run_bash(cmd, log_fn):
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


# ── Main GUI ─────────────────────────────────────────────────────────────────

class PinGui(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("FPGA Pin GND Test GUI")
        self.geometry("1150x820")

        self.clk_line  = None
        self.led0_line = None
        self.led1_line = None
        self.pin_list  = []
        self.pin_count = 0
        self.vars      = []
        self.led1_var  = tk.BooleanVar(value=infer_led1_state())
        self.known_pins = load_known_pins()

        self._load_ucf()
        self._build_ui()

    def _load_ucf(self):
        clk, led0, led1, pins = parse_ucf()
        self.clk_line  = clk
        self.led0_line = led0
        self.led1_line = led1
        self.pin_list  = pins
        self.pin_count = len(pins)
        states = infer_pin_states(self.pin_count) if self.pin_count else []
        self.vars = [tk.BooleanVar(value=s) for s in states]

    def _build_ui(self):
        top = ttk.Frame(self)
        top.pack(fill="x", padx=8, pady=6)

        if self.pin_count:
            status_txt = f"UCF loaded — {self.pin_count} pins"
            status_fg  = "green"
        else:
            status_txt = 'UCF has no pin_out entries — click "Init UCF (Nexys3)" to initialise'
            status_fg  = "red"

        self._status_lbl = ttk.Label(top, text=status_txt, foreground=status_fg)
        self._status_lbl.pack(side="left", padx=4)

        self._led1_btn = ttk.Checkbutton(
            top, text="LED1 HIGH",
            variable=self.led1_var,
            command=self._on_led1_toggle,
        )
        self._led1_btn.pack(side="left", padx=10)

        btn_bar = ttk.Frame(top)
        btn_bar.pack(side="right")
        ttk.Button(btn_bar, text="Init UCF (Nexys3)", command=self._do_init_ucf).pack(side="left", padx=3)
        ttk.Button(btn_bar, text="Apply (.v + .ucf)", command=self.apply_files).pack(side="left", padx=3)
        ttk.Button(btn_bar, text="Compile",           command=self.compile_only).pack(side="left", padx=3)
        ttk.Button(btn_bar, text="Burn",              command=self.burn_only).pack(side="left", padx=3)
        ttk.Button(btn_bar, text="Compile + Burn",    command=self.compile_and_burn).pack(side="left", padx=3)

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=4)

        self._pin_tab   = ttk.Frame(nb)
        self._known_tab = ttk.Frame(nb)
        nb.add(self._pin_tab,   text="Pin Control")
        nb.add(self._known_tab, text="Known Pins")

        self._build_pin_tab()
        self._build_known_tab()

        self.log = ScrolledText(self, height=10, state="disabled")
        self.log.pack(fill="both", expand=False, padx=8, pady=6)

    def _build_pin_tab(self):
        sel_bar = ttk.Frame(self._pin_tab)
        sel_bar.pack(fill="x", padx=6, pady=4)

        ttk.Label(sel_bar, text="Bulk select:").pack(side="left")
        ttk.Button(sel_bar, text="All",    command=self._sel_all).pack(side="left", padx=2)
        ttk.Button(sel_bar, text="None",   command=self._desel_all).pack(side="left", padx=2)
        ttk.Button(sel_bar, text="Invert", command=self._invert).pack(side="left", padx=2)

        ttk.Separator(sel_bar, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Label(
            sel_bar,
            text="Blue = known pin (already identified).  Row ✓/✗ buttons select / clear one row.",
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

        if not self.pin_list:
            ttk.Label(
                self._grid_frame,
                text='No pins loaded. Click "Init UCF (Nexys3)" in the toolbar to get started.',
                foreground="gray",
            ).grid(row=0, column=0, padx=20, pady=20)
            return

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

        for i, (pin, var) in enumerate(zip(self.pin_list, self.vars)):
            row = i // COLS
            col = i % COLS
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
                self._grid_frame,
                text=lbl,
                variable=var,
                fg="royalblue" if is_known else "black",
                selectcolor="white",
                anchor="w",
                padx=2,
            )
            cb.grid(row=grid_row, column=col + 3, sticky="w", padx=4, pady=1)

    def _build_known_tab(self):
        ttk.Label(
            self._known_tab,
            text=(
                "Record pins whose identity is already established so they appear "
                "highlighted in blue in the Pin Control tab."
            ),
        ).pack(anchor="w", padx=8, pady=(6, 2))

        tree_frame = ttk.Frame(self._known_tab)
        tree_frame.pack(fill="both", expand=True, padx=8, pady=4)

        self._tree = ttk.Treeview(
            tree_frame,
            columns=("loc", "label"),
            show="headings",
            height=14,
            selectmode="extended",
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
        ttk.Button(add_bar, text="Refresh Pin Grid", command=self._refresh_grid).pack(side="left", padx=4)

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
        save_known_pins(self.known_pins)
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
        save_known_pins(self.known_pins)
        self._populate_tree()

    def _refresh_grid(self):
        self.known_pins = load_known_pins()
        self._build_checkboxes()
        self._append_log("Pin grid refreshed with current known-pins list.")

    def _sel_all(self):
        for v in self.vars:
            v.set(True)

    def _desel_all(self):
        for v in self.vars:
            v.set(False)

    def _invert(self):
        for v in self.vars:
            v.set(not v.get())

    def _sel_row(self, row: int):
        start = row * COLS
        end   = min(start + COLS, self.pin_count)
        for i in range(start, end):
            self.vars[i].set(True)

    def _desel_row(self, row: int):
        start = row * COLS
        end   = min(start + COLS, self.pin_count)
        for i in range(start, end):
            self.vars[i].set(False)

    def _do_init_ucf(self):
        n = len(NEXYS3_DEFAULT_PINS)
        if not messagebox.askyesno(
            "Init UCF",
            f"Overwrite blink.ucf with the Nexys3 default pin list ({n} I/O pins)?\n\n"
            "The current blink.ucf will be replaced.",
        ):
            return
        init_ucf_defaults()
        self._load_ucf()
        self._build_checkboxes()
        self._status_lbl.config(
            text=f"UCF loaded — {self.pin_count} pins",
            foreground="green",
        )
        self._append_log(f"blink.ucf initialised with {self.pin_count} Nexys3 default pins.")

    def apply_files(self):
        if not self.pin_list:
            messagebox.showwarning("No pins", "No pins loaded. Initialise the UCF first.")
            return
        pin_states = [v.get() for v in self.vars]
        write_verilog(pin_states, led1=self.led1_var.get())
        write_ucf(self.clk_line, self.led0_line, self.led1_line, self.pin_list)
        self._append_log("Wrote index.v and blink.ucf.")

    def _on_led1_toggle(self):
        val = self.led1_var.get()
        patch_led1(val)
        self._append_log(f"LED1 set to {'HIGH (1)' if val else 'LOW (0)'} in index.v.")

    def _run_threaded(self, cmd: str):
        def worker():
            run_bash(cmd, lambda out: self.after(0, self._append_log, out))
        threading.Thread(target=worker, daemon=True).start()

    def compile_only(self):
        run_dir = make_run_dir()
        self._append_log(f"Snapshot → {run_dir}")
        cmd = (
            f"cd '{run_dir}'\n"
            "source /opt/Xilinx/14.7/ISE_DS/settings64.sh\n"
            "mkdir -p xst/projnav.tmp\n"
            "xst -intstyle ise -ifn clk_probe.xst -ofn clk_probe.syr\n"
            "ngdbuild -intstyle ise -dd _ngo -nt timestamp "
            "-uc blink.ucf -p xc6slx16-csg324-2 clk_probe.ngc clk_probe.ngd\n"
            "map -intstyle ise -p xc6slx16-csg324-2 -w -logic_opt off "
            "-ol high -t 1 -o clk_probe_map.ncd clk_probe.ngd clk_probe.pcf\n"
            "par -w -intstyle ise -ol high clk_probe_map.ncd clk_probe.ncd clk_probe.pcf\n"
            "bitgen -intstyle ise -f clk_probe.ut clk_probe.ncd clk_probe.bit clk_probe.pcf\n"
        )
        self._append_log("Starting compile...")
        self._run_threaded(cmd)

    def burn_only(self):
        # Burn from the most recently compiled run_dir (or ROOT_DIR as fallback)
        burn_root = ROOT_DIR / "every_time_burn"
        run_dirs = sorted(burn_root.glob("*/clk_probe.bit"), reverse=True) if burn_root.exists() else []
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
        run_dir = make_run_dir()
        self._append_log(f"Snapshot → {run_dir}")
        cmd = (
            f"cd '{run_dir}'\n"
            "source /opt/Xilinx/14.7/ISE_DS/settings64.sh\n"
            "mkdir -p xst/projnav.tmp\n"
            "xst -intstyle ise -ifn clk_probe.xst -ofn clk_probe.syr\n"
            "ngdbuild -intstyle ise -dd _ngo -nt timestamp "
            "-uc blink.ucf -p xc6slx16-csg324-2 clk_probe.ngc clk_probe.ngd\n"
            "map -intstyle ise -p xc6slx16-csg324-2 -w -logic_opt off "
            "-ol high -t 1 -o clk_probe_map.ncd clk_probe.ngd clk_probe.pcf\n"
            "par -w -intstyle ise -ol high clk_probe_map.ncd clk_probe.ncd clk_probe.pcf\n"
            "bitgen -intstyle ise -f clk_probe.ut clk_probe.ncd clk_probe.bit clk_probe.pcf\n"
            "impact -batch burn.cmd\n"
        )
        self._append_log("Starting compile + burn...")
        self._run_threaded(cmd)

    def _append_log(self, msg: str):
        self.log.configure(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")


if __name__ == "__main__":
    app = PinGui()
    app.mainloop()
