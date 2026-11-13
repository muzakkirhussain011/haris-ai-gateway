# MS MARCO, a million chunks, bge-small-en-v1.5, Qdrant: load benchmark results

Run on 2026-11-08 with [`kaggle_stores.ipynb`](../kaggle_stores.ipynb) (`STORE = "qdrant"`) at
commit `1d8348e` (Kaggle version 1 of the notebook, 2,100 s in total, of which 937 s reading and
embedding). The run embedded the passages and questions itself, with the same model as the
[pgvector run](msmarco-1m-bge-small.md), and drew the questions and permissions from the same seed,
so both stores were measured on the same million passages, questions and permissions.

| | |
|---|---|
| Corpus | The first 1,000,000 passages of BEIR MS MARCO; 300 MS MARCO questions picked at random |
| Embeddings | BAAI/bge-small-en-v1.5, 384 dimensions; queries carry the model's retrieval instruction |
| Permissions | Random layout: 1,000 teams, 100 departments, 10 divisions; selectivity 0.1%, 1%, 10%, 100% |
| Retrieval | k = 10, `max_candidates` = 1000, `overfetch` = 4 |
| Store | Qdrant 1.19.2 from its release build; collection made by `create_collection` (cosine, keyword payload indexes on the permission fields), loaded through `QdrantBackend`, at Qdrant's default HNSW and search settings |
| Load | Throughput runs: 20 s per setting, users pre-filtering at 1% readable, cycling through the 300 questions |
| Hardware | Kaggle, 4 CPUs, shared by Qdrant and the benchmark's client threads; Tesla T4 for the embeddings only |
| Software | Python 3.13.15, wardowl at `1d8348e`, NumPy 2.1.3, sentence-transformers 5.7.0, PyTorch 2.11.0 |

Recall@10 is measured against the exact top 10 among the chunks the subject may read, computed by
brute force over the same vectors. *Leaks* is the total number of returned chunks the subject may
not read, over 300 queries of 10 results. The throughput runs count errors; they do not check
results.

### Load: 1000000 chunks in qdrant

Loaded in 405.7 s, then 418.8 s until the index was built. 1000000 points, 1000000 indexed vectors. Search breadth: the store default.

### Recall and latency by selectivity (one query at a time)

| Selectivity | Strategy | Recall@k | Complete | p50 ms | p95 ms | Leaks |
|---|---|---|---|---|---|---|
| 0.1% | prefilter | 0.998 | 100% | 3.25 | 4.39 | 0 |
| 0.1% | postfilter | 0.094 | 0% | 87.1 | 105.0 | 0 |
| 1.0% | prefilter | 0.996 | 100% | 4.86 | 7.16 | 0 |
| 1.0% | postfilter | 0.855 | 55% | 78.95 | 87.43 | 0 |
| 10.0% | prefilter | 0.983 | 100% | 4.45 | 6.41 | 0 |
| 10.0% | postfilter | 0.959 | 100% | 13.99 | 36.19 | 0 |
| 100.0% | prefilter | 0.972 | 100% | 4.89 | 6.67 | 0 |
| 100.0% | postfilter | 0.972 | 100% | 4.82 | 5.45 | 0 |

### Throughput with concurrent users

| Selectivity | Users | Requests/s | p50 ms | p95 ms | p99 ms | Errors |
|---|---|---|---|---|---|---|
| 1.0% | 1 | 209.5 | 4.65 | 5.58 | 6.32 | 0 |
| 1.0% | 2 | 274.6 | 7.24 | 9.01 | 10.13 | 0 |
| 1.0% | 4 | 305.5 | 12.83 | 18.53 | 21.12 | 0 |
| 1.0% | 8 | 311.5 | 25.07 | 38.89 | 44.83 | 0 |
| 1.0% | 16 | 312.8 | 50.96 | 67.26 | 77.89 | 0 |
