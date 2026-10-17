#!/usr/bin/env python3
"""Command-line client for a NetworkContext LinkedIn archive. Packaged Python CLI.

Configuration comes from the environment, or from the file written by `local init`:
  NETWORKCONTEXT_URL             server address, default from ~/.config/networkcontext/local.json
  NETWORKCONTEXT_USER_EMAIL      email of an account in the `users` collection
  NETWORKCONTEXT_USER_PASSWORD   password of that account
  NETWORKCONTEXT_PUBLIC_URL      origin used in permalinks (defaults to the server URL)

Exit codes: 0 success; 1 HTTP or transport error; 2 usage or configuration error;
3 `check` found schema differences; 4 HTTP 409 (conflict: read again before retrying).
"""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import http.client
import json
import os
from pathlib import Path
from importlib.resources import files
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

from observecontext_client.instrumentation import instrument_cli

from . import ingest, keys, local

ENV = ['NETWORKCONTEXT_URL', 'NETWORKCONTEXT_USER_EMAIL', 'NETWORKCONTEXT_USER_PASSWORD']
SCHEMA_FILE = files('networkcontext_client').joinpath('schema.json')
TIMEOUT = 30
USER_AGENT = 'NetworkContext/0.1'
SURFACES = ('feed', 'notifications', 'network', 'profiles', 'conversations', 'messages', 'export')
# Default lookback per surface; None means the surface has no time window (full list or on demand).
LOOKBACK = {'feed': 7, 'notifications': 30, 'network': None, 'profiles': None, 'conversations': 90, 'messages': 90, 'export': None}
OVERLAP = timedelta(hours=24)
STOP_RULE = {
    'feed': 'The feed is not chronological: stop after 15 consecutive items older than since, after 300 items, or after 10 minutes.',
    'notifications': 'Chronological: stop at the first notification older than since, or after 300 items.',
    'network': 'Full list: scroll to the end and pass --reached-cutoff to finish only if the whole list was captured.',
    'profiles': 'On demand: capture the requested profiles only.',
    'conversations': 'Chronological by last activity: stop at the first thread older than since.',
    'messages': 'Per thread: stop at the first message older than since.',
    'export': 'Import every row of the official LinkedIn data export.',
}
SEARCH_INDEXES = ('profiles', 'posts', 'messages')
hidden = []


class Fail(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def hide(value):
    if value:
        hidden.append(value)
    return value


def say(text, stream=sys.stderr):
    for value in hidden:
        text = text.replace(value, '***')
    print(text, file=stream)


def dump(data, pretty=False):
    if pretty:
        return json.dumps(data, indent=2, ensure_ascii=False)
    return json.dumps(data, separators=(',', ':'), ensure_ascii=False)


def config():
    saved = local.load() or {}
    url = (os.environ.get(ENV[0]) or saved.get('url') or '').rstrip('/')
    email = os.environ.get(ENV[1]) or saved.get('email') or ''
    password = os.environ.get(ENV[2]) or (saved.get('password') if email == saved.get('email') else '')
    if not url or not email:
        raise Fail(2, 'NetworkContext is not configured: run `networkcontext local init --app-dir <dir>` on this machine, or set '
                   'NETWORKCONTEXT_URL and NETWORKCONTEXT_USER_EMAIL. Do not look for credentials elsewhere.')
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise Fail(2, 'NETWORKCONTEXT_URL must be an HTTP(S) URL without credentials, query, or fragment')
    if parsed.scheme == 'http' and parsed.hostname not in ('localhost', '127.0.0.1', '::1'):
        raise Fail(2, 'Use HTTPS for a remote NetworkContext server')
    public = (os.environ.get('NETWORKCONTEXT_PUBLIC_URL') or url).rstrip('/')
    return {'url': url, 'email': email, 'password': hide(password), 'public': public}


def cache_file(cfg):
    base = os.environ.get('XDG_CACHE_HOME') or str(Path.home() / '.cache')
    key = hashlib.sha256((cfg['url'] + '\n' + cfg['email']).encode()).hexdigest()[:32]
    return Path(base) / 'networkcontext' / (key + '.json')


def load_session(cfg):
    try:
        session = json.loads(cache_file(cfg).read_text())
        if not isinstance(session, dict) or session.get('url') != cfg['url'] or session.get('email') != cfg['email']:
            return None
        return session if isinstance(session.get('token'), str) and hide(session['token']) else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_session(cfg, session):
    path = cache_file(cfg)
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, prefix='.session-', delete=False) as handle:
            temporary = Path(handle.name)
            os.fchmod(handle.fileno(), 0o600)
            json.dump(session, handle)
        os.replace(temporary, path)
    except OSError as error:
        say(f'note: token not cached ({error.strerror}); sign-in will be required again')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """A followed redirect would turn a POST into a GET and could send the token to another host."""
    def redirect_request(self, *args):
        return None


