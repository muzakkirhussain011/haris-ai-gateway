# MS MARCO, a million chunks, bge-small-en-v1.5: load benchmark results

Run on 2026-11-08 with [`kaggle_load.ipynb`](../kaggle_load.ipynb) at commit `910f16b`
(Kaggle version 1 of the notebook, 1,871 s in total, of which 1,022 s reading and embedding).
The run before it, on 2026-10-16 at `31b8dbc`, did not yet check results under load; its recall
and request rates were within 4% of these.

| | |
|---|---|
| Corpus | The first 1,000,000 passages of BEIR MS MARCO; 300 MS MARCO questions picked at random |
| Embeddings | BAAI/bge-small-en-v1.5, 384 dimensions, fp16, maximum 512 tokens; queries carry the model's retrieval instruction |
| Permissions | Random layout: 1,000 teams, 100 departments, 10 divisions; selectivity 0.1%, 1%, 10%, 100% |
| Retrieval | k = 10, `max_candidates` = 1000, `overfetch` = 4 |
| Indexes | GIN (`jsonb_path_ops`) on the permission metadata; HNSW (cosine) at default settings: m 16, ef_construction 64, ef_search 40; iterative scans `relaxed_order` |
| PostgreSQL | shared_buffers 4 GB, effective_cache_size 16 GB; indexes built with maintenance_work_mem 4 GB and 3 parallel workers |
| Load | Throughput and permission-service runs: 20 s per setting, users pre-filtering at 1% readable, cycling through the 300 questions |
| Hardware | Kaggle, 4 CPUs, shared by PostgreSQL, OPA and the benchmark's client threads; Tesla T4 for the embeddings only |
| Software | Python 3.13.15, wardowl at `910f16b`, NumPy 2.1.3, sentence-transformers 5.7.0, PyTorch 2.11.0, PostgreSQL 16.15, pgvector 0.8.7, OPA 1.21.2 |

Recall@10 is measured against the exact top 10 among the chunks the subject may read, computed by
brute force over the same vectors. *Leaks* is the number of returned chunks the subject may not
read: over 300 queries of 10 results one at a time, and under load over every chunk returned, each
checked against the permissions of the subject it was returned to (*Chunks checked*).

### Load: 1000000 chunks

COPY 35.8 s, GIN index 2.6 s, HNSW index 264.7 s, total size 4071 MB. hnsw.ef_search: default (40).

### Recall and latency by selectivity (one query at a time)

| Selectivity | Strategy | Recall@k | Complete | p50 ms | p95 ms | Leaks |
|---|---|---|---|---|---|---|
| 0.1% | prefilter | 0.973 | 100% | 11.13 | 77.23 | 0 |
| 0.1% | postfilter | 0.093 | 0% | 63.62 | 80.85 | 0 |
| 1.0% | prefilter | 0.932 | 100% | 21.06 | 41.76 | 0 |
| 1.0% | postfilter | 0.834 | 56% | 59.24 | 77.06 | 0 |
| 10.0% | prefilter | 0.886 | 100% | 4.3 | 6.5 | 0 |
| 10.0% | postfilter | 0.914 | 100% | 8.83 | 12.58 | 0 |
| 100.0% | prefilter | 0.886 | 100% | 3.07 | 4.67 | 0 |
| 100.0% | postfilter | 0.886 | 100% | 2.75 | 3.49 | 0 |

### Throughput with concurrent users

| Selectivity | Users | Requests/s | p50 ms | p95 ms | p99 ms | Errors | Chunks checked | Leaks |
|---|---|---|---|---|---|---|---|---|
| 1.0% | 1 | 43.5 | 22.09 | 40.65 | 45.28 | 0 | 8700 | 0 |
| 1.0% | 2 | 83.1 | 23.0 | 41.92 | 49.84 | 0 | 16630 | 0 |
| 1.0% | 4 | 131.4 | 28.74 | 59.09 | 65.9 | 0 | 26310 | 0 |
| 1.0% | 8 | 122.2 | 60.96 | 123.24 | 168.03 | 0 | 24500 | 0 |
| 1.0% | 16 | 114.0 | 127.34 | 261.25 | 409.91 | 0 | 22890 | 0 |

### Cost of the permission service

| Authorizer | Users | Requests/s | p50 ms | p95 ms | p99 ms | Errors | Chunks checked | Leaks |
|---|---|---|---|---|---|---|---|---|
| in-process (MetadataACLAuthorizer) | 1 | 42.9 | 21.91 | 39.95 | 44.97 | 0 | 8590 | 0 |
| in-process (MetadataACLAuthorizer) | 16 | 114.7 | 128.05 | 261.77 | 372.84 | 0 | 23030 | 0 |
| Open Policy Agent over HTTP | 1 | 39.2 | 24.38 | 41.98 | 47.32 | 0 | 7850 | 0 |
| Open Policy Agent over HTTP | 16 | 105.6 | 139.78 | 272.48 | 414.37 | 0 | 21210 | 0 |
| Open Policy Agent, CachingAuthorizer(ttl=60) | 1 | 41.3 | 22.75 | 40.46 | 48.1 | 0 | 8270 | 0 |
| Open Policy Agent, CachingAuthorizer(ttl=60) | 16 | 114.7 | 128.56 | 262.97 | 354.51 | 0 | 23010 | 0 |
