"""Tkinter desktop front end.

Paste a TCGPlayer link, pick quantity and condition, build a list, hit
"Find cheapest plan". Network and solver work happen on a background thread so
the window never freezes; results are marshalled back through a queue that the
Tk main loop polls.

Run with:  python -m tcgopt.gui
"""

from __future__ import annotations

import csv
import queue
import threading
import tkinter as tk
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .client import TCGPlayerClient, TCGPlayerError, parse_product_id
from .models import Condition, Requirement
from .optimizer import naive_plan, optimize
from .planner import gather_listings, resolve_name, usable_listings
from .report import render_plan
from .wants import WantsError, parse_wants

CONDITIONS = ["NM", "LP", "MP", "HP", "DMG"]
CONDITION_HELP = {
    "NM": "Near Mint", "LP": "Lightly Played", "MP": "Moderately Played",
    "HP": "Heavily Played", "DMG": "Damaged",
}


def secondary_colors(widget: tk.Misc) -> tuple[str, str]:
    """Muted and emphasised label colors readable on the current theme.

    macOS flips the theme background between light and dark with the system
    appearance, so a hardcoded grey is unreadable in one of the two. Pick from
    the resolved background's luminance instead. (``systemSecondaryLabelColor``
    is no help here: Tk drops its alpha and hands back plain white.)
    """
    background = ttk.Style().lookup("TLabel", "background") or "#ececec"
    try:
        red, green, blue = widget.winfo_rgb(background)
    except tk.TclError:
        return "#595959", "#444444"
    luminance = (0.299 * red + 0.587 * green + 0.114 * blue) / 65535
    if luminance < 0.5:
        return "#a0a0a0", "#c8c8c8"
    return "#595959", "#444444"


@dataclass
class Row:
    """One line of the wants list as held by the GUI."""

    url: str
    product_id: int
    need: int
    have: int = 0
    condition: str = "NM"
    exact: bool = False
    name: str = ""
    printing: str = ""
    language: str = "English"
    iid: str = field(default="", compare=False)

    @property
    def to_buy(self) -> int:
        return max(0, self.need - self.have)

    @property
    def condition_text(self) -> str:
        return self.condition if self.exact else f"{self.condition} or better"

    def to_requirement(self, row_number: int) -> Requirement:
        return Requirement(
            row=row_number,
            product_id=self.product_id,
            quantity=self.to_buy,
            condition=Condition.parse(self.condition),
            condition_mode="exact" if self.exact else "min",
            printing=self.printing or None,
            language=self.language or "English",
            name=self.name,
            url=self.url,
            needed=self.need,
            has=self.have,
        )


