# FiQA, Qwen3-Embedding-0.6B: full results

Run on 2026-11-08 with [`kaggle_benchmark.ipynb`](../kaggle_benchmark.ipynb) at commit `18a67f0`
(Kaggle version 1 of the notebook, 3,532 s in total, of which 1,233 s embedding).

| | |
|---|---|
| Corpus | BEIR FiQA, 57,600 non-empty chunks (38 of 57,638 had no text); 200 queries |
| Embeddings | Qwen3-Embedding-0.6B, 1024 dimensions, fp16, maximum 512 tokens |
| Permissions | 1,000 teams, 100 departments, 10 divisions; selectivity 0.1%, 1%, 5%, 20%, 100% |
| Retrieval | k = 10, `max_candidates` = 1000, `overfetch` = 4 |
| Indexes | Default settings: pgvector HNSW (m 16, ef_construction 64, ef_search 40); Chroma HNSW (cosine) |
| Hardware | Kaggle, 4 CPUs, Tesla T4 (embeddings only; all searches on CPU) |
| Software | Python 3.13.15, wardowl 1.0.0.dev0, NumPy 2.1.3, sentence-transformers 5.7.0, PyTorch 2.11.0, chromadb 1.5.9, PostgreSQL 16.15, pgvector 0.8.7 |

Recall@10 is measured against the exact top 10 among the chunks the subject may read, computed by
brute force over the same vectors. *Examined* is the mean number of candidates the authorizer
checked. *Leaks* is the total number of returned chunks the subject may not read, over 200 queries
of 10 results.

### Layout: random

