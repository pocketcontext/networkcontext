#!/usr/bin/env python3
"""Unit checks for natural keys, ids and time handling (no server needed)."""
from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from networkcontext_client import keys  # noqa: E402


class Keys(unittest.TestCase):
    def test_ids_are_deterministic_and_well_formed(self):
        first = keys.record_id('profiles', 'urn:li:fsd_profile:ACoAAExampleAda01')
        self.assertRegex(first, r'^[a-z0-9]{15}$')
        self.assertEqual(first, keys.record_id('profiles', 'urn:li:fsd_profile:ACoAAExampleAda01'))
        self.assertNotEqual(first, keys.record_id('posts', 'urn:li:fsd_profile:ACoAAExampleAda01'))

    def test_canonical_urls_and_urns(self):
        self.assertEqual(keys.profile_url('https://it.linkedin.com/in/Ada-Example?trk=feed')[0], 'https://www.linkedin.com/in/ada-example/')
        self.assertEqual(keys.profile_url('https://evil.example/in/ada/'), (None, None))
        self.assertEqual(keys.profile_urn('urn:li:fs_miniProfile:ACoAAx1'), 'urn:li:fsd_profile:ACoAAx1')
        self.assertEqual(keys.organization_urn('urn:li:fsd_company:42'), 'urn:li:organization:42')
        self.assertEqual(keys.organization_url('https://www.linkedin.com/school/example-u/about/')[1:], ('example-u', 'school'))
        self.assertEqual(keys.post_urn('https://www.linkedin.com/feed/update/urn%3Ali%3Aactivity%3A7516052678246400001/'), 'urn:li:activity:7516052678246400001')
        self.assertEqual(keys.comment_urn('urn:li:comment:(activity:7516052678246400001,7516060000000000007)')[1], 'urn:li:activity:7516052678246400001')
        self.assertEqual(keys.thread_key(url='https://www.linkedin.com/messaging/thread/2-ZXhhbXBsZQ==/')[0], 'thread:2-ZXhhbXBsZQ==')

    def test_times(self):
        moment = datetime(2026, 10, 14, 8, 30, tzinfo=timezone.utc)
        urn = f'urn:li:activity:{int(moment.timestamp() * 1000) << 22}'
        self.assertEqual(keys.snowflake_time(urn), moment)
        self.assertIsNone(keys.snowflake_time('urn:li:activity:123456'))
        observed = datetime(2026, 10, 15, 9, tzinfo=timezone.utc)
        self.assertEqual(keys.item_time('3d', observed), (datetime(2026, 10, 12, 9, tzinfo=timezone.utc), 'approximate'))
        self.assertEqual(keys.item_time('2026-10-01', observed)[1], 'day')
        self.assertEqual(keys.item_time('2026-10-01T10:00:05Z', observed)[1], 'exact')
        self.assertEqual(keys.db_time(moment), '2026-10-14 08:30:00.000Z')
        self.assertEqual(keys.parse_since('7d', observed), datetime(2026, 10, 8, 9, tzinfo=timezone.utc))
        with self.assertRaises(keys.Invalid):
            keys.parse_since('yesterday')

    def test_text(self):
        self.assertEqual(keys.clean('  a\n\n é '), 'a é')
        self.assertEqual(keys.verbatim('  line one\n\nline two ', 100), 'line one\n\nline two')


if __name__ == '__main__':
    unittest.main()
