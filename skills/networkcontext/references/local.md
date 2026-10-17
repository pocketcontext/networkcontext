# Local server

The first version runs on the user's own machine, next to the browser agent.

## Prerequisites

- A NetworkContext application directory (a checkout of `pocketcontext/networkcontext`, containing `pb_migrations/`, `pb_hooks/`, `pocketcontext.json` and, for the reader, a built `ui/dist/`).
- The PocketContext server at the commit in that directory's `POCKETCONTEXT_VERSION`, built from source with CGO and the `sqlite_math_functions,sqlite_percentile,sqlite_fts5` tags, or an installed release that matches it.

## Initialize once

```sh
networkcontext local init --app-dir /path/to/networkcontext --binary /path/to/pocketcontext
```

This creates the database in `~/.local/share/networkcontext/` (or `NETWORKCONTEXT_HOME`), starts the server on `http://127.0.0.1:8095` (`--port` to change), creates one ordinary account (`owner@networkcontext.localhost` unless `--email`), and writes its random password to `~/.config/networkcontext/local.json` with mode 0600. A provisioning superuser is created with a random password that is not kept; an operator can reset it with the server's `superuser upsert` command. Then register the LinkedIn account with `networkcontext account add`.

## Day to day

`networkcontext local start` (after a reboot), `local stop`, `local status`. The server log is `server.log` in the data directory. The reader is at `http://127.0.0.1:8095/`; sign in with the email and the password from `local.json`.

## Why authentication stays on

The server listens only on the loopback interface, but the same machine runs a browser that visits arbitrary websites. Without authentication, any page could read the archive through requests to 127.0.0.1. Every API therefore requires the account's token, and the server only allows its own origin for cross-origin requests. Keep `local.json` private and out of any repository.

## Backups

Stop the server and copy the data directory, or copy `pb_data/data.db` with SQLite's `.backup` command while it runs. Records keep their ids in any copy.

## Moving to a hosted server later

The hosted version uses the same schema and ids. Permalinks keep their path (`/#/<collection>/<id>`); only the origin changes, which `NETWORKCONTEXT_PUBLIC_URL` controls.
