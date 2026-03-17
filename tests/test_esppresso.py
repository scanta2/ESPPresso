"""Tests for the ESPPresso beancount plugin."""

import textwrap
import unittest
from decimal import Decimal

from beancount import loader
from beancount.core import data

import sys
import os

# Ensure ESPPresso.py is importable when running tests from the repo root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ESPPresso


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

OPEN_ACCOUNTS = textwrap.dedent("""\
    2020-01-01 open Assets:ESPP:HOOLI HOOLI
    2020-01-01 open Assets:ESPP:Cash USD
    2020-01-01 open Income:Capital-Gain:HOOLI USD
    2020-01-01 open Income:Ordinary USD
""")

CONFIG = "[{'Asset': 'Assets:ESPP:{ticker}', 'CapGain': 'Income:Capital-Gain:{ticker}', 'OrdIncome': 'Income:Ordinary'}]"

BUY_TXN = textwrap.dedent("""\
    2024-01-31 * "Buy"
      Assets:ESPP:HOOLI 1 HOOLI {90 USD}
        grant_date: 2023-08-01
        fmv_grant: 100 USD
        fmv_acquisition: 200 USD
        discount: 10
      Assets:ESPP:Cash -90 USD
""")


def _load(extra_txn, config=CONFIG, open_accounts=OPEN_ACCOUNTS, buy_txn=BUY_TXN):
    """Load a beancount string with the ESPPresso plugin and return (entries, errors)."""
    text = "\n".join([
        f'plugin "ESPPresso" "{config}"',
        "",
        open_accounts,
        buy_txn,
        extra_txn,
    ])
    return loader.load_string(text)


def _sell_txn(entries, narration="Sell"):
    return next(
        e
        for e in entries
        if isinstance(e, data.Transaction) and e.narration == narration
    )


def _posting(txn, account):
    matches = [p for p in txn.postings if p.account == account]
    return matches[0] if matches else None


# ---------------------------------------------------------------------------
# Unit tests for helper functions
# ---------------------------------------------------------------------------


class TestComputeIncome(unittest.TestCase):
    """Tests for the _compute_income helper directly."""

    def _call(self, sale_price, qualifying=True, quantity=1,
              purchase_price=90, fmv_grant=100, fmv_acquisition=200, discount_pct=10):
        return ESPPresso._compute_income(
            purchase_price=Decimal(str(purchase_price)),
            fmv_grant=Decimal(str(fmv_grant)),
            fmv_acquisition=Decimal(str(fmv_acquisition)),
            discount_pct=Decimal(str(discount_pct)),
            sale_price=Decimal(str(sale_price)),
            quantity=Decimal(str(quantity)),
            qualifying=qualifying,
        )

    # --- Qualifying disposition ---

    def test_qualifying_ordinary_income_capped_by_plan_discount(self):
        # actual_gain=110, plan_discount=10 → ord_income=10, cap_gain=100
        ord_income, cap_gain = self._call(sale_price=200, qualifying=True)
        self.assertEqual(ord_income, Decimal("10"))
        self.assertEqual(cap_gain, Decimal("100"))

    def test_qualifying_ordinary_income_capped_by_actual_gain(self):
        # sale_price=93, actual_gain=3, plan_discount=10 → ord_income=3, cap_gain=0
        ord_income, cap_gain = self._call(sale_price=93, qualifying=True)
        self.assertEqual(ord_income, Decimal("3"))
        self.assertEqual(cap_gain, Decimal("0"))

    def test_qualifying_no_income_on_loss(self):
        # actual_gain=-5 → no ordinary income, capital loss
        ord_income, cap_gain = self._call(sale_price=85, qualifying=True)
        self.assertEqual(ord_income, Decimal("0"))
        self.assertEqual(cap_gain, Decimal("-5"))

    def test_qualifying_multiple_shares(self):
        # 10 shares, sale=200, gain=110 each, plan_discount=10/share
        ord_income, cap_gain = self._call(sale_price=200, qualifying=True, quantity=10)
        self.assertEqual(ord_income, Decimal("100"))   # 10 × 10
        self.assertEqual(cap_gain, Decimal("1000"))    # 10 × 100

    # --- Disqualifying disposition ---

    def test_disqualifying_bargain_element_less_than_gain(self):
        # actual_gain=160, bargain_element=110 → ord_income=110, cap_gain=50
        ord_income, cap_gain = self._call(sale_price=250, qualifying=False)
        self.assertEqual(ord_income, Decimal("110"))
        self.assertEqual(cap_gain, Decimal("50"))

    def test_disqualifying_actual_gain_less_than_bargain_element(self):
        # actual_gain=60, bargain_element=110 → ord_income=60, cap_gain=0
        ord_income, cap_gain = self._call(sale_price=150, qualifying=False)
        self.assertEqual(ord_income, Decimal("60"))
        self.assertEqual(cap_gain, Decimal("0"))

    def test_disqualifying_no_income_on_loss(self):
        # actual_gain<0 → no ordinary income
        ord_income, cap_gain = self._call(sale_price=85, qualifying=False)
        self.assertEqual(ord_income, Decimal("0"))
        self.assertEqual(cap_gain, Decimal("-5"))


