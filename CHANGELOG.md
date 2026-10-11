# Changelog — x402-receipt-reconciler

All notable changes are documented here. Dates are build dates, not release dates.

## [0.1.1] — 2026-10-11 (self-review revision, iteration 2)

Round-2 self-review of the v0.1.0 draft found and fixed the following:

- **Fixed**: `run()` dropped load-time problems from the receipts stream
  (`problems_b` was never merged into the problem list), so malformed receipt
  lines were silently uncounted. Caught by new test
  `malformed_in_both_streams_counted_separately`.
- **Fixed**: `--now` given as an ISO-8601 string was not parsed when calling
  the library API directly; parsing now happens inside `run()`.
- **Improved**: `amount_mismatches` entries now carry a `delta` field
  (receipt − intent) so reviewers see the discrepancy size without
  recomputing it.
- **Improved**: exit-code discipline documented and tested (0 reconciled /
  1 not reconciled / 2 reserved for hard tool errors).
- **Tests**: 40 → 60 cases (+50%), adding the exception/edge families:
  unicode and emoji identifiers, 10k-char ids, boolean/null/negative/zero
  amounts, scientific-notation amounts, duplicate handling at 5× scale,
  100-pair streams, timeout variants, blank-line and mixed-stream garbage.
- **Docs**: added "How this differs" section with anchor facts
  (harsha-moparthy/payper, Custena/agent-payment-protocols) and expanded
  Limits & Second-order Effects.

## [0.1.0] — 2026-10-11 (initial draft, iteration 1)

- Core reconciliation engine: exactly-once validation, duplicate payment
  detection, amount/currency consistency, missing-receipt (timeout) list,
  unexpected-receipt detection, duplicate-intent detection, per-line problem
  attribution.
- JSONL loader with graceful malformed-line handling; JSON report + stable
  text summary; argparse CLI (`--output`, `--timeout-secs`, `--now`,
  `--quiet`).
- 40 self-test cases across functional / exception / edge families.
- README, MIT LICENSE, initial test suite.