opener = urllib.request.build_opener(NoRedirect)


def send(cfg, method, path, body=None, token=None, timeout=TIMEOUT):
    headers = {'Content-Type': 'application/json', 'User-Agent': USER_AGENT}
    if token:
        headers['Authorization'] = token
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(cfg['url'] + path, data=data, headers=headers, method=method)
    try:
        with opener.open(request, timeout=timeout) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read()
        if 300 <= status < 400:
            raise Fail(1, f'HTTP {status}: the server redirects to {error.headers.get("Location")}. Set NETWORKCONTEXT_URL to the final address.')
    except (OSError, ValueError, http.client.HTTPException) as error:
        reason = getattr(error, 'reason', error)
        raise Fail(1, f'cannot reach {cfg["url"]}: {reason}. If this is the local server, run `networkcontext local start`.')
    text = raw.decode('utf-8', 'replace')
    try:
        return status, json.loads(text) if text else None
    except ValueError:
        return status, text[:2000]


def login(cfg):
    if not cfg.get('password'):
        raise Fail(2, 'Set NETWORKCONTEXT_USER_PASSWORD, or use the account created by `networkcontext local init`.')
    status, data = send(cfg, 'POST', '/api/collections/users/auth-with-password', {'identity': cfg['email'], 'password': cfg['password']})
    if status != 200 or not isinstance(data, dict) or 'token' not in data:
        raise Fail(1, f'login as {cfg["email"]} failed: HTTP {status}. Check the NetworkContext configuration with the user. User credentials only.')
    session = {'url': cfg['url'], 'email': cfg['email'], 'token': hide(data['token'])}
    save_session(cfg, session)
    return session


def token_rejected(cfg, token):
    return send(cfg, 'POST', '/api/context/query', {'sql': 'SELECT 1'}, token)[0] == 401


def call(cfg, method, path, body=None):
    """Authenticated request. A stale cached token gets one fresh login; the first attempt wrote nothing."""
    session = load_session(cfg)
    cached = session is not None
    if not cached:
        session = login(cfg)
    status, data = send(cfg, method, path, body, session['token'])
    if cached and 400 <= status < 500 and status != 409 and (status == 401 or token_rejected(cfg, session['token'])):
        session = login(cfg)
        status, data = send(cfg, method, path, body, session['token'])
    return status, data


def must(cfg, method, path, body=None):
    status, data = call(cfg, method, path, body)
    if status < 400:
        return data
    lines = [f'HTTP {status} from {method} {path}', dump(data, pretty=True)]
    if path == '/api/batch':
        lines.append('Nothing in this batch was saved. ' + ingest.batch_message(data))
    if status == 409:
        lines.append('HTTP 409: read the current state again, confirm the change still applies, then retry.')
    raise Fail(4 if status == 409 else 1, '\n'.join(lines))


class Client:
    """The narrow interface the ingestion writer needs."""

    def __init__(self, cfg):
        self.cfg = cfg

    def query(self, sql):
        result = must(self.cfg, 'POST', '/api/context/query', {'sql': sql})
        if result.get('truncated'):
            raise Fail(1, 'an internal lookup exceeded the SQL response limit; ingest smaller files')
        return [dict(zip(result['columns'], row)) for row in result['rows']]

    def batch(self, requests):
        return call(self.cfg, 'POST', '/api/batch', {'requests': requests})


def literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def read_json(text, kind, what):
    if text == '-':
        text = sys.stdin.read()
    try:
        value = json.loads(text)
    except ValueError as error:
        raise Fail(2, f'{what} is not valid JSON: {error}')
    if not isinstance(value, kind):
        raise Fail(2, f'{what} must be a JSON {"array" if kind is list else "object"}')
    return value


def columns(tables):
    return {table['name']: {column['name']: column.get('type') for column in table['columns']} for table in tables}


def check(cfg):
    try:
        reference = columns(json.loads(SCHEMA_FILE.read_text())['tables'])
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise Fail(2, f'cannot read {SCHEMA_FILE}: {error}')
    live = columns(must(cfg, 'GET', '/api/context/schema')['tables'])
    differences = []
    for table in sorted(set(live) | set(reference)):
        if table not in reference:
            differences.append(f'table {table}: on the server, not in the reference files')
        elif table not in live:
            differences.append(f'table {table}: in the reference files, not on the server')
        else:
            for column in sorted(set(live[table]) | set(reference[table])):
                if live[table].get(column) != reference[table].get(column):
                    differences.append(f'column {table}.{column}: server {live[table].get(column)}, reference {reference[table].get(column)}')
    if not differences:
        say(f'OK: the live schema matches references/schema.json ({len(live)} tables)', sys.stdout)
        return 0
    for line in differences:
        say(line, sys.stdout)
    say('The server is authoritative: run `networkcontext schema` and ask the user to update this skill.', sys.stdout)
    return 3


def whoami(cfg):
    data = must(cfg, 'POST', '/api/collections/users/auth-refresh')
    hide(data.get('token'))
    return data['record']


def my_accounts(cfg, client, user):
    return client.query(f"SELECT a.id, a.label, a.profile, p.name, p.profile_url FROM accounts a JOIN profiles p ON p.id = a.profile WHERE a.user = {literal(user)} ORDER BY a.created")


def pick_account(cfg, client, requested):
    user = whoami(cfg)['id']
    accounts = my_accounts(cfg, client, user)
    if requested:
        match = [a for a in accounts if a['id'] == requested]
        if not match:
            raise Fail(2, f'account {requested} is not one of your accounts')
        return match[0]
    if not accounts:
        raise Fail(2, 'No LinkedIn account registered: run `networkcontext account add --url <your profile URL> --name <name>` first.')
    if len(accounts) > 1:
        raise Fail(2, 'Several LinkedIn accounts are registered; pass --account <id> (see `networkcontext account list`).')
    return accounts[0]


def permalink(cfg, collection, record):
    return f'{cfg["public"]}/#/{collection}/{record}'


def run_row(client, run):
    if not re.fullmatch(r'[a-z0-9]{15}', run):
        raise Fail(2, 'run id must be 15 lowercase letters or digits')
    rows = client.query(f'SELECT id, account, surface, status, since, started_at, counts FROM ingestion_runs WHERE id = {literal(run)}')
    if not rows:
        raise Fail(1, f'run {run} not found')
    return rows[0]


def add_counts(total, counts):
    for key in ('seen', 'created', 'updated', 'unchanged', 'rejected'):
        total[key] = total.get(key, 0) + counts.get(key, 0)
    records = total.setdefault('records', {})
    for collection, values in counts.get('records', {}).items():
        target = records.setdefault(collection, {})
        for key, value in values.items():
            target[key] = target.get(key, 0) + value
    return total


