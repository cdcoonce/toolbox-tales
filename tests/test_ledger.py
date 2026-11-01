"""Tests for the append-only spend ledger."""

from datetime import UTC, datetime

import pytest

from toolbox_tales.ledger import Ledger, LedgerError
from toolbox_tales.spend import Estimate

HEADER = "timestamp,model,kind,quantity,estimated_usd,actual_usd,output"


def est(cost=1.0, model="m"):
    return Estimate(model=model, kind="image", quantity=2, cost_usd=cost)


def test_header_written_once(tmp_path):
    lg = Ledger(tmp_path / "sub" / "spend.csv")
    lg.append(est(), "a.png")
    lg.append(est(), "b.png")
    lines = (tmp_path / "sub" / "spend.csv").read_text().splitlines()
    assert lines[0] == HEADER
    assert lines.count(HEADER) == 1
    assert len(lines) == 3


def test_one_row_per_append_and_content(tmp_path):
    p = tmp_path / "spend.csv"
    lg = Ledger(p)
    now = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    lg.append(est(0.5), "out.png", now=now)
    row = p.read_text().splitlines()[1].split(",")
    assert row[0] == now.isoformat()
    assert row[1:4] == ["m", "image", "2.0"] or row[1:4] == ["m", "image", "2"]
    assert float(row[4]) == 0.5
    assert row[5] == ""
    assert row[6] == "out.png"


def test_total_prefers_actual(tmp_path):
    lg = Ledger(tmp_path / "spend.csv")
    lg.append(est(1.0), "a")
    lg.append(est(1.0), "b", actual_usd=0.25)
    assert lg.total_spent() == pytest.approx(1.25)


def test_total_reads_previous_instance(tmp_path):
    p = tmp_path / "spend.csv"
    Ledger(p).append(est(2.0), "a")
    assert Ledger(p).total_spent() == pytest.approx(2.0)


def test_missing_file_total_zero(tmp_path):
    assert Ledger(tmp_path / "nope.csv").total_spent() == 0.0


def test_existing_rows_not_rewritten(tmp_path):
    p = tmp_path / "spend.csv"
    Ledger(p).append(est(1.0), "a")
    before = p.read_text()
    Ledger(p).append(est(1.0), "b")
    assert p.read_text().startswith(before)


def _write(p, *rows):
    p.write_text("\n".join([HEADER, *rows]) + "\n")


@pytest.mark.parametrize("bad", ["abc", "nan", "inf", "-1"])
def test_bad_estimated_raises_with_line(tmp_path, bad):
    p = tmp_path / "spend.csv"
    _write(p, f"t,m,image,1,1.0,,a", f"t,m,image,1,{bad},,b")
    with pytest.raises(LedgerError, match="line 3"):
        Ledger(p).total_spent()


@pytest.mark.parametrize("bad", ["abc", "nan", "-0.5"])
def test_bad_actual_raises_with_line(tmp_path, bad):
    p = tmp_path / "spend.csv"
    _write(p, f"t,m,image,1,1.0,{bad},a")
    with pytest.raises(LedgerError, match="line 2"):
        Ledger(p).total_spent()


def test_short_row_raises(tmp_path):
    p = tmp_path / "spend.csv"
    _write(p, "t,m,image")
    with pytest.raises(LedgerError, match="line 2"):
        Ledger(p).total_spent()


def test_wrong_header_raises(tmp_path):
    p = tmp_path / "spend.csv"
    p.write_text("a,b,c\n")
    with pytest.raises(LedgerError, match="line 1"):
        Ledger(p).total_spent()


def test_append_unpriced_estimate_refused(tmp_path):
    with pytest.raises(LedgerError):
        Ledger(tmp_path / "s.csv").append(est(None), "a")