class TestIsQualifying(unittest.TestCase):
    """Tests for the _is_qualifying helper."""

    import datetime

    def _call(self, grant_year, purchase_year, sale_year,
              grant_month=1, purchase_month=1, sale_month=1):
        from datetime import date
        return ESPPresso._is_qualifying(
            grant_date=date(grant_year, grant_month, 1),
            purchase_date=date(purchase_year, purchase_month, 1),
            sale_date=date(sale_year, sale_month, 1),
        )

    def test_qualifying(self):
        # grant=2020, purchase=2021, sale=2023 → >2yr from grant, >1yr from purchase
        self.assertTrue(self._call(2020, 2021, 2023))

    def test_disqualifying_too_soon_after_grant(self):
        # grant=2021, purchase=2021, sale=2022 → only 1yr from grant
        self.assertFalse(self._call(2021, 2021, 2022))

    def test_disqualifying_too_soon_after_purchase(self):
        # grant=2020, purchase=2022, sale=2022 → only same year as purchase
        self.assertFalse(self._call(2020, 2022, 2022))

    def test_boundary_exactly_two_years_from_grant(self):
        # sale_date must be STRICTLY greater than 2 years from grant
        from datetime import date
        grant = date(2020, 1, 1)
        purchase = date(2020, 6, 1)
        sale_boundary = ESPPresso._add_years(grant, 2)  # exactly 2yr
        self.assertFalse(
            ESPPresso._is_qualifying(grant, purchase, sale_boundary)
        )


class TestParseConfig(unittest.TestCase):
    def test_single_dict(self):
        cfg = ESPPresso._parse_config(
            "{'Asset': 'Assets:ESPP:{ticker}', 'CapGain': 'Income:CG:{ticker}', 'OrdIncome': 'Income:Ord'}"
        )
        self.assertEqual(len(cfg), 1)
        self.assertEqual(cfg[0]["asset_template"], "Assets:ESPP:{ticker}")
        self.assertEqual(cfg[0]["capgain_template"], "Income:CG:{ticker}")
        self.assertEqual(cfg[0]["ordincome_template"], "Income:Ord")

    def test_list_of_dicts(self):
        cfg = ESPPresso._parse_config(
            "[{'Asset': 'A:{ticker}', 'CapGain': 'CG:{ticker}', 'OrdIncome': 'OI'},"
            " {'Asset': 'B:{ticker}', 'CapGain': 'CG2:{ticker}', 'OrdIncome': 'OI2'}]"
        )
        self.assertEqual(len(cfg), 2)

    def test_empty_config(self):
        self.assertEqual(ESPPresso._parse_config(""), [])
        self.assertEqual(ESPPresso._parse_config(None), [])


