# x402-receipt-reconciler

Two JSONL streams in — payment intents and payment receipts — one verdict out: was every intent settled exactly once? This is a pure-validation audit tool for x402-style agent payments. No network, no keys, no fund movement. Use it when you must prove your payment logs agree with themselves.

## Problem

Agent-payment stacks (x402-style HTTP 402 flows) produce *payment intents* in one component and *settlement receipts* in another. Between them, things go wrong quietly:

- the same intent gets settled twice (duplicate payment),
- an intent never gets settled at all (silent loss),
- a receipt settles the right intent for the wrong amount or currency,
- a receipt arrives for an intent nobody issued.

Every component involved has an incentive to believe it is fine. There is usually no offline, dependency-free auditor whose only job is to diff the two streams and say so.

## How it works

```
intents.jsonl ─┐
               ├─► load & validate ─► group by intent_id ─► checks ─► report (JSON + text)
receipts.jsonl ┘
```

Checks performed:

1. **Exactly-once** — every valid intent must have exactly one settled receipt.
2. **Duplicate payment detection** — the same `intent_id` settled by more than one receipt (including one settled + one failed: still reported as a duplicate *attempt* and a missing settlement).
3. **Amount consistency** — receipt amount must equal intent amount exactly (decimal arithmetic via `Decimal`, so `0.1` float and `"0.1"` string compare correctly; scientific notation works).
4. **Currency consistency** — case-insensitive, whitespace-trimmed comparison.
5. **Missing receipts / timeout list** — intents with no settled receipt, optionally flagged `timed_out` against `--timeout-secs`.
6. **Unexpected receipts** — receipts referencing intents that do not exist.
7. **Stream hygiene** — duplicate `intent_id`s inside the intents stream, malformed JSON lines, non-object lines, records with missing/invalid fields; every problem carries file + line number.

Output is a JSON report (`verdict`, `counts`, and per-finding lists) plus a stable one-line-per-finding text summary on stderr. Exit codes: `0` reconciled, `1` not reconciled, `2` reserved for hard tool errors.

## Quickstart (~2 min)

```bash
# 1. two tiny streams (~10 s)
cat > intents.jsonl <<'EOF'
{"intent_id": "pay-001", "amount": "0.05", "currency": "USDC", "created_at": "2026-10-11T10:00:00Z"}
{"intent_id": "pay-002", "amount": "0.10", "currency": "USDC"}
EOF
cat > receipts.jsonl <<'EOF'
{"receipt_id": "rcp-001", "intent_id": "pay-001", "amount": "0.05", "currency": "USDC", "settled_at": "2026-10-11T10:00:02Z"}
{"receipt_id": "rcp-002", "intent_id": "pay-001", "amount": "0.05", "currency": "USDC"}
EOF

# 2. reconcile (~5 s)
python reconciler.py intents.jsonl receipts.jsonl
# exit code 1 (pay-001 settled twice, pay-002 has no receipt)

# 3. run the self-test suite (60 cases, ~10 s)
python self_test.py
# SUMMARY: PASS 60/60
```

With timeout detection: `python reconciler.py intents.jsonl receipts.jsonl --timeout-secs 300 --now 2026-10-11T10:05:00Z`.

## why-not-X

| Alternative | What it is | Why not this job |
|---|---|---|
| harsha-moparthy/payper (0★, as of 2026-10-11) | A payment-collecting API: it *is* the rail that accepts payments | Builds the payment path, does not audit intent/receipt streams after the fact; also a live service, not an offline checker |
| Custena/agent-payment-protocols (10★, as of 2026-10-11) | A landscape/survey document comparing agent payment protocols | Explains protocols including x402; it is a document, not an executable reconciliation tool |
| Double-entry ledger engines | Authoritative bookkeeping systems | Require instrumenting the write path and owning the ledger; useless for auditing logs you already have |
| jq/grep one-liners over the JSONL | Ad-hoc scripting | No exactly-once semantics, no decimal-exact amount comparison, no per-line problem attribution; breaks on the first duplicate |

## How this differs