| Selectivity | Readable | Backend | Strategy | Recall@10 | Complete | Examined | p50 ms | p95 ms | Leaks |
|---|---|---|---|---|---|---|---|---|---|
| 0.1% | 0.10% | exact | prefilter | 1.000 | 100% | 10 | 8.6 | 9.0 | 0 |
| 0.1% | 0.10% | exact | postfilter | 0.097 | 0% | 1000 | 47.3 | 54.2 | 0 |
| 0.1% | 0.10% | exact | naive top-k | 0.001 | 0% | 10 | 8.8 | 9.3 | 0 |
| 0.1% | 0.10% | exact | unprotected | 0.001 | 100% | 0 | 8.6 | 9.9 | 1998 |
| 0.1% | 0.10% | chroma | prefilter | 0.999 | 100% | 10 | 252.3 | 267.9 | 0 |
| 0.1% | 0.10% | chroma | postfilter | 0.096 | 0% | 1000 | 223.2 | 277.7 | 0 |
| 0.1% | 0.10% | chroma | naive top-k | 0.001 | 0% | 10 | 16.3 | 18.1 | 0 |
| 0.1% | 0.10% | pgvector | prefilter | 0.999 | 100% | 10 | 3.1 | 3.7 | 0 |
| 0.1% | 0.10% | pgvector | postfilter | 0.096 | 0% | 1000 | 99.7 | 122.0 | 0 |
| 0.1% | 0.10% | pgvector | naive top-k | 0.001 | 0% | 10 | 4.2 | 5.5 | 0 |
| 0.1% | 0.10% | pgvector, no iterative scan | prefilter | 0.999 | 100% | 10 | 2.9 | 3.3 | 0 |
| 1.0% | 1.00% | exact | prefilter | 1.000 | 100% | 10 | 8.8 | 9.3 | 0 |
| 1.0% | 1.00% | exact | postfilter | 0.858 | 52% | 960.4 | 46.4 | 49.6 | 0 |
| 1.0% | 1.00% | exact | naive top-k | 0.010 | 0% | 10 | 8.6 | 9.7 | 0 |
| 1.0% | 1.00% | exact | unprotected | 0.010 | 100% | 0 | 8.3 | 8.7 | 1980 |
| 1.0% | 1.00% | chroma | prefilter | 1.000 | 100% | 10 | 214.7 | 227.7 | 0 |
| 1.0% | 1.00% | chroma | postfilter | 0.853 | 53% | 958.6 | 223.1 | 270.0 | 0 |
| 1.0% | 1.00% | chroma | naive top-k | 0.010 | 0% | 10 | 16.7 | 18.2 | 0 |
| 1.0% | 1.00% | pgvector | prefilter | 1.000 | 100% | 10 | 9.8 | 11.0 | 0 |
| 1.0% | 1.00% | pgvector | postfilter | 0.850 | 52% | 953.2 | 99.5 | 123.5 | 0 |
| 1.0% | 1.00% | pgvector | naive top-k | 0.010 | 0% | 10 | 4.3 | 5.8 | 0 |
| 1.0% | 1.00% | pgvector, no iterative scan | prefilter | 1.000 | 100% | 10 | 9.6 | 11.1 | 0 |
| 5.0% | 5.01% | exact | prefilter | 1.000 | 100% | 10 | 8.6 | 9.2 | 0 |
| 5.0% | 5.01% | exact | postfilter | 1.000 | 100% | 491.2 | 32.1 | 34.5 | 0 |
| 5.0% | 5.01% | exact | naive top-k | 0.047 | 0% | 10 | 8.5 | 9.1 | 0 |
| 5.0% | 5.01% | exact | unprotected | 0.047 | 100% | 0 | 8.4 | 8.9 | 1906 |
| 5.0% | 5.01% | chroma | prefilter | 1.000 | 100% | 10 | 401.6 | 426.1 | 0 |
| 5.0% | 5.01% | chroma | postfilter | 0.988 | 100% | 503.2 | 119.2 | 146.0 | 0 |
| 5.0% | 5.01% | chroma | naive top-k | 0.046 | 0% | 10 | 16.4 | 17.8 | 0 |
| 5.0% | 5.01% | pgvector | prefilter | 0.974 | 100% | 10 | 12.0 | 19.6 | 0 |
| 5.0% | 5.01% | pgvector | postfilter | 0.992 | 100% | 496 | 52.7 | 68.6 | 0 |
| 5.0% | 5.01% | pgvector | naive top-k | 0.045 | 0% | 10 | 4.0 | 5.5 | 0 |
| 5.0% | 5.01% | pgvector, no iterative scan | prefilter | 0.197 | 100% | 2 | 4.3 | 5.7 | 0 |
| 20.0% | 20.00% | exact | prefilter | 1.000 | 100% | 10 | 8.9 | 9.4 | 0 |
| 20.0% | 20.00% | exact | postfilter | 1.000 | 100% | 128.2 | 18.6 | 19.9 | 0 |
| 20.0% | 20.00% | exact | naive top-k | 0.193 | 0% | 10 | 8.5 | 9.0 | 0 |
| 20.0% | 20.00% | exact | unprotected | 0.193 | 100% | 0 | 8.4 | 8.9 | 1614 |
| 20.0% | 20.00% | chroma | prefilter | 0.996 | 100% | 10 | 287.7 | 306.9 | 0 |
| 20.0% | 20.00% | chroma | postfilter | 0.979 | 100% | 127 | 47.3 | 55.2 | 0 |
| 20.0% | 20.00% | chroma | naive top-k | 0.194 | 0% | 10 | 16.3 | 17.4 | 0 |
| 20.0% | 20.00% | pgvector | prefilter | 0.963 | 100% | 10 | 5.8 | 9.0 | 0 |
| 20.0% | 20.00% | pgvector | postfilter | 0.975 | 100% | 127 | 15.2 | 21.1 | 0 |
| 20.0% | 20.00% | pgvector | naive top-k | 0.192 | 0% | 10 | 4.0 | 5.5 | 0 |
| 20.0% | 20.00% | pgvector, no iterative scan | prefilter | 0.765 | 100% | 7.8 | 4.3 | 5.7 | 0 |
| 100.0% | 100.00% | exact | prefilter | 1.000 | 100% | 10 | 9.0 | 9.8 | 0 |
| 100.0% | 100.00% | exact | postfilter | 1.000 | 100% | 40 | 8.9 | 9.4 | 0 |
| 100.0% | 100.00% | exact | naive top-k | 1.000 | 100% | 10 | 8.5 | 8.9 | 0 |
| 100.0% | 100.00% | exact | unprotected | 1.000 | 100% | 0 | 8.4 | 11.9 | 0 |
| 100.0% | 100.00% | chroma | prefilter | 0.986 | 100% | 10 | 848.4 | 904.8 | 0 |
| 100.0% | 100.00% | chroma | postfilter | 0.986 | 100% | 40 | 19.5 | 23.1 | 0 |
| 100.0% | 100.00% | chroma | naive top-k | 0.986 | 100% | 10 | 16.5 | 17.6 | 0 |
| 100.0% | 100.00% | pgvector | prefilter | 0.976 | 100% | 10 | 5.1 | 7.5 | 0 |
| 100.0% | 100.00% | pgvector | postfilter | 0.976 | 100% | 40 | 5.1 | 6.5 | 0 |
| 100.0% | 100.00% | pgvector | naive top-k | 0.976 | 100% | 10 | 4.2 | 5.7 | 0 |
| 100.0% | 100.00% | pgvector, no iterative scan | prefilter | 0.976 | 100% | 10 | 5.0 | 6.3 | 0 |

