# Queries

Use `networkcontext query '<SQL>'` (or `-` with the SQL on standard input). Escape a string literal by doubling single quotes. Times are UTC text, so `datetime('now', '-14 days')` style comparisons need the same format: compare with `strftime('%Y-%m-%d %H:%M:%fZ', 'now', '-14 days')`.

## Within NetworkContext

Recent posts by people I am connected to:

```sql
SELECT p.posted_at, pr.name, substr(p.text, 1, 200) AS text, p.url
FROM posts p JOIN profiles pr ON pr.id = p.author_profile
JOIN connections c ON c.profile = pr.id AND c.status = 'connected'
WHERE p.posted_at >= strftime('%Y-%m-%d %H:%M:%fZ', 'now', '-7 days')
ORDER BY p.posted_at DESC LIMIT 50
```

Conversations where the other person wrote last (unanswered):

```sql
SELECT c.id, c.title, m.sent_at, pr.name, substr(m.body, 1, 200) AS last_message
FROM conversations c
JOIN messages m ON m.id = (SELECT id FROM messages WHERE conversation = c.id ORDER BY sent_at DESC, id DESC LIMIT 1)
JOIN profiles pr ON pr.id = m.sender
JOIN accounts a ON a.id = c.account
WHERE m.sender != a.profile
ORDER BY m.sent_at DESC LIMIT 50
```

Pending incoming invitations, and job changes seen in profiles:

```sql
SELECT pr.name, pr.headline, i.message, i.sent_at FROM invitations i JOIN profiles pr ON pr.id = i.profile
WHERE i.direction = 'incoming' AND i.status = 'pending' ORDER BY i.sent_at DESC LIMIT 50;

SELECT pr.name, a.created, json_extract(a.changes, '$.before.headline') AS before, json_extract(a.changes, '$.after.headline') AS after
FROM audit_log a JOIN profiles pr ON pr.id = a.record
WHERE a.collection = 'profiles' AND a.action = 'update' AND json_extract(a.changes, '$.after.headline') IS NOT NULL
ORDER BY a.created DESC LIMIT 50
```

Who engages with my posts:

```sql
SELECT pr.name, count(*) AS reactions FROM reactions r
JOIN posts p ON p.id = r.post JOIN accounts a ON a.profile = p.author_profile
JOIN profiles pr ON pr.id = r.actor GROUP BY pr.id ORDER BY reactions DESC LIMIT 20
```

`networkcontext profile <url or name>` and `networkcontext thread <id>` answer the common single-person questions without SQL.

## With DealContext (CRM)

The two applications are separate servers; join them in your own reasoning, never by copying data wholesale.

1. Read CRM people with their LinkedIn URLs through the DealContext skill, for example `SELECT id, name, linkedin_url, organization FROM people WHERE linkedin_url != ''`.
2. Normalize each URL with `networkcontext normalize <url>` (or apply the same rule: lowercase slug, `https://www.linkedin.com/in/<slug>/`) and look it up in `aliases`:
   `SELECT profile FROM aliases WHERE value IN ('https://www.linkedin.com/in/ada-example/', …)`.
3. For CRM people without a URL, match by `profiles.name` and current organization name; present candidates and ask the user when more than one fits or the organization differs.
4. Record each confirmed match: `networkcontext crm-link <profile id> dealcontext <person id> --url <DealContext permalink>`. Later questions use `crm_links` directly.

Typical questions:

- CRM contacts who posted in the last 14 days: profiles in `crm_links` joined to `posts` by `author_profile`.
- Unanswered LinkedIn messages from people on open deals: open deals' people from DealContext → `crm_links` → the unanswered-conversations query above filtered by those profiles.
- New connections not yet in the CRM: `connections` with `first_seen_at` in the period and no `crm_links` row.
- People in my network at organizations with an open deal: DealContext organization names or LinkedIn company URLs → `organizations` → `positions` with `current = 1` → `connections`.

To record a LinkedIn message in the CRM, use the DealContext skill (`channel: linkedin`, `source_url`: the conversation's LinkedIn URL) only when the user asks. Mention the NetworkContext permalink in notes so the record can be found again.