class TestTickerExtraction(unittest.TestCase):
    def test_extracts_ticker(self):
        pattern = ESPPresso._ticker_pattern("Assets:ESPP:{ticker}")
        self.assertEqual(ESPPresso._extract_ticker("Assets:ESPP:HOOLI", pattern), "HOOLI")
        self.assertIsNone(ESPPresso._extract_ticker("Assets:Brokerage:HOOLI", pattern))

    def test_no_ticker_placeholder(self):
        # A fixed (non-parameterised) template matches the account and returns ""
        pattern = ESPPresso._ticker_pattern("Assets:ESPP:FIXED")
        self.assertEqual(ESPPresso._extract_ticker("Assets:ESPP:FIXED", pattern), "")
        # A different account does not match at all → None
        self.assertIsNone(ESPPresso._extract_ticker("Assets:ESPP:OTHER", pattern))


# ---------------------------------------------------------------------------
# Integration tests via loader.load_string
# ---------------------------------------------------------------------------


class TestQualifyingDisposition(unittest.TestCase):
    """Sale more than 2 years after grant and more than 1 year after purchase."""

    def setUp(self):
        # Sale on 2026-02-01: grant 2023-08-01 (>2yr), purchase 2024-01-31 (>1yr)
        sell = textwrap.dedent("""\
            2026-02-01 * "Sell"
              Assets:ESPP:HOOLI -1 HOOLI {90 USD, 2024-01-31} @ 200 USD
              Assets:ESPP:Cash 200 USD
              Income:Capital-Gain:HOOLI
        """)
        self.entries, self.errors, _ = _load(sell)

    def test_no_errors(self):
        self.assertEqual([], self.errors)

    def test_ordinary_income_posting_added(self):
        txn = _sell_txn(self.entries)
        p = _posting(txn, "Income:Ordinary")
        self.assertIsNotNone(p, "Expected an Income:Ordinary posting")
        # plan_discount = 100 * 10% = 10 → ord_income = min(10, 110) = 10
        self.assertEqual(p.units.number, Decimal("-10"))
        self.assertEqual(p.units.currency, "USD")

    def test_capital_gain_posting_adjusted(self):
        txn = _sell_txn(self.entries)
        p = _posting(txn, "Income:Capital-Gain:HOOLI")
        self.assertIsNotNone(p)
        # Original auto-balanced: -(200-90) = -110; adjusted: -110 + 10 = -100
        self.assertEqual(p.units.number, Decimal("-100"))

    def test_transaction_still_balances(self):
        # beancount's own validation would raise a ValidationError if the
        # transaction didn't balance; absence of errors proves it does.
        self.assertEqual([], self.errors)


class TestDisqualifyingDisposition(unittest.TestCase):
    """Sale within 1 year of purchase (disqualifying)."""

    def setUp(self):
        # Sale on 2024-06-01: purchase 2024-01-31 → only ~4 months → disqualifying
        sell = textwrap.dedent("""\
            2024-06-01 * "Sell"
              Assets:ESPP:HOOLI -1 HOOLI {90 USD, 2024-01-31} @ 200 USD
              Assets:ESPP:Cash 200 USD
              Income:Capital-Gain:HOOLI
        """)
        self.entries, self.errors, _ = _load(sell)

    def test_no_errors(self):
        self.assertEqual([], self.errors)

    def test_ordinary_income_is_bargain_element(self):
        txn = _sell_txn(self.entries)
        p = _posting(txn, "Income:Ordinary")
        self.assertIsNotNone(p)
        # bargain_element = 200 - 90 = 110, actual_gain = 110 → ord_income = 110
        self.assertEqual(p.units.number, Decimal("-110"))

    def test_capital_gain_is_zero(self):
        txn = _sell_txn(self.entries)
        p = _posting(txn, "Income:Capital-Gain:HOOLI")
        self.assertIsNotNone(p)
        # -110 + 110 = 0
        self.assertEqual(p.units.number, Decimal("0"))

    def test_transaction_still_balances(self):
        self.assertEqual([], self.errors)


