"""esppresso: Beancount plugin to compute ordinary income for ESPP dispositions.

Handles Section 423 ESPP (Employee Stock Purchase Plan) tax rules:
  - Qualifying disposition: sold > 2 years after grant date AND > 1 year after purchase date
  - Disqualifying disposition: sold before meeting qualifying holding periods

Tax treatment applied by this plugin:
  - Qualifying:     ordinary_income = min(plan_discount, actual_gain)
                    capital_gain    = actual_gain - ordinary_income  (long-term)
  - Disqualifying:  ordinary_income = min(bargain_element, actual_gain)
                    capital_gain    = actual_gain - ordinary_income

where:
  plan_discount    = fmv_grant * (discount / 100)
  bargain_element  = fmv_acquisition - purchase_price
  actual_gain      = (sale_price - purchase_price) * quantity

Configuration in your beancount file:

    plugin "esppresso.esppresso" "[{'Asset': 'Assets:ESPP:{ticker}', 'CapGain': 'Income:Capital-Gain:{ticker}', 'OrdIncome': 'Income:Ordinary'}]"

Multiple ESPP plans are supported by listing multiple config dicts in the array.

Buy transaction (with required posting metadata):

  2024-01-31 * "Buy"
    Assets:ESPP:HOOLI 1 HOOLI {90 USD}
      grant_date: 2023-08-01
      fmv_grant: 100 USD
      fmv_acquisition: 200 USD
      discount: 10
    Assets:ESPP:Cash -90 USD

Sell transaction (plugin rewrites the capital-gain posting to split income):

  2026-02-01 * "Sell"
    Assets:ESPP:HOOLI -1 HOOLI {90 USD, 2024-01-31} @ 200 USD
    Assets:ESPP:Cash 200 USD
    Income:Capital-Gain:HOOLI
"""

__copyright__ = "Copyright (C) 2026 Stefano Mihai Canta"
__license__ = "MIT"

import ast
import collections
import re
from decimal import Decimal

from beancount.core import data
from beancount.core.amount import Amount
from beancount.core.number import ZERO

__plugins__ = ("esppresso",)
DEBUG = 0

ESPPError = collections.namedtuple("ESPPError", "source message entry")


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------

def _ticker_pattern(template):
    """Compile a regex to extract {ticker} from an account string."""
    escaped = re.escape(template)
    # re.escape turns { } into \{ \}; replace the escaped placeholder
    pattern = escaped.replace(r"\{ticker\}", r"(?P<ticker>[^:]+)")
    return re.compile(r"^" + pattern + r"$")


def _extract_ticker(account, pattern):
    """Return the ticker captured from *account* using a compiled pattern.

    Returns:
      None  - the account does not match the pattern at all.
      ""    - the account matches but the pattern has no ``{ticker}`` group
              (i.e. a fixed-account configuration with no ticker placeholder).
      str   - the captured ticker string.
    """
    m = pattern.match(account)
    if not m:
        return None
    try:
        return m.group("ticker")
    except IndexError:
        return ""


def _resolve(template, ticker):
    """Replace ``{ticker}`` in *template* with the actual *ticker* string.

    When *ticker* is falsy (``None`` or ``""``) the template is returned
    unchanged — this covers fixed-account configurations that contain no
    ``{ticker}`` placeholder.
    """
    if not ticker:
        return template
    return template.replace("{ticker}", ticker)


def _parse_config(config_str):
    """Parse the plugin configuration string.

    Expected format (Python literal):
      [{'Asset': 'Assets:ESPP:{ticker}',
        'CapGain': 'Income:Capital-Gain:{ticker}',
        'OrdIncome': 'Income:Ordinary'},
       ...]

    A single dict (without the surrounding list) is also accepted.

    Returns a list of config dicts, each with keys:
      asset_template, asset_pattern, capgain_template, ordincome_template
    """
    if not config_str or not config_str.strip():
        return []
    configs = ast.literal_eval(config_str)
    if isinstance(configs, dict):
        configs = [configs]
    return [
        {
            "asset_template": cfg["Asset"],
            "asset_pattern": _ticker_pattern(cfg["Asset"]),
            "capgain_template": cfg["CapGain"],
            "ordincome_template": cfg["OrdIncome"],
        }
        for cfg in configs
    ]