### Layout: aligned

| Selectivity | Readable | Backend | Strategy | Recall@10 | Complete | Examined | p50 ms | p95 ms | Leaks |
|---|---|---|---|---|---|---|---|---|---|
| 0.1% | 0.14% | exact | prefilter | 1.000 | 100% | 10 | 8.6 | 9.1 | 0 |
| 0.1% | 0.14% | exact | postfilter | 0.972 | 91% | 290.8 | 18.6 | 46.7 | 0 |
| 0.1% | 0.14% | exact | naive top-k | 0.289 | 4% | 10 | 8.5 | 9.1 | 0 |
| 0.1% | 0.14% | exact | unprotected | 0.289 | 100% | 0 | 8.4 | 9.1 | 1424 |
| 0.1% | 0.14% | chroma | prefilter | 0.999 | 100% | 10 | 247.8 | 257.6 | 0 |
| 0.1% | 0.14% | chroma | postfilter | 0.968 | 92% | 289.6 | 48.6 | 220.0 | 0 |
| 0.1% | 0.14% | chroma | naive top-k | 0.291 | 4% | 10 | 16.4 | 17.4 | 0 |
| 0.1% | 0.14% | pgvector | prefilter | 0.999 | 100% | 10 | 3.1 | 4.7 | 0 |
| 0.1% | 0.14% | pgvector | postfilter | 0.970 | 92% | 290.2 | 16.6 | 112.6 | 0 |
| 0.1% | 0.14% | pgvector | naive top-k | 0.288 | 4% | 10 | 4.3 | 5.7 | 0 |
| 0.1% | 0.14% | pgvector, no iterative scan | prefilter | 0.999 | 100% | 10 | 3.0 | 4.5 | 0 |
| 1.0% | 1.08% | exact | prefilter | 1.000 | 100% | 10 | 8.7 | 11.8 | 0 |
| 1.0% | 1.08% | exact | postfilter | 0.992 | 98% | 247 | 18.5 | 36.3 | 0 |
| 1.0% | 1.08% | exact | naive top-k | 0.295 | 4% | 10 | 8.5 | 11.4 | 0 |
| 1.0% | 1.08% | exact | unprotected | 0.295 | 100% | 0 | 8.4 | 8.8 | 1409 |
| 1.0% | 1.08% | chroma | prefilter | 0.999 | 100% | 10 | 202.2 | 226.3 | 0 |
| 1.0% | 1.08% | chroma | postfilter | 0.987 | 97% | 243.4 | 48.8 | 145.7 | 0 |
| 1.0% | 1.08% | chroma | naive top-k | 0.297 | 4% | 10 | 16.4 | 17.4 | 0 |
| 1.0% | 1.08% | pgvector | prefilter | 0.999 | 100% | 10 | 9.7 | 14.9 | 0 |
| 1.0% | 1.08% | pgvector | postfilter | 0.986 | 97% | 245.8 | 15.8 | 74.2 | 0 |
| 1.0% | 1.08% | pgvector | naive top-k | 0.295 | 4% | 10 | 4.2 | 5.7 | 0 |
| 1.0% | 1.08% | pgvector, no iterative scan | prefilter | 0.974 | 100% | 9.7 | 9.1 | 14.0 | 0 |
| 5.0% | 5.21% | exact | prefilter | 1.000 | 100% | 10 | 8.7 | 9.1 | 0 |
| 5.0% | 5.21% | exact | postfilter | 1.000 | 100% | 50.2 | 8.8 | 18.4 | 0 |
| 5.0% | 5.21% | exact | naive top-k | 0.642 | 12% | 10 | 8.5 | 12.1 | 0 |
| 5.0% | 5.21% | exact | unprotected | 0.642 | 100% | 0 | 8.5 | 9.0 | 716 |
| 5.0% | 5.21% | chroma | prefilter | 0.999 | 100% | 10 | 405.1 | 427.9 | 0 |
| 5.0% | 5.21% | chroma | postfilter | 0.993 | 100% | 49.6 | 18.6 | 47.7 | 0 |
| 5.0% | 5.21% | chroma | naive top-k | 0.641 | 12% | 10 | 16.5 | 17.6 | 0 |
| 5.0% | 5.21% | pgvector | prefilter | 0.977 | 100% | 10 | 5.4 | 46.3 | 0 |
| 5.0% | 5.21% | pgvector | postfilter | 0.978 | 100% | 49.6 | 5.0 | 17.6 | 0 |
| 5.0% | 5.21% | pgvector | naive top-k | 0.644 | 13% | 10 | 4.0 | 5.3 | 0 |
| 5.0% | 5.21% | pgvector, no iterative scan | prefilter | 0.964 | 100% | 9.8 | 5.0 | 45.3 | 0 |
| 20.0% | 20.36% | exact | prefilter | 1.000 | 100% | 10 | 9.0 | 9.4 | 0 |
| 20.0% | 20.36% | exact | postfilter | 1.000 | 100% | 48.4 | 9.1 | 18.6 | 0 |
| 20.0% | 20.36% | exact | naive top-k | 0.549 | 7% | 10 | 8.6 | 9.1 | 0 |
| 20.0% | 20.36% | exact | unprotected | 0.549 | 100% | 0 | 8.5 | 9.0 | 902 |
| 20.0% | 20.36% | chroma | prefilter | 0.998 | 100% | 10 | 288.6 | 309.4 | 0 |
| 20.0% | 20.36% | chroma | postfilter | 0.988 | 100% | 48.4 | 18.7 | 47.6 | 0 |
| 20.0% | 20.36% | chroma | naive top-k | 0.552 | 7% | 10 | 16.5 | 17.4 | 0 |
| 20.0% | 20.36% | pgvector | prefilter | 0.970 | 100% | 10 | 4.5 | 6.8 | 0 |
| 20.0% | 20.36% | pgvector | postfilter | 0.975 | 100% | 49.6 | 5.2 | 17.0 | 0 |
| 20.0% | 20.36% | pgvector | naive top-k | 0.552 | 8% | 10 | 4.3 | 6.2 | 0 |
| 20.0% | 20.36% | pgvector, no iterative scan | prefilter | 0.953 | 100% | 9.8 | 4.2 | 5.7 | 0 |
| 100.0% | 100.00% | exact | prefilter | 1.000 | 100% | 10 | 9.0 | 12.4 | 0 |
| 100.0% | 100.00% | exact | postfilter | 1.000 | 100% | 40 | 8.8 | 9.4 | 0 |
| 100.0% | 100.00% | exact | naive top-k | 1.000 | 100% | 10 | 8.5 | 9.0 | 0 |
| 100.0% | 100.00% | exact | unprotected | 1.000 | 100% | 0 | 8.3 | 9.3 | 0 |
| 100.0% | 100.00% | chroma | prefilter | 0.986 | 100% | 10 | 860.7 | 904.3 | 0 |
| 100.0% | 100.00% | chroma | postfilter | 0.986 | 100% | 40 | 19.1 | 22.2 | 0 |
| 100.0% | 100.00% | chroma | naive top-k | 0.986 | 100% | 10 | 16.6 | 17.9 | 0 |
| 100.0% | 100.00% | pgvector | prefilter | 0.976 | 100% | 10 | 4.8 | 6.1 | 0 |
| 100.0% | 100.00% | pgvector | postfilter | 0.976 | 100% | 40 | 5.0 | 6.3 | 0 |
| 100.0% | 100.00% | pgvector | naive top-k | 0.976 | 100% | 10 | 3.9 | 5.3 | 0 |
| 100.0% | 100.00% | pgvector, no iterative scan | prefilter | 0.976 | 100% | 10 | 4.8 | 6.5 | 0 |

