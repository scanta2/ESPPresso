# ESPPresso
Beancount plugin to compute ordinary income for ESPP dispositions

## Overview

ESPPresso is a [beancount](https://github.com/beancount/beancount) plugin that
automatically applies **Section 423 ESPP (Employee Stock Purchase Plan)** tax
rules to your beancount ledger.

When you sell ESPP shares, the plugin:

1. Determines whether the sale is a **qualifying** or **disqualifying** disposition.
2. Computes the **ordinary income** (W-2 income) component.
3. Adjusts the capital-gain posting so that only the true capital-gain portion
   remains.

### Disposition rules

| | Qualifying | Disqualifying |
|---|---|---|
| **Condition** | Held > 2 years from grant date **and** > 1 year from purchase date | Sold before satisfying qualifying conditions |
| **Ordinary income** | `min(fmv_grant × discount%, actual_gain)` | `min(fmv_acquisition − purchase_price, actual_gain)` |
| **Capital gain/loss** | `actual_gain − ordinary_income` (long-term) | `actual_gain − ordinary_income` |

where `actual_gain = (sale_price − purchase_price) × quantity`.

No ordinary income is recognised if the sale is at a loss.

## Installation

```bash
pip install ESPPresso
```

Or install from source:

```bash
pip install -e .
```

## Usage

### 1. Configure the plugin in your beancount file

```beancount
plugin "ESPPresso" "[{'Asset': 'Assets:ESPP:{ticker}', 'CapGain': 'Income:Capital-Gain:{ticker}', 'OrdIncome': 'Income:Ordinary'}]"
```

`{ticker}` is a placeholder that is replaced with the actual stock ticker
extracted from the posting account.  You can list multiple config dicts in the
array to support multiple ESPP plans with different account structures.

### 2. Record buy transactions with ESPP metadata

The following posting-level metadata fields are required on ESPP buy postings:

| Field | Type | Description |
|---|---|---|
| `grant_date` | date | Offering period start date (grant date) |
| `fmv_grant` | amount | Fair market value per share on the grant date |
| `fmv_acquisition` | amount | Fair market value per share on the purchase date |
| `discount` | number | Purchase discount percentage (e.g. `15` for 15%) |

```beancount
2024-01-31 * "ESPP Purchase"
  Assets:ESPP:HOOLI 1 HOOLI {90 USD}
    grant_date: 2023-08-01
    fmv_grant: 100 USD
    fmv_acquisition: 200 USD
    discount: 10
  Assets:ESPP:Cash -90 USD
```

### 3. Record sell transactions normally

Include a capital-gain posting so that beancount can auto-balance the
transaction.  The plugin will split it into an ordinary-income component and an
adjusted capital-gain component.

```beancount
2026-02-01 * "ESPP Sale"
  Assets:ESPP:HOOLI -1 HOOLI {90 USD, 2024-01-31} @ 200 USD
  Assets:ESPP:Cash 200 USD
  Income:Capital-Gain:HOOLI
```

After the plugin runs, the transaction becomes equivalent to:

```beancount
2026-02-01 * "ESPP Sale"
  Assets:ESPP:HOOLI -1 HOOLI {90 USD, 2024-01-31} @ 200 USD
  Assets:ESPP:Cash 200 USD
  Income:Capital-Gain:HOOLI -100 USD   ; adjusted (was -110)
  Income:Ordinary             -10 USD  ; added by ESPPresso
```

*(Qualifying disposition: plan discount = 100 × 10% = 10 USD ordinary income,
remaining 100 USD is long-term capital gain.)*

## Running the tests

```bash
pip install -e ".[dev]"
pytest
```

## License

MIT — see [LICENSE](LICENSE).
