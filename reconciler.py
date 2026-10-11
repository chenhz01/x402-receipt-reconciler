#!/usr/bin/env python3
"""x402-receipt-reconciler: exactly-once reconciliation for x402 payment streams.

Pure validation tool. Reads a payment-intent JSONL stream and a receipt JSONL
stream, checks that every intent is settled by exactly one receipt, flags
duplicate payments, amount/currency mismatches and intents that never got a
receipt, and emits a reconciliation report (JSON + text summary).

No network access, no keys, no fund movement. Standard library only.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

TOOL = "x402-receipt-reconciler"
VERSION = "0.1.1"

# Receipts carrying a status string outside this set are treated as
# non-settlements (e.g. "failed", "reversed"). A missing status counts as
# settled — the conservative reading of an unqualified receipt.
SETTLED_STATUSES = {"settled", "success", "completed", "ok"}


# ---------- parsing helpers ----------

def parse_iso(value):
    """Parse an ISO-8601 timestamp; return datetime or None."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def to_decimal(value):
    """Coerce a JSON amount (int/float/str) to Decimal; None if impossible."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if isinstance(value, str):
        try:
            return Decimal(value.strip())
        except InvalidOperation:
            return None
    return None


def load_jsonl(path):
    """Load a JSONL file. Returns (records, problems). Blank lines are skipped."""
    records, problems = [], []
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        problems.append({"file": str(path), "line": None, "error": "unreadable_file", "detail": str(exc)})
        return records, problems
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError as exc:
            problems.append({"file": str(path), "line": lineno, "error": "invalid_json", "detail": exc.msg})
            continue
        if not isinstance(obj, dict):
            problems.append({"file": str(path), "line": lineno, "error": "not_an_object"})
            continue
        rec = dict(obj)
        rec["_line"] = lineno
        records.append(rec)
    return records, problems


def _problem(file_label, rec, error, **extra):
    item = {"file": file_label, "line": rec.get("_line"), "error": error}
    item.update(extra)
    return item


def clean_intent(rec, file_label):
    """Validate one intent record. Returns (clean_dict_or_None, problems)."""
    problems = []
    for key in ("intent_id", "amount", "currency"):
        if key not in rec:
            problems.append(_problem(file_label, rec, "intent_missing_field", field=key))
    if problems:
        return None, problems
    iid = rec.get("intent_id")
    if not isinstance(iid, str) or not iid:
        problems.append(_problem(file_label, rec, "intent_id_invalid", value=iid))
    amount = to_decimal(rec.get("amount"))
    if amount is None or amount <= 0:
        problems.append(_problem(file_label, rec, "intent_amount_invalid", value=rec.get("amount")))
    currency = rec.get("currency")
    if not isinstance(currency, str) or not currency.strip():
        problems.append(_problem(file_label, rec, "intent_currency_invalid", value=currency))
    if problems:
        return None, problems
    return {
        "intent_id": iid,
        "amount": amount,
        "currency": currency.strip(),
        "created_at": parse_iso(rec.get("created_at")),
        "created_at_raw": rec.get("created_at"),
        "_line": rec.get("_line"),
    }, problems


def clean_receipt(rec, file_label):
    """Validate one receipt record. Returns (clean_dict_or_None, problems)."""
    problems = []
    for key in ("receipt_id", "intent_id", "amount"):
        if key not in rec:
            problems.append(_problem(file_label, rec, "receipt_missing_field", field=key))
    if problems:
        return None, problems
    rid = rec.get("receipt_id")
    if not isinstance(rid, str) or not rid:
        problems.append(_problem(file_label, rec, "receipt_id_invalid", value=rid))
    iid = rec.get("intent_id")
    if not isinstance(iid, str) or not iid:
        problems.append(_problem(file_label, rec, "receipt_intent_id_invalid", value=iid))
    amount = to_decimal(rec.get("amount"))
    if amount is None or amount < 0:
        problems.append(_problem(file_label, rec, "receipt_amount_invalid", value=rec.get("amount")))
    currency = rec.get("currency")
    if currency is not None and not isinstance(currency, str):
        problems.append(_problem(file_label, rec, "receipt_currency_invalid", value=currency))
    if problems:
        return None, problems
    status = rec.get("status")
    settled = True
    if isinstance(status, str) and status.strip().lower() not in SETTLED_STATUSES:
        settled = False
    return {
        "receipt_id": rid,
        "intent_id": iid,
        "amount": amount,
        "currency": currency.strip() if isinstance(currency, str) else None,
        "settled": settled,
        "settled_at": parse_iso(rec.get("settled_at")),
        "settled_at_raw": rec.get("settled_at"),
        "_line": rec.get("_line"),
    }, problems


def clean_stream(records, cleaner, label):
    out, problems = [], []
    for rec in records:
        item, probs = cleaner(rec, label)
        problems.extend(probs)
        if item is not None:
            out.append(item)
    return out, problems


# ---------- reconciliation ----------

def reconcile(intents, receipts, problems=None, timeout_secs=None, now=None):
    """Core check. `intents`/`receipts` must already be cleaned records."""
    problems = list(problems or [])

    intent_groups = defaultdict(list)
    for it in intents:
        intent_groups[it["intent_id"]].append(it)
    receipt_groups = defaultdict(list)
    for rc in receipts:
        receipt_groups[rc["intent_id"]].append(rc)

    duplicate_intents = [
        {"intent_id": iid, "occurrences": len(group), "lines": [g["_line"] for g in group]}
        for iid, group in sorted(intent_groups.items()) if len(group) > 1
    ]

    reference = now
    if timeout_secs is not None and reference is None:
        settled_times = [r["settled_at"] for r in receipts if r.get("settled_at") is not None]
        reference = max(settled_times) if settled_times else None

    duplicate_receipts, missing = [], []
    amount_mismatches, currency_mismatches, matched = [], [], []

    for iid, group in sorted(intent_groups.items()):
        intent = group[0]  # first occurrence wins; duplicates are reported separately
        rgroup = receipt_groups.get(iid, [])
        if len(rgroup) > 1:
            duplicate_receipts.append({
                "intent_id": iid,
                "receipt_count": len(rgroup),
                "receipt_ids": [r["receipt_id"] for r in rgroup],
                "lines": [r["_line"] for r in rgroup],
            })
        settled = [r for r in rgroup if r["settled"]]
        if not settled:
            timed_out = None
            if timeout_secs is not None and reference is not None and intent["created_at"] is not None:
                timed_out = (reference - intent["created_at"]) > timedelta(seconds=timeout_secs)
            missing.append({
                "intent_id": iid,
                "amount": str(intent["amount"]),
                "currency": intent["currency"],
                "created_at": intent["created_at_raw"],
                "receipt_ids_seen": [r["receipt_id"] for r in rgroup],
                "timed_out": timed_out,
            })
            continue
        receipt = settled[0]
        if receipt["currency"] is not None and receipt["currency"].upper() != intent["currency"].upper():
            currency_mismatches.append({
                "intent_id": iid,
                "receipt_id": receipt["receipt_id"],
                "intent_currency": intent["currency"],
                "receipt_currency": receipt["currency"],
            })
            continue
        if receipt["amount"] != intent["amount"]:
            amount_mismatches.append({
                "intent_id": iid,
                "receipt_id": receipt["receipt_id"],
                "intent_amount": str(intent["amount"]),
                "receipt_amount": str(receipt["amount"]),
                "delta": str(receipt["amount"] - intent["amount"]),
            })
            continue
        matched.append({
            "intent_id": iid,
            "receipt_id": receipt["receipt_id"],
            "amount": str(receipt["amount"]),
            "currency": intent["currency"],
            "settled_at": receipt["settled_at_raw"],
        })

    unexpected_receipts = [
        {"receipt_id": r["receipt_id"], "intent_id": r["intent_id"], "amount": str(r["amount"]), "line": r["_line"]}
        for iid, group in sorted(receipt_groups.items()) if iid not in intent_groups for r in group
    ]

    counts = {
        "intents": len(intents),
        "receipts": len(receipts),
        "matched": len(matched),
        "missing_receipts": len(missing),
        "duplicate_intent_ids": len(duplicate_intents),
        "duplicate_receipt_intents": len(duplicate_receipts),
        "unexpected_receipts": len(unexpected_receipts),
        "amount_mismatches": len(amount_mismatches),
        "currency_mismatches": len(currency_mismatches),
        "malformed_records": len(problems),
    }
    is_reconciled = (
        counts["intents"] == counts["matched"]
        and counts["receipts"] == counts["matched"]
        and not problems
    )

    return {
        "tool": TOOL,
        "version": VERSION,
        "is_reconciled": is_reconciled,
        "verdict": "RECONCILED" if is_reconciled else "NOT_RECONCILED",
        "counts": counts,
        "matched": matched,
        "missing_receipts": missing,
        "duplicate_receipts": duplicate_receipts,
        "duplicate_intents": duplicate_intents,
        "unexpected_receipts": unexpected_receipts,
        "amount_mismatches": amount_mismatches,
        "currency_mismatches": currency_mismatches,
        "malformed_records": problems,
    }


def run(intents_path, receipts_path, timeout_secs=None, now=None):
    """Load two JSONL files end-to-end and reconcile them."""
    intent_records, problems_a = load_jsonl(intents_path)
    receipt_records, problems_b = load_jsonl(receipts_path)
    intents, extra_a = clean_stream(intent_records, clean_intent, "intents")
    receipts, extra_b = clean_stream(receipt_records, clean_receipt, "receipts")
    if isinstance(now, str):
        now = parse_iso(now)
    return reconcile(intents, receipts,
                     problems_a + extra_a + problems_b + extra_b,
                     timeout_secs, now)


# ---------- rendering ----------

def render_text(report):
    """Stable text summary of a report (goes to stderr by default)."""
    c = report["counts"]
    lines = [
        f"{TOOL} v{VERSION} - reconciliation summary",
        f"verdict: {report['verdict']}",
        f"intents={c['intents']} receipts={c['receipts']} matched={c['matched']}",
        f"missing_receipts={c['missing_receipts']} duplicate_receipt_intents={c['duplicate_receipt_intents']}",
        f"unexpected_receipts={c['unexpected_receipts']} duplicate_intent_ids={c['duplicate_intent_ids']}",
        f"amount_mismatches={c['amount_mismatches']} currency_mismatches={c['currency_mismatches']}",
        f"malformed_records={c['malformed_records']}",
    ]
    for item in report["missing_receipts"]:
        lines.append(f"MISSING   intent={item['intent_id']} amount={item['amount']} {item['currency']} timed_out={item['timed_out']}")
    for item in report["duplicate_receipts"]:
        lines.append(f"DUPLICATE intent={item['intent_id']} receipts={item['receipt_ids']}")
    for item in report["unexpected_receipts"]:
        lines.append(f"UNEXPECTED receipt={item['receipt_id']} intent={item['intent_id']}")
    for item in report["amount_mismatches"]:
        lines.append(f"AMOUNT    intent={item['intent_id']} expected={item['intent_amount']} got={item['receipt_amount']} delta={item['delta']}")
    for item in report["currency_mismatches"]:
        lines.append(f"CURRENCY  intent={item['intent_id']} expected={item['intent_currency']} got={item['receipt_currency']}")
    return "\n".join(lines)


# ---------- CLI ----------

def main(argv=None):
    parser = argparse.ArgumentParser(
        prog=TOOL,
        description="Exactly-once reconciliation for x402 payment intent/receipt JSONL streams.",
    )
    parser.add_argument("intents", help="JSONL file of payment intents")
    parser.add_argument("receipts", help="JSONL file of payment receipts")
    parser.add_argument("-o", "--output", help="write the JSON report to this file instead of stdout")
    parser.add_argument("--timeout-secs", type=float, default=None,
                        help="flag missing receipts older than this many seconds (needs parseable created_at)")
    parser.add_argument("--now", default=None,
                        help="ISO-8601 reference time for timeout checks; defaults to the newest receipt settled_at")
    parser.add_argument("--quiet", action="store_true", help="suppress the text summary on stderr")
    args = parser.parse_args(argv)

    now = parse_iso(args.now)
    if args.now and now is None:
        parser.error("--now must be an ISO-8601 timestamp")

    report = run(args.intents, args.receipts, args.timeout_secs, now)
    payload = json.dumps(report, indent=2, ensure_ascii=True)
    if args.output:
        Path(args.output).write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)
    if not args.quiet:
        print(render_text(report), file=sys.stderr)
    # Exit code discipline: 0 = reconciled, 1 = not reconciled, 2 = reserved
    # for hard tool errors (argparse failures).
    return 0 if report["is_reconciled"] else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
