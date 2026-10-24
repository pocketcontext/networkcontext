#!/usr/bin/env python3
"""Test the installable skill in skills/networkcontext from a copy outside the repository.

  python3 tests/skill.py --binary /path/to/pocketcontext [--python /path/to/venv/python]
  python3 tests/skill.py --binary /path/to/pocketcontext --python ... --write-schema

The first form checks the skill's files, initializes a private local server through the copied client
(`local init/start/stop/status`), ingests the synthetic fixtures and runs every SQL example in
references/queries.md. The second form rewrites the schema snapshots after a migration.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / 'skills' / 'networkcontext'
FIXTURES = ROOT / 'tests' / 'fixtures'
sys.path.insert(0, str(ROOT / 'tests'))
import support  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--binary', required=True)
    parser.add_argument('--python', default=sys.executable, help='interpreter with the client dependencies installed')
    parser.add_argument('--write-schema', action='store_true')
    args = parser.parse_args()
    binary = str(Path(args.binary).resolve())

    # Static checks on the skill files.
    text = (SKILL / 'SKILL.md').read_text()
    assert text.startswith('---\nname: networkcontext\ndescription: '), 'frontmatter'
    for link in re.findall(r'\]\((references/[^)#]+)', text):
        assert (SKILL / link).is_file(), link
    assert os.access(SKILL / 'networkcontext', os.X_OK)
    for path in SKILL.rglob('*'):
        if path.is_file():
            content = path.read_text()
            for forbidden in ('_superusers', 'superuser upsert admin', '.envrc'):
                assert forbidden not in content, (path, forbidden)

    with tempfile.TemporaryDirectory(prefix='networkcontext-skill-') as temporary:
        tmp = Path(temporary)
        installed = tmp / 'installed'
        shutil.copytree(SKILL, installed)
        shutil.copytree(ROOT / 'src/networkcontext_client', installed / 'networkcontext_client', ignore=shutil.ignore_patterns('__pycache__'))
        port = support.free_port()
        env = {'PATH': os.environ.get('PATH', ''), 'HOME': str(tmp / 'home'), 'XDG_CACHE_HOME': str(tmp / 'cache'),
               'XDG_STATE_HOME': str(tmp / 'state'),
               'XDG_CONFIG_HOME': str(tmp / 'config'), 'NETWORKCONTEXT_HOME': str(tmp / 'data')}
        outputs = []

        def run(*argv, expected=0, stdin=None, **overrides):
            environment = {k: v for k, v in {**env, **overrides}.items() if v is not None}
            result = subprocess.run([args.python, str(installed / 'networkcontext'), *argv], cwd=tmp, env=environment,
                                    input=stdin, text=True, capture_output=True, timeout=60)
            assert result.returncode == expected, (argv, result.returncode, result.stdout, result.stderr)
            outputs.append(result.stdout + result.stderr)
            return result.stdout, result.stderr

        def data(*argv, **options):
            return json.loads(run(*argv, **options)[0])

        assert 'local init' in run('whoami', expected=2)[1]
        assert 'not initialized' in run('local', 'start', expected=2)[1]
        run('local', 'init', '--app-dir', str(tmp / 'missing'), '--binary', binary, expected=2)
        try:
            init = data('local', 'init', '--app-dir', str(ROOT), '--binary', binary, '--port', str(port))
            assert init['url'] == f'http://127.0.0.1:{port}'
            credentials = tmp / 'config/networkcontext/local.json'
            assert stat.S_IMODE(credentials.stat().st_mode) == 0o600 and stat.S_IMODE(credentials.parent.stat().st_mode) == 0o700
            password = json.loads(credentials.read_text())['password']
            run('local', 'init', '--app-dir', str(ROOT), '--binary', binary, '--port', str(port), expected=2)
            if args.write_schema:
                schema = data('schema')
                snapshot = {'tables': sorted([{'name': t['name'], 'columns': sorted(t['columns'], key=lambda c: c['name'])} for t in schema['tables']], key=lambda t: t['name'])}
                for target in (SKILL / 'references/schema.json', ROOT / 'src/networkcontext_client/schema.json', ROOT / 'ui/src/schema.snapshot.json'):
                    target.write_text(json.dumps(snapshot, indent=2) + '\n')
                print('Wrote schema snapshots from an isolated server')
                return
            assert data('whoami')['email'] == 'owner@networkcontext.localhost'
            session, = (tmp / 'state/networkcontext').glob('*.json')
            assert stat.S_IMODE(session.stat().st_mode) == 0o600 and password not in session.read_text()
            assert not (tmp / 'cache').exists(), 'the session lives in XDG state, not the cache'
            # A session left in the cache by an older client is moved once; logout removes both.
            legacy = tmp / 'cache/networkcontext' / session.name
            legacy.parent.mkdir(parents=True)
            session.rename(legacy)
            assert data('whoami')['email'] == 'owner@networkcontext.localhost'
            assert session.exists() and not legacy.exists()
            assert stat.S_IMODE(session.stat().st_mode) == 0o600
            legacy.write_text(session.read_text())
            run('logout')
            assert not session.exists() and not legacy.exists()
            assert data('whoami', NETWORKCONTEXT_SESSION_DIR=str(tmp / 'sessions'))['email'] == 'owner@networkcontext.localhost'
            assert len(list((tmp / 'sessions').glob('*.json'))) == 1 and not session.exists()
            assert run('check')[0].startswith('OK')
            snapshot = installed / 'networkcontext_client/schema.json'
            original = snapshot.read_text()
            snapshot.write_text('{"tables":[]}')
            assert 'posts' in run('check', expected=3)[0]
            snapshot.write_text(original)

            # Cross-origin requests from other sites get no CORS grant.
            request = urllib.request.Request(f'http://127.0.0.1:{port}/api/health', headers={'Origin': 'https://attacker.example'})
            with urllib.request.urlopen(request, timeout=10) as response:
                assert response.headers.get('Access-Control-Allow-Origin') in (None, ''), response.headers.get('Access-Control-Allow-Origin')

            data('account', 'add', '--url', 'https://www.linkedin.com/in/owner-example/', '--urn', 'urn:li:fsd_profile:ACoAAExampleOwn00', '--name', 'Owner Example')
            for surface, fixture in (('feed', 'feed.jsonl'), ('profiles', 'profiles.jsonl'), ('messages', 'messages.jsonl'),
                                     ('network', 'network.jsonl'), ('notifications', 'notifications.jsonl')):
                started = data('run', 'start', '--surface', surface, '--since', '2026-09-01', '--agent', 'skill-test')
                result = data('ingest', '--run', started['run'], '-', stdin=(FIXTURES / fixture).read_text())
                assert result['this_ingest']['rejected'] == 0, result
                data('run', 'finish', started['run'], '--reached-cutoff')
            assert len(data('run', 'list')) == 5
            post = data('query', "SELECT id FROM posts WHERE text LIKE 'We just%'")['rows'][0][0]
            got = data('get', 'posts', post)
            assert got['permalink'] == f'http://127.0.0.1:{port}/#/posts/{post}'
            assert data('link', 'posts', post, NETWORKCONTEXT_PUBLIC_URL='https://network.example.com')['permalink'] == f'https://network.example.com/#/posts/{post}'
            run('get', 'posts" WHERE 1=1 --', post, expected=2)
            run('query', 'SELECT * FROM users', expected=1)
            assert data('profile', 'Ada Example')['positions']
            conversation = data('query', 'SELECT id FROM conversations')['rows'][0][0]
            assert len(data('thread', conversation)['messages']) == 3

            # Every SQL example in queries.md runs against the schema.
            queries = (SKILL / 'references/queries.md').read_text()
            blocks = re.findall(r'```sql\n(.*?)```', queries, re.S)
            assert len(blocks) >= 4
            for block in blocks:
                for statement in [s.strip() for s in block.split(';') if s.strip()]:
                    data('query', '-', stdin=statement)
            unanswered = data('query', '-', stdin=blocks[1])['rows']
            assert unanswered and unanswered[0][3] == 'Ben Sample', unanswered

            before = data('query', 'SELECT count(*) FROM posts')['rows']
            status = data('local', 'status')
            assert status['running'] and status['healthy']
            assert data('local', 'stop')['status'] == 'stopped'
            assert not data('local', 'status')['healthy']
            assert data('local', 'start')['status'] == 'started'
            assert data('query', 'SELECT count(*) FROM posts')['rows'] == before == [[4]], 'data survives a restart'
            run('logout')
            for output in outputs:
                assert password not in output
            print('PASS: portable skill, local init/start/stop, private credentials, CORS, schema check, fixtures, queries.md, permalinks')
        finally:
            subprocess.run([args.python, str(installed / 'networkcontext'), 'local', 'stop'], env=env, capture_output=True, timeout=60)


if __name__ == '__main__':
    main()