def _find_config(account, configs):
    """Return *(config, ticker)* for the first config whose asset pattern matches
    *account*, or *(None, None)* if no match is found."""
    for cfg in configs:
        ticker = _extract_ticker(account, cfg["asset_pattern"])
        if ticker is not None:
            return cfg, ticker
    return None, None


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def _add_years(d, years):
    """Return *d* plus *years* years, clamping Feb 29 to Feb 28."""
    try:
        return d.replace(year=d.year + years)
    except ValueError:
        return d.replace(year=d.year + years, day=28)


def _is_qualifying(grant_date, purchase_date, sale_date):
    """Return True if the disposition is qualifying.

    Qualifying conditions (both must hold):
      * sold more than 2 years after the offering/grant date
      * sold more than 1 year after the purchase/acquisition date
    """
    return (
        sale_date > _add_years(grant_date, 2)
        and sale_date > _add_years(purchase_date, 1)
    )


# ---------------------------------------------------------------------------
# Income computation
# ---------------------------------------------------------------------------

def _to_decimal(value):
    """Coerce *value* (Amount, Decimal, int, or str) to Decimal."""
    if isinstance(value, Amount):
        return value.number
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _compute_income(
    purchase_price,
    fmv_grant,
    fmv_acquisition,
    discount_pct,
    sale_price,
    quantity,
    qualifying,
):
    """Compute (ordinary_income, capital_gain) for an ESPP lot sale.

    All price/income parameters are *per-share* Decimals; *quantity* is the
    number of shares sold (positive).  The returned amounts are total amounts
    (already multiplied by *quantity*) and are *positive* values representing
    income / gain (or negative for a loss on capital_gain).

        Algorithm:
            - actual_gain is the total realized gain: (sale_price - purchase_price) * quantity
            - For a qualifying disposition:
                    * benefit = plan_discount = fmv_grant * (discount_pct / 100)
                    * ordinary_income = max(0, min(actual_gain, benefit * quantity))
            - For a disqualifying disposition:
                    * benefit = bargain_element = (fmv_acquisition - purchase_price)
                    * ordinary_income = benefit * quantity
            - capital_gain = actual_gain - ordinary_income

        Notes:
            - All price/income values in the arguments are per-share values; the
                returned `ordinary_income` and `capital_gain` are total amounts already
                multiplied by `quantity`.
            - `ordinary_income` may be zero when the sale is at a loss; `capital_gain`
                will be negative for a capital loss.
    """
    actual_gain = (sale_price - purchase_price) * quantity

    if qualifying:
        benefit = fmv_grant * (discount_pct / Decimal("100")) * quantity
        ordinary_income = round(max(0,min(actual_gain, benefit)), 2)
    else:
        benefit = (fmv_acquisition - purchase_price) * quantity
        ordinary_income = round(benefit, 2)

    capital_gain = actual_gain - ordinary_income
    return ordinary_income, capital_gain


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------

