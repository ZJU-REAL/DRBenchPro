# Fixed-snapshot Wikipedia Search/Open

The reference snapshot is `enwiki-20260701-v1`, with canonical index identity
`enwiki-20260701-v1-sqlite-fts5-bm25-v1`. The index uses SQLite FTS5/BM25, namespace
0, 2,200-character chunks, 200-character overlap and the `mwparserfromhell` cleaner.
Noncanonical build settings receive a different index identity.

The published
[DRBenchPro Local Research Framework — Docker Image](https://huggingface.co/datasets/zhuiluozhiji/DRBenchPro-runtime)
embeds the finalized
SQLite/FTS5 database and manifests, so its users do not download raw dumps or
build an index. Follow the [download and Docker import guide](../../../../docker/README.md#download-the-framework-image),
then start the image's Wikipedia service. It starts Wiki without model credentials and checks the complete
canonical data identity. Data and notices remain read-only in the image.

The raw dump/build workflow below is for source-based reconstruction; the
prebuilt Framework Image is the direct way to obtain the matching index.
For source installations, install the `local-research` extra. The following are
installed commands:

```bash
drbp-wiki-download --output-dir downloads/enwiki-20260701

drbp-wiki-build   --shard-manifest downloads/enwiki-20260701/download-manifest.json   --database downloads/enwiki-20260701/wiki.sqlite   --verify-source-sha1

drbp-wiki-serve   --database downloads/enwiki-20260701/wiki.sqlite   --snapshot-id enwiki-20260701-v1 --host 127.0.0.1 --port 8091
```

The downloader verifies Wikimedia checksums and supports resumable transfers. The
builder has transactional checkpoints. The service exposes `/health`, `/manifest`,
`/v1/search`, `/v1/open` and `/v1/find`; the research model is given only Search/Open.
Source IDs encode snapshot/page/revision/section; `open` accepts pagination cursors.

For source installations the full dump and index are large external artifacts.
If the dated upstream dump
is no longer available, provide the matching archived files or use the
downloader's `--base-url` with a source containing the same dated/checksummed
artifacts. An arbitrary newer snapshot is not an equivalent experiment.

For local plumbing tests, `--shard-limit`, `--max-pages` and `--allow-incomplete`
permit partial data; such runs are not full benchmark reproductions. The research
configuration must explicitly set `wiki.allow_incomplete_index: true` for a pilot.
Do not present a partial index as the formal complete snapshot.

Keep the snapshot and index identities consistent with the research configuration.
The commands run independently of the Research API; start this service before
running research jobs and keep the database read-only during serving.