class TestDisqualifyingHighSalePrice(unittest.TestCase):
    """Disqualifying disposition where sale price > fmv_acquisition."""

    def setUp(self):
        # Sale at 250 USD: bargain_element=110, actual_gain=160
        # → ord_income=110, cap_gain=50
        sell = textwrap.dedent("""\
            2024-06-01 * "Sell"
              Assets:ESPP:HOOLI -1 HOOLI {90 USD, 2024-01-31} @ 250 USD
              Assets:ESPP:Cash 250 USD
              Income:Capital-Gain:HOOLI
        """)
        self.entries, self.errors, _ = _load(sell)

    def test_ordinary_income(self):
        txn = _sell_txn(self.entries)
        p = _posting(txn, "Income:Ordinary")
        self.assertEqual(p.units.number, Decimal("-110"))

    def test_capital_gain(self):
        txn = _sell_txn(self.entries)
        p = _posting(txn, "Income:Capital-Gain:HOOLI")
        # Original: -(250-90) = -160; adjusted: -160 + 110 = -50
        self.assertEqual(p.units.number, Decimal("-50"))


class TestSaleAtLoss(unittest.TestCase):
    """No ordinary income is recognized when the sale is at a loss."""

    def setUp(self):
        sell = textwrap.dedent("""\
            2026-02-01 * "Sell"
              Assets:ESPP:HOOLI -1 HOOLI {90 USD, 2024-01-31} @ 80 USD
              Assets:ESPP:Cash 80 USD
              Income:Capital-Gain:HOOLI
        """)
        self.entries, self.errors, _ = _load(sell)

    def test_no_ordinary_income_posting(self):
        txn = _sell_txn(self.entries)
        p = _posting(txn, "Income:Ordinary")
        self.assertIsNone(p)

    def test_capital_loss_unchanged(self):
        txn = _sell_txn(self.entries)
        p = _posting(txn, "Income:Capital-Gain:HOOLI")
        # Loss: -(80-90) = 10 USD (auto-balanced as positive for Income = credit flip)
        # The plugin does not touch it since ord_income == 0
        self.assertEqual(p.units.number, Decimal("10"))


class TestNoESPPMetadata(unittest.TestCase):
    """Transactions on ESPP accounts without ESPP metadata are not modified."""

    def setUp(self):
        # Buy without grant_date metadata – no ESPP lot info stored
        buy_no_meta = textwrap.dedent("""\
            2024-01-31 * "Buy"
              Assets:ESPP:HOOLI 1 HOOLI {90 USD}
              Assets:ESPP:Cash -90 USD
        """)
        sell = textwrap.dedent("""\
            2026-02-01 * "Sell"
              Assets:ESPP:HOOLI -1 HOOLI {90 USD, 2024-01-31} @ 200 USD
              Assets:ESPP:Cash 200 USD
              Income:Capital-Gain:HOOLI
        """)
        self.entries, self.errors, _ = _load(sell, buy_txn=buy_no_meta)

    def test_no_errors(self):
        self.assertEqual([], self.errors)

    def test_no_ordinary_income_posting(self):
        txn = _sell_txn(self.entries)
        p = _posting(txn, "Income:Ordinary")
        self.assertIsNone(p)

    def test_capital_gain_not_modified(self):
        txn = _sell_txn(self.entries)
        p = _posting(txn, "Income:Capital-Gain:HOOLI")
        self.assertEqual(p.units.number, Decimal("-110"))


