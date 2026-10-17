"""Run a private NetworkContext server on the loopback interface.

The server is the pinned PocketContext binary run from a NetworkContext
application directory (migrations, hooks, reader). `local init` creates one
ordinary `users` account with a random password stored in a 0600 file; the
superuser it needs for that is given a random password that is never kept.
"""
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

DEFAULT_PORT = 8095
DEFAULT_EMAIL = 'owner@networkcontext.localhost'


class LocalError(Exception):
    pass


def config_dir():
    return Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'networkcontext'


def data_dir():
    return Path(os.environ.get('NETWORKCONTEXT_HOME') or Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local' / 'share') / 'networkcontext')


def credentials_file():
    return config_dir() / 'local.json'


def load():
    try:
        value = json.loads(credentials_file().read_text())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) and value.get('url') and value.get('email') else None


def write_private(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    with tempfile.NamedTemporaryFile('w', dir=path.parent, prefix='.local-', delete=False) as handle:
        os.fchmod(handle.fileno(), 0o600)
        json.dump(value, handle, indent=2)
        temporary = handle.name
    os.replace(temporary, path)


def app_dir(value):
    path = Path(value or os.environ.get('NETWORKCONTEXT_APP_DIR') or '').expanduser()
    if not value and not os.environ.get('NETWORKCONTEXT_APP_DIR'):
        raise LocalError('Pass --app-dir (or set NETWORKCONTEXT_APP_DIR) to a NetworkContext application directory containing pb_migrations/ and pb_hooks/.')
    path = path.resolve()
    if not (path / 'pb_migrations').is_dir() or not (path / 'pb_hooks').is_dir() or not (path / 'pocketcontext.json').is_file():
        raise LocalError(f'{path} is not a NetworkContext application directory (pb_migrations/, pb_hooks/ and pocketcontext.json are required).')
    return path


def binary(value):
    found = value or os.environ.get('NETWORKCONTEXT_POCKETCONTEXT') or shutil.which('pocketcontext')
    if not found or not os.access(found, os.X_OK):
        raise LocalError('PocketContext server binary not found: pass --binary or install the version in the application\'s POCKETCONTEXT_VERSION.')
    return str(Path(found).resolve())


def command(settings):
    data = Path(settings['data_dir'])
    return [settings['binary'], '--dir', str(data / 'pb_data'), '--migrationsDir', str(Path(settings['app_dir']) / 'pb_migrations'),
            '--hooksDir', str(Path(settings['app_dir']) / 'pb_hooks')]


def healthy(url, timeout=2):
    try:
        with urllib.request.urlopen(url + '/up', timeout=timeout) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def pid_file(settings):
    return Path(settings['data_dir']) / 'server.pid'


def running_pid(settings):
    try:
        pid = int(pid_file(settings).read_text().strip())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def start(settings, wait=30):
    if running_pid(settings):
        if healthy(settings['url']):
            return 'already running'
        raise LocalError('A recorded server process exists but does not answer; run `networkcontext local stop` first.')
    port = settings['port']
    data = Path(settings['data_dir'])
    data.mkdir(mode=0o700, parents=True, exist_ok=True)
    log = open(data / 'server.log', 'a')
    origin = f'http://127.0.0.1:{port}'
    # Run from the application directory so pocketcontext.json and the reader resolve.
    process = subprocess.Popen(command(settings) + ['serve', '--http', f'127.0.0.1:{port}', '--origins', origin],
                               cwd=settings['app_dir'], stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
    pid_file(settings).write_text(str(process.pid))
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pid_file(settings).unlink(missing_ok=True)
            raise LocalError(f'The server exited during startup; see {data / "server.log"}.')
        if healthy(settings['url'], 1):
            return 'started'
        time.sleep(.2)
    raise LocalError(f'The server did not become healthy within {wait} seconds; see {data / "server.log"}.')


def stop(settings, wait=20):
    pid = running_pid(settings)
    if not pid:
        pid_file(settings).unlink(missing_ok=True)
        return 'not running'
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            pid_file(settings).unlink(missing_ok=True)
            return 'stopped'
        time.sleep(.2)
    raise LocalError(f'Process {pid} did not stop within {wait} seconds.')


def post(url, path, body, token=None):
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = token
    request = urllib.request.Request(url + path, data=json.dumps(body).encode(), headers=headers, method='POST')
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        raise LocalError(f'HTTP {error.code} from {path} during initialization') from None


def init(app, server, port, email, name):
    if load():
        raise LocalError(f'{credentials_file()} already exists; this machine is initialized. Use `networkcontext local start`.')
    data = data_dir()
    if (data / 'pb_data').exists():
        raise LocalError(f'{data / "pb_data"} already exists without local credentials; move it aside before initializing.')
    settings = {'url': f'http://127.0.0.1:{port}', 'port': port, 'app_dir': str(app), 'binary': server,
                'data_dir': str(data), 'email': email}
    data.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(data, 0o700)
    admin_email, admin_password = 'operator@networkcontext.localhost', secrets.token_urlsafe(32)
    result = subprocess.run(command(settings) + ['superuser', 'upsert', admin_email, admin_password], cwd=app, capture_output=True, text=True)
    if result.returncode:
        raise LocalError('Could not create the provisioning superuser; check the binary version.')
    start(settings)
    admin = post(settings['url'], '/api/collections/_superusers/auth-with-password', {'identity': admin_email, 'password': admin_password})['token']
    password = secrets.token_urlsafe(32)
    user = post(settings['url'], '/api/collections/users/records',
                {'email': email, 'name': name, 'password': password, 'passwordConfirm': password}, admin)
    write_private(credentials_file(), dict(settings, password=password, user_id=user['id']))
    return settings, user
