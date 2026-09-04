# Ticket #19: Import and inspect an immutable CPSC source revision

Ticket #19 establishes the PostgreSQL evidence-lineage foundation for recorded
CPSC responses: exact received bytes, content-addressed recall versions,
immutable observations, completed source revisions, and a consistent current
projection exposed through production-style job commands.

## Links

- [Original ticket](https://github.com/LaverdeS/agent-data-oracle/issues/19)
- Pushed code: [source-revision implementation](https://github.com/LaverdeS/agent-data-oracle/commit/f1bb26a10b06b04965f8a559e5718c47183cf87a), [lineage constraints](https://github.com/LaverdeS/agent-data-oracle/commit/ecf0d1cc3e036e3879e48b047579bfb7ab4fa538), and [lifecycle refactor](https://github.com/LaverdeS/agent-data-oracle/commit/96193f764fde80881f3110e455146be866c84a2b)

## Evidence captured on 2026-09-04

This ticket has no meaningful visual proof: screenshots cannot demonstrate
transaction rollback, immutable rows, content-hash reuse, or isolation of
uncommitted PostgreSQL state. Evidence was therefore gathered with the focused
real-PostgreSQL integration suite:

```console
uv run pytest tests/integration/test_cpsc_source.py tests/integration/test_migrations.py -q --disable-warnings -p no:cacheprovider
.........                                                                [100%]
9 passed in 34.06s
```

The observed checks prove that:

- the recorded official CPSC fixture imports offline and preserves the exact
  response bytes, source URL, CPSC date literals, and UTC lifecycle times;
- harmless JSON formatting changes retain the same canonical content hash,
  reuse the recall version, and append a new observation;
- record-count mismatch is retained as a rejected partial revision and cannot
  replace the current completed revision;
- a forced PostgreSQL promotion failure rolls back source/current rows, records
  the failed run, and preserves the prior current revision;
- recall versions reject mutation after creation;
- current rows cannot mix completed revisions or disagree with the selected
  version's evidence fields;
- pending revisions and late additions to completed revisions cannot enter the
  current projection; and
- a projection switch remains invisible to a second connection until its
  transaction commits.

No screenshot was created because it would not substantiate these database
invariants.