class TestMultipleShares(unittest.TestCase):
    """Selling multiple shares scales income proportionally."""

    def setUp(self):
        # Buy 10 shares
        buy = textwrap.dedent("""\
            2024-01-31 * "Buy"
              Assets:ESPP:HOOLI 10 HOOLI {90 USD}
                grant_date: 2023-08-01
                fmv_grant: 100 USD
                fmv_acquisition: 200 USD
                discount: 10
              Assets:ESPP:Cash -900 USD
        """)
        sell = textwrap.dedent("""\
            2026-02-01 * "Sell"
              Assets:ESPP:HOOLI -10 HOOLI {90 USD, 2024-01-31} @ 200 USD
              Assets:ESPP:Cash 2000 USD
              Income:Capital-Gain:HOOLI
        """)
        self.entries, self.errors, _ = _load(sell, buy_txn=buy)

    def test_no_errors(self):
        self.assertEqual([], self.errors)

    def test_ordinary_income_scaled(self):
        txn = _sell_txn(self.entries)
        p = _posting(txn, "Income:Ordinary")
        # 10 shares × 10 USD/share plan discount = 100 USD ordinary income
        self.assertEqual(p.units.number, Decimal("-100"))

    def test_capital_gain_scaled(self):
        txn = _sell_txn(self.entries)
        p = _posting(txn, "Income:Capital-Gain:HOOLI")
        # Original: -(200-90)*10 = -1100; adjusted: -1100 + 100 = -1000
        self.assertEqual(p.units.number, Decimal("-1000"))


class TestMultipleTickers(unittest.TestCase):
    """Plugin handles multiple tickers correctly with one config entry."""

    def setUp(self):
        extra_open = textwrap.dedent("""\
            2020-01-01 open Assets:ESPP:ACME ACME
            2020-01-01 open Income:Capital-Gain:ACME USD
        """)
        buy_acme = textwrap.dedent("""\
            2024-01-31 * "Buy ACME"
              Assets:ESPP:ACME 1 ACME {90 USD}
                grant_date: 2023-08-01
                fmv_grant: 100 USD
                fmv_acquisition: 200 USD
                discount: 10
              Assets:ESPP:Cash -90 USD
        """)
        sell_hooli = textwrap.dedent("""\
            2026-02-01 * "Sell HOOLI"
              Assets:ESPP:HOOLI -1 HOOLI {90 USD, 2024-01-31} @ 200 USD
              Assets:ESPP:Cash 200 USD
              Income:Capital-Gain:HOOLI
        """)
        sell_acme = textwrap.dedent("""\
            2026-02-01 * "Sell ACME"
              Assets:ESPP:ACME -1 ACME {90 USD, 2024-01-31} @ 200 USD
              Assets:ESPP:Cash 200 USD
              Income:Capital-Gain:ACME
        """)
        text = "\n".join([
            f'plugin "ESPPresso" "{CONFIG}"',
            "",
            OPEN_ACCOUNTS,
            extra_open,
            BUY_TXN,
            buy_acme,
            sell_hooli,
            sell_acme,
        ])
        self.entries, self.errors, _ = loader.load_string(text)

    def test_no_errors(self):
        self.assertEqual([], self.errors)

    def test_hooli_ordinary_income(self):
        txn = _sell_txn(self.entries, "Sell HOOLI")
        p = _posting(txn, "Income:Ordinary")
        self.assertEqual(p.units.number, Decimal("-10"))

    def test_acme_ordinary_income(self):
        txn = _sell_txn(self.entries, "Sell ACME")
        p = _posting(txn, "Income:Ordinary")
        self.assertEqual(p.units.number, Decimal("-10"))

    def test_hooli_capital_gain(self):
        txn = _sell_txn(self.entries, "Sell HOOLI")
        p = _posting(txn, "Income:Capital-Gain:HOOLI")
        self.assertEqual(p.units.number, Decimal("-100"))

    def test_acme_capital_gain(self):
        txn = _sell_txn(self.entries, "Sell ACME")
        p = _posting(txn, "Income:Capital-Gain:ACME")
        self.assertEqual(p.units.number, Decimal("-100"))