def ingest_file(cfg, client, args):
    run = run_row(client, args.run)
    if run['status'] != 'running':
        raise Fail(2, f'run {run["id"]} is {run["status"]}; start a new run')
    stream = sys.stdin if args.file == '-' else open(args.file, encoding='utf-8')
    writer = ingest.Writer(client)
    total, rejects = {}, []
    with stream:
        batch = []

        def flush():
            plan = ingest.Plan(run['account'], run['id'])
            for number, value in batch:
                if isinstance(value, Exception):
                    plan.rejects.append({'line': number, 'reason': f'not valid JSON: {value}'})
                else:
                    plan.envelope(number, value, run['surface'])
            add_counts(total, writer.write(plan))
            rejects.extend(plan.rejects)
            batch.clear()

        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                batch.append((number, json.loads(line)))
            except ValueError as error:
                batch.append((number, error))
            if len(batch) >= args.chunk:
                flush()
        if batch:
            flush()
    stored = run_row(client, run['id'])['counts']
    if isinstance(stored, str):
        try:
            stored = json.loads(stored)
        except ValueError:
            stored = {}
    counts = add_counts(dict(stored) if isinstance(stored, dict) else {}, total)
    must(cfg, 'PATCH', f'/api/collections/ingestion_runs/records/{run["id"]}', {'counts': counts})
    if rejects:
        if args.rejects:
            with open(args.rejects, 'a', encoding='utf-8') as handle:
                for reject in rejects:
                    handle.write(json.dumps(reject, ensure_ascii=False) + '\n')
        for reject in rejects[:20]:
            say(f'rejected line {reject["line"]}: {reject["reason"]}')
        if len(rejects) > 20:
            say(f'... {len(rejects) - 20} more rejects' + ('' if args.rejects else '; pass --rejects FILE to keep them all'))
    return {'run': run['id'], 'this_ingest': total, 'run_counts': counts}


def resolve_profile(cfg, client, value):
    if re.fullmatch(r'[a-z0-9]{15}', value):
        rows = client.query(f'SELECT id FROM profiles WHERE id = {literal(value)}')
        if rows:
            return rows[0]['id'], []
    url, _ = keys.profile_url(value)
    urn = keys.profile_urn(value)
    if url or urn:
        rows = client.query(f'SELECT profile FROM aliases WHERE value = {literal(url or urn)} AND profile != \'\'')
        if not rows:
            raise Fail(1, f'no archived profile for {url or urn}')
        return rows[0]['profile'], []
    needle = literal(value.lower())
    rows = client.query(f"SELECT id, name, headline, profile_url FROM profiles WHERE instr(lower(name), {needle}) > 0 AND merged_into = '' ORDER BY name, id LIMIT 20")
    if len(rows) == 1:
        return rows[0]['id'], []
    return None, rows


def profile_report(cfg, client, value):
    record, candidates = resolve_profile(cfg, client, value)
    if record is None:
        return {'matches': candidates, 'note': 'Ambiguous or no match: choose one id and run `profile <id>`.'}
    q = lambda sql: client.query(sql)
    rid = literal(record)
    profile = q(f'SELECT id, name, headline, location, about, profile_url, public_id, member_urn, connection_degree, current_organization, merged_into, first_seen_at, last_seen_at FROM profiles WHERE id = {rid}')[0]
    return {
        'profile': profile,
        'permalink': permalink(cfg, 'profiles', record),
        'positions': q(f"SELECT p.title, coalesce(nullif(o.name, ''), p.organization_name) AS organization, p.organization AS organization_id, p.start_month, p.end_month, p.current FROM positions p LEFT JOIN organizations o ON o.id = p.organization WHERE p.profile = {rid} ORDER BY p.current DESC, p.start_month DESC LIMIT 30"),
        'education': q(f"SELECT coalesce(nullif(o.name, ''), e.school_name) AS school, e.degree, e.field, e.start_year, e.end_year FROM education e LEFT JOIN organizations o ON o.id = e.organization WHERE e.profile = {rid} ORDER BY e.start_year DESC LIMIT 20"),
        'recent_posts': q(f'SELECT id, posted_at, substr(text, 1, 280) AS text, url, reaction_count, comment_count FROM posts WHERE author_profile = {rid} ORDER BY posted_at DESC LIMIT 10'),
        'connection': q(f'SELECT account, status, connected_at FROM connections WHERE profile = {rid}'),
        'invitations': q(f'SELECT account, direction, status, sent_at FROM invitations WHERE profile = {rid}'),
        'conversations': q(f'SELECT c.id, c.title, c.last_activity_at, (SELECT count(*) FROM messages m WHERE m.conversation = c.id) AS messages, (SELECT max(m.sent_at) FROM messages m WHERE m.conversation = c.id AND m.sender = {rid}) AS last_from_them FROM conversation_participants cp JOIN conversations c ON c.id = cp.conversation WHERE cp.profile = {rid} ORDER BY c.last_activity_at DESC LIMIT 10'),
        'crm_links': q(f'SELECT app, target_id, target_url FROM crm_links WHERE profile = {rid}'),
        'aliases': [row['value'] for row in q(f'SELECT value FROM aliases WHERE profile = {rid} ORDER BY value')],
    }


