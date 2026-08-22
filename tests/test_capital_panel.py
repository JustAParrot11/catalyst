"""The capital panel answers the leverage question with a fact.

Owner-asked: "if we use leverage we could make more money right? ...
Can we factor leverage in to ensure we can trade more than we could?"

Every ceiling in risk/sizing.py is a percentage of EQUITY. Borrowed
money does not change equity, so the only rule more buying power could
ever relax is the final settled-cash clamp. Whether THAT has bound is a
matter of record, not opinion, and this panel reads it off the record.
"""

from datetime import datetime, timezone

import pytest

from catalyst.dashboard.db import Db
from catalyst.dashboard import panels
from catalyst.storage import init_db


@pytest.fixture
def db_path(tmp_path):
    p = str(tmp_path / "cap.db")
    init_db(p).close()
    return p


def snapshot(path, equity, cash, notional):
    conn = init_db(path)
    conn.execute(
        "INSERT INTO equity_snapshots (day, taken_at, source, equity_usd, "
        "settled_cash_usd, positions_notional) VALUES (?,?,?,?,?,?)",
        ("2026-08-22", datetime.now(timezone.utc).isoformat(), "broker_read",
         str(equity), str(cash), str(notional)))
    conn.commit()
    conn.close()


def bind(path, rule, times=1):
    conn = init_db(path)
    for i in range(times):
        conn.execute(
            "INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?)",
            (f"c{rule}{i}", "AAA", "insider_cluster", "2026-08-20",
             "confirmed", "[]", datetime.now(timezone.utc).isoformat(),
             "tech", "[]"))
        conn.execute(
            "INSERT INTO risk_decisions (id, candidate_id, action, "
            "skip_reasons, adaptive_params_snapshot, decided_at) "
            "VALUES (?,?,?,?,?,?)",
            (f"d{rule}{i}", f"c{rule}{i}", "trade", "[]", "{}",
             datetime.now(timezone.utc).isoformat()))
        conn.execute(
            "INSERT INTO limit_applications (decision_id, rule_name, "
            "bound_value, requested_value, bound_type, binding) "
            "VALUES (?,?,?,?,?,1)",
            (f"d{rule}{i}", rule, "1", "2", "hard"))
    conn.commit()
    conn.close()


class TestItAnswersTheLeverageQuestion:
    def test_equity_limits_binding_says_leverage_would_not_help(self, db_path):
        """The answer that matters. Every one of these caps is a fraction
        of equity, which borrowing does not change."""
        snapshot(db_path, 2000, 2000, 400)
        bind(db_path, "max_loss_per_position", 7)
        bind(db_path, "max_total_exposure", 2)

        html = panels.capital_panel(Db(db_path))

        assert "Available cash has never been the limit" in html
        assert "borrowing does not change equity" in html

    def test_cash_binding_says_so_instead(self, db_path):
        """The honest other branch. If cash really is the constraint,
        the panel must not keep asserting it never is."""
        snapshot(db_path, 2000, 100, 1900)
        bind(db_path, "settled_cash", 3)
        bind(db_path, "max_total_exposure", 1)

        html = panels.capital_panel(Db(db_path))

        assert "Cash was the binding limit 3 of 4 times" in html
        assert "not a reason to borrow on its own" in html

    def test_leverage_reads_one_x_when_unborrowed(self, db_path):
        snapshot(db_path, 2000, 2000, 1000)
        html = panels.capital_panel(Db(db_path))
        assert "0.50x" in html and "1.00x without borrowing" in html

    def test_never_traded_is_explained_not_left_blank(self, db_path):
        """House rule 3. No bound limits because it has never sized a
        position is a very different fact from a broken query."""
        snapshot(db_path, 2000, 2000, 0)
        html = panels.capital_panel(Db(db_path))
        assert "it has never traded" in html

    def test_no_broker_reading_says_so(self, db_path):
        html = panels.capital_panel(Db(db_path))
        assert "No broker reading yet" in html

    def test_a_zero_equity_prints_the_raw_row_rather_than_dividing(self, db_path):
        """Every figure here divides by equity. A zero must be explained,
        never rendered as a percentage of nothing."""
        snapshot(db_path, 0, 0, 0)
        html = panels.capital_panel(Db(db_path))
        assert "equity read as" in html

    def test_an_unreadable_equity_does_not_raise(self, db_path):
        snapshot(db_path, "nonsense", 0, 0)
        html = panels.capital_panel(Db(db_path))
        assert "equity read as" in html