class App(ttk.Frame):
    def __init__(self, master: tk.Tk) -> None:
        super().__init__(master, padding=12)
        self.master = master
        self.rows: list[Row] = []
        self.events: queue.Queue = queue.Queue()
        self.busy = False
        self.muted, self.emphasis = secondary_colors(self)
        self.grid(row=0, column=0, sticky="nsew")
        master.columnconfigure(0, weight=1)
        master.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        self.rowconfigure(3, weight=2)

        self._build_add_panel()
        self._build_list_panel()
        self._build_action_panel()
        self._build_report_panel()
        self._poll_events()

    # -- layout ----------------------------------------------------------

    def _build_add_panel(self) -> None:
        box = ttk.LabelFrame(self, text="Add a card", padding=10)
        box.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        box.columnconfigure(1, weight=1)

        ttk.Label(box, text="TCGPlayer link:").grid(row=0, column=0, sticky="w")
        self.url_var = tk.StringVar()
        url_entry = ttk.Entry(box, textvariable=self.url_var)
        url_entry.grid(row=0, column=1, columnspan=5, sticky="ew", padx=(8, 0))
        url_entry.bind("<Return>", lambda _e: self.add_card())
        url_entry.focus_set()

        controls = ttk.Frame(box)
        controls.grid(row=1, column=0, columnspan=6, sticky="ew", pady=(10, 0))

        ttk.Label(controls, text="Need:").pack(side="left")
        self.need_var = tk.StringVar(value="1")
        ttk.Spinbox(controls, from_=1, to=999, width=5,
                    textvariable=self.need_var).pack(side="left", padx=(4, 14))

        ttk.Label(controls, text="Have:").pack(side="left")
        self.have_var = tk.StringVar(value="0")
        ttk.Spinbox(controls, from_=0, to=999, width=5,
                    textvariable=self.have_var).pack(side="left", padx=(4, 14))

        ttk.Label(controls, text="Condition:").pack(side="left")
        self.cond_var = tk.StringVar(value="NM")
        ttk.Combobox(controls, textvariable=self.cond_var, values=CONDITIONS,
                     state="readonly", width=6).pack(side="left", padx=(4, 6))

        self.exact_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(controls, text="exact grade only",
                        variable=self.exact_var).pack(side="left", padx=(0, 14))

        ttk.Button(controls, text="Add to list",
                   command=self.add_card).pack(side="right")

        ttk.Label(
            box,
            text="Condition means that grade or better unless you tick "
                 "'exact'. Need two grades of one card? Add it twice.",
            foreground=self.muted,
        ).grid(row=2, column=0, columnspan=6, sticky="w", pady=(8, 0))

    def _build_list_panel(self) -> None:
        box = ttk.LabelFrame(self, text="Your list", padding=10)
        box.grid(row=1, column=0, sticky="nsew")
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)

        columns = ("card", "need", "have", "buy", "condition")
        self.tree = ttk.Treeview(box, columns=columns, show="headings", height=7)
        for key, label, width, anchor in (
            ("card", "Card", 380, "w"),
            ("need", "Need", 60, "center"),
            ("have", "Have", 60, "center"),
            ("buy", "To buy", 70, "center"),
            ("condition", "Condition", 130, "w"),
        ):
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, anchor=anchor,
                             stretch=(key == "card"))
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.tree.bind("<Double-1>", self._open_selected_url)

        scroll = ttk.Scrollbar(box, orient="vertical", command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)

        buttons = ttk.Frame(box)
        buttons.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Button(buttons, text="Remove selected",
                   command=self.remove_selected).pack(side="left")
        ttk.Button(buttons, text="Clear list",
                   command=self.clear_list).pack(side="left", padx=6)
        ttk.Button(buttons, text="Save list...",
                   command=self.save_list).pack(side="right")
        ttk.Button(buttons, text="Load list...",
                   command=self.load_list).pack(side="right", padx=6)

    def _build_action_panel(self) -> None:
        box = ttk.Frame(self)
        box.grid(row=2, column=0, sticky="ew", pady=10)

        ttk.Label(box, text="Max orders:").pack(side="left")
        self.max_sellers_var = tk.StringVar(value="")
        ttk.Entry(box, textvariable=self.max_sellers_var,
                  width=5).pack(side="left", padx=(4, 4))
        ttk.Label(box, text="(blank = no limit)",
                  foreground=self.muted).pack(side="left", padx=(0, 14))

        self.allow_extra_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(box, text="buy spares if cheaper",
                        variable=self.allow_extra_var).pack(side="left")

        self.thorough_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(box, text="thorough search (slower)",
                        variable=self.thorough_var).pack(side="left", padx=12)

        self.run_button = ttk.Button(box, text="Find cheapest plan",
                                     command=self.run_plan)
        self.run_button.pack(side="right")

        self.status_var = tk.StringVar(value="Add some cards to get started.")
        ttk.Label(self, textvariable=self.status_var,
                  foreground=self.emphasis).grid(
            row=4, column=0, sticky="w", pady=(8, 0)
        )

    def _build_report_panel(self) -> None:
        box = ttk.LabelFrame(self, text="Report", padding=10)
        box.grid(row=3, column=0, sticky="nsew")
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)

        # Deliberately no background/foreground override: Tk's defaults are a
        # matched pair (systemTextBackgroundColor / systemTextColor on macOS)
        # that track the system appearance. Pinning just the background made
        # the report white-on-white in dark mode.
        self.report = tk.Text(box, wrap="none", height=18, font=("Menlo", 11))
        self.report.grid(row=0, column=0, sticky="nsew")
        self.report.configure(state="disabled")

        yscroll = ttk.Scrollbar(box, orient="vertical", command=self.report.yview)
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll = ttk.Scrollbar(box, orient="horizontal", command=self.report.xview)
        xscroll.grid(row=1, column=0, sticky="ew")
        self.report.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)

        actions = ttk.Frame(box)
        actions.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Button(actions, text="Copy report",
                   command=self.copy_report).pack(side="left")
        ttk.Button(actions, text="Save report...",
                   command=self.save_report).pack(side="left", padx=6)

    # -- list management --------------------------------------------------

    def add_card(self) -> None:
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("Missing link", "Paste a TCGPlayer product link.")
            return
        try:
            product_id = parse_product_id(url)
        except ValueError:
            messagebox.showerror(
                "Bad link",
                "That doesn't look like a TCGPlayer product link.\n\n"
                "Expected something like:\n"
                "https://www.tcgplayer.com/product/722583/...",
            )
            return

        try:
            need = int(self.need_var.get())
            have = int(self.have_var.get())
        except ValueError:
            messagebox.showerror("Bad quantity", "Need and Have must be whole numbers.")
            return
        if need < 1 or have < 0:
            messagebox.showerror("Bad quantity", "Need must be at least 1.")
            return
        if have >= need:
            messagebox.showwarning(
                "Nothing to buy",
                f"You already have {have} of the {need} you need.",
            )
            return

        row = Row(
            url=url,
            product_id=product_id,
            need=need,
            have=have,
            condition=self.cond_var.get(),
            exact=self.exact_var.get(),
            name=f"Loading name for product {product_id}...",
        )
        self.rows.append(row)
        row.iid = self.tree.insert("", "end", values=self._row_values(row))

        self.url_var.set("")
        self.need_var.set("1")
        self.have_var.set("0")
        self._refresh_status()

        # Resolve the real card name without blocking the UI.
        threading.Thread(
            target=self._resolve_name_worker, args=(row,), daemon=True
        ).start()

    def _resolve_name_worker(self, row: Row) -> None:
        try:
            client = TCGPlayerClient()
            name = resolve_name(client, row.product_id, "")
        except (TCGPlayerError, OSError):
            name = f"Product {row.product_id}"
        self.events.put(("name", row, name or f"Product {row.product_id}"))

    def _row_values(self, row: Row) -> tuple:
        return (row.name, row.need, row.have, row.to_buy, row.condition_text)

    def remove_selected(self) -> None:
        selected = self.tree.selection()
        if not selected:
            self.status_var.set("Select a row in the list first.")
            return
        for iid in selected:
            self.tree.delete(iid)
            self.rows = [r for r in self.rows if r.iid != iid]
        self._refresh_status()

    def clear_list(self) -> None:
        if self.rows and not messagebox.askyesno(
            "Clear list", "Remove every card from the list?"
        ):
            return
        for row in self.rows:
            self.tree.delete(row.iid)
        self.rows.clear()
        self._refresh_status()

    def _open_selected_url(self, _event=None) -> None:
        for iid in self.tree.selection():
            for row in self.rows:
                if row.iid == iid and row.url.startswith("http"):
                    webbrowser.open(row.url)

    def _refresh_status(self) -> None:
        if self.busy:
            return
        if not self.rows:
            self.status_var.set("Add some cards to get started.")
            return
        cards = sum(r.to_buy for r in self.rows)
        self.status_var.set(
            f"{len(self.rows)} row(s), {cards} card(s) to buy. "
            f"Click 'Find cheapest plan' when ready."
        )

    # -- save / load ------------------------------------------------------

    def save_list(self) -> None:
        if not self.rows:
            messagebox.showinfo("Nothing to save", "Your list is empty.")
            return
        path = filedialog.asksaveasfilename(
            title="Save wants list",
            defaultextension=".csv",
            initialfile="wants.csv",
            filetypes=[("CSV files", "*.csv")],
        )
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["url", "name", "needed", "has", "condition",
                             "condition_mode", "printing", "language"])
            for row in self.rows:
                writer.writerow([
                    row.url, row.name, row.need, row.have, row.condition,
                    "exact" if row.exact else "min", row.printing, row.language,
                ])
        self.status_var.set(f"Saved {len(self.rows)} row(s) to {Path(path).name}")

    def load_list(self) -> None:
        path = filedialog.askopenfilename(
            title="Load wants list", filetypes=[("CSV files", "*.csv"), ("All", "*")]
        )
        if not path:
            return
        try:
            requirements = parse_wants(path)
        except WantsError as exc:
            messagebox.showerror("Could not load list", str(exc))
            return

        for row in self.rows:
            self.tree.delete(row.iid)
        self.rows.clear()
        for requirement in requirements:
            row = Row(
                url=requirement.url,
                product_id=requirement.product_id,
                need=requirement.needed or requirement.quantity,
                have=requirement.has,
                condition=requirement.condition.label,
                exact=requirement.condition_mode == "exact",
                name=requirement.name or f"Product {requirement.product_id}",
                printing=requirement.printing or "",
                language=requirement.language,
            )
            self.rows.append(row)
            row.iid = self.tree.insert("", "end", values=self._row_values(row))
        self.status_var.set(f"Loaded {len(self.rows)} row(s) from {Path(path).name}")

    # -- solving ----------------------------------------------------------

    def run_plan(self) -> None:
        if self.busy:
            return
        buyable = [r for r in self.rows if r.to_buy > 0]
        if not buyable:
            messagebox.showinfo("Empty list", "Add at least one card first.")
            return

        max_sellers = None
        raw = self.max_sellers_var.get().strip()
        if raw:
            try:
                max_sellers = int(raw)
                if max_sellers < 1:
                    raise ValueError
            except ValueError:
                messagebox.showerror(
                    "Bad value", "Max orders must be a whole number of 1 or more."
                )
                return

        self.busy = True
        self.run_button.configure(state="disabled")
        self._set_report("Working...")
        self.status_var.set("Fetching listings...")

        threading.Thread(
            target=self._solve_worker,
            args=(
                [r.to_requirement(i + 1) for i, r in enumerate(buyable)],
                max_sellers,
                self.allow_extra_var.get(),
                None if self.thorough_var.get() else 60,
            ),
            daemon=True,
        ).start()

    def _solve_worker(self, requirements, max_sellers, allow_extra, per_card) -> None:
        try:
            client = TCGPlayerClient()

            def progress(step: int, total: int, label: str) -> None:
                self.events.put(
                    ("status", f"Fetching listings {step}/{total}: {label}")
                )

            listings = gather_listings(
                client, requirements, listings_per_card=per_card, progress=progress
            )
            usable = usable_listings(listings, requirements)
            self.events.put(
                ("status", f"Optimizing {len(usable)} listings from "
                           f"{len({l.seller_id for l in usable})} sellers...")
            )
            plan = optimize(
                requirements, usable, max_sellers=max_sellers,
                allow_extra=allow_extra, time_limit=60.0,
            )
            baseline = naive_plan(requirements, usable)
            self.events.put(("plan", plan, baseline, requirements))
        except TCGPlayerError as exc:
            self.events.put(("error", f"Could not reach TCGPlayer:\n\n{exc}"))
        except Exception as exc:  # keep the window alive on unexpected faults
            self.events.put(("error", f"{type(exc).__name__}: {exc}"))

    # -- event pump -------------------------------------------------------

    def _poll_events(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "name":
                    _, row, name = event
                    row.name = name
                    if self.tree.exists(row.iid):
                        self.tree.item(row.iid, values=self._row_values(row))
                elif kind == "status":
                    self.status_var.set(event[1])
                elif kind == "plan":
                    _, plan, baseline, requirements = event
                    self._show_plan(plan, baseline, requirements)
                elif kind == "error":
                    self._finish()
                    self._set_report("")
                    self.status_var.set("Failed.")
                    messagebox.showerror("Something went wrong", event[1])
        except queue.Empty:
            pass
        self.after(120, self._poll_events)

    def _show_plan(self, plan, baseline, requirements) -> None:
        self._finish()
        self._set_report(render_plan(plan, baseline))
        if plan.is_complete:
            saved = round(baseline.grand_total - plan.grand_total, 2)
            extra = f"  (saves ${saved:,.2f} vs cheapest-per-card)" if saved > 0 else ""
            self.status_var.set(
                f"Done: ${plan.grand_total:,.2f} across "
                f"{len(plan.orders)} order(s).{extra}"
            )
        else:
            missing = sum(n for _, n in plan.unfilled)
            self.status_var.set(
                f"Partial plan: ${plan.grand_total:,.2f}; "
                f"{missing} copy/copies could not be sourced."
            )
        # Refresh names the fetch may have filled in.
        for row in self.rows:
            for requirement in requirements:
                if requirement.url == row.url and requirement.name:
                    row.name = requirement.name
            if self.tree.exists(row.iid):
                self.tree.item(row.iid, values=self._row_values(row))

    def _finish(self) -> None:
        self.busy = False
        self.run_button.configure(state="normal")

    # -- report helpers ---------------------------------------------------

    def _set_report(self, text: str) -> None:
        self.report.configure(state="normal")
        self.report.delete("1.0", "end")
        self.report.insert("1.0", text)
        self.report.configure(state="disabled")

    def _report_text(self) -> str:
        return self.report.get("1.0", "end").strip()

    def copy_report(self) -> None:
        text = self._report_text()
        if not text:
            return
        self.master.clipboard_clear()
        self.master.clipboard_append(text)
        self.status_var.set("Report copied to clipboard.")

    def save_report(self) -> None:
        text = self._report_text()
        if not text:
            messagebox.showinfo("Nothing to save", "Run a plan first.")
            return
        path = filedialog.asksaveasfilename(
            title="Save report", defaultextension=".txt",
            initialfile="plan.txt", filetypes=[("Text files", "*.txt")],
        )
        if path:
            Path(path).write_text(text, encoding="utf-8")
            self.status_var.set(f"Report saved to {Path(path).name}")


def main() -> int:
    root = tk.Tk()
    root.title("TCGPlayer Cost Optimizer")
    root.geometry("940x860")
    root.minsize(820, 640)
    try:
        ttk.Style().theme_use("aqua")  # native on macOS
    except tk.TclError:
        pass
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
