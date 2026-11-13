# MS MARCO, a million chunks, bge-small-en-v1.5, six stores: load benchmark results

Run on 2026-11-08 with [`kaggle_stores.ipynb`](../kaggle_stores.ipynb) (plan `all-stores`, Kaggle
version 1 of the notebook, 10,138 s in total) at commit `e3d3730`, on main with the 1.4.0 code. The
stores took 2,265 s (Qdrant, the first, including reading and embedding the corpus), 1,681 s
(Elasticsearch), 2,083 s (OpenSearch), 1,597 s (Weaviate), 721 s (LanceDB) and 1,071 s (Redis).
The six stores ran one after another in one session, on the same machine, so their numbers compare
directly; each was stopped and its data deleted before the next started. The session embedded the
passages and questions once, with the same model as the [pgvector run](msmarco-1m-bge-small.md),
and loaded every store with those vectors, so every store was measured on the same million vectors,
queries and permissions.

| | |
|---|---|
| Corpus | The first 1,000,000 passages of BEIR MS MARCO; 300 MS MARCO questions picked at random |
| Embeddings | BAAI/bge-small-en-v1.5, 384 dimensions; queries carry the model's retrieval instruction |
| Permissions | Random layout: 1,000 teams, 100 departments, 10 divisions; selectivity 0.1%, 1%, 10%, 100% |
| Retrieval | k = 10, `max_candidates` = 1000, `overfetch` = 4 |
| Stores | Release builds, each at its own defaults and loaded through its wardowl backend: Qdrant 1.19.2, Elasticsearch 9.5.5 (default vector index for 384 dimensions: `bbq_hnsw`, 1-bit quantization), OpenSearch 3.9.0, Weaviate 1.39.12, LanceDB (the `lancedb` extra, 0.40 or later; the installed version was not recorded; embedded; default IVF_PQ index), Redis 8.10.3 (HNSW, M 16, EF_CONSTRUCTION 200) |
| Load | Throughput: users pre-filtering at 1% readable, cycling through the 300 questions; each setting run 3 times for 20 s; every returned chunk checked against the subject's permissions |
| Hardware | Kaggle, 4 CPUs, shared by the store and the benchmark's client threads; Tesla T4 for the embeddings only |
| Software | Python 3.13.15, NumPy 2.1.3, sentence-transformers 5.7.0, PyTorch 2.11.0 |

Recall@10 is measured against the exact top 10 among the chunks the subject may read, computed by
brute force over the same vectors. *Leaks* is the number of returned chunks the subject may not read.
In the throughput tables each row is the median of the 3 runs, with the slowest and fastest run in
brackets; errors, chunks checked and leaks are totals over the runs.

## Qdrant

Loaded in 412.1 s, then 440.9 s until the index was built. 1,000,000 points, 1,000,000 indexed
vectors.

| Selectivity | Strategy | Recall@10 | Complete | p50 ms | p95 ms | Leaks |
|---|---|---|---|---|---|---|
| 0.1% | prefilter | 0.998 | 100% | 3.35 | 4.56 | 0 |
| 0.1% | postfilter | 0.093 | 0% | 87.77 | 106.89 | 0 |
| 1% | prefilter | 0.996 | 100% | 4.95 | 6.54 | 0 |
| 1% | postfilter | 0.857 | 55% | 78.85 | 86.46 | 0 |
| 10% | prefilter | 0.977 | 100% | 4.33 | 5.66 | 0 |
| 10% | postfilter | 0.961 | 100% | 13.93 | 17.96 | 0 |
| 100% | prefilter | 0.978 | 100% | 4.93 | 6.20 | 0 |
| 100% | postfilter | 0.978 | 100% | 4.78 | 5.46 | 0 |

| Users | Requests/s | p50 ms | p95 ms | p99 ms | Errors | Chunks checked | Leaks |
|---|---|---|---|---|---|---|---|
| 1 | 204.3 (202.9 to 204.6) | 4.80 | 5.60 | 6.13 | 0 | 122,380 | 0 |
| 2 | 277.7 (277.5 to 278.1) | 7.15 | 8.83 | 9.77 | 0 | 166,690 | 0 |
| 4 | 307.4 (307.0 to 307.6) | 12.76 | 18.24 | 20.71 | 0 | 184,460 | 0 |
| 8 | 313.8 (310.8 to 315.0) | 24.73 | 38.73 | 45.81 | 1 | 188,070 | 0 |
| 16 | 315.2 (313.6 to 315.4) | 50.34 | 66.32 | 76.57 | 2 | 189,020 | 0 |

## Elasticsearch

Loaded in 1,024.5 s, then 1.0 s until the index was built. 1,000,000 documents, 3.4 GiB.

