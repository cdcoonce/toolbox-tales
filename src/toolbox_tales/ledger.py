"""Append-only spend ledger (CSV). A budget that cannot be read is a failure, not zero."""

import csv
import math
from datetime import UTC, datetime
from pathlib import Path

from toolbox_tales.spend import Estimate

COLUMNS = ["timestamp", "model", "kind", "quantity", "estimated_usd", "actual_usd", "output"]


class LedgerError(Exception):
    """Raised when the ledger cannot be trusted."""


def _cost(text: str, name: str, line: int) -> float:
    try:
        value = float(text)
    except ValueError:
        raise LedgerError(f"ledger line {line}: {name} {text!r} is not a number") from None
    if not math.isfinite(value) or value < 0:
        raise LedgerError(f"ledger line {line}: {name} {text!r} must be finite and non-negative")
    return value


class Ledger:
    def __init__(self, path: Path = Path("ledger/spend.csv")) -> None:
        self.path = Path(path)

    def append(
        self,
        est: Estimate,
        output: str,
        actual_usd: float | None = None,
        now: datetime | None = None,
    ) -> None:
        if est.cost_usd is None:
            raise LedgerError("cannot record an unpriced estimate")
        stamp = (now or datetime.now(UTC)).isoformat()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new = not self.path.exists() or self.path.stat().st_size == 0
        with self.path.open("a", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh, lineterminator="\n")
            if new:
                writer.writerow(COLUMNS)
            writer.writerow(
                [
                    stamp,
                    est.model,
                    est.kind,
                    est.quantity,
                    est.cost_usd,
                    "" if actual_usd is None else actual_usd,
                    output,
                ]
            )

    def total_spent(self) -> float:
        if not self.path.exists():
            return 0.0
        total = 0.0
        with self.path.open(newline="", encoding="utf-8") as fh:
            reader = csv.reader(fh)
            for row in reader:
                line = reader.line_num
                if line == 1:
                    if row != COLUMNS:
                        raise LedgerError(f"ledger line 1: header must be {','.join(COLUMNS)}")
                    continue
                if not row:
                    continue
                if len(row) != len(COLUMNS):
                    raise LedgerError(
                        f"ledger line {line}: expected {len(COLUMNS)} columns, got {len(row)}"
                    )
                actual = row[5].strip()
                if actual:
                    total += _cost(actual, "actual_usd", line)
                else:
                    total += _cost(row[4].strip(), "estimated_usd", line)
        return total
