#!/usr/bin/env python3
"""Self-test suite for x402-receipt-reconciler. Stdlib only, no pytest.

Three coverage families (Rules 97 logic triad):
  A. functional unit tests       - core reconciliation behaviour
  B. exception / error paths     - malformed, missing, invalid inputs
  C. extreme edge cases          - empty, unicode, huge, duplicate, out-of-order

Final line printed:  SUMMARY: PASS n/n
"""
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import reconciler as rc

HERE = Path(__file__).resolve().parent
TMP = Path(tempfile.mkdtemp(prefix="x402_selftest_"))

TESTS = []
FAILURES = []


def test(fn):
    TESTS.append(fn)
    return fn


# ---------- helpers ----------

_counter = {"n": 0}


def write_jsonl(records=None, raw_lines=None):
    _counter["n"] += 1
    path = TMP / f"s{_counter['n']}.jsonl"
    if raw_lines is not None:
        path.write_text("\n".join(raw_lines) + "\n", encoding="utf-8")
    else:
        path.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records),
            encoding="utf-8",
        )
    return str(path)


def intent(iid="i1", amount="0.05", currency="USDC", created_at=None, **kw):
    d = {"intent_id": iid, "amount": amount, "currency": currency}
    if created_at is not None:
        d["created_at"] = created_at
    d.update(kw)
    return d


def receipt(rid="r1", iid="i1", amount="0.05", currency="USDC", status=None, settled_at=None, **kw):
    d = {"receipt_id": rid, "intent_id": iid, "amount": amount, "currency": currency}
    if status is not None:
        d["status"] = status
    if settled_at is not None:
        d["settled_at"] = settled_at
    d.update(kw)
    return d


def reconcile_pairs(intents, receipts, **kw):
    return rc.run(write_jsonl(intents), write_jsonl(receipts),
                  kw.get("timeout_secs"), kw.get("now"))


def run_cli(*argv):
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    return subprocess.run(
        [sys.executable, str(HERE / "reconciler.py"), *argv],
        capture_output=True, text=True, encoding="utf-8", env=env, timeout=60,
    )


# ---------- A. functional ----------

@test
def perfect_pair_reconciles():
    r = reconcile_pairs([intent()], [receipt()])
    assert r["is_reconciled"] is True and r["verdict"] == "RECONCILED"


@test
def two_intents_both_matched():
    r = reconcile_pairs([intent("a"), intent("b")],
                        [receipt("r1", "a"), receipt("r2", "b")])
    assert r["counts"]["matched"] == 2 and r["is_reconciled"]


@test
def report_has_verdict_fields():
    r = reconcile_pairs([intent()], [receipt()])
    assert r["tool"] == "x402-receipt-reconciler" and r["version"] and "counts" in r


@test
def matched_entry_fields():
    r = reconcile_pairs([intent("i9", "1.25", "USD")], [receipt("r9", "i9", "1.25", "USD")])
    m = r["matched"][0]
    assert m == {"intent_id": "i9", "receipt_id": "r9", "amount": "1.25",
                 "currency": "USD", "settled_at": None}


@test
def duplicate_receipt_detected():
    r = reconcile_pairs([intent()], [receipt("r1"), receipt("r2")])
    assert r["counts"]["duplicate_receipt_intents"] == 1 and not r["is_reconciled"]


@test
def duplicate_receipt_ids_listed():
    r = reconcile_pairs([intent()], [receipt("r1"), receipt("r2"), receipt("r3")])
    assert r["duplicate_receipts"][0]["receipt_ids"] == ["r1", "r2", "r3"]
    assert r["duplicate_receipts"][0]["receipt_count"] == 3


@test
def missing_receipt_listed():
    r = reconcile_pairs([intent("ghost")], [])
    assert r["counts"]["missing_receipts"] == 1
    assert r["missing_receipts"][0]["intent_id"] == "ghost"
    assert not r["is_reconciled"]


@test
def unexpected_receipt_listed():
    r = reconcile_pairs([], [receipt("r1", "no-such-intent")])
    assert r["counts"]["unexpected_receipts"] == 1
    assert r["unexpected_receipts"][0]["intent_id"] == "no-such-intent"


