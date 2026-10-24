#!/usr/bin/env python3
"""Exercise NetworkContext ingestion, deduplication, runs and access rules against an isolated temporary database.

  python3 tests/integration.py --binary /absolute/path/to/pinned/pocketcontext [--python /path/to/venv/python]

--python must have the client's dependencies installed (`uv pip install -e .`).
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402

ROOT = support.ROOT
FIXTURES = ROOT / 'tests' / 'fixtures'
sys.path.insert(0, str(ROOT / 'src'))
from networkcontext_client.keys import record_id  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--binary', required=True)
    parser.add_argument('--python', default=sys.executable)
    args = parser.parse_args()
    with support.server(args.binary) as s, tempfile.TemporaryDirectory(prefix='networkcontext-client-') as home:
        owner, token = s.user('owner@example.com', 'Owner')
        other, other_token = s.user('other@example.com', 'Other')
        q = lambda sql, auth=token: s.query(sql, auth)

        def nc(*argv, email='owner@example.com', expect=0):
            env = dict({k: v for k, v in os.environ.items() if k != 'NETWORKCONTEXT_SESSION_DIR'}, NETWORKCONTEXT_URL=s.base, NETWORKCONTEXT_USER_EMAIL=email, NETWORKCONTEXT_USER_PASSWORD=support.PASSWORD,
                       XDG_CACHE_HOME=home + '/cache', XDG_STATE_HOME=home + '/state', XDG_CONFIG_HOME=home + '/config', PYTHONPATH=str(ROOT / 'src'))
            result = subprocess.run([args.python, '-m', 'networkcontext_client', *argv], env=env, capture_output=True, text=True, cwd=home)
            assert result.returncode == expect, (argv, result.returncode, result.stdout, result.stderr)
            return json.loads(result.stdout) if result.stdout.strip().startswith(('{', '[')) else result

        # Identity: deterministic ids are identical in Python and the server hook, including non-ASCII keys.
        account = nc('account', 'add', '--url', 'https://www.linkedin.com/in/owner-example/', '--urn', 'urn:li:fsd_profile:ACoAAExampleOwn00', '--name', 'Owner Example')
        assert account['profile'] == record_id('profiles', 'urn:li:fsd_profile:ACoAAExampleOwn00')
        key = 'url:https://www.linkedin.com/company/caf%C3%A9-ü/'
        org = s.request('POST', '/api/collections/organizations/records', {'natural_key': key, 'key_kind': 'url', 'name': 'Café'}, token)
        assert org['id'] == record_id('organizations', key), org
        s.request('POST', '/api/collections/organizations/records', {'id': 'aaaaaaaaaaaaaaa', 'natural_key': 'url:x', 'key_kind': 'url'}, token, 400)
        s.request('PATCH', f'/api/collections/organizations/records/{org["id"]}', {'natural_key': 'url:changed'}, token, 400)
        s.request('PATCH', f'/api/collections/organizations/records/{org["id"]}', {'created_by': other['id']}, token, 400)
        s.request('DELETE', f'/api/collections/organizations/records/{org["id"]}', token=token, expected=403)
        again = nc('account', 'add', '--url', 'https://www.linkedin.com/in/owner-example/', '--name', 'Owner Example')
        assert again['account'] == account['account']
        nc('account', 'add', '--url', 'https://www.linkedin.com/in/owner-example/', '--name', 'Owner Example', email='other@example.com', expect=1)

        # First feed capture.
        run = nc('run', 'start', '--surface', 'feed', '--since', '2026-10-01', '--agent', 'test')
        assert run['since'].startswith('2026-10-01') and run['since_basis'] == 'requested'
        nc('run', 'start', '--surface', 'feed', expect=4)  # one running run per account and surface
        first = nc('ingest', '--run', run['run'], str(FIXTURES / 'feed.jsonl'))['this_ingest']
        assert first['created'] == 7 and first['rejected'] == 0, first
        audit_rows = q('SELECT count(*) AS n FROM audit_log')[0]['n']
        posts_before = q('SELECT id, updated FROM posts ORDER BY id')
        second = nc('ingest', '--run', run['run'], str(FIXTURES / 'feed.jsonl'))
        assert second['this_ingest']['created'] == 0 and second['this_ingest']['updated'] == 0 and second['this_ingest']['unchanged'] == 7, second
        assert second['run_counts']['seen'] == 14, second['run_counts']
        assert q('SELECT count(*) AS n FROM audit_log')[0]['n'] == audit_rows, 'an unchanged capture wrote history'
        assert len(q('SELECT id FROM posts')) == len(posts_before) == 4

        p1 = q("SELECT * FROM posts WHERE text LIKE 'We just%'")[0]
        assert p1['time_precision'] == 'exact' and p1['posted_at'].startswith('2026-10-14 08:30:00'), p1
        assert p1['reaction_count'] == 42 and p1['media_kind'] == 'article'
        assert p1['text'] == 'We just open-sourced our  data catalog.\nFeedback welcome!', 'text must be stored verbatim'
        reshare = q("SELECT reshare_of FROM posts WHERE text = 'Sharing this.'")[0]
        assert reshare['reshare_of'] == p1['id']
        reply = q("SELECT c.parent, c.post FROM comments c WHERE c.text = 'Thanks Ben!'")[0]
        assert reply['post'] == p1['id'] and reply['parent']
        assert q("SELECT kind FROM reactions")[0]['kind'] == 'celebrate'
        feed = q('SELECT reason, position, account FROM feed_items ORDER BY position')
        assert len(feed) == 4 and all(row['account'] == account['account'] for row in feed)
        assert feed[1]['reason'] == 'Ben Sample likes this'
        ben = q("SELECT id, natural_key, key_kind, headline FROM profiles WHERE name = 'Ben Sample'")[0]
        assert ben['key_kind'] == 'url' and ben['natural_key'] == 'https://www.linkedin.com/in/ben-sample/'
        assert q("SELECT p.id FROM organizations o JOIN posts p ON p.author_organization = o.id WHERE o.name = 'Example Labs'")

        # A later, overlapping capture: counters change without history; one new post; rejects never abort.
        update = nc('ingest', '--run', run['run'], str(FIXTURES / 'feed-next.jsonl'), '--rejects', home + '/rejects.jsonl')['this_ingest']
        assert update['created'] == 1 and update['unchanged'] == 1 and update['rejected'] == 3, update
        rejects = [json.loads(line) for line in open(home + '/rejects.jsonl')]
        assert {r['line'] for r in rejects} == {3, 4, 5}, rejects
        p1_after = q(f"SELECT reaction_count, comment_count, updated, last_seen_at, first_seen_at FROM posts WHERE id = '{p1['id']}'")[0]
        assert p1_after['reaction_count'] == 58 and p1_after['comment_count'] == 9
        assert p1_after['last_seen_at'].startswith('2026-10-16') and p1_after['first_seen_at'].startswith('2026-10-15')
        assert not q(f"SELECT id FROM audit_log WHERE record = '{p1['id']}' AND action = 'update'"), 'counter changes are not history'
        ada = q("SELECT headline FROM profiles WHERE name = 'Ada Example'")[0]
        assert ada['headline'] == 'Head of Data at Example Labs', 'a reference without a headline must not erase it'
        assert len(q('SELECT id FROM feed_items')) == 6, 'a post seen on another day is a new feed sighting'

        finished = nc('run', 'finish', run['run'], '--reached-cutoff')
        assert finished['status'] == 'complete' and finished['completed_at']
        state = q(f"SELECT high_water_at, last_complete_run FROM sync_state WHERE account = '{account['account']}' AND surface = 'feed'")[0]
        assert state['last_complete_run'] == run['run'] and state['high_water_at'] == run['started_at'].replace('T', ' ')
        nc('ingest', '--run', run['run'], str(FIXTURES / 'feed.jsonl'), expect=2)
        s.request('PATCH', f'/api/collections/ingestion_runs/records/{run["run"]}', {'status': 'running'}, token, 400)
        incremental = nc('run', 'start', '--surface', 'feed')
        assert incremental['since_basis'] == 'incremental' and incremental['since'] < run['started_at'].replace('T', ' '), incremental
        nc('run', 'fail', incremental['run'], '--note', 'synthetic failure')
        assert q(f"SELECT status FROM ingestion_runs WHERE id = '{incremental['run']}'")[0]['status'] == 'failed'
        s.request('PATCH', f'/api/collections/posts/records/{p1["id"]}', {'last_run': incremental['run']}, token, 400)

        # Profiles: a URN seen for the first time resolves to the profile already known by URL.
        prun = nc('run', 'start', '--surface', 'profiles')
        assert prun['since'] is None
        result = nc('ingest', '--run', prun['run'], str(FIXTURES / 'profiles.jsonl'))['this_ingest']
        assert result['rejected'] == 0, result
        ben_after = q(f"SELECT id, member_urn, headline, natural_key FROM profiles WHERE id = '{ben['id']}'")[0]
        assert ben_after['member_urn'] == 'urn:li:fsd_profile:ACoAAExampleBen02' and ben_after['headline'] == 'Founder & CEO, Sample Robotics'
        assert ben_after['natural_key'] == ben['natural_key'], 'natural keys never change'
        assert len(q("SELECT id FROM profiles WHERE name = 'Ben Sample'")) == 1
        assert {row['value'] for row in q(f"SELECT value FROM aliases WHERE profile = '{ben['id']}'")} == {'https://www.linkedin.com/in/ben-sample/', 'urn:li:fsd_profile:ACoAAExampleBen02'}
        positions = q("SELECT p.title, p.current, p.organization, p.organization_name FROM positions p JOIN profiles pr ON pr.id = p.profile WHERE pr.name = 'Ada Example' ORDER BY p.start_month")
        assert [p['title'] for p in positions] == ['Data Engineer', 'Head of Data'] and positions[1]['current'] and not positions[0]['organization']
        assert q("SELECT school_name FROM education")[0]['school_name'] == 'Example University'
        assert q("SELECT kind FROM organizations WHERE org_urn = 'urn:li:school:2000002'")[0]['kind'] == 'school'
        assert q("SELECT current_organization FROM profiles WHERE name = 'Ada Example'")[0]['current_organization']
        assert nc('ingest', '--run', prun['run'], str(FIXTURES / 'profiles.jsonl'))['this_ingest']['unchanged'] == 2
        history = q(f"SELECT changes FROM audit_log WHERE record = '{ben['id']}' AND action = 'update'")
        assert history and 'headline' in json.dumps(history), history
        nc('run', 'finish', prun['run'])

        # Conversations and messages, including hash-keyed messages without event URNs.
        mrun = nc('run', 'start', '--surface', 'messages', '--since', '30d')
        assert mrun['since_basis'] == 'requested'
        assert nc('ingest', '--run', mrun['run'], str(FIXTURES / 'messages.jsonl'))['this_ingest']['created'] == 4
        assert nc('ingest', '--run', mrun['run'], str(FIXTURES / 'messages.jsonl'))['this_ingest']['unchanged'] == 4
        conversation = q('SELECT id, thread_urn, url, account FROM conversations')[0]
        assert conversation['thread_urn'] == 'thread:2-ZXhhbXBsZVRocmVhZDE=' and conversation['url'].endswith('/messaging/thread/2-ZXhhbXBsZVRocmVhZDE=/')
        messages = q('SELECT key_kind, attachments FROM messages ORDER BY sent_at')
        assert [m['key_kind'] for m in messages] == ['hash', 'urn', 'hash'] and 'deck.pdf' in json.dumps(messages[2]['attachments'])
        assert len(q('SELECT id FROM conversation_participants')) == 2
        thread = nc('thread', conversation['id'])
        assert [m['body'] for m in thread['messages']][0].startswith('Hi!') and len(thread['participants']) == 2
        nc('run', 'finish', mrun['run'], '--reached-cutoff')

        # Network and notifications.
        nrun = nc('run', 'start', '--surface', 'network')
        assert nc('ingest', '--run', nrun['run'], str(FIXTURES / 'network.jsonl'))['this_ingest']['created'] == 4
        assert q("SELECT time_precision FROM invitations")[0]['time_precision'] == 'approximate'
        assert q("SELECT time_precision FROM connections LIMIT 1")[0]['time_precision'] == 'day'
        nc('ingest', '--run', nrun['run'], str(FIXTURES / 'feed.jsonl'), '--rejects', home + '/kind.jsonl')
        assert 'does not belong to surface' in open(home + '/kind.jsonl').read()
        nc('run', 'finish', nrun['run'], '--reached-cutoff')
        orun = nc('run', 'start', '--surface', 'notifications', '--since', '7d')
        assert nc('ingest', '--run', orun['run'], str(FIXTURES / 'notifications.jsonl'))['this_ingest']['created'] == 2
        assert nc('ingest', '--run', orun['run'], str(FIXTURES / 'notifications.jsonl'))['this_ingest']['unchanged'] == 2
        assert sorted(r['key_kind'] for r in q('SELECT key_kind FROM notifications')) == ['hash', 'urn']
        nc('run', 'finish', orun['run'])

        # Questions: profile summary, search, permalinks, CRM links.
        summary = nc('profile', 'https://www.linkedin.com/in/ben-sample/')
        assert summary['profile']['id'] == ben['id'] and summary['conversations'] and summary['connection']
        assert summary['permalink'] == f'{s.base}/#/profiles/{ben["id"]}'
        assert nc('profile', 'Example')['matches'], 'ambiguous names list candidates'
        hits = nc('search', 'posts', 'catalog')['hits']
        assert hits and hits[0]['id'] == p1['id'] and hits[0]['permalink'].endswith('/#/posts/' + p1['id'])
        assert nc('search', 'messages', 'deck')['hits']
        assert nc('search', 'profiles', 'robotics')['hits'][0]['id'] == ben['id']
        link = nc('crm-link', ben['id'], 'dealcontext', 'abcdefghijklmno', '--url', 'https://crm.example.com/#/people/abcdefghijklmno')
        assert link['profile'] == ben['id']
        nc('crm-link', ben['id'], 'dealcontext', 'abcdefghijklmno', expect=1)
        assert nc('normalize', 'https://linkedin.com/in/Ben-Sample?trk=x')['profile_url'] == 'https://www.linkedin.com/in/ben-sample/'

        # Ownership: another user cannot write into this account's private collections or runs.
        p = p1['id']
        s.request('POST', '/api/collections/feed_items/records', {'natural_key': 'x', 'key_kind': 'composite', 'account': account['account'], 'post': p}, other_token, 403)
        s.request('POST', '/api/collections/ingestion_runs/records', {'account': account['account'], 'surface': 'feed'}, other_token, 403)
        s.request('PATCH', f'/api/collections/ingestion_runs/records/{incremental["run"]}', {'note': 'x'}, other_token, (400, 403))
        own = s.request('POST', '/api/collections/ingestion_runs/records', {'account': account['account'], 'surface': 'export'}, token)
        s.request('PATCH', f'/api/collections/posts/records/{p}', {'last_run': own['id']}, other_token, 403)
        s.request('POST', '/api/collections/sync_state/records', {'account': account['account'], 'surface': 'feed'}, token, 403)
        s.request('POST', '/api/collections/audit_log/records', {}, token, 403)

        # SQL access rules.
        s.request('GET', '/api/context/schema', expected=401)
        s.request('POST', '/api/context/query', {'sql': 'SELECT * FROM users'}, token, 400)
        s.request('POST', '/api/context/query', {'sql': 'SELECT * FROM posts_search'}, token, 400)
        schema = s.request('GET', '/api/context/schema', token=token)
        assert sorted(schema['search']['indexes']) == ['messages', 'posts', 'profiles']
        assert 'users' not in {t['name'] for t in schema['tables']}
        print('NetworkContext integration checks passed')


if __name__ == '__main__':
    main()
