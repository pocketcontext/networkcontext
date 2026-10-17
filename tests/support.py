"""Shared helpers: a disposable NetworkContext server on an isolated temporary database."""
import contextlib
import json
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
ADMIN = ('admin@example.com', 'SyntheticAdminPassword123!')
PASSWORD = 'SyntheticUserPassword123!'


class Server:
    def __init__(self, base, data_dir):
        self.base, self.data_dir = base, data_dir
        self.admin = None

    def request(self, method, path, body=None, token=None, expected=200):
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['Authorization'] = token
        req = urllib.request.Request(self.base + path, data=None if body is None else json.dumps(body).encode(), headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as error:
            status, raw = error.code, error.read()
        allowed = expected if isinstance(expected, tuple) else (expected,)
        assert status in allowed, (method, path, status, raw.decode()[:2000])
        return json.loads(raw) if raw else None

    def user(self, email, name):
        record = self.request('POST', '/api/collections/users/records', {'email': email, 'name': name, 'password': PASSWORD, 'passwordConfirm': PASSWORD}, self.admin)
        token = self.request('POST', '/api/collections/users/auth-with-password', {'identity': email, 'password': PASSWORD})['token']
        return record, token

    def query(self, sql, token):
        result = self.request('POST', '/api/context/query', {'sql': sql}, token)
        return [dict(zip(result['columns'], row)) for row in result['rows']]


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


@contextlib.contextmanager
def server(binary, hooks_extra=None):
    with tempfile.TemporaryDirectory(prefix='networkcontext-test-') as tmp:
        hooks = Path(tmp) / 'pb_hooks'
        shutil.copytree(ROOT / 'pb_hooks', hooks)
        for name, source in (hooks_extra or {}).items():
            (hooks / name).write_text(source)
        common = [str(Path(binary).resolve()), '--dir', str(Path(tmp) / 'pb_data'), '--migrationsDir', str(ROOT / 'pb_migrations'), '--hooksDir', str(hooks)]
        result = subprocess.run(common + ['superuser', 'upsert', *ADMIN], cwd=ROOT, capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr
        port = free_port()
        with open(Path(tmp) / 'server.log', 'w+') as log:
            proc = subprocess.Popen(common + ['serve', '--http', f'127.0.0.1:{port}'], cwd=ROOT, stdout=log, stderr=log)
            srv = Server(f'http://127.0.0.1:{port}', Path(tmp) / 'pb_data')
            try:
                for _ in range(150):
                    try:
                        srv.request('GET', '/api/health')
                        break
                    except (OSError, AssertionError):
                        if proc.poll() is not None:
                            log.seek(0)
                            raise AssertionError(log.read())
                        time.sleep(.1)
                else:
                    raise AssertionError('Server startup timed out')
                srv.admin = srv.request('POST', '/api/collections/_superusers/auth-with-password', {'identity': ADMIN[0], 'password': ADMIN[1]})['token']
                yield srv
            finally:
                proc.terminate()
                proc.wait(timeout=15)