@test
def amount_mismatch_with_delta():
    r = reconcile_pairs([intent("i1", "0.05")], [receipt("r1", "i1", "0.50")])
    assert r["counts"]["amount_mismatches"] == 1
    assert r["amount_mismatches"][0]["delta"] == "0.45"
    assert r["amount_mismatches"][0]["intent_amount"] == "0.05"


@test
def currency_mismatch_detected():
    r = reconcile_pairs([intent("i1", currency="USD")], [receipt("r1", "i1", currency="EUR")])
    assert r["counts"]["currency_mismatches"] == 1
    assert r["currency_mismatches"][0]["receipt_currency"] == "EUR"


@test
def duplicate_intent_stream_detected():
    r = reconcile_pairs([intent("dup"), intent("dup", amount="0.99")], [receipt("r1", "dup")])
    assert r["counts"]["duplicate_intent_ids"] == 1
    assert r["duplicate_intents"][0]["occurrences"] == 2
    assert len(r["duplicate_intents"][0]["lines"]) == 2


@test
def text_verdict_reconciled():
    r = reconcile_pairs([intent()], [receipt()])
    assert "RECONCILED" in rc.render_text(r)


@test
def text_verdict_not_reconciled():
    r = reconcile_pairs([intent()], [])
    assert "NOT_RECONCILED" in rc.render_text(r)


@test
def text_lists_missing_line():
    r = reconcile_pairs([intent("ghost", "0.10")], [])
    assert "MISSING" in rc.render_text(r) and "ghost" in rc.render_text(r)


@test
def malformed_invalid_json_counted():
    ip, rp = write_jsonl([intent()]), write_jsonl(raw_lines=["{oops", json.dumps(receipt())])
    r = rc.run(ip, rp)
    assert r["counts"]["malformed_records"] == 1
    assert r["malformed_records"][0]["error"] == "invalid_json"


@test
def malformed_non_object_counted():
    rp = write_jsonl(raw_lines=["123", json.dumps(receipt())])
    r = rc.run(write_jsonl([intent()]), rp)
    assert r["malformed_records"][0]["error"] == "not_an_object"


@test
def intent_missing_field_counted():
    bad = {"intent_id": "i1", "currency": "USDC"}  # no amount
    r = reconcile_pairs([bad], [receipt()])
    assert any(p["error"] == "intent_missing_field" and p["field"] == "amount"
               for p in r["malformed_records"])


@test
def receipt_missing_field_counted():
    bad = {"receipt_id": "r1", "amount": "0.05"}  # no intent_id
    r = reconcile_pairs([intent()], [bad])
    assert any(p["error"] == "receipt_missing_field" and p["field"] == "intent_id"
               for p in r["malformed_records"])


@test
def failed_status_counts_missing():
    r = reconcile_pairs([intent()], [receipt(status="failed")])
    assert r["counts"]["missing_receipts"] == 1
    assert r["missing_receipts"][0]["receipt_ids_seen"] == ["r1"]


@test
def status_success_counts_settled():
    r = reconcile_pairs([intent()], [receipt(status="success")])
    assert r["is_reconciled"]


@test
def status_case_insensitive():
    r = reconcile_pairs([intent()], [receipt(status="Settled")])
    assert r["is_reconciled"]


@test
def amount_string_vs_number_match():
    r = reconcile_pairs([intent("i1", "0.1")], [receipt("r1", "i1", 0.1)])
    assert r["is_reconciled"]


@test
def scientific_notation_match():
    r = reconcile_pairs([intent("i1", "1e-2")], [receipt("r1", "i1", 0.01)])
    assert r["is_reconciled"]


@test
def float_precision_match():
    r = reconcile_pairs([intent("i1", 0.1)], [receipt("r1", "i1", 0.1)])
    assert r["is_reconciled"]


@test
def currency_case_insensitive():
    r = reconcile_pairs([intent("i1", currency="usd")], [receipt("r1", "i1", currency="USD")])
    assert r["is_reconciled"]


@test
def currency_whitespace_trimmed():
    r = reconcile_pairs([intent("i1", currency=" USD ")], [receipt("r1", "i1", currency="USD")])
    assert r["is_reconciled"]


@test
def unknown_fields_tolerated():
    r = reconcile_pairs([intent("i1", memo="hello", scheme="exact")],
                        [receipt("r1", "i1", note="x", network="base")])
    assert r["is_reconciled"]


