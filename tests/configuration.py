#!/usr/bin/env python3
"""Saved login configuration and identity isolation, with synthetic authentication."""
import contextlib
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from networkcontext_client import cli


class ConfigurationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.environment = patch.dict(os.environ, {'XDG_STATE_HOME': self.tmp.name, 'XDG_CONFIG_HOME': self.tmp.name, 'NETWORKCONTEXT_USER_PASSWORD': 'synthetic-password'}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.cfg = {'url': 'https://one.example.com', 'email': 'one@example.com', 'password': 'synthetic-password'}
        self.auth = {'token': 'synthetic-token', 'record': {'id': 'syntheticuser01', 'collectionName': 'users', 'email': self.cfg['email']}}

    def sign_in(self, url=None, email=None):
        args = ['login', '--password', '--url', url or self.cfg['url'], '--email', email or self.cfg['email']]
        auth = {**self.auth, 'record': {**self.auth['record'], 'email': email or self.cfg['email']}}
        with patch.object(cli, 'send', return_value=(200, auth)), contextlib.redirect_stdout(io.StringIO()):
            cli.run(cli.parse(args))

    def test_fresh_login_persists_private_settings(self):
        self.sign_in()
        self.assertEqual(cli.read_default(), {'url': self.cfg['url'], 'email': self.cfg['email']})
        self.assertEqual(cli.config()['email'], self.cfg['email'])
        self.assertEqual(cli.load_session(cli.config())['token'], 'synthetic-token')
        self.assertEqual(stat.S_IMODE(cli.default_file().stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(cli.default_file().parent.stat().st_mode), 0o700)
        self.assertNotIn('token', cli.default_file().read_text())
        self.assertNotIn('synthetic-password', cli.session_file(self.cfg).read_text())

    def test_precedence_and_origin_isolation(self):
        self.sign_in()
        with patch.dict(os.environ, {'NETWORKCONTEXT_URL': 'https://two.example.com', 'NETWORKCONTEXT_USER_EMAIL': 'two@example.com'}):
            self.assertEqual(cli.config()['url'], 'https://two.example.com')
            self.assertIsNone(cli.load_session(cli.config()))
            args = cli.parse(['--url', self.cfg['url'], '--email', self.cfg['email'], 'whoami'])
            self.assertEqual(cli.config(args=args)['email'], self.cfg['email'])
            self.assertEqual(cli.load_session(cli.config(args=args))['token'], 'synthetic-token')
            args = cli.parse(['whoami', '--url', self.cfg['url'], '--email', self.cfg['email']])
            self.assertEqual(cli.config(args=args)['url'], self.cfg['url'])
        self.assertEqual(cli.read_default()['url'], self.cfg['url'])

    def test_failed_login_does_not_replace_default(self):
        self.sign_in()
        with patch.object(cli, 'send', return_value=(401, {})), self.assertRaises(cli.Fail):
            cli.run(cli.parse(['login', '--password', '--url', 'https://two.example.com', '--email', 'two@example.com']))
        self.assertEqual(cli.read_default()['email'], self.cfg['email'])

    def test_implicit_auth_does_not_choose_default(self):
        with patch.object(cli, 'send', return_value=(200, self.auth)):
            cli.login(self.cfg)
        self.assertFalse(cli.default_file().exists())

    def test_logout_forget_and_other_accounts(self):
        self.sign_in()
        self.sign_in('https://two.example.com', 'two@example.com')
        cli.run(cli.parse(['logout', '--url', self.cfg['url'], '--email', self.cfg['email'], '--forget']))
        self.assertEqual(cli.read_default()['email'], 'two@example.com')
        cli.run(cli.parse(['logout']))
        self.assertIsNone(cli.load_session(cli.config()))
        self.assertTrue(cli.default_file().exists())
        cli.run(cli.parse(['logout', '--forget']))
        self.assertFalse(cli.default_file().exists())

    def test_unsafe_defaults_rejected(self):
        self.sign_in()
        cli.default_file().chmod(0o644)
        with self.assertRaises(cli.Fail):
            cli.config()
        cli.default_file().unlink()
        target = Path(self.tmp.name) / 'foreign.json'
        target.write_text('{}')
        cli.default_file().symlink_to(target)
        with self.assertRaises(cli.Fail):
            cli.config()

    def test_local_password_never_sent_to_overridden_server(self):
        os.environ.pop('NETWORKCONTEXT_USER_PASSWORD')
        saved = {**self.cfg, 'password': 'local-only-secret'}
        with patch.object(cli.local, 'load', return_value=saved):
            args = cli.parse(['login', '--url', 'https://other.example.com', '--email', self.cfg['email']])
            self.assertIsNone(cli.config(args=args)['password'])
            with patch.object(cli, 'send') as send, self.assertRaises(cli.Fail):
                cli.run(args)
            send.assert_not_called()

    def test_invalid_remote_url_rejected_before_network(self):
        for url in ('http://remote.example.com', 'https://a:b@example.com', 'https://example.com?token=x'):
            with patch.object(cli, 'send') as send, self.assertRaises(cli.Fail):
                cli.run(cli.parse(['login', '--password', '--url', url, '--email', self.cfg['email']]))
            send.assert_not_called()


if __name__ == '__main__':
    unittest.main()
