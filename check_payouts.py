#!/usr/bin/env python3
"""
Automated Payout Reconciliation, Fulfillment Reserve Guardrail, Two-Stage Gate
Allocator (Stage 1: 65/35/0/0 | Stage 2: 50/30/10/10), and CRA Ledger Logger.

Read-only against external payment APIs: never initiates live payouts or transfers.

Supports:
  --mode rolling-3day : Tracks 2-3 business day Stripe/Shopify clearing cohorts and
                        checks Sellvia commission withdrawal readiness.
  --mode friday-sweep : Computes net sweepable revenue for CIBC every Friday after
                        withholding a fulfillment reserve buffer, applies Stage 1 or
                        Stage 2 vault splits, and logs idempotent entries to
                        2026_cra_ledger.csv.
"""

import argparse
import csv
import datetime as dt
import json
import os
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Dict, List, Optional
import urllib.error
import urllib.request


DEFAULT_LEDGER_PATH = "2026_cra_ledger.csv"
DEFAULT_STATE_PATH = "artifacts/payout_monitor_state.json"
DEFAULT_SHOPIFY_API_VERSION = "2025-01"
LEDGER_HEADERS = [
    "date",
    "source",
    "category",
    "reference_id",
    "description",
    "amount",
    "currency",
    "status",
]


def load_dotenv(env_path: str = ".env") -> None:
    """Loads key=value pairs from .env if not already present in os.environ."""
    path = Path(env_path)
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def to_decimal(value: Any, default: str = "0.00") -> Decimal:
    """Safely converts numeric inputs to quantized 2-decimal-place Decimal."""
    if value is None or value == "":
        return Decimal(default)
    cleaned = str(value).replace("$", "").replace(",", "").strip()
    try:
        return Decimal(cleaned).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except Exception:
        return Decimal(default)


def http_get_json(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = 15,
) -> Optional[Dict[str, Any]]:
    """Performs an authenticated read-only HTTP GET request and parses JSON safely."""
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
        print(f"[WARN] GET {url} failed: {exc}")
        return None


def fetch_stripe_summary(stripe_key: str) -> Dict[str, Any]:
    """Fetches available/pending balances and recent payouts from Stripe."""
    if not stripe_key:
        return {
            "enabled": False,
            "available_usd": Decimal("0.00"),
            "pending_usd": Decimal("0.00"),
            "recent_payouts": [],
        }

    headers = {"Authorization": f"Bearer {stripe_key}"}
    balance_data = http_get_json("https://api.stripe.com/v1/balance", headers=headers)
    payouts_data = http_get_json("https://api.stripe.com/v1/payouts?limit=10", headers=headers)

    avail_usd = Decimal("0.00")
    pend_usd = Decimal("0.00")
    if balance_data:
        for entry in balance_data.get("available", []):
            if str(entry.get("currency", "")).lower() == "usd":
                avail_usd += (Decimal(entry.get("amount", 0)) / Decimal("100")).quantize(Decimal("0.01"))
        for entry in balance_data.get("pending", []):
            if str(entry.get("currency", "")).lower() == "usd":
                pend_usd += (Decimal(entry.get("amount", 0)) / Decimal("100")).quantize(Decimal("0.01"))

    payouts = payouts_data.get("data", []) if payouts_data else []
    return {
        "enabled": True,
        "available_usd": avail_usd,
        "pending_usd": pend_usd,
        "recent_payouts": payouts,
    }


def fetch_shopify_payouts(store_domain: str, access_token: str, api_version: str) -> List[Dict[str, Any]]:
    """Fetches recent Shopify Payments payouts if configured."""
    if not store_domain or not access_token:
        return []
    clean_domain = store_domain.replace("https://", "").replace("http://", "").strip("/")
    url = f"https://{clean_domain}/admin/api/{api_version}/shopify_payments/payouts.json?limit=10"
    headers = {
        "X-Shopify-Access-Token": access_token,
        "Content-Type": "application/json",
    }
    data = http_get_json(url, headers=headers)
    return data.get("payouts", []) if data else []