def thread_report(cfg, client, conversation, limit):
    if not re.fullmatch(r'[a-z0-9]{15}', conversation):
        raise Fail(2, 'conversation id must be 15 lowercase letters or digits')
    rows = client.query(f'SELECT id, title, url, last_activity_at FROM conversations WHERE id = {literal(conversation)}')
    if not rows:
        raise Fail(1, 'conversation not found')
    participants = client.query(f'SELECT p.id, p.name, p.profile_url FROM conversation_participants cp JOIN profiles p ON p.id = cp.profile WHERE cp.conversation = {literal(conversation)} ORDER BY p.name')
    messages = client.query(f'SELECT * FROM (SELECT m.id, m.sent_at, m.time_precision, m.sender, p.name AS sender_name, m.body, m.attachments FROM messages m LEFT JOIN profiles p ON p.id = m.sender WHERE m.conversation = {literal(conversation)} ORDER BY m.sent_at DESC, m.id DESC LIMIT {int(limit)}) ORDER BY sent_at, id')
    return {'conversation': rows[0], 'permalink': permalink(cfg, 'conversations', conversation), 'participants': participants, 'messages': messages}


def since_for(client, account, surface, requested, now):
    if requested:
        try:
            return keys.parse_since(requested, now), 'requested'
        except keys.Invalid as error:
            raise Fail(2, f'--since: {error}')
    days = LOOKBACK[surface]
    state = client.query(f'SELECT high_water_at FROM sync_state WHERE account = {literal(account)} AND surface = {literal(surface)}')
    floor = now - timedelta(days=days) if days else None
    if state and state[0]['high_water_at']:
        incremental = keys.parse_instant(state[0]['high_water_at']) - OVERLAP
        return (max(incremental, floor) if floor else incremental), 'incremental'
    return floor, 'default' if floor else 'none'