@test
def blank_lines_skipped():
    ip = write_jsonl(raw_lines=["", json.dumps(intent()), "", ""])
    rp = write_jsonl(raw_lines=["", json.dumps(receipt()), ""])
    assert rc.run(ip, rp)["is_reconciled"]


@test
def out_of_order_streams_match():
    # receipt appears "before" its intent across/inside files; grouping is id-based
    rp = write_jsonl([receipt("r2", "b"), receipt("r1", "a")])
    ip = write_jsonl([intent("b"), intent("a")])
    r = rc.run(ip, rp)
    assert r["is_reconciled"] and r["counts"]["matched"] == 2


@test
def hundred_pair_stream_reconciles():
    ints = [intent(f"i{n}", "0.01") for n in range(100)]
    recs = [receipt(f"r{n}", f"i{n}", "0.01") for n in range(100)]
    r = reconcile_pairs(ints, recs)
    assert r["is_reconciled"] and r["counts"]["matched"] == 100


@test
def hundred_pair_one_duplicate_spotted():
    ints = [intent(f"i{n}", "0.01") for n in range(100)]
    recs = [receipt(f"r{n}", f"i{n}", "0.01") for n in range(100)] + [receipt("rx", "i7", "0.01")]
    r = reconcile_pairs(ints, recs)
    assert r["duplicate_receipts"][0]["intent_id"] == "i7" and not r["is_reconciled"]


@test
def timeout_flagged_old_intent():
    r = reconcile_pairs([intent("i1", created_at="2026-01-01T00:00:00+00:00")], [receipt("r1", "i1", status="failed")],
                        timeout_secs=60, now="2026-01-02T00:00:00+00:00")
    assert r["missing_receipts"][0]["timed_out"] is True


@test
def timeout_not_flagged_recent():
    recent = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    r = reconcile_pairs([intent("i1", created_at=recent)], [receipt("r1", "i1", status="failed")],
                        timeout_secs=3600, now=datetime.now(timezone.utc).isoformat())
    assert r["missing_receipts"][0]["timed_out"] is False


@test
def timeout_unknown_without_timestamps():
    r = reconcile_pairs([intent("i1")], [receipt("r1", "i1", status="failed")], timeout_secs=60)
    assert r["missing_receipts"][0]["timed_out"] is None


@test
def timeout_defaults_to_max_settled():
    r = reconcile_pairs([intent("i1", created_at="2020-01-01T00:00:00+00:00")],
                        [receipt("r1", "i1", status="failed", settled_at="2026-01-01T00:00:00+00:00")],
                        timeout_secs=60)
    assert r["missing_receipts"][0]["timed_out"] is True


@test
def cli_exit_zero():
    ip, rp = write_jsonl([intent()]), write_jsonl([receipt()])
    assert run_cli(ip, rp, "--quiet").returncode == 0


@test
def cli_exit_one():
    ip, rp = write_jsonl([intent()]), write_jsonl([])
    assert run_cli(ip, rp, "--quiet").returncode == 1


@test
def cli_output_json_file():
    ip, rp = write_jsonl([intent()]), write_jsonl([receipt()])
    out = str(TMP / "cli_report.json")
    proc = run_cli(ip, rp, "-o", out, "--quiet")
    assert proc.returncode == 0
    loaded = json.loads(Path(out).read_text(encoding="utf-8"))
    assert loaded["verdict"] == "RECONCILED"


@test
def cli_text_summary_on_stderr():
    ip, rp = write_jsonl([intent("gx")]), write_jsonl([])
    proc = run_cli(ip, rp)
    assert "NOT_RECONCILED" in proc.stderr and "gx" in proc.stderr


# ---------- B/C. exception paths + extreme edges ----------

@test
def empty_streams_vacuously_reconciled():
    r = reconcile_pairs([], [])
    assert r["is_reconciled"] and r["counts"]["intents"] == 0


@test
def report_json_serializable():
    r = reconcile_pairs([intent("u1")], [receipt("v1", "u1")])
    json.dumps(r)  # must not raise


@test
def missing_intents_file_problem():
    rp = write_jsonl([receipt()])
    r = rc.run(str(TMP / "definitely_missing.jsonl"), rp)
    assert r["malformed_records"][0]["error"] == "unreadable_file"
    assert not r["is_reconciled"]


@test
def missing_both_files_zero_counts():
    r = rc.run(str(TMP / "no1.jsonl"), str(TMP / "no2.jsonl"))
    assert r["counts"]["intents"] == 0 and r["counts"]["receipts"] == 0


