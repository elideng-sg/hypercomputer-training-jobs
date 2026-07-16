# Bugfixes Log

A running record of non-obvious bugs hit while operating the AI Hypercomputer /
A3 DWS infrastructure, and how they were diagnosed and fixed. The goal is that
the *next* person (or the next you, six weeks from now) can recognize a symptom
fast and jump straight to the root cause instead of re-deriving it.

## How to use this folder

- One file per bug: `NNNN-short-kebab-title.md` (zero-padded, incrementing).
- Copy [`TEMPLATE.md`](TEMPLATE.md) to start a new entry.
- Keep the **Symptom** section skimmable — it's the index people grep.
- Record the actual commands / log lines used to diagnose, not just prose.
- Link the PR(s) that fixed it.

## Index

| ID | Title | Severity | Status | Fixed in |
|----|-------|----------|--------|----------|
| [0001](0001-dws-zone-requests-not-zone-pinned.md) | "Multi-zone" DWS requests were not actually zone-scoped | High | Fixed | [#1](https://github.com/elideng-sg/hypercomputer-training-jobs/pull/1) |
| [0002](0002-dws-a3-node-reclaimed-after-10min.md) | Provisioned A3 DWS node reclaimed ~10 min after boot | Critical | Fixed | [#2](https://github.com/elideng-sg/hypercomputer-training-jobs/pull/2) |
