Update `check_payouts.py` cleanly without changing its offline, read-only architecture (it must still make zero network calls, never modify `2026_cra_ledger.csv` or any other file, and preserve all existing `Decimal` validation, `_read_amount`, `_add_exact`, `_multiply_exact`, `_format_cad`, and `_select_schema` logic):

1. **Update `STORE_DISPLAY_NAME` (Line 23):**
   - Change `STORE_DISPLAY_NAME = "jesses-digital-ventures.myshopify.com"` to:
     `STORE_DISPLAY_NAME = "ppd1xp-1c.myshopify.com"`

2. **Replace the Illustrative `50/30/20` Printout Block (`if difference > 0:` in `check_payout_status`) with the Two-Stage Gate Protocol Display:**
   - Keep all existing header lines, schema notices, row counts, `Ledger REVENUE`, `Ledger EXPENSE`, `Ledger difference (REVENUE - EXPENSE)`, and assumption disclaimers intact.
   - When `difference > 0`, replace the `PURELY ILLUSTRATIVE 50/30/20 CALCULATION` block with a read-only illustrative breakdown for both stages of the **Two-Stage Gate Protocol** using `_multiply_exact` and `_format_cad`:
     - **Stage 1 — Stabilization Mode (`65 / 35 / 0 / 0`, active until overdue bills = $0 and Fulfillment Vault >= $150 USD):**
       - `Fulfillment & Tax Vault (65%): {_format_cad(_multiply_exact(difference, Decimal('0.65')))}`
       - `Debt & Bill Sweep (35%): {_format_cad(_multiply_exact(difference, Decimal('0.35')))}`
       - `Ads Budget (0%): $0.00 CAD`
       - `Owner Profit Draw (0%): $0.00 CAD`
     - **Stage 2 — Steady-State Growth Mode (`50 / 30 / 10 / 10`, unlocks once overdue bills = $0 and Fulfillment Vault >= $150 USD):**
       - `Fulfillment & Tax Reserve (50%): {_format_cad(_multiply_exact(difference, Decimal('0.50')))}`
       - `Growth & Ads Budget (30%): {_format_cad(_multiply_exact(difference, Decimal('0.30')))}`
       - `Platform Buffer (10%): {_format_cad(_multiply_exact(difference, Decimal('0.10')))}`
       - `Owner Profit Draw (10%): {_format_cad(_multiply_exact(difference, Decimal('0.10')))}`
   - In the `else:` branch (`difference <= 0`), update the message from `"No 50/30/20 estimates: the exact ledger difference is not positive."` to:
     `"No Two-Stage Gate estimates: the exact ledger difference is not positive."`
   - Keep `print("No money is moved.\n")` at the end of `check_payout_status`.

3. **Verify Before Pushing:**
   - Run `python3 -m py_compile check_payouts.py` and execute a read-only test run against a temporary sample CSV (deleting the temporary CSV afterward) to confirm zero syntax errors, zero modifications to `2026_cra_ledger.csv`, and exact cent-rounded outputs, then commit and push to `main`.