def fetch_sellvia_balances(api_key: str, base_url: str, balance_url: str = "") -> Dict[str, Any]:
    """
    Fetches Sellvia commission and fulfillment balances if a balance endpoint is provided,
    or falls back to environment snapshot variables when Sellvia lacks a public balance API.
    """
    if api_key and balance_url:
        headers = {"Authorization": f"Bearer {api_key}", "X-Api-Key": api_key}
        data = http_get_json(balance_url, headers=headers)
        if data:
            return {
                "source": "api",
                "available_commission_usd": to_decimal(data.get("available_commission", 0)),
                "pending_commission_usd": to_decimal(data.get("pending_commission", 0)),
                "fulfillment_sum_usd": to_decimal(data.get("fulfillment_sum", 0)),
                "unfulfilled_orders": int(data.get("unfulfilled_orders", 0)),
            }

    return {
        "source": "env_snapshot",
        "available_commission_usd": to_decimal(os.getenv("SELLVIA_AVAILABLE_USD", "0.00")),
        "pending_commission_usd": to_decimal(os.getenv("SELLVIA_PENDING_USD", "370.02")),
        "fulfillment_sum_usd": to_decimal(os.getenv("SELLVIA_FULFILLMENT_SUM_USD", "0.88")),
        "unfulfilled_orders": int(os.getenv("SELLVIA_UNFULFILLED_ORDERS", "32")),
    }