- **vs payper**: payper implements the receiving side of the payment flow (the API that gets paid). This repo touches zero payment endpoints — it consumes the two JSONL artifacts both sides already emit and audits their agreement. Adjacent mechanism, different layer: they are the rail, this is the auditor. [High confidence — mechanism verified from the anchor's own description, 2026-10-11]
- **vs agent-payment-protocols**: that repo surveys and compares protocols; it cannot be pointed at your data. This repo has no opinion about protocols and one function: diff two streams, emit findings. [High confidence]

## Minimal Fallback

- No pip, no venv, no config: copy the single `reconciler.py` file anywhere with Python ≥3.8 and run it. Zero third-party imports.
- No Python at all: the JSON report schema below is stable and documented — any reviewer can re-implement the checks, and the text summary is one `grep 'MISSING\|DUPLICATE\|AMOUNT'` away from being readable.
- Only one of the two streams? Run with an empty file: all intents land in `missing_receipts` (or all receipts in `unexpected_receipts`), which is itself a useful inventory.

Report skeleton: `{"verdict": "RECONCILED" | "NOT_RECONCILED", "counts": {...}, "matched": [...], "missing_receipts": [...], "duplicate_receipts": [...], "duplicate_intents": [...], "unexpected_receipts": [...], "amount_mismatches": [...], "currency_mismatches": [...], "malformed_records": [...]}`.

## Design Trade-offs

- **Stdlib-only over pandas/arrow**: reconciliation volumes are log-sized, not warehouse-sized; the install-friction saving is worth more than raw throughput.
- **`Decimal` over float**: money. Float `0.1+0.2 != 0.3` is a false-mismatch generator.
- **Report, don't resolve**: on duplicates the first settled receipt wins for matching and the conflict is reported; the tool never decides which payment is "real".
- **Permissive status vocabulary**: `settled/success/completed/ok` count as settlement, anything else (e.g. `failed`) does not; a missing status counts as settled. The set is one constant (`SETTLED_STATUSES`) if you need to change it.
- **First occurrence wins for duplicate intents**: the duplicate is reported as a finding rather than rejecting the whole stream.

## Limits & Second-order Effects

- **Offline only**: it audits files you hand it. It cannot see on-chain state, issuer-side truth, or receipts that were never written to the stream. A green report means "the two streams agree", not "the money moved".
- **Shared upstream blindness**: if both streams are generated by the same buggy component, they can agree perfectly and both be wrong. Reconciliation green ≠ books correct.
- **Adoption side-effect**: once a team trusts this report, there is a temptation to skip proper idempotency keys and "let reconciliation catch it". Exactly-once checking is a safety net, not a substitute for idempotent settlement.
- **Timeout heuristic**: `timed_out` depends on parseable ISO-8601 `created_at`/`settled_at` values and a reference time; missing timestamps degrade to `timed_out: null` rather than a guess. [Medium confidence that the default reference (newest receipt `settled_at`) is right for most setups — supply `--now` if yours differs.]

## FAQ

**Does it move money or call any endpoint?** No. It reads two files and writes one report. There is no network code in the repository.

**What counts as "exactly once"?** Exactly one *settled* receipt per valid intent id. Extra receipts — settled or not — are duplicates; zero settled receipts is a missing settlement.

**My amounts are integers of atomic units.** Fine — integers pass through `Decimal` unchanged.

**Can I feed it millions of lines?** It loads both files into memory; log-scale (≤ a few million records) is fine, warehouse-scale is not the design target. [estimated]

**When NOT to use this?**
- As an authoritative ledger or source of financial truth — it is an auditor, not a system of record.
- For real-time fraud detection — it is offline and batch.
- For dispute arbitration between two parties — it reports discrepancies, it does not adjudicate them.
- If your payment network is not intent/receipt-shaped at all, the input contract will fight you.

## Core Assumptions

1. Amounts are representable as exact decimals (JSON numbers or their string forms); atomic-unit integers are exact decimals too.
2. The two JSONL files are complete exports for the period being audited — the tool cannot detect records that were never logged.
3. `intent_id` is the join key and identifiers are strings; a repeated `intent_id` in the intents stream is a defect to report, not a new intent.
4. Timestamps, when present, are ISO-8601; unparseable timestamps degrade features instead of failing the run.
5. The settlement status vocabulary is the `SETTLED_STATUSES` constant; teams with different vocabularies edit one line.

---
Data/status claims about external repositories are as of 2026-10-11. Self-test evidence: `python self_test.py` → `SUMMARY: PASS 60/60`.