| Selectivity | Strategy | Recall@10 | Complete | p50 ms | p95 ms | Leaks |
|---|---|---|---|---|---|---|
| 0.1% | prefilter | 0.934 | 100% | 11.15 | 20.23 | 0 |
| 0.1% | postfilter | 0.092 | 0% | 114.29 | 165.73 | 0 |
| 1% | prefilter | 0.620 | 100% | 9.32 | 29.33 | 0 |
| 1% | postfilter | 0.828 | 57% | 108.35 | 117.87 | 0 |
| 10% | prefilter | 0.819 | 100% | 5.64 | 16.48 | 0 |
| 10% | postfilter | 0.933 | 100% | 18.16 | 50.66 | 0 |
| 100% | prefilter | 0.875 | 100% | 7.02 | 8.90 | 0 |
| 100% | postfilter | 0.956 | 100% | 5.80 | 6.70 | 0 |

| Users | Requests/s | p50 ms | p95 ms | p99 ms | Errors | Chunks checked | Leaks |
|---|---|---|---|---|---|---|---|
| 1 | 179.5 (170.9 to 185.2) | 5.34 | 7.00 | 8.37 | 0 | 107,130 | 0 |
| 2 | 246.4 (237.6 to 249.9) | 7.81 | 10.77 | 12.89 | 0 | 146,810 | 0 |
| 4 | 295.9 (293.9 to 296.0) | 13.01 | 19.48 | 23.52 | 0 | 177,240 | 0 |
| 8 | 319.9 (319.4 to 320.4) | 23.82 | 39.76 | 50.20 | 0 | 192,040 | 0 |
| 16 | 322.9 (317.2 to 323.1) | 29.54 | 51.54 | 65.23 | 0 | 192,970 | 0 |

## OpenSearch

Loaded in 1,433.4 s, then 1.0 s until the index was built. 1,000,000 documents, 1.7 GiB.

| Selectivity | Strategy | Recall@10 | Complete | p50 ms | p95 ms | Leaks |
|---|---|---|---|---|---|---|
| 0.1% | prefilter | 0.998 | 100% | 9.69 | 16.03 | 0 |
| 0.1% | postfilter | 0.092 | 0% | 114.61 | 140.88 | 0 |
| 1% | prefilter | 0.996 | 100% | 8.83 | 16.53 | 0 |
| 1% | postfilter | 0.852 | 56% | 110.46 | 120.49 | 0 |
| 10% | prefilter | 0.993 | 100% | 18.66 | 24.47 | 0 |
| 10% | postfilter | 0.963 | 100% | 20.05 | 30.06 | 0 |
| 100% | prefilter | 0.969 | 100% | 6.76 | 11.06 | 0 |
| 100% | postfilter | 0.969 | 100% | 6.76 | 8.52 | 0 |

| Users | Requests/s | p50 ms | p95 ms | p99 ms | Errors | Chunks checked | Leaks |
|---|---|---|---|---|---|---|---|
| 1 | 148.4 (146.4 to 154.7) | 6.46 | 8.28 | 9.67 | 0 | 89,920 | 0 |
| 2 | 192.6 (186.5 to 195.4) | 10.24 | 13.60 | 15.33 | 0 | 114,960 | 0 |
| 4 | 217.0 (212.5 to 219.4) | 18.26 | 26.94 | 31.13 | 0 | 129,830 | 0 |
| 8 | 237.5 (236.8 to 237.7) | 33.03 | 48.08 | 57.85 | 0 | 142,520 | 0 |
| 16 | 239.6 (239.5 to 239.7) | 65.15 | 99.32 | 123.23 | 0 | 144,010 | 0 |

## Weaviate

Loaded in 961.5 s; Weaviate indexes while it imports. 1,000,000 objects.

| Selectivity | Strategy | Recall@10 | Complete | p50 ms | p95 ms | Leaks |
|---|---|---|---|---|---|---|
| 0.1% | prefilter | 0.998 | 100% | 3.28 | 4.20 | 0 |
| 0.1% | postfilter | 0.095 | 0% | 93.52 | 109.79 | 0 |
| 1% | prefilter | 0.996 | 100% | 9.52 | 11.34 | 0 |
| 1% | postfilter | 0.845 | 54% | 92.72 | 104.85 | 0 |
| 10% | prefilter | 0.912 | 100% | 7.90 | 11.04 | 0 |
| 10% | postfilter | 0.979 | 100% | 20.08 | 44.53 | 0 |
| 100% | prefilter | 0.962 | 100% | 4.67 | 5.98 | 0 |
| 100% | postfilter | 0.984 | 100% | 7.13 | 9.93 | 0 |