def compute_stage_allocations(net_sweepable_usd: Decimal, stage: int) -> Dict[str, str]:
    """
    Applies the Two-Stage Gate allocation protocol:
      - Stage 1 (Stabilization): 65% Overdue Bills / Operating Catch-Up,
                                 35% Fulfillment & Tax Reserve,
                                 0% Reinvestment, 0% Owner Draw.
      - Stage 2 (Normal Growth): 50% Fulfillment & Operating Reserve,
                                 30% Tax & Debt Buffer,
                                 10% Reinvestment, 10% Owner Draw.
    """
    if stage == 2:
        rates = {
            "operating_reserve_50pct": Decimal("0.50"),
            "tax_and_buffer_30pct": Decimal("0.30"),
            "reinvestment_10pct": Decimal("0.10"),
            "owner_draw_10pct": Decimal("0.10"),
        }
    else:
        rates = {
            "overdue_bills_catchup_65pct": Decimal("0.65"),
            "fulfillment_and_tax_reserve_35pct": Decimal("0.35"),
            "reinvestment_0pct": Decimal("0.00"),
            "owner_draw_0pct": Decimal("0.00"),
        }

    allocations: Dict[str, str] = {}
    for label, rate in rates.items():
        amt = (net_sweepable_usd * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        allocations[label] = f"{amt:.2f}"
    return allocations


def save_monitor_state(state_path: Path, payload: Dict[str, Any]) -> None:
    """Persists the latest run summary to artifacts/payout_monitor_state.json."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    existing: Dict[str, Any] = {}
    if state_path.is_file():
        try:
            existing = json.loads(state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {}

    history = existing.get("history", [])
    history.append(payload)
    existing["last_run"] = payload
    existing["history"] = history[-30:]
    state_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")


def ensure_ledger_exists(ledger_path: Path) -> List[str]:
    """Ensures the CSV exists and returns its active column headers."""
    if not ledger_path.exists():
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with ledger_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(LEDGER_HEADERS)
        return LEDGER_HEADERS

    with ledger_path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        if header and len(header) >= 3:
            return header
    return LEDGER_HEADERS


def load_existing_references(ledger_path: Path) -> set:
    """Collects existing reference IDs/descriptions from the CSV to prevent duplicate writes."""
    refs = set()
    if not ledger_path.exists():
        return refs
    with ledger_path.open("r", newline="", encoding="utf-8") as handle:
        for row in csv.reader(handle):
            for cell in row:
                cleaned = cell.strip()
                if cleaned:
                    refs.add(cleaned)
    return refs


def summarize_ledger_totals(ledger_path: Path) -> Dict[str, str]:
    """Reads 2026_cra_ledger.csv safely across both standard and shifted rows."""
    gross_sales = Decimal("0.00")
    cogs = Decimal("0.00")
    software = Decimal("0.00")
    other_expenses = Decimal("0.00")

    if not ledger_path.exists():
        return {
            "gross_sales": "0.00",
            "cogs": "0.00",
            "software_subscriptions": "0.00",
            "other_expenses": "0.00",
            "net_ledger_cashflow": "0.00",
        }

    with ledger_path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        if not header:
            return {
                "gross_sales": "0.00",
                "cogs": "0.00",
                "software_subscriptions": "0.00",
                "other_expenses": "0.00",
                "net_ledger_cashflow": "0.00",
            }

        for row in reader:
            if not row or len(row) < 5:
                continue
            row_text = " ".join(row).lower()
            # Find first numeric amount cell after category columns
            amount_val = Decimal("0.00")
            for cell in row[3:]:
                cleaned = cell.strip().replace("$", "").replace(",", "")
                try:
                    amount_val = Decimal(cleaned).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                    break
                except Exception:
                    continue

            if "gross sales" in row_text or "revenue" in row_text:
                gross_sales += abs(amount_val)
            elif "cost of goods" in row_text or "cogs" in row_text:
                cogs += abs(amount_val)
            elif "software" in row_text or "subscription" in row_text:
                software += abs(amount_val)
            elif "expense" in row_text:
                other_expenses += abs(amount_val)

    net_cashflow = gross_sales - (cogs + software + other_expenses)
    return {
        "gross_sales": f"{gross_sales:.2f}",
        "cogs": f"{cogs:.2f}",
        "software_subscriptions": f"{software:.2f}",
        "other_expenses": f"{other_expenses:.2f}",
        "net_ledger_cashflow": f"{net_cashflow:.2f}",
    }


def append_ledger_entry(
    ledger_path: Path,
    date_str: str,
    source: str,
    category: str,
    reference_id: str,
    description: str,
    amount: Decimal,
    currency: str = "USD",
    status: str = "Completed",
) -> bool:
    """Appends a single idempotent row mapped cleanly to existing CSV column headers."""
    headers = ensure_ledger_exists(ledger_path)
    existing_refs = load_existing_references(ledger_path)
    if reference_id in existing_refs:
        print(f"[SKIP] Reference {reference_id} already exists in {ledger_path}.")
        return False

    row_map = {
        "date": date_str,
        "source": source,
        "category": category,
        "reference_id": reference_id,
        "description": f"{description} [{reference_id}]",
        "amount": f"{amount:.2f}",
        "currency": currency,
        "status": status,
    }

    output_row = []
    for col in headers:
        key = col.strip().lower().replace(" ", "_")
        if key in row_map:
            output_row.append(row_map[key])
        elif "date" in key:
            output_row.append(date_str)
        elif "source" in key or "merchant" in key or "platform" in key:
            output_row.append(source)
        elif "cat" in key or "type" in key:
            output_row.append(category)
        elif "desc" in key or "note" in key or "memo" in key:
            output_row.append(f"{description} [{reference_id}]")
        elif "amount" in key or "total" in key or "net" in key:
            output_row.append(f"{amount:.2f}")
        elif "curr" in key:
            output_row.append(currency)
        elif "status" in key:
            output_row.append(status)
        else:
            output_row.append("")

    with ledger_path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(output_row)

    print(f"[LEDGER] Appended {reference_id}: {source} | {amount:.2f} {currency} ({status})")
    return True


def run_rolling_3day(args: argparse.Namespace) -> Dict[str, Any]:
    """Tracks 2-3 business day clearing cohorts and Sellvia withdrawal readiness."""
    today = dt.date.today()
    cohort_cutoff = today - dt.timedelta(days=args.cadence_days)

    stripe_info = fetch_stripe_summary(os.getenv("STRIPE_SECRET_KEY", ""))
    shopify_payouts = fetch_shopify_payouts(
        os.getenv("SHOPIFY_STORE_DOMAIN", ""),
        os.getenv("SHOPIFY_ADMIN_ACCESS_TOKEN", "") or os.getenv("SHOPIFY_API_TOKEN", ""),
        os.getenv("SHOPIFY_API_VERSION", DEFAULT_SHOPIFY_API_VERSION),
    )
    sellvia_info = fetch_sellvia_balances(
        os.getenv("SELLVIA_API_KEY", "") or os.getenv("SELLVIA_MASTER_KEY", ""),
        os.getenv("SELLVIA_API_BASE_URL", "https://api.sellvia.com"),
        os.getenv("SELLVIA_BALANCE_URL", ""),
    )

    min_withdrawal = to_decimal(args.min_withdrawal_usd)
    avail_comm = sellvia_info["available_commission_usd"]
    ready_for_sellvia_payout = avail_comm >= min_withdrawal

    ledger_path = Path(args.ledger)
    logged_count = 0

    for payout in shopify_payouts:
        payout_id = str(payout.get("id", ""))
        status = str(payout.get("status", "")).lower()
        payout_date = str(payout.get("date", today.isoformat()))
        amount = to_decimal(payout.get("amount", "0.00"))
        currency = str(payout.get("currency", "CAD")).upper()
        if status == "paid" and payout_id and amount > Decimal("0.00"):
            added = append_ledger_entry(
                ledger_path=ledger_path,
                date_str=payout_date,
                source="Shopify / Stripe",
                category="Payout Settlement",
                reference_id=f"SHP-PAYOUT-{payout_id}",
                description=f"Shopify Payments rolling settlement ({payout_date})",
                amount=amount,
                currency=currency,
                status="Completed",
            )
            if added:
                logged_count += 1

    summary = {
        "mode": "rolling-3day",
        "gate_stage": args.stage,
        "run_date": today.isoformat(),
        "cohort_window_start": cohort_cutoff.isoformat(),
        "stripe_available_usd": str(stripe_info["available_usd"]),
        "stripe_pending_usd": str(stripe_info["pending_usd"]),
        "sellvia_available_commission_usd": str(avail_comm),
        "sellvia_pending_commission_usd": str(sellvia_info["pending_commission_usd"]),
        "sellvia_fulfillment_sum_usd": str(sellvia_info["fulfillment_sum_usd"]),
        "min_sellvia_withdrawal_usd": str(min_withdrawal),
        "ready_for_sellvia_withdrawal": ready_for_sellvia_payout,
        "new_ledger_rows_logged": logged_count,
        "ledger_audit": summarize_ledger_totals(ledger_path),
    }
    save_monitor_state(Path(args.state_file), summary)
    print(json.dumps(summary, indent=2))
    return summary


def run_friday_sweep(args: argparse.Namespace) -> Dict[str, Any]:
    """Computes Friday CIBC sweep after protecting the Sellvia fulfillment reserve."""
    today = dt.date.today()
    reserve_target = to_decimal(args.reserve_usd)

    stripe_info = fetch_stripe_summary(os.getenv("STRIPE_SECRET_KEY", ""))
    shopify_payouts = fetch_shopify_payouts(
        os.getenv("SHOPIFY_STORE_DOMAIN", ""),
        os.getenv("SHOPIFY_ADMIN_ACCESS_TOKEN", "") or os.getenv("SHOPIFY_API_TOKEN", ""),
        os.getenv("SHOPIFY_API_VERSION", DEFAULT_SHOPIFY_API_VERSION),
    )
    sellvia_info = fetch_sellvia_balances(
        os.getenv("SELLVIA_API_KEY", "") or os.getenv("SELLVIA_MASTER_KEY", ""),
        os.getenv("SELLVIA_API_BASE_URL", "https://api.sellvia.com"),
        os.getenv("SELLVIA_BALANCE_URL", ""),
    )

    fulfillment_sum = sellvia_info["fulfillment_sum_usd"]
    reserve_shortfall = max(Decimal("0.00"), reserve_target - fulfillment_sum)

    cleared_shopify_total = sum(
        (to_decimal(p.get("amount", "0.00")) for p in shopify_payouts if str(p.get("status", "")).lower() == "paid"),
        Decimal("0.00"),
    )
    gross_sweepable = stripe_info["available_usd"] + sellvia_info["available_commission_usd"]
    net_sweepable_usd = max(Decimal("0.00"), gross_sweepable - reserve_shortfall)
    vault_allocations = compute_stage_allocations(net_sweepable_usd, args.stage)

    ledger_path = Path(args.ledger)
    iso_year, iso_week, _ = today.isocalendar()
    sweep_ref = f"CIBC-SWEEP-{iso_year}-W{iso_week:02d}"

    logged = False
    if net_sweepable_usd > Decimal("0.00"):
        logged = append_ledger_entry(
            ledger_path=ledger_path,
            date_str=today.isoformat(),
            source=f"CIBC Sweep ({args.target_bank})",
            category="Owner Payout Sweep",
            reference_id=sweep_ref,
            description=(
                f"Weekly Friday sweep estimate to {args.target_bank} [Stage {args.stage}] "
                f"(Gross ${gross_sweepable:.2f} - Reserve Holdback ${reserve_shortfall:.2f})"
            ),
            amount=net_sweepable_usd,
            currency="USD",
            status="Scheduled",
        )

    summary = {
        "mode": "friday-sweep",
        "gate_stage": args.stage,
        "run_date": today.isoformat(),
        "target_bank": args.target_bank,
        "fulfillment_sum_usd": str(fulfillment_sum),
        "fulfillment_reserve_target_usd": str(reserve_target),
        "reserve_holdback_usd": str(reserve_shortfall),
        "gross_sweepable_usd": str(gross_sweepable),
        "cleared_shopify_payouts_recent": str(cleared_shopify_total),
        "net_sweepable_to_cibc_usd": str(net_sweepable_usd),
        "stage_allocations_usd": vault_allocations,
        "sweep_reference": sweep_ref,
        "ledger_entry_created": logged,
        "ledger_audit": summarize_ledger_totals(ledger_path),
    }
    save_monitor_state(Path(args.state_file), summary)
    print(json.dumps(summary, indent=2))
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rolling 3-day payout monitor & Friday CIBC sweep guard.")
    parser.add_argument(
        "--mode",
        choices=["rolling-3day", "friday-sweep"],
        required=True,
        help="Execution mode: rolling-3day or friday-sweep",
    )
    parser.add_argument(
        "--stage",
        type=int,
        choices=[1, 2],
        default=int(os.getenv("GATE_STAGE", "1")),
        help="Allocation stage: 1 = Stabilization (65/35/0/0), 2 = Growth (50/30/10/10) (default: 1)",
    )
    parser.add_argument(
        "--ledger",
        