class TestQualifyingSmallGain(unittest.TestCase):
    """Qualifying disposition where actual gain < plan discount (ordinary income capped)."""

    def setUp(self):
        # Sale at 93 USD: actual_gain = 93-90 = 3, plan_discount = 10
        # → ord_income = min(10, 3) = 3, cap_gain = 0
        sell = textwrap.dedent("""\
            2026-02-01 * "Sell"
              Assets:ESPP:HOOLI -1 HOOLI {90 USD, 2024-01-31} @ 93 USD
              Assets:ESPP:Cash 93 USD
              Income:Capital-Gain:HOOLI
        """)
        self.entries, self.errors, _ = _load(sell)

    def test_no_errors(self):
        self.assertEqual([], self.errors)

    def test_ordinary_income_capped_by_actual_gain(self):
        txn = _sell_txn(self.entries)
        p = _posting(txn, "Income:Ordinary")
        self.assertIsNotNone(p)
        # Capped at actual gain of 3, not plan discount of 10
        self.assertEqual(p.units.number, Decimal("-3"))

    def test_capital_gain_is_zero(self):
        txn = _sell_txn(self.entries)
        p = _posting(txn, "Income:Capital-Gain:HOOLI")
        self.assertIsNotNone(p)
        # Original auto-balanced: -(93-90) = -3; adjusted: -3 + 3 = 0
        self.assertEqual(p.units.number, Decimal("0"))

    def test_transaction_still_balances(self):
        self.assertEqual([], self.errors)


class TestDisqualifyingAtLoss(unittest.TestCase):
    """Disqualifying disposition at a loss — no ordinary income recognised."""

    def setUp(self):
        # Sale on 2024-06-01 (only ~4 months after purchase → disqualifying)
        # Sale at 80 USD: actual_gain = 80-90 = -10 → no ordinary income
        sell = textwrap.dedent("""\
            2024-06-01 * "Sell"
              Assets:ESPP:HOOLI -1 HOOLI {90 USD, 2024-01-31} @ 80 USD
              Assets:ESPP:Cash 80 USD
              Income:Capital-Gain:HOOLI
        """)
        self.entries, self.errors, _ = _load(sell)

    def test_no_errors(self):
        self.assertEqual([], self.errors)

    def test_no_ordinary_income_posting(self):
        txn = _sell_txn(self.entries)
        self.assertIsNone(_posting(txn, "Income:Ordinary"))

    def test_capital_loss_unchanged(self):
        txn = _sell_txn(self.entries)
        p = _posting(txn, "Income:Capital-Gain:HOOLI")
        # auto-balanced: -(80-90) = 10 USD (positive = debit to income = capital loss)
        self.assertEqual(p.units.number, Decimal("10"))


