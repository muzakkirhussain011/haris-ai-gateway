# FiQA: wardowl on pgvector against PostgreSQL row-level security

Run on 2026-11-07 with [`kaggle_benchmark.ipynb`](../kaggle_benchmark.ipynb) at commit `42fcca6`
(Kaggle version 1 of the notebook, 4,217 s in total, of which 1,384 s embedding), on the corpus,
embeddings, permissions and hardware of the [full FiQA results](fiqa-qwen3-embedding-0.6b.md). That
run's other rows repeated the full run's: recall within 0.04 in every row, and within 0.007 except
for pgvector without iterative scans; only the new baseline is listed here.

*Row-level security* enforces the same permissions inside PostgreSQL instead of in wardowl: a
policy on the table (`metadata -> 'allowed_groups' ?| ` the subject's groups, set per transaction),
a GIN index on the groups, the same HNSW index with iterative scans, and searches run as a role
without `BYPASSRLS`. Nothing is checked outside the database, so *Examined* does not apply.
*Complete* is the share of the 200 queries that returned 10 results. Leaks were 0 in every row.

| Layout | Readable | wardowl prefilter: recall | p50 ms | Row-level security: recall | Complete | p50 ms | p95 ms |
|---|---|---|---|---|---|---|---|
| random | 0.10% | 1.000 | 3.4 | 1.000 | 100% | 3.0 | 3.6 |
| random | 1.00% | 1.000 | 10.2 | 1.000 | 100% | 9.9 | 12.0 |
| random | 5.01% | 0.974 | 12.2 | 1.000 | 100% | 38.9 | 42.5 |
| random | 20.00% | 0.965 | 6.5 | 0.998 | 100% | 153.6 | 161.7 |
| random | 100.00% | 0.979 | 5.3 | 0.998 | 100% | 545.4 | 570.1 |
| aligned | 0.14% | 0.999 | 3.2 | 0.999 | 99% | 2.9 | 4.3 |
| aligned | 1.08% | 0.999 | 10.2 | 0.999 | 100% | 9.0 | 13.0 |
| aligned | 5.21% | 0.977 | 5.7 | 0.999 | 100% | 38.6 | 47.3 |
| aligned | 20.36% | 0.971 | 4.7 | 0.998 | 100% | 155.2 | 166.2 |
| aligned | 100.00% | 0.979 | 5.1 | 0.998 | 100% | 549.9 | 576.9 |
| misaligned | 0.06% | 1.000 | 2.6 | 1.000 | 92% | 1.9 | 3.0 |
| misaligned | 0.92% | 0.984 | 9.9 | 0.999 | 100% | 7.7 | 12.6 |
| misaligned | 4.78% | 0.950 | 83.1 | 1.000 | 100% | 32.1 | 42.5 |
| misaligned | 19.89% | 0.950 | 10.9 | 0.998 | 100% | 160.5 | 179.2 |
| misaligned | 100.00% | 0.979 | 5.2 | 0.998 | 100% | 556.1 | 580.5 |

The query plans were not captured. Recall near 1.0 at every selectivity, above the HNSW index's own
0.979 when everything is readable, and latency that grows with the number of readable rows, are
what an exact scan of the rows the policy admits would produce, rather than a walk of the HNSW
index.