def esppresso(entries, options_map, config=None):
    """ESPPresso plugin entry point.

    Rewrites ESPP sell transactions to split the auto-balanced capital-gain
    posting into an ordinary-income component and an adjusted capital-gain
    component according to Section 423 ESPP disposition rules.

    Args:
      entries:     List of beancount directives (after booking / interpolation).
      options_map: Beancount options dict.
      config:      Plugin configuration string (Python literal).

    Returns:
      (new_entries, errors)
    """
    configs = _parse_config(config)
    errors = []

    if not configs:
        return entries, errors

    # ------------------------------------------------------------------
    # Pass 1 – collect ESPP lot metadata from buy transactions
    # ------------------------------------------------------------------
    # Key: (account, commodity, cost_number, cost_currency, cost_date)
    espp_lots = {}

    for entry in entries:
        if not isinstance(entry, data.Transaction):
            continue
        for posting in entry.postings:
            # Only buy postings (positive quantity)
            if posting.units.number <= ZERO:
                continue
            cfg, ticker = _find_config(posting.account, configs)
            if cfg is None or posting.cost is None:
                continue
            meta = posting.meta
            if "grant_date" not in meta:
                continue  # Not an ESPP lot with required metadata

            cost = posting.cost
            cost_date = cost.date if cost.date else entry.date
            key = (
                posting.account,
                posting.units.currency,
                cost.number,
                cost.currency,
                cost_date,
            )
            espp_lots[key] = {
                "grant_date": meta["grant_date"],
                "fmv_grant": _to_decimal(meta.get("fmv_grant", ZERO)),
                "fmv_acquisition": _to_decimal(meta.get("fmv_acquisition", ZERO)),
                "discount": _to_decimal(meta.get("discount", ZERO)),
                "purchase_date": entry.date,
                "cfg": cfg,
                "ticker": ticker,
            }
            if DEBUG:
                print(f"[ESPPresso] Stored lot key={key} lot={espp_lots[key]}")

    # ------------------------------------------------------------------
    # Pass 2 – rewrite sell transactions that involve ESPP lots
    # ------------------------------------------------------------------
    new_entries = []

    for entry in entries:
        if not isinstance(entry, data.Transaction):
            new_entries.append(entry)
            continue

        # Collect adjustments needed for this transaction
        # Each element: (capgain_account, ordincome_account, ordinary_income, currency)
        adjustments = []

        for posting in entry.postings:
            # Only sell postings (negative quantity) with a cost
            if posting.units.number >= ZERO or posting.cost is None:
                continue
            cfg, ticker = _find_config(posting.account, configs)
            if cfg is None:
                continue

            cost = posting.cost
            cost_date = cost.date if cost.date else entry.date
            key = (
                posting.account,
                posting.units.currency,
                cost.number,
                cost.currency,
                cost_date,
            )
            lot = espp_lots.get(key)
            if lot is None:
                continue  # Not an ESPP lot we know about

            if DEBUG:
                print(f"[ESPPresso] Found lot for sell key={key}: {lot}")

            if posting.price is None:
                errors.append(
                    ESPPError(
                        source=entry.meta,
                        message=(
                            f"ESPPresso: sell posting on {posting.account} has no price; "
                            "cannot compute ESPP income"
                        ),
                        entry=entry,
                    )
                )
                continue

            quantity = abs(posting.units.number)
            sale_price = posting.price.number
            purchase_price = cost.number
            currency = cost.currency

            qualifying = _is_qualifying(
                lot["grant_date"], lot["purchase_date"], entry.date
            )
            if DEBUG:
                print(f"[ESPPresso] qualifying={qualifying}")
            ordinary_income, _capital_gain = _compute_income(
                purchase_price=purchase_price,
                fmv_grant=lot["fmv_grant"],
                fmv_acquisition=lot["fmv_acquisition"],
                discount_pct=lot["discount"],
                sale_price=sale_price,
                quantity=quantity,
                qualifying=qualifying,
            )

            if ordinary_income == ZERO:
                continue  # Nothing to reclassify

            capgain_account = _resolve(cfg["capgain_template"], ticker)
            ordincome_account = _resolve(cfg["ordincome_template"], ticker)
            adjustments.append(
                (capgain_account, ordincome_account, ordinary_income, currency)
            )

        if not adjustments:
            new_entries.append(entry)
            continue

        # Rebuild the posting list, adjusting cap-gain and adding ordinary income
        new_postings = list(entry.postings)
        extra_postings = []
        plugin_meta = data.new_metadata("<ESPPresso>", 0)

        for capgain_acct, ordincome_acct, ord_income, currency in adjustments:
            # Find the capital-gain posting and reduce it by ordinary_income.
            # (In beancount's sign convention, income postings are negative credits,
            # so adding a positive ord_income makes the cap-gain posting less negative.)
            for i, p in enumerate(new_postings):
                if p.account == capgain_acct and p.units is not None:
                    adjusted = Amount(p.units.number + ord_income, p.units.currency)
                    new_postings[i] = p._replace(units=adjusted)
                    break

            # Add the ordinary-income posting (negative = credit / income)
            extra_postings.append(
                data.Posting(
                    account=ordincome_acct,
                    units=Amount(-ord_income, currency),
                    cost=None,
                    price=None,
                    flag=None,
                    meta=plugin_meta,
                )
            )

        new_entries.append(entry._replace(postings=new_postings + extra_postings))

    return new_entries, errors
