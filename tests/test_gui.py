"""GUI logic tests.

These exercise list management and CSV round-tripping without starting the
event loop or touching the network. Skipped automatically where Tk cannot open
a display (headless CI).
"""

from __future__ import annotations

import csv

import pytest

tk = pytest.importorskip("tkinter")

from tcgopt.gui import App, Row  # noqa: E402


@pytest.fixture
def app():
    try:
        root = tk.Tk()
    except tk.TclError as exc:  # pragma: no cover - headless environments
        pytest.skip(f"no display available: {exc}")
    root.withdraw()
    instance = App(root)
    # Don't let the background name-resolver fire during tests.
    instance._resolve_name_worker = lambda row: None
    yield instance
    root.destroy()


def add(app, url="722583", need=1, have=0, condition="NM", exact=False):
    app.url_var.set(url)
    app.need_var.set(str(need))
    app.have_var.set(str(have))
    app.cond_var.set(condition)
    app.exact_var.set(exact)
    app.add_card()


def test_add_card_populates_list_and_tree(app):
    add(app, "https://www.tcgplayer.com/product/722583/pepita", need=3, have=1)
    assert len(app.rows) == 1
    assert len(app.tree.get_children()) == 1
    row = app.rows[0]
    assert row.product_id == 722583
    assert row.to_buy == 2


def test_add_card_clears_inputs_for_the_next_entry(app):
    add(app, "722583", need=4, have=2)
    assert app.url_var.get() == ""
    assert app.need_var.get() == "1"
    assert app.have_var.get() == "0"


def test_condition_defaults_to_nm_or_better(app):
    add(app)
    assert app.rows[0].condition == "NM"
    assert app.rows[0].condition_text == "NM or better"


def test_exact_mode_is_labelled_without_or_better(app):
    add(app, condition="LP", exact=True)
    assert app.rows[0].condition_text == "LP"
    assert app.rows[0].to_requirement(1).condition_mode == "exact"


def test_same_card_twice_at_different_conditions(app):
    add(app, "722583", need=2, condition="NM")
    add(app, "722583", need=1, condition="MP")
    assert len(app.rows) == 2
    reqs = [r.to_requirement(i + 1) for i, r in enumerate(app.rows)]
    assert [r.condition.label for r in reqs] == ["NM", "MP"]
    assert all(r.product_id == 722583 for r in reqs)


def test_remove_selected_row(app):
    add(app, "722500")
    add(app, "722520")
    app.tree.selection_set(app.rows[0].iid)
    app.remove_selected()
    assert len(app.rows) == 1
    assert len(app.tree.get_children()) == 1
    assert app.rows[0].product_id == 722520


def test_remove_with_nothing_selected_is_harmless(app):
    add(app, "722500")
    app.remove_selected()
    assert len(app.rows) == 1
    assert "Select a row" in app.status_var.get()


def test_bad_url_is_rejected(app, monkeypatch):
    errors = []
    monkeypatch.setattr("tcgopt.gui.messagebox.showerror", lambda *a, **k: errors.append(a))
    add(app, "https://example.com/not-a-card")
    assert app.rows == []
    assert errors


def test_have_greater_than_need_is_rejected(app, monkeypatch):
    warnings = []
    monkeypatch.setattr("tcgopt.gui.messagebox.showwarning", lambda *a, **k: warnings.append(a))
    add(app, "722583", need=2, have=3)
    assert app.rows == []
    assert warnings


def test_non_numeric_quantity_is_rejected(app, monkeypatch):
    errors = []
    monkeypatch.setattr("tcgopt.gui.messagebox.showerror", lambda *a, **k: errors.append(a))
    app.url_var.set("722583")
    app.need_var.set("lots")
    app.add_card()
    assert app.rows == []
    assert errors


def test_save_and_load_round_trip(app, tmp_path, monkeypatch):
    add(app, "https://www.tcgplayer.com/product/722583/pepita", need=3, have=1)
    add(app, "722500", need=4, condition="MP", exact=True)
    target = tmp_path / "wants.csv"

    monkeypatch.setattr("tcgopt.gui.filedialog.asksaveasfilename", lambda **k: str(target))
    app.save_list()
    assert target.exists()

    with open(target) as handle:
        saved = list(csv.DictReader(handle))
    assert [r["condition_mode"] for r in saved] == ["min", "exact"]
    assert [r["needed"] for r in saved] == ["3", "4"]
    assert [r["has"] for r in saved] == ["1", "0"]

    monkeypatch.setattr("tcgopt.gui.filedialog.askopenfilename", lambda **k: str(target))
    app.load_list()
    assert [(r.product_id, r.need, r.have, r.condition, r.exact) for r in app.rows] == [
        (722583, 3, 1, "NM", False),
        (722500, 4, 0, "MP", True),
    ]


def test_load_replaces_rather_than_appends(app, tmp_path, monkeypatch):
    add(app, "722540", need=2)
    target = tmp_path / "w.csv"
    target.write_text("url,needed,condition\n722583,1,NM\n")
    monkeypatch.setattr("tcgopt.gui.filedialog.askopenfilename", lambda **k: str(target))
    app.load_list()
    assert len(app.rows) == 1
    assert app.rows[0].product_id == 722583
    assert len(app.tree.get_children()) == 1


def test_load_reports_a_bad_file(app, tmp_path, monkeypatch):
    bad = tmp_path / "bad.csv"
    bad.write_text("nothing,useful\n1,2\n")
    errors = []
    monkeypatch.setattr("tcgopt.gui.filedialog.askopenfilename", lambda **k: str(bad))
    monkeypatch.setattr("tcgopt.gui.messagebox.showerror", lambda *a, **k: errors.append(a))
    app.load_list()
    assert errors


def test_clear_list(app, monkeypatch):
    add(app, "722500")
    add(app, "722520")
    monkeypatch.setattr("tcgopt.gui.messagebox.askyesno", lambda *a, **k: True)
    app.clear_list()
    assert app.rows == []
    assert app.tree.get_children() == ()


def test_run_plan_rejects_bad_max_orders(app, monkeypatch):
    add(app, "722583")
    errors = []
    monkeypatch.setattr("tcgopt.gui.messagebox.showerror", lambda *a, **k: errors.append(a))
    app.max_sellers_var.set("zero")
    app.run_plan()
    assert errors
    assert not app.busy  # must not latch the button off


def test_run_plan_with_empty_list_does_nothing(app, monkeypatch):
    infos = []
    monkeypatch.setattr("tcgopt.gui.messagebox.showinfo", lambda *a, **k: infos.append(a))
    app.run_plan()
    assert infos
    assert not app.busy


def test_error_event_unlocks_the_ui(app):
    app.busy = True
    app.run_button.configure(state="disabled")
    shown = []
    import tcgopt.gui as gui_module
    original = gui_module.messagebox.showerror
    gui_module.messagebox.showerror = lambda *a, **k: shown.append(a)
    try:
        app.events.put(("error", "boom"))
        app._poll_events()
    finally:
        gui_module.messagebox.showerror = original
    assert not app.busy
    assert str(app.run_button["state"]) == "normal"
    assert shown


def test_row_to_requirement_carries_everything():
    row = Row(url="u", product_id=5, need=4, have=1, condition="LP", exact=False,
              name="Card", printing="Holofoil", language="English")
    req = row.to_requirement(7)
    assert (req.row, req.product_id, req.quantity) == (7, 5, 3)
    assert req.condition_mode == "min"
    assert req.printing == "Holofoil"
    assert req.needed == 4 and req.has == 1