class TestFixedAccountConfig(unittest.TestCase):
    """Config with no {ticker} placeholder — all accounts are named literally."""

    FIXED_CONFIG = (
        "[{'Asset': 'Assets:ESPP:HOOLI', "
        "'CapGain': 'Income:Capital-Gain:HOOLI', "
        "'OrdIncome': 'Income:Ordinary'}]"
    )

    def _build(self, sell_price, qualifying=True):
        # qualifying: sell 2+ years after grant, 1+ year after purchase
        sell_date = "2026-02-01" if qualifying else "2024-06-01"
        text = "\n".join([
            f'plugin "ESPPresso" "{self.FIXED_CONFIG}"',
            "",
            "2020-01-01 open Assets:ESPP:HOOLI HOOLI",
            "2020-01-01 open Assets:ESPP:Cash USD",
            "2020-01-01 open Income:Capital-Gain:HOOLI USD",
            "2020-01-01 open Income:Ordinary USD",
            "",
            "2024-01-31 * \"Buy\"",
            "  Assets:ESPP:HOOLI 1 HOOLI {90 USD}",
            "    grant_date: 2023-08-01",
            "    fmv_grant: 100 USD",
            "    fmv_acquisition: 200 USD",
            "    discount: 10",
            "  Assets:ESPP:Cash -90 USD",
            "",
            f'{sell_date} * "Sell"',
            f"  Assets:ESPP:HOOLI -1 HOOLI {{90 USD, 2024-01-31}} @ {sell_price} USD",
            "  Assets:ESPP:Cash 200 USD",
            "  Income:Capital-Gain:HOOLI",
        ])
        return loader.load_string(text)

    def test_qualifying_no_errors(self):
        _, errors, _ = self._build(200)
        self.assertEqual([], errors)

    def test_qualifying_ordinary_income(self):
        entries, _, _ = self._build(200)
        txn = _sell_txn(entries)
        p = _posting(txn, "Income:Ordinary")
        self.assertIsNotNone(p, "Expected Income:Ordinary posting")
        # plan_discount = 100 × 10% = 10 USD
        self.assertEqual(p.units.number, Decimal("-10"))

    def test_qualifying_capital_gain(self):
        entries, _, _ = self._build(200)
        txn = _sell_txn(entries)
        p = _posting(txn, "Income:Capital-Gain:HOOLI")
        # -110 + 10 = -100
        self.assertEqual(p.units.number, Decimal("-100"))

    def test_disqualifying_ordinary_income(self):
        entries, errors, _ = self._build(200, qualifying=False)
        self.assertEqual([], errors)
        txn = _sell_txn(entries)
        p = _posting(txn, "Income:Ordinary")
        # bargain_element = 200 - 90 = 110
        self.assertEqual(p.units.number, Decimal("-110"))

    def test_unrelated_account_not_matched(self):
        """A different asset account is not touched by a fixed-account config."""
        text = "\n".join([
            f'plugin "ESPPresso" "{self.FIXED_CONFIG}"',
            "",
            "2020-01-01 open Assets:ESPP:HOOLI HOOLI",
            "2020-01-01 open Assets:ESPP:ACME ACME",
            "2020-01-01 open Assets:ESPP:Cash USD",
            "2020-01-01 open Income:Capital-Gain:HOOLI USD",
            "2020-01-01 open Income:Capital-Gain:ACME USD",
            "2020-01-01 open Income:Ordinary USD",
            "",
            # ACME buy — has ESPP metadata but ACME is not in the fixed config
            "2024-01-31 * \"Buy ACME\"",
            "  Assets:ESPP:ACME 1 ACME {90 USD}",
            "    grant_date: 2023-08-01",
            "    fmv_grant: 100 USD",
            "    fmv_acquisition: 200 USD",
            "    discount: 10",
            "  Assets:ESPP:Cash -90 USD",
            "",
            "2026-02-01 * \"Sell ACME\"",
            "  Assets:ESPP:ACME -1 ACME {90 USD, 2024-01-31} @ 200 USD",
            "  Assets:ESPP:Cash 200 USD",
            "  Income:Capital-Gain:ACME",
        ])
        entries, errors, _ = loader.load_string(text)
        self.assertEqual([], errors)
        txn = _sell_txn(entries, "Sell ACME")
        # No ordinary income — ACME is not in the fixed config
        self.assertIsNone(_posting(txn, "Income:Ordinary"))
        p = _posting(txn, "Income:Capital-Gain:ACME")
        self.assertEqual(p.units.number, Decimal("-110"))


class TestEmptyConfig(unittest.TestCase):
    """When no config is given, plugin is a no-op."""

    def setUp(self):
        text = textwrap.dedent("""\
            plugin "ESPPresso"

            2020-01-01 open Assets:ESPP:HOOLI HOOLI
            2020-01-01 open Assets:ESPP:Cash USD
            2020-01-01 open Income:Capital-Gain:HOOLI USD
            2020-01-01 open Income:Ordinary USD

            2024-01-31 * "Buy"
              Assets:ESPP:HOOLI 1 HOOLI {90 USD}
                grant_date: 2023-08-01
                fmv_grant: 100 USD
                fmv_acquisition: 200 USD
                discount: 10
              Assets:ESPP:Cash -90 USD

            2026-02-01 * "Sell"
              Assets:ESPP:HOOLI -1 HOOLI {90 USD, 2024-01-31} @ 200 USD
              Assets:ESPP:Cash 200 USD
              Income:Capital-Gain:HOOLI
        """)
        self.entries, self.errors, _ = loader.load_string(text)

    def test_no_errors(self):
        self.assertEqual([], self.errors)

    def test_no_ordinary_income(self):
        txn = _sell_txn(self.entries)
        self.assertIsNone(_posting(txn, "Income:Ordinary"))


if __name__ == "__main__":
    unittest.main()