| Users | Requests/s | p50 ms | p95 ms | p99 ms | Errors | Chunks checked | Leaks |
|---|---|---|---|---|---|---|---|
| 1 | 104.8 (104.4 to 105.5) | 9.35 | 10.86 | 12.13 | 0 | 62,960 | 0 |
| 2 | 189.7 (189.4 to 192.9) | 10.38 | 11.43 | 13.98 | 0 | 114,460 | 0 |
| 4 | 316.2 (314.5 to 316.8) | 12.19 | 16.08 | 19.22 | 0 | 189,590 | 0 |
| 8 | 333.2 (332.6 to 337.0) | 23.10 | 39.19 | 47.15 | 0 | 200,730 | 0 |
| 16 | 334.7 (329.7 to 335.3) | 45.63 | 84.59 | 105.04 | 0 | 200,190 | 0 |

## LanceDB

Bulk-loaded with `table.add` in 15.0 s, then 51.3 s to build the indexes; LanceDB's default vector
index (IVF_PQ), cosine. Embedded: the store runs in the benchmark's own process.

| Selectivity | Strategy | Recall@10 | Complete | p50 ms | p95 ms | Leaks |
|---|---|---|---|---|---|---|
| 0.1% | prefilter | 0.501 | 100% | 10.47 | 24.10 | 0 |
| 0.1% | postfilter | 0.079 | 0% | 92.93 | 138.63 | 0 |
| 1% | prefilter | 0.457 | 100% | 11.95 | 30.61 | 0 |
| 1% | postfilter | 0.431 | 53% | 91.62 | 138.69 | 0 |
| 10% | prefilter | 0.465 | 100% | 11.37 | 28.12 | 0 |
| 10% | postfilter | 0.466 | 100% | 29.56 | 54.75 | 0 |
| 100% | prefilter | 0.512 | 100% | 15.48 | 49.20 | 0 |
| 100% | postfilter | 0.512 | 100% | 12.18 | 23.64 | 0 |

| Users | Requests/s | p50 ms | p95 ms | p99 ms | Errors | Chunks checked | Leaks |
|---|---|---|---|---|---|---|---|
| 1 | 67.0 (66.8 to 67.3) | 11.73 | 30.22 | 33.51 | 0 | 40,250 | 0 |
| 2 | 104.1 (104.1 to 105.4) | 15.50 | 38.34 | 44.49 | 0 | 62,750 | 0 |
| 4 | 127.5 (126.9 to 129.3) | 27.75 | 53.79 | 62.95 | 0 | 76,840 | 0 |
| 8 | 138.5 (136.4 to 138.7) | 55.00 | 85.12 | 100.80 | 0 | 82,870 | 0 |
| 16 | 142.0 (140.3 to 142.8) | 110.03 | 150.26 | 174.14 | 0 | 85,370 | 0 |

## Redis

Loaded in 449.0 s; the index (HNSW, M 16, EF_CONSTRUCTION 200) builds while documents arrive.
1,000,000 documents.

| Selectivity | Strategy | Recall@10 | Complete | p50 ms | p95 ms | Leaks |
|---|---|---|---|---|---|---|
| 0.1% | prefilter | 0.998 | 100% | 2.12 | 2.63 | 0 |
| 0.1% | postfilter | 0.093 | 0% | 91.04 | 97.18 | 0 |
| 1% | prefilter | 0.996 | 100% | 9.41 | 10.23 | 0 |
| 1% | postfilter | 0.843 | 56% | 91.66 | 98.05 | 0 |
| 10% | prefilter | 0.900 | 100% | 1.95 | 3.40 | 0 |
| 10% | postfilter | 0.926 | 100% | 9.92 | 38.76 | 0 |
| 100% | prefilter | 0.759 | 100% | 1.48 | 2.00 | 0 |
| 100% | postfilter | 0.907 | 100% | 2.47 | 3.91 | 0 |

| Users | Requests/s | p50 ms | p95 ms | p99 ms | Errors | Chunks checked | Leaks |
|---|---|---|---|---|---|---|---|
| 1 | 107.2 (106.3 to 107.6) | 9.24 | 9.87 | 10.82 | 0 | 64,230 | 0 |
| 2 | 208.8 (207.3 to 209.9) | 9.40 | 10.69 | 11.41 | 0 | 125,240 | 0 |
| 4 | 343.3 (341.7 to 344.3) | 11.24 | 14.36 | 16.93 | 0 | 205,960 | 0 |
| 8 | 377.4 (371.3 to 379.0) | 20.97 | 25.49 | 28.95 | 0 | 225,730 | 0 |
| 16 | 374.6 (372.5 to 376.4) | 42.29 | 47.57 | 55.10 | 0 | 225,120 | 0 |