def run(args):
    if args.command == 'normalize':
        url, slug = keys.profile_url(args.value)
        org_url, universal, kind = keys.organization_url(args.value)
        data = {'profile_url': url, 'public_id': slug, 'profile_urn': keys.profile_urn(args.value), 'organization_url': org_url,
                'universal_name': universal, 'organization_kind': kind, 'post_urn': keys.post_urn(args.value)}
        say(dump({k: v for k, v in data.items() if v}, args.pretty), sys.stdout)
        return 0
    if args.command == 'local':
        return run_local(args)
    cfg = config()
    if args.command == 'logout':
        path = cache_file(cfg)
        path.unlink(missing_ok=True)
        say(f'removed {path}', sys.stdout)
        return 0
    client = Client(cfg)
    if args.command == 'check':
        return check(cfg)
    if args.command == 'whoami':
        record = whoami(cfg)
        data = {'id': record['id'], 'name': record.get('name', ''), 'email': cfg['email'], 'url': cfg['url']}
    elif args.command == 'schema':
        data = must(cfg, 'GET', '/api/context/schema')
    elif args.command in ('sql', 'query'):
        sql = sys.stdin.read() if args.query == '-' else args.query
        data = must(cfg, 'POST', '/api/context/query', {'sql': sql})
        if isinstance(data, dict) and data.get('truncated'):
            say(f'WARNING: result truncated to {len(data.get("rows", []))} rows. Select fewer columns, narrow the query, or page with ORDER BY and LIMIT/OFFSET.')
    elif args.command == 'get':
        if not re.fullmatch(r'[a-z0-9]{15}', args.id) or not re.fullmatch(r'[a-z_]{1,64}', args.collection):
            raise Fail(2, 'usage: get <collection> <15-character id>')
        rows = client.query(f'SELECT * FROM "{args.collection}" WHERE id = {literal(args.id)} LIMIT 1')
        if not rows:
            raise Fail(1, f'HTTP 404: record not found in {args.collection}')
        data = dict(rows[0], permalink=permalink(cfg, args.collection, args.id))
    elif args.command == 'search':
        result = must(cfg, 'POST', '/api/context/search', {'index': args.index, 'query': args.text, 'limit': args.limit})
        data = result
        ids = [hit['id'] for hit in result.get('hits', [])]
        if ids:
            title = {'profiles': "name || ' — ' || headline", 'posts': "substr(text, 1, 120)", 'messages': "substr(body, 1, 120)"}[args.index]
            labels = {row['id']: row['label'] for row in client.query(f'SELECT id, {title} AS label FROM {args.index} WHERE id IN ({",".join(literal(i) for i in ids)})')}
            for hit in result['hits']:
                hit['label'] = labels.get(hit['id'], '')
                hit['permalink'] = permalink(cfg, args.index, hit['id'])
    elif args.command == 'account':
        if args.action == 'list':
            data = my_accounts(cfg, client, whoami(cfg)['id'])
        else:
            data = account_add(cfg, client, args)
    elif args.command == 'run':
        data = run_command(cfg, client, args)
    elif args.command == 'ingest':
        data = ingest_file(cfg, client, args)
    elif args.command == 'profile':
        data = profile_report(cfg, client, args.value)
    elif args.command == 'thread':
        data = thread_report(cfg, client, args.conversation, args.limit)
    elif args.command == 'link':
        if not re.fullmatch(r'[a-z0-9]{15}', args.id) or not re.fullmatch(r'[a-z_]{1,64}', args.collection):
            raise Fail(2, 'usage: link <collection> <15-character id>')
        data = {'permalink': permalink(cfg, args.collection, args.id)}
    elif args.command == 'crm-link':
        record, candidates = resolve_profile(cfg, client, args.profile)
        if record is None:
            raise Fail(2, 'profile is ambiguous or unknown; pass its 15-character id')
        body = {'profile': record, 'app': args.app, 'target_id': args.target_id}
        if args.url:
            body['target_url'] = args.url
        if args.note:
            body['note'] = args.note
        data = must(cfg, 'POST', '/api/collections/crm_links/records', body)
    say(dump(data, args.pretty), sys.stdout)
    return 0


def account_add(cfg, client, args):
    user = whoami(cfg)['id']
    plan = ingest.Plan('', '')
    ref = {'url': args.url, 'urn': args.urn, 'name': args.name}
    try:
        profile = plan.profile({k: v for k, v in ref.items() if v}, datetime.now(timezone.utc), full={'name': args.name, 'connection_degree': 'self', 'is_self': True})
    except keys.Invalid as error:
        raise Fail(2, str(error))
    writer = ingest.Writer(client)
    writer.resolve(plan)
    writer.finalize_keys(plan)
    operations, _ = writer.requests(plan)
    for _, op in operations:
        op['body'].pop('last_run', None)
    status, data = client.batch([op for _, op in operations])
    if status >= 400:
        raise Fail(1, 'could not record your profile: ' + ingest.batch_message(data))
    existing = client.query(f'SELECT id, user FROM accounts WHERE profile = {literal(profile.id)}')
    if existing:
        if existing[0]['user'] != user:
            raise Fail(1, 'this LinkedIn profile is registered by another user')
        account = existing[0]['id']
    else:
        account = must(cfg, 'POST', '/api/collections/accounts/records', {'profile': profile.id, 'label': args.label or args.name})['id']
    return {'account': account, 'profile': profile.id, 'permalink': permalink(cfg, 'profiles', profile.id)}


