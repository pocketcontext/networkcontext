# Ingestion runs

## Once per LinkedIn account

`networkcontext account add --url https://www.linkedin.com/in/<you>/ --name "<name>" [--urn urn:li:fsd_profile:…]` records the account owner's profile and links it to your NetworkContext user. Every run and every private record (feed sightings, notifications, network, conversations, messages) belongs to that account. Running it again returns the same account.

## A run

1. `networkcontext run start --surface <surface> [--since <lookback>] --agent <browser agent>` prints `run`, `since`, `since_basis` and `stop_rule`.
   - `--since` takes a duration (`12h`, `7d`, `2w`) or an ISO date or time.
   - Without it: the last complete run's start minus a 24-hour overlap (`incremental`), but never further back than the surface default (`default`): feed 7 days, notifications 30, conversations and messages 90. Network, profiles and export have no window (`none`).
   - Only one run per account and surface can be running (exit 4 otherwise). Finish or fail the earlier one first; a crashed capture leaves its run running until you do.
2. Give the browser agent the run's `since`, `stop_rule` and [capture.md](capture.md). It writes envelopes to a JSONL file.
3. `networkcontext ingest --run <run> <file> --rejects <rejects.jsonl>` writes them. You can ingest several files, or the same file again, into the same run. The output gives counts for this call (`this_ingest`) and the run so far (`run_counts`): `seen` captured items, `created`, `updated` (content changed), `unchanged`, `rejected`, and per-collection detail including referenced profiles, posts and organizations.
4. `networkcontext run finish <run> --reached-cutoff` when the capture reached `since` (or the end of a full list). Only then does the next run start from this run. Finish without the flag when the capture stopped at its cap or was cut short: the next run re-covers the gap. `networkcontext run fail <run> --note '<reason>'` when the capture failed; rerunning the same window is always safe.

Finished runs cannot change. A record can only be written into a running run of your own.

## What makes it idempotent

- Every record has a natural key: a LinkedIn URN when the page shows one, otherwise a canonical URL, a combination of related records (for example account + post + reason + day for a feed sighting), or a hash of stable fields (messages and notifications without URNs).
- The record id is derived from the collection and the natural key (15 characters of a SHA-256), so the same item always has the same id and the same permalink, also in another database built from the same captures.
- Profiles and organizations are resolved through `aliases`: a profile first known by its URL keeps its record when its URN appears later, and both identifiers are recorded. Two records for one person happen only when one capture showed only the URN and another only the URL before any capture showed both; report such duplicates to the user.
- A record whose content is unchanged only advances `last_seen_at` and `last_run`; engagement counters are updated without history. A changed record writes an `audit_log` row with the changed fields.
- Absence never deletes. A post missing from today's feed, or a message outside the window, stays archived. Only a complete scan of the connection list may record `status: removed`.

## Rejects

A line is rejected with its line number and reason when it is not JSON, has another schema version, an unknown kind, a kind that does not belong to the run's surface, a missing or unrecognized identifier, an invalid enum value, or an `observed_at` in the future. A write the server refuses is reported with the record's collection and key. Rejects never stop the rest of the file. Fix the envelopes (usually in the browser agent's extraction) and ingest the corrected lines into a running run.

## Official LinkedIn data export

LinkedIn's "Get a copy of your data" archive gives deep history (connections, messages, invitations, the account's own posts and reactions) without scrolling. Convert its CSV rows to envelopes with the same kinds and ingest them into a run with `--surface export`. Export rows often lack URNs; profile URLs and message times then identify them, and later browser captures merge through aliases.