@test
def bad_amount_string_flagged():
    r = reconcile_pairs([intent("i1", amount="abc")], [])
    assert any(p["error"] == "intent_amount_invalid" for p in r["malformed_records"])


@test
def negative_amount_flagged():
    r = reconcile_pairs([intent("i1", amount=-1)], [])
    assert any(p["error"] == "intent_amount_invalid" for p in r["malformed_records"])


@test
def zero_amount_intent_flagged():
    r = reconcile_pairs([intent("i1", amount=0)], [])
    assert any(p["error"] == "intent_amount_invalid" for p in r["malformed_records"])


@test
def boolean_amount_flagged():
    r = reconcile_pairs([intent("i1", amount=True)], [])
    assert any(p["error"] == "intent_amount_invalid" for p in r["malformed_records"])


@test
def null_amount_flagged():
    r = reconcile_pairs([intent("i1", amount=None)], [])
    assert any(p["error"] == "intent_amount_invalid" for p in r["malformed_records"])


@test
def numeric_intent_id_flagged():
    r = reconcile_pairs([intent(123)], [])
    assert any(p["error"] == "intent_id_invalid" for p in r["malformed_records"])


@test
def empty_intent_id_flagged():
    r = reconcile_pairs([intent("")], [])
    assert any(p["error"] == "intent_id_invalid" for p in r["malformed_records"])


@test
def unicode_intent_id_matches():
    r = reconcile_pairs([intent("支付-01", "0.05")], [receipt("回执-1", "支付-01", "0.05")])
    assert r["is_reconciled"] and r["matched"][0]["intent_id"] == "支付-01"


@test
def emoji_receipt_id_matches():
    r = reconcile_pairs([intent("i1")], [receipt("🧾-001", "i1")])
    assert r["is_reconciled"]


@test
def long_id_10k_chars():
    big = "x" * 10000
    r = reconcile_pairs([intent(big)], [receipt("r1", big)])
    assert r["is_reconciled"]


@test
def five_duplicate_receipts_counted():
    recs = [receipt(f"r{n}", "i1") for n in range(5)]
    r = reconcile_pairs([intent("i1")], recs)
    assert r["duplicate_receipts"][0]["receipt_count"] == 5


@test
def currency_missing_on_receipt_skips_check():
    r = reconcile_pairs([intent("i1", currency="USDC")],
                        [receipt("r1", "i1", currency=None)])
    assert r["is_reconciled"]


@test
def negative_receipt_amount_flagged():
    r = reconcile_pairs([intent()], [receipt("r1", "i1", amount=-0.05)])
    assert any(p["error"] == "receipt_amount_invalid" for p in r["malformed_records"])


@test
def non_string_receipt_currency_flagged():
    r = reconcile_pairs([intent()], [receipt("r1", "i1", currency=42)])
    assert any(p["error"] == "receipt_currency_invalid" for p in r["malformed_records"])


@test
def problem_records_carry_line_numbers():
    rp = write_jsonl(raw_lines=[json.dumps(receipt()), "{broken"])
    r = rc.run(write_jsonl([intent()]), rp)
    assert r["malformed_records"][0]["line"] == 2


@test
def malformed_in_both_streams_counted_separately():
    ip = write_jsonl(raw_lines=["{a"])
    rp = write_jsonl(raw_lines=["{b"])
    r = rc.run(ip, rp)
    files = {p["file"] for p in r["malformed_records"]}
    assert len(files) == 2 and r["counts"]["malformed_records"] == 2


@test
def mixed_everything_still_runs():
    ip = write_jsonl(raw_lines=["{bad", json.dumps(intent("ok")), json.dumps(intent())])
    rp = write_jsonl(raw_lines=[json.dumps(receipt("r1", "ok")), "null"])
    r = rc.run(ip, rp)
    assert r["counts"]["matched"] == 1 and not r["is_reconciled"]


def main():
    for fn in TESTS:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - report and continue
            FAILURES.append((fn.__name__, repr(exc)))
    for name, err in FAILURES:
        print(f"FAIL {name}: {err}")
    print(f"tests={len(TESTS)} failures={len(FAILURES)}")
    print(f"SUMMARY: PASS {len(TESTS) - len(FAILURES)}/{len(TESTS)}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