def run_command(cfg, client, args):
    if args.action == 'start':
        account = pick_account(cfg, client, args.account)
        now = datetime.now(timezone.utc)
        since, basis = since_for(client, account['id'], args.surface, args.since, now)
        body = {'account': account['id'], 'surface': args.surface, 'capture_schema': ingest.SCHEMA, 'agent': args.agent or ''}
        if since:
            body['since'] = keys.db_time(since)
        record = must(cfg, 'POST', '/api/collections/ingestion_runs/records', body)
        return {'run': record['id'], 'account': account['id'], 'surface': args.surface, 'since': record.get('since') or None,
                'since_basis': basis, 'started_at': record['started_at'], 'capture_schema': ingest.SCHEMA,
                'stop_rule': STOP_RULE[args.surface],
                'next': f'networkcontext ingest --run {record["id"]} <captures.jsonl>; then networkcontext run finish {record["id"]} [--reached-cutoff]'}
    if args.action == 'list':
        user = whoami(cfg)['id']
        return client.query(f"SELECT r.id, r.surface, r.status, r.since, r.started_at, r.completed_at, r.reached_cutoff, r.counts FROM ingestion_runs r JOIN accounts a ON a.id = r.account WHERE a.user = {literal(user)} ORDER BY r.started_at DESC LIMIT {int(args.limit)}")
    row = run_row(client, args.id)
    body = {'status': 'complete' if args.action == 'finish' else 'failed'}
    if args.action == 'finish':
        body['reached_cutoff'] = bool(args.reached_cutoff)
    if args.note:
        body['note'] = args.note
    record = must(cfg, 'PATCH', f'/api/collections/ingestion_runs/records/{row["id"]}', body)
    return {'run': record['id'], 'status': record['status'], 'reached_cutoff': record['reached_cutoff'], 'counts': record['counts'],
            'completed_at': record['completed_at']}


def run_local(args):
    try:
        if args.action == 'init':
            app = local.app_dir(args.app_dir)
            settings, user = local.init(app, local.binary(args.binary), args.port, args.email, args.name)
            data = {'status': 'initialized and running', 'url': settings['url'], 'email': settings['email'], 'user': user['id'],
                    'credentials': str(local.credentials_file()), 'data': settings['data_dir'],
                    'next': 'networkcontext account add --url https://www.linkedin.com/in/<you>/ --name "<your name>"'}
        else:
            settings = local.load()
            if not settings:
                raise local.LocalError('This machine is not initialized: run `networkcontext local init --app-dir <dir>`.')
            if args.action == 'start':
                data = {'status': local.start(settings), 'url': settings['url'], 'reader': settings['url'] + '/'}
            elif args.action == 'stop':
                data = {'status': local.stop(settings)}
            else:
                data = {'running': bool(local.running_pid(settings)), 'healthy': local.healthy(settings['url']), 'url': settings['url'],
                        'data': settings['data_dir'], 'app_dir': settings['app_dir']}
    except local.LocalError as error:
        raise Fail(2, str(error))
    say(dump(data, args.pretty), sys.stdout)
    return 0


