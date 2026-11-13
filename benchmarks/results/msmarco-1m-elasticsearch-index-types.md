# MS MARCO, a million chunks, bge-small-en-v1.5, Elasticsearch index types: load benchmark results

Run on 2026-11-08 with [`kaggle_stores.ipynb`](../kaggle_stores.ipynb) and its
`elasticsearch-index-types` plan, at commit `07ced7a` (Kaggle version 1 of the notebook, 4,472 s in
total; the `int8_hnsw` run took 2,608 s, of which 1,056 s reading and embedding, and the `hnsw` run
1,678 s). Elasticsearch ran twice, one index type after the other, in one session on one
machine, and was stopped with its data deleted between the runs. The questions and permissions are
the same as in the [run on every store](msmarco-1m-every-store.md), which measured Elasticsearch's
default index; the embeddings, of the same passages with the same model, were computed in this
session and used by both runs.

Two settings changed together from that run: the index type, and the candidates each shard
considers (`num_candidates`), 500 here against the backend's default of 100. The recall gains below
come from both. The default run was a different session on a different machine, so compare its
throughput with these only loosely.

| | |
|---|---|
| Corpus | The first 1,000,000 passages of BEIR MS MARCO; 300 MS MARCO questions picked at random |
| Embeddings | BAAI/bge-small-en-v1.5, 384 dimensions; queries carry the model's retrieval instruction |
| Permissions | Random layout: 1,000 teams, 100 departments, 10 divisions; selectivity 0.1%, 1%, 10%, 100% |
| Retrieval | k = 10, `max_candidates` = 1000, `overfetch` = 4, `num_candidates` = 500 |
| Store | Elasticsearch 9.5.5, release build, loaded through `ElasticsearchBackend`; `create_index(..., index_options={"type": "int8_hnsw"})`, then `{"type": "hnsw"}` |
| Load | Throughput runs: 20 s per setting, users pre-filtering at 1% readable, cycling through the 300 questions |
| Hardware | Kaggle, 4 CPUs, shared by the store and the benchmark's client threads; Tesla T4 for the embeddings only |
| Software | Python 3.13.15, wardowl at `07ced7a`, NumPy 2.1.3, sentence-transformers 5.7.0, PyTorch 2.11.0 |

Recall@10 is measured against the exact top 10 among the chunks the subject may read, computed by
brute force over the same vectors. *Leaks* is the total number of returned chunks the subject may
not read, over 300 queries of 10 results. The throughput runs count errors; they do not check
results.

## Summary

Pre-filtered recall@10 and median latency, one query at a time:

| Readable | `bbq_hnsw`, 100 candidates (default) | `int8_hnsw`, 500 candidates | `hnsw`, 500 candidates |
|---|---|---|---|
| 0.1% | 0.934, 11.2 ms | 0.986, 15.6 ms | 1.000, 14.5 ms |
| 1% | 0.620, 9.3 ms | 0.979, 9.2 ms | 1.000, 10.1 ms |
| 10% | 0.819, 5.6 ms | 0.930, 9.3 ms | 0.928, 9.4 ms |
| 100% | 0.875, 7.0 ms | 0.971, 7.1 ms | 0.986, 7.4 ms |

| Index | Size on disk | Loading through `add` | Peak throughput (users, p99) | At 16 users (p99) |
|---|---|---|---|---|
| `bbq_hnsw` (default) | 3.4 GiB | 17.1 min | 323 requests/s (16, 65 ms) | 323 requests/s (65 ms) |
| `int8_hnsw` | 3.0 GiB | 19.8 min | 271 requests/s (16, 79 ms) | 271 requests/s (79 ms) |
| `hnsw` | 2.5 GiB | 19.8 min | 264 requests/s (16, 79 ms) | 264 requests/s (79 ms) |

The default's throughput is the median of the 3 runs of each setting in the run on every store; the
other two ran each setting once.

No retrieval leaked, pre- or post-filtered, and no request failed under load.

## int8_hnsw

### Load: 1000000 chunks in elasticsearch

Loaded in 1190.5 s, then 2.1 s until the index was built. 1000000 documents, 3.0 GiB. Search breadth: 500. Index type: int8_hnsw.

### Recall and latency by selectivity (one query at a time)

| Selectivity | Strategy | Recall@k | Complete | p50 ms | p95 ms | Leaks |
|---|---|---|---|---|---|---|
| 0.1% | prefilter | 0.986 | 100% | 15.56 | 61.71 | 0 |
| 0.1% | postfilter | 0.096 | 0% | 151.9 | 233.54 | 0 |
| 1.0% | prefilter | 0.979 | 100% | 9.15 | 15.88 | 0 |
| 1.0% | postfilter | 0.821 | 52% | 115.87 | 128.02 | 0 |
| 10.0% | prefilter | 0.930 | 100% | 9.32 | 28.57 | 0 |
| 10.0% | postfilter | 0.957 | 100% | 20.39 | 54.22 | 0 |
| 100.0% | prefilter | 0.971 | 100% | 7.06 | 12.93 | 0 |
| 100.0% | postfilter | 0.971 | 100% | 7.17 | 9.19 | 0 |

### Throughput with concurrent users

| Selectivity | Users | Requests/s | p50 ms | p95 ms | p99 ms | Errors |
|---|---|---|---|---|---|---|
| 1.0% | 1 | 137.6 | 7.12 | 8.53 | 10.49 | 0 |
| 1.0% | 2 | 188.0 | 10.06 | 15.26 | 18.91 | 0 |
| 1.0% | 4 | 235.8 | 16.29 | 24.75 | 30.91 | 0 |
| 1.0% | 8 | 264.6 | 29.4 | 46.4 | 56.9 | 0 |
| 1.0% | 16 | 270.7 | 35.65 | 59.8 | 78.54 | 0 |

## hnsw

### Load: 1000000 chunks in elasticsearch

Loaded in 1186.8 s, then 0.2 s until the index was built. 1000000 documents, 2.5 GiB. Search breadth: 500. Index type: hnsw.

### Recall and latency by selectivity (one query at a time)

| Selectivity | Strategy | Recall@k | Complete | p50 ms | p95 ms | Leaks |
|---|---|---|---|---|---|---|
| 0.1% | prefilter | 1.000 | 100% | 14.46 | 25.57 | 0 |
| 0.1% | postfilter | 0.094 | 0% | 121.29 | 167.91 | 0 |
| 1.0% | prefilter | 1.000 | 100% | 10.14 | 14.9 | 0 |
| 1.0% | postfilter | 0.838 | 56% | 114.44 | 126.88 | 0 |
| 10.0% | prefilter | 0.928 | 100% | 9.38 | 21.64 | 0 |
| 10.0% | postfilter | 0.973 | 100% | 21.67 | 30.69 | 0 |
| 100.0% | prefilter | 0.986 | 100% | 7.38 | 13.64 | 0 |
| 100.0% | postfilter | 0.986 | 100% | 7.27 | 9.14 | 0 |

### Throughput with concurrent users

| Selectivity | Users | Requests/s | p50 ms | p95 ms | p99 ms | Errors |
|---|---|---|---|---|---|---|
| 1.0% | 1 | 145.4 | 6.57 | 8.38 | 10.69 | 0 |
| 1.0% | 2 | 192.9 | 9.86 | 14.64 | 18.22 | 0 |
| 1.0% | 4 | 241.5 | 15.92 | 24.03 | 30.31 | 0 |
| 1.0% | 8 | 262.7 | 29.61 | 47.23 | 56.96 | 0 |
| 1.0% | 16 | 263.9 | 36.54 | 61.64 | 78.82 | 0 |