### Layout: misaligned

| Selectivity | Readable | Backend | Strategy | Recall@10 | Complete | Examined | p50 ms | p95 ms | Leaks |
|---|---|---|---|---|---|---|---|---|---|
| 0.1% | 0.06% | exact | prefilter | 1.000 | 100% | 9.7 | 8.8 | 9.3 | 0 |
| 0.1% | 0.06% | exact | postfilter | 0.000 | 0% | 1000 | 46.5 | 48.9 | 0 |
| 0.1% | 0.06% | exact | naive top-k | 0.000 | 0% | 10 | 8.6 | 9.2 | 0 |
| 0.1% | 0.06% | exact | unprotected | 0.000 | 100% | 0 | 8.5 | 8.9 | 2000 |
| 0.1% | 0.06% | chroma | prefilter | 1.000 | 100% | 9.7 | 250.2 | 258.6 | 0 |
| 0.1% | 0.06% | chroma | postfilter | 0.000 | 0% | 1000 | 224.5 | 276.4 | 0 |
| 0.1% | 0.06% | chroma | naive top-k | 0.000 | 0% | 10 | 16.5 | 18.1 | 0 |
| 0.1% | 0.06% | pgvector | prefilter | 1.000 | 100% | 9.7 | 2.5 | 3.7 | 0 |
| 0.1% | 0.06% | pgvector | postfilter | 0.000 | 0% | 1000 | 97.9 | 118.4 | 0 |
| 0.1% | 0.06% | pgvector | naive top-k | 0.000 | 0% | 10 | 4.0 | 5.6 | 0 |
| 0.1% | 0.06% | pgvector, no iterative scan | prefilter | 1.000 | 100% | 9.7 | 2.3 | 3.5 | 0 |
| 1.0% | 0.92% | exact | prefilter | 1.000 | 100% | 10 | 8.7 | 9.1 | 0 |
| 1.0% | 0.92% | exact | postfilter | 0.049 | 0% | 1000 | 46.5 | 49.9 | 0 |
| 1.0% | 0.92% | exact | naive top-k | 0.000 | 0% | 10 | 8.5 | 11.0 | 0 |
| 1.0% | 0.92% | exact | unprotected | 0.000 | 100% | 0 | 8.3 | 8.8 | 2000 |
| 1.0% | 0.92% | chroma | prefilter | 0.999 | 100% | 10 | 236.8 | 250.2 | 0 |
| 1.0% | 0.92% | chroma | postfilter | 0.045 | 0% | 1000 | 227.9 | 275.4 | 0 |
| 1.0% | 0.92% | chroma | naive top-k | 0.000 | 0% | 10 | 16.4 | 17.6 | 0 |
| 1.0% | 0.92% | pgvector | prefilter | 0.991 | 100% | 10 | 8.9 | 14.0 | 0 |
| 1.0% | 0.92% | pgvector | postfilter | 0.048 | 0% | 1000 | 96.5 | 116.3 | 0 |
| 1.0% | 0.92% | pgvector | naive top-k | 0.001 | 0% | 10 | 3.9 | 5.5 | 0 |
| 1.0% | 0.92% | pgvector, no iterative scan | prefilter | 0.954 | 100% | 9.6 | 7.3 | 11.9 | 0 |
| 5.0% | 4.78% | exact | prefilter | 1.000 | 100% | 10 | 8.9 | 9.4 | 0 |
| 5.0% | 4.78% | exact | postfilter | 0.325 | 10% | 991 | 47.3 | 50.6 | 0 |
| 5.0% | 4.78% | exact | naive top-k | 0.000 | 0% | 10 | 8.6 | 9.2 | 0 |
| 5.0% | 4.78% | exact | unprotected | 0.000 | 100% | 0 | 8.6 | 9.5 | 2000 |
| 5.0% | 4.78% | chroma | prefilter | 0.997 | 100% | 10 | 435.1 | 456.5 | 0 |
| 5.0% | 4.78% | chroma | postfilter | 0.309 | 8% | 994.6 | 226.7 | 278.9 | 0 |
| 5.0% | 4.78% | chroma | naive top-k | 0.000 | 0% | 10 | 16.2 | 18.2 | 0 |
| 5.0% | 4.78% | pgvector | prefilter | 0.952 | 100% | 10 | 81.5 | 134.6 | 0 |
| 5.0% | 4.78% | pgvector | postfilter | 0.314 | 10% | 994.6 | 95.5 | 114.8 | 0 |
| 5.0% | 4.78% | pgvector | naive top-k | 0.001 | 0% | 10 | 4.2 | 5.7 | 0 |
| 5.0% | 4.78% | pgvector, no iterative scan | prefilter | 0.137 | 100% | 1.4 | 5.0 | 46.0 | 0 |
| 20.0% | 19.89% | exact | prefilter | 1.000 | 100% | 10 | 8.9 | 9.6 | 0 |
| 20.0% | 19.89% | exact | postfilter | 1.000 | 100% | 365.8 | 19.2 | 34.2 | 0 |
| 20.0% | 19.89% | exact | naive top-k | 0.041 | 0% | 10 | 8.4 | 8.9 | 0 |
| 20.0% | 19.89% | exact | unprotected | 0.041 | 100% | 0 | 8.4 | 8.8 | 1918 |
| 20.0% | 19.89% | chroma | prefilter | 0.995 | 100% | 10 | 293.7 | 314.2 | 0 |
| 20.0% | 19.89% | chroma | postfilter | 0.967 | 100% | 373 | 55.5 | 143.1 | 0 |
| 20.0% | 19.89% | chroma | naive top-k | 0.041 | 0% | 10 | 16.4 | 17.8 | 0 |
| 20.0% | 19.89% | pgvector | prefilter | 0.950 | 100% | 10 | 10.1 | 15.3 | 0 |
| 20.0% | 19.89% | pgvector | postfilter | 0.977 | 100% | 371.8 | 20.5 | 58.8 | 0 |
| 20.0% | 19.89% | pgvector | naive top-k | 0.042 | 0% | 10 | 4.0 | 5.5 | 0 |
| 20.0% | 19.89% | pgvector, no iterative scan | prefilter | 0.218 | 100% | 2.2 | 4.0 | 5.4 | 0 |
| 100.0% | 100.00% | exact | prefilter | 1.000 | 100% | 10 | 9.0 | 9.4 | 0 |
| 100.0% | 100.00% | exact | postfilter | 1.000 | 100% | 40 | 9.0 | 12.2 | 0 |
| 100.0% | 100.00% | exact | naive top-k | 1.000 | 100% | 10 | 8.5 | 9.1 | 0 |
| 100.0% | 100.00% | exact | unprotected | 1.000 | 100% | 0 | 8.4 | 8.8 | 0 |
| 100.0% | 100.00% | chroma | prefilter | 0.986 | 100% | 10 | 862.1 | 915.3 | 0 |
| 100.0% | 100.00% | chroma | postfilter | 0.986 | 100% | 40 | 18.8 | 21.0 | 0 |
| 100.0% | 100.00% | chroma | naive top-k | 0.986 | 100% | 10 | 16.3 | 17.2 | 0 |
| 100.0% | 100.00% | pgvector | prefilter | 0.976 | 100% | 10 | 4.9 | 6.3 | 0 |
| 100.0% | 100.00% | pgvector | postfilter | 0.976 | 100% | 40 | 5.1 | 6.7 | 0 |
| 100.0% | 100.00% | pgvector | naive top-k | 0.976 | 100% | 10 | 4.2 | 5.7 | 0 |
| 100.0% | 100.00% | pgvector, no iterative scan | prefilter | 0.976 | 100% | 10 | 4.8 | 6.2 | 0 |