def parse(argv):
    pretty = argparse.ArgumentParser(add_help=False)
    pretty.add_argument('--pretty', action='store_true', default=argparse.SUPPRESS, help='indent the JSON output')
    parser = argparse.ArgumentParser(prog='networkcontext', parents=[pretty],
                                     description='NetworkContext LinkedIn archive client. Ingests capture envelopes idempotently and answers questions with SQL.',
                                     epilog='Exit codes: 0 ok, 1 HTTP or transport error, 2 usage or configuration, 3 check found differences, 4 HTTP 409.')
    commands = parser.add_subparsers(dest='command', required=True, metavar='command')

    def add(name, text, *arguments):
        command = commands.add_parser(name, parents=[pretty], help=text, description=text)
        for argument, argument_help in arguments:
            command.add_argument(argument, help=argument_help)
        return command

    loc = add('local', 'manage the private loopback server on this machine')
    loc.add_argument('action', choices=['init', 'start', 'stop', 'status'])
    loc.add_argument('--app-dir', help='NetworkContext application directory (or NETWORKCONTEXT_APP_DIR)')
    loc.add_argument('--binary', help='pinned PocketContext server binary (or NETWORKCONTEXT_POCKETCONTEXT, or pocketcontext on PATH)')
    loc.add_argument('--port', type=int, default=local.DEFAULT_PORT)
    loc.add_argument('--email', default=local.DEFAULT_EMAIL)
    loc.add_argument('--name', default='Local owner')
    add('whoami', 'print the user id, name and server URL')
    add('check', 'compare the live schema with references/schema.json; exit 3 when they differ')
    add('schema', 'print the live SQL tables and columns')
    add('query', 'run one read-only SELECT', ('query', 'SQL text, or - for standard input'))
    add('sql', 'run one read-only SELECT', ('query', 'SQL text, or - for standard input'))
    add('get', 'read one record as an SQL row object with its permalink', ('collection', 'collection name'), ('id', 'record id'))
    search = add('search', 'full-text search', ('index', 'profiles, posts or messages'), ('text', 'words to find (all must match)'))
    search.add_argument('--limit', type=int, default=20)
    account = add('account', 'register or list the LinkedIn accounts you capture from')
    account.add_argument('action', choices=['add', 'list'])
    account.add_argument('--url', help='your LinkedIn profile URL')
    account.add_argument('--urn', help='your profile URN, when known')
    account.add_argument('--name', help='your display name')
    account.add_argument('--label', help='a short label for this account')
    runs = add('run', 'start, finish, fail or list ingestion runs')
    runs.add_argument('action', choices=['start', 'finish', 'fail', 'list'])
    runs.add_argument('id', nargs='?', help='run id for finish and fail')
    runs.add_argument('--surface', choices=SURFACES)
    runs.add_argument('--since', help='lookback: 7d, 12h, 2w, or an ISO date/time; default: incremental or the surface default')
    runs.add_argument('--account', help='account id when you have several')
    runs.add_argument('--agent', help='name of the capturing agent, for example antigravity')
    runs.add_argument('--reached-cutoff', action='store_true', help='finish: the capture reached `since` (or the end of a full list)')
    runs.add_argument('--note')
    runs.add_argument('--limit', type=int, default=20)
    ing = add('ingest', 'write capture envelopes (JSON Lines) into a running run', ('file', 'JSONL file, or - for standard input'))
    ing.add_argument('--run', required=True)
    ing.add_argument('--rejects', help='append rejected lines with reasons to this JSONL file')
    ing.add_argument('--chunk', type=int, default=200, help=argparse.SUPPRESS)
    add('profile', 'summarize one profile: identity, positions, posts, connection, conversations, CRM links', ('value', 'id, LinkedIn URL, URN or name'))
    thread = add('thread', 'print one conversation with its messages in time order', ('conversation', 'conversation id'))
    thread.add_argument('--limit', type=int, default=200)
    add('link', 'print the permalink of a record', ('collection', 'collection name'), ('id', 'record id'))
    crm = add('crm-link', 'record a confirmed match between a profile and a record in another Context app',
              ('profile', 'profile id or LinkedIn URL'), ('app', 'dealcontext, raisecontext, peoplecontext or other'), ('target_id', 'record id in that app'))
    crm.add_argument('--url', help='permalink of the target record')
    crm.add_argument('--note')
    add('normalize', 'print the canonical LinkedIn identifiers of a URL or URN', ('value', 'URL or URN'))
    add('logout', 'remove the cached token')
    args = parser.parse_args(argv)
    args.pretty = getattr(args, 'pretty', False)
    if args.command == 'run':
        if args.action == 'start' and not args.surface:
            parser.error('run start requires --surface')
        if args.action in ('finish', 'fail') and not args.id:
            parser.error(f'run {args.action} requires a run id')
    if args.command == 'account' and args.action == 'add' and not ((args.url or args.urn) and args.name):
        parser.error('account add requires --name and --url or --urn')
    if args.command == 'search' and args.index not in SEARCH_INDEXES:
        parser.error('index must be profiles, posts or messages')
    if args.command == 'ingest' and not 1 <= args.chunk <= 1000:
        parser.error('chunk must be 1–1000')
    return args


def main():
    try:
        with instrument_cli(service='networkcontext.client', opener=opener):
            return run(parse(sys.argv[1:]))
    except Fail as error:
        say(f'networkcontext: {error}')
        return error.code
    except KeyboardInterrupt:
        return 130
    except Exception as error:  # No traceback: keep the output short and free of request data.
        say(f'networkcontext: unexpected {type(error).__name__}: {error}')
        return 1


if __name__ == '__main__':
    sys.exit(main())
