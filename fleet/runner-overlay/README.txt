Ascend Bench Web standalone Runner

Installation is unchanged:
  bash setup.sh <existing-host-network-test-container> <persistent-rw-project-path> [existing-ais-bench-path] [runner-port]
Then establish your own SSH local forward, open the forwarded loopback URL and log in with the token stored in <project>/runner-token. Do not publish the token.

Testing: accuracy uses AISBench all; performance uses perf. Concurrency belongs in models.batch_size. Stable metrics and pressure are independent; retry is passed unchanged. Native timeout is unlimited unless custom seconds are selected. Extra parameters, Chat/Completions, streaming, independent warmup output, async dataset generation and bounded log reads are supported by the current editable scripts/website sources.

Full task config, environment replay script, execution log and AISBench output remain under results/jobs. The page shows a bounded log tail, not the entire log. Inspect failures and native exit results together. Large configured output or concurrency does not imply service support or full-scale validation.
