# Schema

Ids are 15 characters of `[a-z0-9]`. Dates are UTC strings such as `2026-10-14 08:30:00.000Z`; empty optional text, dates and relations are `''`, not NULL. Bools compare as 0 or 1. `references/schema.json` is the machine-readable column snapshot used by `check`; the live server is authoritative.

## Common fields

Every ingested collection has `natural_key` (unique, immutable), `key_kind` (`urn`, `url`, `composite`, `hash`), `content_hash`, `first_seen_at`, `last_seen_at`, `last_run` (the run that last saw it) and the stamps `created`, `updated`, `created_by`, `updated_by`. `first_seen_at` and `last_seen_at` are capture times, not LinkedIn times. Times captured from LinkedIn carry `time_precision`: `exact`, `minute`, `day`, `approximate` (from labels such as "3d") or `unknown`.

## Entities (shared)

| Collection | Fields |
| --- | --- |
| profiles | member_urn, public_id (slug), name, headline, location, about, profile_url, connection_degree (self, 1st, 2nd, 3rd, out_of_network), is_self, current_organization, merged_into |
| organizations | org_urn, universal_name, name, kind (company, school, unknown), industry, size_range, headquarters, url, description |
| aliases | value (unique), kind (urn, url, public_id), profile or organization: every identifier seen for a record |
| positions | profile, organization (empty when only a name was shown), organization_name, title, start_month, end_month, current, location, description |
| education | profile, organization, school_name, degree, field, start_year, end_year |
| posts | post_urn, author_profile or author_organization, text, posted_at, time_precision, url, article_url, media_kind, reaction_count, comment_count, repost_count, reshare_of |
| comments | comment_urn, post, parent (reply to), author, text, posted_at, time_precision |
| reactions | actor, post or comment, kind |
| crm_links | profile, app (dealcontext, raisecontext, peoplecontext, other), target_id, target_url, note; unique per profile, app and target |

`profiles.url`-style fields are canonical: `https://www.linkedin.com/in/<slug>/`, `https://www.linkedin.com/company/<slug>/`. A profile known only from a reference has an empty `content_hash` until its own page is captured. Posts and comments referenced but not captured (for example a reshared original) have only their URN, a time decoded from it and possibly text.

## Per LinkedIn account (private when hosted)

| Collection | Fields |
| --- | --- |
| accounts | user (your NetworkContext user id), profile (the owner's profile), label |
| feed_items | account, post, seen_at, position, reason, actor, promoted: one row per post, reason and day seen |
| notifications | account, notification_urn, kind, text, actor, target_post, target_profile, occurred_at, time_precision, url |
| connections | account, profile, status (connected, removed), connected_at, time_precision |
| invitations | account, profile, direction (incoming, outgoing), status (pending, accepted, ignored, withdrawn), message, sent_at |
| follows | account, profile or organization, direction (following, follower) |
| conversations | account, thread_urn (`thread:2-…`), title, last_activity_at, url |
| conversation_participants | conversation, profile |
| messages | conversation, sender, event_urn, body, sent_at, time_precision, attachments (JSON list of name and url) |
| ingestion_runs | account, surface, since, started_at, completed_at, status (running, complete, failed), reached_cutoff, counts (JSON), capture_schema, agent, note |
| sync_state | account, surface, high_water_at, last_complete_run (written by the server when a run completes with reached_cutoff) |

## Other tables

`user_directory` (id, name) resolves user ids in `created_by`, `updated_by`, `accounts.user` and `audit_log.actor`. `audit_log` (action create or update, collection, record, actor, actor_type, changes, created) records content changes; observation fields, engagement counters and run counts are not history. The auth collection is not SQL-readable.

## Search

`networkcontext search <index> '<words>'` uses full-text indexes: `profiles` (name, headline, about), `posts` (text), `messages` (body). All words must match; results have ids, excerpts and permalinks.

## Write rules

Records are written by `ingest`, `account add` and `crm-link`. The server derives ids from natural keys and rejects any other id, keeps natural keys immutable, keeps the earliest `first_seen_at` and latest `last_seen_at`, sets stamps, accepts writes to private collections only for your own accounts and runs, and refuses deletion.
