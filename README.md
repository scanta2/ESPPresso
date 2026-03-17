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
extracted from the posting account.  `{ticker}` is **optional** — if you omit
it, all three account names are treated as literal fixed strings (useful when
you have a single-ticker ESPP plan):

```beancount
plugin "ESPPresso" "[{'Asset': 'Assets:ESPP:HOOLI', 'CapGain': 'Income:Capital-Gain:HOOLI', 'OrdIncome': 'Income:Ordinary'}]"
```

You can list multiple config dicts in the array to support multiple ESPP plans
with different account structures.

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

## Scenario Examples

All examples below use the same ESPP plan parameters:

| Parameter | Value |
|---|---|
| Purchase price | 90 USD |
| FMV at grant date | 100 USD |
| FMV at purchase date | 200 USD |
| Discount | 10% |

Derived: **plan discount** = 100 × 10% = **10 USD/share**,
**bargain element** = 200 − 90 = **110 USD/share**.

```beancount
2024-01-31 * "ESPP Purchase"
  Assets:ESPP:HOOLI 1 HOOLI {90 USD}
    grant_date: 2023-08-01
    fmv_grant: 100 USD
    fmv_acquisition: 200 USD
    discount: 10
  Assets:ESPP:Cash -90 USD
```

---

### Qualifying — large gain

Held > 2 years after grant **and** > 1 year after purchase.
Sale at **200 USD**: actual\_gain = 110, ordinary\_income = min(10, 110) = **10 USD**.

```beancount
; Before (as written in ledger):
2026-02-01 * "ESPP Sale"
  Assets:ESPP:HOOLI -1 HOOLI {90 USD, 2024-01-31} @ 200 USD
  Assets:ESPP:Cash 200 USD
  Income:Capital-Gain:HOOLI        ; auto-balanced by beancount

; After the plugin runs:
2026-02-01 * "ESPP Sale"
  Assets:ESPP:HOOLI -1 HOOLI {90 USD, 2024-01-31} @ 200 USD
  Assets:ESPP:Cash 200 USD
  Income:Capital-Gain:HOOLI -100 USD   ; adjusted (was −110)
  Income:Ordinary              -10 USD  ; added by ESPPresso (W-2 income)
```

---

### Qualifying — small gain (capped below plan discount)

Sale at **93 USD**: actual\_gain = 3, ordinary\_income = min(10, 3) = **3 USD**
(capped by the actual gain, not the plan discount).

```beancount
; After the plugin runs:
2026-02-01 * "ESPP Sale"
  Assets:ESPP:HOOLI -1 HOOLI {90 USD, 2024-01-31} @ 93 USD
  Assets:ESPP:Cash 93 USD
  Income:Capital-Gain:HOOLI   0 USD   ; adjusted (was −3)
  Income:Ordinary            -3 USD   ; added by ESPPresso
```

---

### Qualifying — sale at a loss

Sale at **80 USD**: actual\_gain = −10 → no ordinary income is recognised.
The capital-gain posting is left unchanged (a capital loss).

```beancount
; After the plugin runs (no change to income split):
2026-02-01 * "ESPP Sale"
  Assets:ESPP:HOOLI -1 HOOLI {90 USD, 2024-01-31} @ 80 USD
  Assets:ESPP:Cash 80 USD
  Income:Capital-Gain:HOOLI  10 USD   ; capital loss (positive debit to income)
```

---

### Disqualifying — gain at or below bargain element (all ordinary income)

Sold before satisfying the qualifying holding periods (e.g. only 4 months after purchase).
Sale at **200 USD**: bargain\_element = 110, actual\_gain = 110,
ordinary\_income = min(110, 110) = **110 USD**, capital\_gain = 0.

```beancount
; After the plugin runs:
2024-06-01 * "ESPP Sale"
  Assets:ESPP:HOOLI -1 HOOLI {90 USD, 2024-01-31} @ 200 USD
  Assets:ESPP:Cash 200 USD
  Income:Capital-Gain:HOOLI    0 USD   ; adjusted (was −110)
  Income:Ordinary            -110 USD  ; added by ESPPresso (W-2 income)
```

---

### Disqualifying — gain above bargain element (ordinary income + capital gain)

Sale at **250 USD**: bargain\_element = 110, actual\_gain = 160,
ordinary\_income = min(110, 160) = **110 USD**, capital\_gain = **50 USD**.

```beancount
; After the plugin runs:
2024-06-01 * "ESPP Sale"
  Assets:ESPP:HOOLI -1 HOOLI {90 USD, 2024-01-31} @ 250 USD
  Assets:ESPP:Cash 250 USD
  Income:Capital-Gain:HOOLI  -50 USD   ; adjusted (was −160)
  Income:Ordinary           -110 USD   ; added by ESPPresso (W-2 income)
```

---

### Disqualifying — sale at a loss

Sale at **80 USD**: actual\_gain = −10 → no ordinary income, regardless of
disposition type. The capital-gain posting is left unchanged.

```beancount
; After the plugin runs (no change to income split):
2024-06-01 * "ESPP Sale"
  Assets:ESPP:HOOLI -1 HOOLI {90 USD, 2024-01-31} @ 80 USD
  Assets:ESPP:Cash 80 USD
  Income:Capital-Gain:HOOLI  10 USD   ; capital loss (unchanged)
```

---

## Running the tests

```bash
pip install -e ".[dev]"
pytest
```

## License

MIT — see [LICENSE](LICENSE).
