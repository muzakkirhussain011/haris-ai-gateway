# MS MARCO, a million chunks, bge-small-en-v1.5, LanceDB: load benchmark results

Run on 2026-11-08 with [`kaggle_stores.ipynb`](../kaggle_stores.ipynb) and its `lancedb` plan, at
commit `0f4a182` (Kaggle version 1 of the notebook, 1,435 s in total, 1,320 s of it for the
benchmark itself, of which 941 s reading and embedding). The questions and permissions are the same
as in the [run on every store](msmarco-1m-every-store.md); the embeddings, of the same passages with
the same model, were computed in this run.
LanceDB is embedded, so it ran inside the benchmark's own process, on a different machine and in a
different session from the other stores: compare its numbers with theirs only loosely.

| | |
|---|---|
| Corpus | The first 1,000,000 passages of BEIR MS MARCO; 300 MS MARCO questions picked at random |
| Embeddings | BAAI/bge-small-en-v1.5, 384 dimensions; queries carry the model's retrieval instruction |
| Permissions | Random layout: 1,000 teams, 100 departments, 10 divisions; selectivity 0.1%, 1%, 10%, 100% |
| Retrieval | k = 10, `max_candidates` = 1000, `overfetch` = 4 |
| Store | LanceDB (the `lancedb` extra, 0.40 or later), embedded, on local disk. The backend's row layout bulk-loaded with `table.add` in batches of 100,000, then `create_indexes` (the `LABEL_LIST` index on the permission terms) and LanceDB's default vector index, IVF_PQ with cosine distance; `nprobes` and `refine_factor` at their defaults |
| Load | Throughput runs: 20 s per setting, users pre-filtering at 1% readable, cycling through the 300 questions |
| Hardware | Kaggle, 4 CPUs, shared by LanceDB and the benchmark's client threads; Tesla T4 for the embeddings only |
| Software | Python 3.13.15, wardowl at `0f4a182`, NumPy 2.1.3, sentence-transformers 5.7.0, PyTorch 2.11.0 |

Recall@10 is measured against the exact top 10 among the chunks the subject may read, computed by
brute force over the same vectors. *Leaks* is the total number of returned chunks the subject may
not read, over 300 queries of 10 results. The throughput runs count errors; they do not check
results.

## Summary

- **No retrieval leaked**, pre- or post-filtered, and no request failed under load.
- **Recall is about half at LanceDB's defaults, at every share readable** (0.479 to 0.512
  pre-filtered). The default index quantizes vectors (product quantization) and, without
  `refine_factor`, ranks by the quantized distances. Pre-filtering itself costs nothing here:
  recall at 0.1% readable (0.512) is no lower than with no filter (0.506).
  `LanceDBBackend(..., refine_factor=...)` re-ranks with the full vectors and `nprobes=` searches
  more partitions; both trade latency for recall and are not measured here yet.
- **Loading is fast in bulk**: 14 s for the million chunks with `table.add`, then 51 s for the
  indexes.
- **Throughput peaked at 161 requests a second** with 16 users (p99 152 ms), with LanceDB running
  inside the client process.

## Load: 1000000 chunks in lancedb

Loaded in 13.8 s, then 50.7 s until the index was built. Bulk-loaded with table.add; LanceDB's default vector index (IVF_PQ), cosine. Search breadth: the store default.

## Recall and latency by selectivity (one query at a time)

| Selectivity | Strategy | Recall@k | Complete | p50 ms | p95 ms | Leaks |
|---|---|---|---|---|---|---|
| 0.1% | prefilter | 0.512 | 100% | 10.46 | 23.85 | 0 |
| 0.1% | postfilter | 0.085 | 0% | 95.45 | 137.19 | 0 |
| 1.0% | prefilter | 0.479 | 100% | 11.44 | 29.77 | 0 |
| 1.0% | postfilter | 0.451 | 55% | 87.81 | 139.4 | 0 |
| 10.0% | prefilter | 0.487 | 100% | 11.45 | 27.9 | 0 |
| 10.0% | postfilter | 0.488 | 100% | 31.11 | 53.57 | 0 |
| 100.0% | prefilter | 0.506 | 100% | 15.08 | 46.28 | 0 |
| 100.0% | postfilter | 0.508 | 100% | 12.47 | 23.32 | 0 |

## Throughput with concurrent users

| Selectivity | Users | Requests/s | p50 ms | p95 ms | p99 ms | Errors |
|---|---|---|---|---|---|---|
| 1.0% | 1 | 70.5 | 11.41 | 28.83 | 31.27 | 0 |
| 1.0% | 2 | 111.1 | 14.88 | 35.72 | 41.56 | 0 |
| 1.0% | 4 | 139.4 | 25.82 | 49.38 | 57.92 | 0 |
| 1.0% | 8 | 153.8 | 49.27 | 76.86 | 95.77 | 0 |
| 1.0% | 16 | 160.9 | 97.01 | 131.46 | 152.46 | 0 |
