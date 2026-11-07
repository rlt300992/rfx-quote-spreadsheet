import csv
from pathlib import Path

import pytest

from core.config import load_config
from core.convert import convert, theoretical_weight

SEED = Path(__file__).resolve().parent.parent / "data" / "seed"
LINE = {"theoretical_weight_kg": 0.192, "weight_calc_trace": "Sheet = ..."}


def test_per_piece_and_per_box_are_direct():
    for unit in ("per_piece", "per_box"):
        v, method, _ = convert({"price": 12.5, "currency": "INR", "unit": unit}, LINE)
        assert v == 12.5 and method == "direct"


def test_per_100_is_divided_out():
    v, method, trace = convert({"price": 887, "currency": "INR", "unit": "per_100"}, LINE)
    assert v == pytest.approx(8.87) and method == "per_n_pieces"
    assert any("/ 100" in t for t in trace)


def test_per_1000_is_divided_out():
    v, _, _ = convert({"price": 8870, "currency": "INR", "unit": "per_1000"}, LINE)
    assert v == pytest.approx(8.87)


def test_per_kg_uses_theoretical_weight_and_shows_formula():
    # V5 line 1: 38 per kg x 0.192 kg = 7.296 per piece
    v, method, trace = convert({"price": 38, "currency": "INR", "unit": "per_kg"}, LINE)
    assert v == pytest.approx(7.296) and method == "per_kg_formula"
    assert any("0.192 kg" in t for t in trace) and any("Weight formula" in t for t in trace)


def test_per_kg_hand_check_against_seed_csv():
    row = next(r for r in csv.DictReader(open(SEED / "line_items.csv", encoding="utf-8")) if r["line_no"] == "13")
    v, _, _ = convert({"price": 42, "currency": "INR", "unit": "per_kg"}, {"theoretical_weight_kg": float(row["theoretical_weight_kg"])})
    assert v == pytest.approx(42 * 0.856)


def test_usd_converted_at_fixed_dated_rate():
    cfg = load_config()
    assert cfg["fx"]["USD_INR"] == 95.17 and cfg["fx"]["date"] == "2026-10-26"
    v, _, trace = convert({"price": 0.0866, "currency": "USD", "unit": "per_piece"}, LINE)
    assert v == pytest.approx(0.0866 * 95.17)
    assert any("95.17" in t for t in trace)


def test_usd_per_100_and_per_kg_compose():
    v, _, _ = convert({"price": 5.0, "currency": "USD", "unit": "per_100"}, LINE)
    assert v == pytest.approx(5.0 * 95.17 / 100)
    v, _, _ = convert({"price": 0.5, "currency": "USD", "unit": "per_kg"}, LINE)
    assert v == pytest.approx(0.5 * 95.17 * 0.192)


@pytest.mark.parametrize("raw", [
    {"price": None, "currency": "INR", "unit": "per_piece"},
    {"price": 1, "currency": "EUR", "unit": "per_piece"},
    {"price": 1, "currency": "INR", "unit": "as_last_year"},
    {"price": 1, "currency": "INR", "unit": "per_dozen"},
])
def test_bad_inputs_raise(raw):
    with pytest.raises(ValueError):
        convert(raw, LINE)


def test_unknown_category_not_implemented():
    with pytest.raises(NotImplementedError):
        convert({"price": 1, "currency": "INR", "unit": "per_piece"}, LINE, category="labels")


def test_weight_formula_reproduces_all_30_seed_weights():
    cfg = load_config()
    rows = list(csv.DictReader(open(SEED / "line_items.csv", encoding="utf-8")))
    assert len(rows) == 30
    for r in rows:
        size_class, w, trace = theoretical_weight(r, cfg)
        assert w == pytest.approx(float(r["theoretical_weight_kg"]), abs=0.0005), r["line_no"]
        assert size_class == r["size_class"]
        assert trace == r["weight_calc_trace"]
