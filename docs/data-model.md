# Data model and design decisions

## Scope

One local deployment archives what one or more LinkedIn accounts (normally the user's own) can see. Nothing is ever posted, sent, liked or accepted. The browser agent owns page navigation and DOM extraction; the client owns the capture envelope contract, normalization and writes; the server owns storage, invariants and reads.

## Entities and observations

Entities (profiles, organizations, posts, comments, reactions, positions, education) are shared facts about LinkedIn. Observations are what one account saw: feed sightings, notifications, connections, invitations, follows, conversations and messages, all carrying `account`. A post seen on three days is one `posts` row and up to three `feed_items` rows (one per post, reason and UTC day).

## Identity

| Collection | Natural key |
| --- | --- |
| profiles | canonical profile URN (`urn:li:fsd_profile:…` or `urn:li:member:…`), else canonical profile URL |
| organizations | `urn:li:organization:<n>` / `urn:li:school:<n>`, else canonical company/school URL |
| posts, comments | post URN (`activity`, `ugcPost`, `share`), canonical comment URN |
| reactions | actor id + target id |
| positions, education | profile id + organization id (or lowercased name) + title/degree + start |
| feed_items | account + post id + lowercased reason + UTC day |
| notifications | account + notification URN, else hash(kind, text, day) + target ids |
| connections, invitations, follows | account + profile/organization id (+ direction) |
| conversations | account + `thread:2-…` |
| conversation_participants | conversation id + profile id |
| messages | conversation id + event URN, else sender id + hash(minute, text) |

Ids are derived from the collection and natural key. Composite keys are built from the resolved ids of their related records, so they stay stable when a related profile's identifier changes. Profiles and organizations resolve through `aliases`, which holds every URN and URL seen for a record; the first identifier seen becomes the immutable natural key. Two records exist for one person only if one capture showed only the URN and another only the URL before any capture showed both; a merge command (repointing relations and recording `merged_into`) is future work.

## Writes

References (an author, a reshared original, a message sender) create a minimal record when missing and otherwise only advance observation fields; they never overwrite captured fields. A record captured on its own page (or a post captured in the feed) replaces its content fields, and identity fields are only ever added. `content_hash` excludes counters; an unchanged hash sends only `last_seen_at`, `last_run` and counters. The server keeps the earliest `first_seen_at` and latest `last_seen_at`, sets stamps, and writes `audit_log` rows for content changes only (not observation fields, counters or run counts).

Writes are ordered by tier (organizations, profiles, aliases, posts and conversations, network and positions, messages and comments, feed sightings and notifications, reactions), and within a collection referenced records come first. Each batch of 50 requests is atomic; a failing batch is replayed request by request so one bad item becomes one reported reject.

## Runs and lookback

A run belongs to one account and surface; only one can be running per pair. `since` comes from `--since`, else the last complete run's start minus 24 hours (bounded by the surface default), else the default (feed 7 days, notifications 30, conversations and messages 90; none for network, profiles, export). Records can only be written into a running run of the writer's own account. Finishing with `reached_cutoff` lets the server advance `sync_state` in the same transaction. Absence never deletes; only a complete connection-list scan may mark a connection removed. Deletes are refused for every collection.

## Access

Local mode uses shared-workspace SQL with one user. Writes to account-scoped collections and runs are already restricted to the account's user. A hosted version must switch account-scoped collections (including `messages` and `conversation_participants` through their conversation, and `audit_log` rows about them) to filtered snapshots and use private search indexes; shared entities can stay visible to every user. The local server binds to 127.0.0.1, requires a token for every API, and allows CORS only for its own origin, because a browser on the same machine visits arbitrary sites.

## Time

LinkedIn times are stored with `time_precision`. Post and comment URNs embed a millisecond timestamp in their top 41 bits; when decodable, it replaces relative labels. Relative labels (`5h`, `3d`, `2w`, `1mo`, `1y`) are resolved against `observed_at` as `approximate`; ISO dates are `day`; ISO times are `minute` or `exact`.
