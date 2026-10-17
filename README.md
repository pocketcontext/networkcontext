# NetworkContext

NetworkContext archives what a LinkedIn account sees: the feed, notifications, network, profiles, conversations and messages. A browser agent (for example Antigravity) reads LinkedIn pages and writes capture envelopes; the portable `networkcontext` skill validates them and writes them idempotently; the PocketContext server stores them and serves authenticated SQL, full-text search and a read-only browser reader. Re-capturing the same item, or capturing overlapping time windows, never duplicates records.

The first version runs locally on the user's machine. It never acts on LinkedIn. Humans and agents authenticate through PocketBase's default `users` collection.

## Layout

| Path | Content |
| --- | --- |
| `pb_migrations/` | Schema: users, LinkedIn entities, per-account observations, runs, audit log, FTS5 indexes |
| `pb_hooks/ingest.js` | Deterministic ids, immutable natural keys, observation times, account ownership, run lifecycle, sync state, audit |
| `pb_hooks/search.js` | Transactional maintenance of the `profiles`, `posts` and `messages` search indexes |
| `pb_hooks/account_access.js`, `user_directory.js`, `reader.js`, `deploy.pb.js` | Disabled accounts and revoked sessions, safe display names, reader assets, `/up` |
| `pocketcontext.json` | Explicit SQL column allowlist and search indexes |
| `src/networkcontext_client/` | Packaged client: `keys.py` (natural keys, ids, times), `ingest.py` (envelopes to batch upserts), `local.py` (loopback server), `cli.py` |
| `skills/networkcontext/` | Portable skill: `SKILL.md`, references (capture envelope, ingestion, schema, queries, local server), uv launcher |
| `ui/` | Read-only reader adapted from TaskContext |
| `tests/` | Integration, portable skill and reader tests with synthetic fixtures |
| `docs/data-model.md` | Design decisions |

## Data model

See [docs/data-model.md](docs/data-model.md) and `skills/networkcontext/references/schema.md`. Shared entities: `profiles`, `organizations`, `aliases`, `positions`, `education`, `posts`, `comments`, `reactions`, `crm_links`. Per LinkedIn account: `accounts`, `feed_items`, `notifications`, `connections`, `invitations`, `follows`, `conversations`, `conversation_participants`, `messages`, `ingestion_runs`, `sync_state`. Plus `user_directory` and `audit_log`.

Every ingested record has a `natural_key` (a LinkedIn URN, a canonical URL, a composite of related record ids, or a content hash) and an id derived from it: the first 15 bytes of `sha256("<collection>:<natural_key>")`, each mapped to `[a-z0-9]` by `byte % 36`. The server hook rejects any other id, so the client (`keys.record_id`) and server (`recordId`) must stay identical. Writes use PocketBase batch upserts (`PUT /api/collections/<c>/records`).

## Run locally

Build the server at `POCKETCONTEXT_VERSION` with CGO and the `sqlite_math_functions,sqlite_percentile,sqlite_fts5` tags, install the client, build the reader, then initialize:

```sh
git -C ../pocketcontext worktree add --detach /tmp/pocketcontext-pin "$(cat POCKETCONTEXT_VERSION)"
(cd /tmp/pocketcontext-pin && CGO_ENABLED=1 go build -tags sqlite_math_functions,sqlite_percentile,sqlite_fts5 -o bin/pocketcontext ./cmd/pocketcontext)
uv venv .venv && uv pip install --python .venv/bin/python -e .
(cd ui && pnpm install --frozen-lockfile && pnpm build)
.venv/bin/networkcontext local init --app-dir "$PWD" --binary /tmp/pocketcontext-pin/bin/pocketcontext
.venv/bin/networkcontext account add --url https://www.linkedin.com/in/<you>/ --name "<name>"
```

The server listens on `http://127.0.0.1:8095` and allows cross-origin requests only from that origin. `local init` stores the generated account password in `~/.config/networkcontext/local.json` (0600) and the database in `~/.local/share/networkcontext/`. The reader is at `/`; permalinks are `/#/<collection>/<record-id>`. See `skills/networkcontext/references/local.md`.

For a server run by hand, run it from this directory so `pocketcontext.json`, migrations, hooks and `ui/dist` resolve. Never use a local `pb_data/` for tests.

## Validation

```sh
S=/absolute/path/to/pinned/pocketcontext; PY=.venv/bin/python
python3 tests/keys.py
python3 tests/integration.py --binary "$S" --python "$PY"
python3 tests/skill.py --binary "$S" --python "$PY"
(cd ui && pnpm typecheck && pnpm test && pnpm build && pnpm e2e)
python3 tests/reader.py --binary "$S" --browser
```

- `integration.py`: deterministic ids (including non-ASCII keys), rejected forged ids, immutable natural keys, managed fields and deletes; idempotent re-ingestion with no history; overlapping captures where counters change without audit rows; verbatim text; snowflake-decoded times; reshares, replies and reactions; profile identity resolution through aliases (URL first, URN later); references that never erase captured fields; hash-keyed messages and notifications; run lifecycle, one running run per surface, immutable finished runs, sync state and incremental lookback; surface/kind validation and reject reporting; search; profile and thread reports; CRM links; ownership of accounts, runs and private collections; SQL restrictions.
- `skill.py`: skill files and links; the copied client outside the repository; `local init/start/stop/status`, private credential and token files, no secrets in output, CORS refusal for foreign origins; schema snapshot check; every SQL example in `references/queries.md`. `--write-schema` regenerates the three schema snapshots after a migration.
- `ui` and `reader.py`: reader unit and mocked browser tests, then a real browser against an isolated server (deep link through sign-in, cross-tab sign-out, pagination, search, mobile width, every collection's query).

All tests use synthetic records and temporary databases.

## Status and next steps

- Local only. No container, CI, Google OAuth/Workspace JIT, R2 replication or ONCE deployment yet; hosting needs those, filtered snapshots by `account` for private collections, private search indexes, and separate authorization.
- The skill launcher pins a published package commit. After client changes, push, update the pin and run the copied launcher from an unrelated directory.
- No profile merge command yet: two records for one person arise only when separate captures showed only a URN and only a URL. An official-export converter (CSV to envelopes) is not included.
- LinkedIn's User Agreement restricts automated access. Capture only with the account owner's decision, at human pace, and keep the archive private.
