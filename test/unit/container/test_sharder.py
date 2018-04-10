# Copyright (c) 2010-2017 OpenStack Foundation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import hashlib
import json
import random

import eventlet
import os
import shutil
from contextlib import contextmanager
from tempfile import mkdtemp

import mock
import unittest

from collections import defaultdict

from swift.container.backend import ContainerBroker, UNSHARDED, SHARDING, \
    SHARDED
from swift.container.sharder import ContainerSharder, sharding_enabled
from swift.common.utils import ShardRange, Timestamp, hash_path, \
    encode_timestamps, parse_db_filename, quorum_size
from test import annotate_failure

from test.unit import FakeLogger, debug_logger, FakeRing, \
    make_timestamp_iter, unlink_files, mocked_http_conn, mock_timestamp_now


class TestSharder(unittest.TestCase):
    def setUp(self):
        self.tempdir = mkdtemp()
        self.ts_iter = make_timestamp_iter()

    def _assert_shard_ranges_equal(self, expected, actual):
        self.assertEqual([dict(sr) for sr in expected],
                         [dict(sr) for sr in actual])

    def _make_broker(self, account='a', container='c', epoch=None,
                     device='sda', part=0, hash_=None):
        hash_ = hash_ or hashlib.md5(container).hexdigest()
        datadir = os.path.join(
            self.tempdir, device, 'containers', str(part), hash_[-3:], hash_)
        if epoch:
            filename = '%s_%s.db' % (hash, epoch)
        else:
            filename = hash_ + '.db'
        db_file = os.path.join(datadir, filename)
        broker = ContainerBroker(
            db_file, account=account, container=container,
            logger=debug_logger())
        broker.initialize()
        return broker

    def _make_shard_ranges(self, bounds, state=None):
        return [ShardRange('.shards_a/c_%s' % upper, Timestamp.now(),
                           lower, upper, state=state)
                for lower, upper in bounds]

    def ts_encoded(self):
        # make a unique timestamp string with multiple timestamps encoded;
        # use different deltas between component timestamps
        timestamps = [next(self.ts_iter) for i in range(4)]
        return encode_timestamps(
            timestamps[0], timestamps[1], timestamps[3])

    def tearDown(self):
        shutil.rmtree(self.tempdir, ignore_errors=True)

    def test_init(self):
        def do_test(conf, expected):
            with mock.patch(
                    'swift.container.sharder.internal_client.InternalClient') \
                    as mock_ic:
                with mock.patch('swift.common.db_replicator.ring.Ring')\
                        as mock_ring:
                    sharder = ContainerSharder(conf)
            mock_ring.assert_called_once_with(
                '/etc/swift', ring_name='container')
            self.assertEqual(
                'container-sharder', sharder.logger.logger.name)
            for k, v in expected.items():
                self.assertTrue(hasattr(sharder, k), 'Missing attr %s' % k)
                self.assertEqual(v, getattr(sharder, k),
                                 'Incorrect value: expected %s=%s but got %s' %
                                 (k, v, getattr(sharder, k)))
            return mock_ic

        expected = {
            'mount_check': True, 'bind_ip': '0.0.0.0', 'port': 6201,
            'per_diff': 1000, 'max_diffs': 100, 'interval': 30,
            'node_timeout': 10, 'conn_timeout': 5,
            'rsync_compress': False,
            'rsync_module': '{replication_ip}::container',
            'reclaim_age': 86400 * 7,
            'shard_shrink_point': 0.25,
            'shrink_merge_point': 0.75,
            'shard_container_size': 10000000,
            'split_size': 5000000,
            'shard_batch_size': 2,
            'scanner_batch_size': 10,
            'rcache': '/var/cache/swift/container.recon',
            'shards_account_prefix': '.shards_',
            'auto_shard': False,
            'recon_candidates_limit': 5
        }
        mock_ic = do_test({}, expected)
        mock_ic.assert_called_once_with(
            '/etc/swift/internal-client.conf', 'Swift Container Sharder', 3,
            allow_modify_pipeline=False)

        conf = {
            'mount_check': False, 'bind_ip': '10.11.12.13', 'bind_port': 62010,
            'per_diff': 2000, 'max_diffs': 200, 'interval': 60,
            'node_timeout': 20, 'conn_timeout': 1,
            'rsync_compress': True,
            'rsync_module': '{replication_ip}::container_sda/',
            'reclaim_age': 86400 * 14,
            'shard_shrink_point': 35,
            'shard_shrink_merge_point': 85,
            'shard_container_size': 20000000,
            'shard_batch_size': 4,
            'shard_scanner_batch_size': 8,
            'request_tries': 2,
            'internal_client_conf_path': '/etc/swift/my-sharder-ic.conf',
            'recon_cache_path': '/var/cache/swift-alt',
            'auto_create_account_prefix': '...',
            'auto_shard': 'yes',
            'recon_candidates_limit': 10
        }
        expected = {
            'mount_check': False, 'bind_ip': '10.11.12.13', 'port': 62010,
            'per_diff': 2000, 'max_diffs': 200, 'interval': 60,
            'node_timeout': 20, 'conn_timeout': 1,
            'rsync_compress': True,
            'rsync_module': '{replication_ip}::container_sda',
            'reclaim_age': 86400 * 14,
            'shard_shrink_point': 0.35,
            'shrink_merge_point': 0.85,
            'shard_container_size': 20000000,
            'split_size': 10000000,
            'shard_batch_size': 4,
            'scanner_batch_size': 8,
            'rcache': '/var/cache/swift-alt/container.recon',
            'shards_account_prefix': '...shards_',
            'auto_shard': True,
            'recon_candidates_limit': 10
        }
        mock_ic = do_test(conf, expected)
        mock_ic.assert_called_once_with(
            '/etc/swift/my-sharder-ic.conf', 'Swift Container Sharder', 2,
            allow_modify_pipeline=False)

        with self.assertRaises(ValueError) as cm:
            do_test({'shard_shrink_point': 101}, {})
        self.assertIn(
            'greater than 0, less than 100, not "101"', cm.exception.message)
        self.assertIn('shard_shrink_point', cm.exception.message)

        with self.assertRaises(ValueError) as cm:
            do_test({'shard_shrink_merge_point': 101}, {})
        self.assertIn(
            'greater than 0, less than 100, not "101"', cm.exception.message)
        self.assertIn('shard_shrink_merge_point', cm.exception.message)

    def _assert_stats(self, expected, sharder, category):
        # assertEqual doesn't work with a defaultdict
        stats = sharder.stats['sharding'][category]
        for k, v in expected.items():
            actual = stats[k]
            self.assertEqual(
                v, actual, 'Expected %s but got %s for %s in %s' %
                           (v, actual, k, stats))
        return stats

    def test_increment_stats(self):
        with self._mock_sharder() as sharder:
            sharder._increment_stat('visited', 'success')
            sharder._increment_stat('visited', 'success')
            sharder._increment_stat('visited', 'failure')
            sharder._increment_stat('visited', 'completed')
            sharder._increment_stat('cleaved', 'success')
            sharder._increment_stat('scanned', 'found', step=4)
        expected = {'success': 2,
                    'failure': 1,
                    'completed': 1}
        self._assert_stats(expected, sharder, 'visited')
        self._assert_stats({'success': 1}, sharder, 'cleaved')
        self._assert_stats({'found': 4}, sharder, 'scanned')

    def test_increment_stats_with_statsd(self):
        with self._mock_sharder() as sharder:
            sharder._increment_stat('visited', 'success', statsd=True)
            sharder._increment_stat('visited', 'success', statsd=True)
            sharder._increment_stat('visited', 'failure', statsd=True)
            sharder._increment_stat('visited', 'failure', statsd=False)
            sharder._increment_stat('visited', 'completed')
        expected = {'success': 2,
                    'failure': 2,
                    'completed': 1}
        self._assert_stats(expected, sharder, 'visited')
        counts = sharder.logger.get_increment_counts()
        self.assertEqual(2, counts.get('visited_success'))
        self.assertEqual(1, counts.get('visited_failure'))
        self.assertIsNone(counts.get('visited_completed'))

    def test_run_forever(self):
        @contextmanager
        def setup_mocks():
            mod = 'swift.container.sharder'
            with mock.patch(mod + '.internal_client.InternalClient'):
                with mock.patch('swift.common.db_replicator.ring.Ring'):
                    conf = {'recon_cache_path': self.tempdir,
                            'devices': self.tempdir}
                    sharder = ContainerSharder(conf, logger=FakeLogger())
                    sharder.ring = FakeRing()
                    sharder._check_node = lambda *args: True
                    sharder.logger.clear()
                    yield sharder

        with setup_mocks() as sharder:
            for container in ('c1', 'c2'):
                broker = self._make_broker(
                    container=container, hash_=container + 'hash',
                    device=sharder.ring.devs[0]['device'], part=0)
                broker.update_metadata({'X-Container-Sysmeta-Sharding':
                                        ('true', next(self.ts_iter).internal)})

            fake_stats = {
                'scanned': {'attempted': 1, 'success': 1, 'failure': 0,
                            'found': 2, 'min_time': 99, 'max_time': 123},
                'created': {'attempted': 1, 'success': 1, 'failure': 1},
                'cleaved': {'attempted': 1, 'success': 1, 'failure': 0,
                            'min_time': 0.01, 'max_time': 1.3},
                'misplaced': {'attempted': 1, 'success': 1, 'failure': 0,
                              'found': 1, 'placed': 1, 'unplaced': 0},
                'audit_root': {'attempted': 5, 'success': 4, 'failure': 1},
                'audit_shard': {'attempted': 2, 'success': 2, 'failure': 0},
                'sharding_candidates': {'found': 0, 'top': []}
            }
            # NB these are time increments not absolute times...
            fake_periods = [1, 2, 3, 3600, 4, 15, 15]
            fake_periods_iter = iter(fake_periods)
            recon_data = []

            def mock_dump_recon_cache(data, *args):
                recon_data.append(data)

            with mock.patch('swift.container.sharder.time.time') as fake_time:
                def fake_process_broker(*args, **kwargs):
                    # increment time and inject some fake stats
                    try:
                        fake_time.return_value += next(fake_periods_iter)
                    except StopIteration:
                        # bail out
                        fake_time.side_effect = Exception('Test over')
                    sharder.stats['sharding'].update(fake_stats)

                with mock.patch(
                        'swift.container.sharder.time.sleep') as mock_sleep:
                    with mock.patch(
                            'swift.container.sharder.dump_recon_cache',
                            mock_dump_recon_cache):
                        fake_time.return_value = next(fake_periods_iter)
                        sharder._process_broker = fake_process_broker
                        with self.assertRaises(Exception) as cm:
                            sharder.run_forever()

            self.assertEqual('Test over', cm.exception.message)
            # expect initial random sleep then one sleep between first and
            # second pass
            self.assertEqual(2, mock_sleep.call_count)
            self.assertLessEqual(mock_sleep.call_args_list[0][0][0], 30)
            self.assertLessEqual(mock_sleep.call_args_list[1][0][0],
                                 30 - fake_periods[0])

            lines = sharder.logger.get_lines_for_level('info')
            categories = ('visited', 'scanned', 'created', 'cleaved',
                          'misplaced', 'audit_root', 'audit_shard')

            def check_categories():
                for category in categories:
                    line = lines.pop(0)
                    self.assertIn(category, line)
                    for k, v in fake_stats.get(category, {}).items():
                        self.assertIn('%s:%s' % (k, v), line)

            def check_logs(cycle_time, expect_periodic_stats=False):
                self.assertIn('Container sharder cycle starting', lines.pop(0))
                check_categories()
                if expect_periodic_stats:
                    check_categories()
                self.assertIn('Container sharder cycle completed: %.02fs' %
                              cycle_time, lines.pop(0))

            check_logs(sum(fake_periods[1:3]))
            check_logs(sum(fake_periods[3:5]), expect_periodic_stats=True)
            check_logs(sum(fake_periods[5:7]))
            # final cycle start but then exception pops to terminate test
            self.assertIn('Container sharder cycle starting', lines.pop(0))
            self.assertFalse(lines)
            lines = sharder.logger.get_lines_for_level('error')
            self.assertIn('Exception in sharder', lines[0])

            def check_recon(data, time, last, expected_stats):
                self.assertEqual(time, data['sharding_time'])
                self.assertEqual(last, data['sharding_last'])
                self.assertEqual(
                    expected_stats, dict(data['sharding_stats']['sharding']))

            self.assertEqual(4, len(recon_data))
            # stats report at end of first cycle
            fake_stats.update({'visited': {'attempted': 2, 'skipped': 0,
                                           'success': 2, 'failure': 0,
                                           'completed': 0}})
            check_recon(recon_data[0], sum(fake_periods[1:3]),
                        sum(fake_periods[:3]), fake_stats)
            # periodic stats report during second cycle
            fake_stats.update({'visited': {'attempted': 1, 'skipped': 0,
                                           'success': 1, 'failure': 0,
                                           'completed': 0}})
            check_recon(recon_data[1], fake_periods[3],
                        sum(fake_periods[:4]), fake_stats)
            # stats report at end of second cycle
            check_recon(recon_data[2], fake_periods[4], sum(fake_periods[:5]),
                        fake_stats)
            # stats report at end of third cycle
            fake_stats.update({'visited': {'attempted': 2, 'skipped': 0,
                                           'success': 2, 'failure': 0,
                                           'completed': 0}})
            check_recon(recon_data[3], sum(fake_periods[5:7]),
                        sum(fake_periods[:7]), fake_stats)

    @contextmanager
    def _mock_sharder(self, conf=None, replicas=3):
        conf = conf or {}
        conf['devices'] = self.tempdir
        with mock.patch(
                'swift.container.sharder.internal_client.InternalClient'):
            with mock.patch(
                    'swift.common.db_replicator.ring.Ring',
                    lambda *args, **kwargs: FakeRing(replicas=replicas)):
                sharder = ContainerSharder(conf, logger=debug_logger())
                sharder._local_device_ids = {0, 1, 2}
                sharder._replicate_object = mock.MagicMock(
                    return_value=(True, [True] * sharder.ring.replica_count))
                sharder.shard_cleanups = dict()  # TODO: try to eliminate this
                yield sharder

    def _get_raw_object_records(self, broker):
        return [list(obj) for obj in broker.list_objects_iter(
            10, '', '', '', '', include_deleted=True,
            transform_func=lambda record, policy_index: record)]

    def _check_objects(self, expected_objs, shard_db):
        shard_broker = ContainerBroker(shard_db)
        # use list_objects_iter with no-op transform_func to get back actual
        # un-transformed rows with encoded timestamps
        shard_objs = self._get_raw_object_records(shard_broker)
        expected_objs = [list(obj) for obj in expected_objs]
        self.assertEqual(expected_objs, shard_objs)

    def _check_shard_range(self, expected, actual):
        expected_dict = dict(expected)
        actual_dict = dict(actual)
        self.assertGreater(actual_dict.pop('meta_timestamp'),
                           expected_dict.pop('meta_timestamp'))
        self.assertEqual(expected_dict, actual_dict)

    def test_cleave_root(self):
        broker = self._make_broker()
        objects = [
            # shard 0
            ('a', self.ts_encoded(), 10, 'text/plain', 'etag_a', 0),
            ('here', self.ts_encoded(), 10, 'text/plain', 'etag_here', 0),
            # shard 1
            ('m', self.ts_encoded(), 1, 'text/plain', 'etag_m', 0),
            ('n', self.ts_encoded(), 2, 'text/plain', 'etag_n', 0),
            ('there', self.ts_encoded(), 3, 'text/plain', 'etag_there', 0),
            # shard 2
            ('where', self.ts_encoded(), 100, 'text/plain', 'etag_where', 0),
            # shard 3
            ('x', self.ts_encoded(), 0, '', '', 1),  # deleted
            ('y', self.ts_encoded(), 1000, 'text/plain', 'etag_y', 0),
            # shard 4
            ('yyyy', self.ts_encoded(), 14, 'text/plain', 'etag_yyyy', 0),
        ]
        for obj in objects:
            broker.put_object(*obj)
        context_ref = broker._get_context_ref()
        initial_root_info = broker.get_info()
        own_shard_range = broker.get_own_shard_range()
        own_shard_range.update_state(ShardRange.SHARDING)
        own_shard_range.epoch = Timestamp.now()
        broker.merge_shard_ranges([own_shard_range])

        shard_bounds = (('', 'here'), ('here', 'there'),
                        ('there', 'where'), ('where', 'yonder'),
                        ('yonder', ''))
        shard_ranges = self._make_shard_ranges(shard_bounds)
        expected_shard_dbs = []
        for shard_range in shard_ranges:
            db_hash = hash_path(shard_range.account, shard_range.container)
            expected_shard_dbs.append(
                os.path.join(self.tempdir, 'sda', 'containers', '0',
                             db_hash[-3:], db_hash, db_hash + '.db'))

        # used to accumulate stats from sharded dbs
        total_shard_stats = {'object_count': 0, 'bytes_used': 0}
        # run cleave - no shard ranges, nothing happens
        with self._mock_sharder() as sharder:
            self.assertTrue(sharder._cleave(broker))

        self.assertEqual({'misplaced_done': True, 'ref': context_ref},
                         broker.load_cleave_context())
        self.assertEqual(UNSHARDED, broker.get_db_state())
        sharder._replicate_object.assert_not_called()
        for db in expected_shard_dbs:
            with annotate_failure(db):
                self.assertFalse(os.path.exists(db))

        # run cleave - all shard ranges in found state, nothing happens
        broker.merge_shard_ranges(shard_ranges[:4])
        self.assertTrue(broker.set_sharding_state())

        with self._mock_sharder() as sharder:
            self.assertTrue(sharder._cleave(broker))

        self.assertEqual({'misplaced_done': True, 'ref': context_ref},
                         broker.load_cleave_context())

        self.assertEqual(SHARDING, broker.get_db_state())
        sharder._replicate_object.assert_not_called()
        for db in expected_shard_dbs:
            with annotate_failure(db):
                self.assertFalse(os.path.exists(db))
        for shard_range in broker.get_shard_ranges():
            with annotate_failure(shard_range):
                self.assertEqual(ShardRange.FOUND, shard_range.state)

        # move first shard range to created state, first shard range is cleaved
        shard_ranges[0].update_state(ShardRange.CREATED)
        broker.merge_shard_ranges(shard_ranges[:1])
        with self._mock_sharder() as sharder:
            self.assertTrue(sharder._cleave(broker))

        expected = {'attempted': 1, 'success': 1, 'failure': 0,
                    'min_time': mock.ANY, 'max_time': mock.ANY}
        stats = self._assert_stats(expected, sharder, 'cleaved')
        self.assertIsInstance(stats['min_time'], float)
        self.assertIsInstance(stats['max_time'], float)
        self.assertLessEqual(stats['min_time'], stats['max_time'])
        self.assertEqual(SHARDING, broker.get_db_state())
        sharder._replicate_object.assert_called_once_with(
            0, expected_shard_dbs[0], 0)
        shard_broker = ContainerBroker(expected_shard_dbs[0])
        shard_own_sr = shard_broker.get_own_shard_range()
        self.assertEqual(ShardRange.CLEAVED, shard_own_sr.state)
        shard_info = shard_broker.get_info()
        total_shard_stats['object_count'] += shard_info['object_count']
        total_shard_stats['bytes_used'] += shard_info['bytes_used']

        updated_shard_ranges = broker.get_shard_ranges()
        self.assertEqual(4, len(updated_shard_ranges))
        # update expected state and metadata, check cleaved shard range
        shard_ranges[0].bytes_used = 20
        shard_ranges[0].object_count = 2
        shard_ranges[0].state = ShardRange.CLEAVED
        self._check_shard_range(shard_ranges[0], updated_shard_ranges[0])
        self._check_objects(objects[:2], expected_shard_dbs[0])
        # other shard ranges should be unchanged
        for i in range(1, len(shard_ranges)):
            with annotate_failure(i):
                self.assertFalse(os.path.exists(expected_shard_dbs[i]))
        for i in range(1, len(updated_shard_ranges)):
            with annotate_failure(i):
                self.assertEqual(dict(shard_ranges[i]),
                                 dict(updated_shard_ranges[i]))
        self.assertEqual({'cursor': 'here', 'misplaced_done': True,
                          'ref': context_ref}, broker.load_cleave_context())
        unlink_files(expected_shard_dbs)

        # move more shard ranges to created state
        for i in range(1, 4):
            shard_ranges[i].update_state(ShardRange.CREATED)
        broker.merge_shard_ranges(shard_ranges[1:4])

        # replication of next shard range is not sufficiently successful
        with self._mock_sharder() as sharder:
            quorum = quorum_size(sharder.ring.replica_count)
            successes = [True] * (quorum - 1)
            fails = [False] * (sharder.ring.replica_count - len(successes))
            responses = successes + fails
            random.shuffle(responses)
            sharder._replicate_object = mock.MagicMock(
                side_effect=((False, responses),))
            self.assertFalse(sharder._cleave(broker))
        sharder._replicate_object.assert_called_once_with(
            0, expected_shard_dbs[1], 0)

        # cleaving state is unchanged
        updated_shard_ranges = broker.get_shard_ranges()
        self.assertEqual(4, len(updated_shard_ranges))
        for i in range(1, len(updated_shard_ranges)):
            with annotate_failure(i):
                self.assertEqual(dict(shard_ranges[i]),
                                 dict(updated_shard_ranges[i]))
        self.assertEqual({'cursor': 'here', 'misplaced_done': True,
                          'ref': context_ref}, broker.load_cleave_context())

        # try again, this time replication is sufficiently successful
        with self._mock_sharder() as sharder:
            successes = [True] * quorum
            fails = [False] * (sharder.ring.replica_count - len(successes))
            responses1 = successes + fails
            responses2 = fails + successes
            sharder._replicate_object = mock.MagicMock(
                side_effect=((False, responses1), (False, responses2)))
            self.assertFalse(sharder._cleave(broker))

        expected = {'attempted': 2, 'success': 2, 'failure': 0,
                    'min_time': mock.ANY, 'max_time': mock.ANY}
        stats = self._assert_stats(expected, sharder, 'cleaved')
        self.assertIsInstance(stats['min_time'], float)
        self.assertIsInstance(stats['max_time'], float)
        self.assertLessEqual(stats['min_time'], stats['max_time'])

        self.assertEqual(SHARDING, broker.get_db_state())
        sharder._replicate_object.assert_has_calls(
            [mock.call(0, db, 0) for db in expected_shard_dbs[1:3]]
        )
        for db in expected_shard_dbs[1:3]:
            shard_broker = ContainerBroker(db)
            shard_own_sr = shard_broker.get_own_shard_range()
            self.assertEqual(ShardRange.CLEAVED, shard_own_sr.state)
            shard_info = shard_broker.get_info()
            total_shard_stats['object_count'] += shard_info['object_count']
            total_shard_stats['bytes_used'] += shard_info['bytes_used']

        updated_shard_ranges = broker.get_shard_ranges()
        self.assertEqual(4, len(updated_shard_ranges))

        # only 2 are cleaved per batch
        # update expected state and metadata, check cleaved shard ranges
        shard_ranges[1].bytes_used = 6
        shard_ranges[1].object_count = 3
        shard_ranges[1].state = ShardRange.CLEAVED
        shard_ranges[2].bytes_used = 100
        shard_ranges[2].object_count = 1
        shard_ranges[2].state = ShardRange.CLEAVED
        for i in range(0, 3):
            with annotate_failure(i):
                self._check_shard_range(
                    shard_ranges[i], updated_shard_ranges[i])
        self._check_objects(objects[2:5], expected_shard_dbs[1])
        self._check_objects(objects[5:6], expected_shard_dbs[2])
        # other shard ranges should be unchanged
        self.assertFalse(os.path.exists(expected_shard_dbs[0]))
        for i, db in enumerate(expected_shard_dbs[3:], 3):
            with annotate_failure(i):
                self.assertFalse(os.path.exists(db))
        for i, updated_shard_range in enumerate(updated_shard_ranges[3:], 3):
            with annotate_failure(i):
                self.assertEqual(dict(shard_ranges[i]),
                                 dict(updated_shard_range))
        self.assertEqual({'cursor': 'where', 'misplaced_done': True,
                          'ref': context_ref}, broker.load_cleave_context())
        unlink_files(expected_shard_dbs)

        # run cleave again - should process the fourth range
        with self._mock_sharder() as sharder:
            self.assertTrue(sharder._cleave(broker))

        expected = {'attempted': 1, 'success': 1, 'failure': 0,
                    'min_time': mock.ANY, 'max_time': mock.ANY}
        stats = self._assert_stats(expected, sharder, 'cleaved')
        self.assertIsInstance(stats['min_time'], float)
        self.assertIsInstance(stats['max_time'], float)
        self.assertLessEqual(stats['min_time'], stats['max_time'])

        self.assertEqual(SHARDING, broker.get_db_state())
        sharder._replicate_object.assert_called_once_with(
            0, expected_shard_dbs[3], 0)
        shard_broker = ContainerBroker(expected_shard_dbs[3])
        shard_own_sr = shard_broker.get_own_shard_range()
        self.assertEqual(ShardRange.CLEAVED, shard_own_sr.state)
        shard_info = shard_broker.get_info()
        total_shard_stats['object_count'] += shard_info['object_count']
        total_shard_stats['bytes_used'] += shard_info['bytes_used']

        updated_shard_ranges = broker.get_shard_ranges()
        self.assertEqual(4, len(updated_shard_ranges))

        shard_ranges[3].bytes_used = 1000
        shard_ranges[3].object_count = 1
        shard_ranges[3].state = ShardRange.CLEAVED
        for i in range(0, 4):
            with annotate_failure(i):
                self._check_shard_range(
                    shard_ranges[i], updated_shard_ranges[i])
        # NB includes the deleted object
        self._check_objects(objects[6:8], expected_shard_dbs[3])
        # other shard ranges should be unchanged
        for i, db in enumerate(expected_shard_dbs[:3]):
            with annotate_failure(i):
                self.assertFalse(os.path.exists(db))
        self.assertFalse(os.path.exists(expected_shard_dbs[4]))
        for i, updated_shard_range in enumerate(updated_shard_ranges[4:], 4):
            with annotate_failure(i):
                self.assertEqual(dict(shard_ranges[i]),
                                 dict(updated_shard_range))

        self.assertFalse(os.path.exists(expected_shard_dbs[4]))
        self.assertEqual({'cursor': 'yonder', 'misplaced_done': True,
                          'ref': context_ref}, broker.load_cleave_context())
        unlink_files(expected_shard_dbs)

        # run cleave - should be a no-op, all existing ranges have been cleaved
        with self._mock_sharder() as sharder:
            self.assertTrue(sharder._cleave(broker))

        self.assertEqual(SHARDING, broker.get_db_state())
        sharder._replicate_object.assert_not_called()

        # add final shard range - move this to ACTIVE state and update stats to
        # simulate another replica having cleaved it and replicated its state
        shard_ranges[4].update_state(ShardRange.ACTIVE)
        shard_ranges[4].update_meta(2, 15)
        broker.merge_shard_ranges(shard_ranges[4:])
        broker.update_sharding_info({'Scan-Done': 'True'})

        with self._mock_sharder() as sharder:
            self.assertTrue(sharder._cleave(broker))

        expected = {'attempted': 1, 'success': 1, 'failure': 0,
                    'min_time': mock.ANY, 'max_time': mock.ANY}
        stats = self._assert_stats(expected, sharder, 'cleaved')
        self.assertIsInstance(stats['min_time'], float)
        self.assertIsInstance(stats['max_time'], float)
        self.assertLessEqual(stats['min_time'], stats['max_time'])

        sharder._replicate_object.assert_called_once_with(
            0, expected_shard_dbs[4], 0)
        shard_broker = ContainerBroker(expected_shard_dbs[4])
        shard_own_sr = shard_broker.get_own_shard_range()
        self.assertEqual(ShardRange.ACTIVE, shard_own_sr.state)
        shard_info = shard_broker.get_info()
        total_shard_stats['object_count'] += shard_info['object_count']
        total_shard_stats['bytes_used'] += shard_info['bytes_used']

        updated_shard_ranges = broker.get_shard_ranges()
        self.assertEqual(5, len(updated_shard_ranges))
        # NB stats of the ACTIVE shard range should not be reset by cleaving
        for i in range(0, 4):
            with annotate_failure(i):
                self._check_shard_range(
                    shard_ranges[i], updated_shard_ranges[i])
        self.assertEqual(dict(shard_ranges[4]), dict(updated_shard_ranges[4]))

        # object copied to shard
        self._check_objects(objects[8:], expected_shard_dbs[4])
        # other shard ranges should be unchanged
        for i, db in enumerate(expected_shard_dbs[:4]):
            with annotate_failure(i):
                self.assertFalse(os.path.exists(db))

        self.assertEqual(initial_root_info['object_count'],
                         total_shard_stats['object_count'])
        self.assertEqual(initial_root_info['bytes_used'],
                         total_shard_stats['bytes_used'])

        self.assertEqual(
            {'cursor': '', 'cleaving_done': True, 'misplaced_done': True,
             'ref': context_ref}, broker.load_cleave_context())

        with self._mock_sharder() as sharder:
            self.assertTrue(sharder._cleave(broker))
        sharder._replicate_object.assert_not_called()

        self.assertTrue(broker.set_sharded_state())
        # run cleave - should be a no-op
        with self._mock_sharder() as sharder:
            self.assertTrue(sharder._cleave(broker))

        sharder._replicate_object.assert_not_called()

    def test_cleave_root_ranges_change(self):
        # verify that objects are not missed if shard ranges change between
        # cleaving batches
        broker = self._make_broker()
        objects = [
            ('a', self.ts_encoded(), 10, 'text/plain', 'etag_a', 0),
            ('b', self.ts_encoded(), 10, 'text/plain', 'etag_b', 0),
            ('c', self.ts_encoded(), 1, 'text/plain', 'etag_c', 0),
            ('d', self.ts_encoded(), 2, 'text/plain', 'etag_d', 0),
            ('e', self.ts_encoded(), 3, 'text/plain', 'etag_e', 0),
            ('f', self.ts_encoded(), 100, 'text/plain', 'etag_f', 0),
            ('x', self.ts_encoded(), 0, '', '', 1),  # deleted
            ('z', self.ts_encoded(), 1000, 'text/plain', 'etag_z', 0)
        ]
        for obj in objects:
            broker.put_object(*obj)
        context_ref = broker._get_context_ref()
        own_shard_range = broker.get_own_shard_range()
        own_shard_range.update_state(ShardRange.SHARDING)
        own_shard_range.epoch = Timestamp.now()
        broker.merge_shard_ranges([own_shard_range])

        shard_bounds = (('', 'd'), ('d', 'x'), ('x', ''))
        shard_ranges = self._make_shard_ranges(
            shard_bounds, state=ShardRange.CREATED)
        expected_shard_dbs = []
        for shard_range in shard_ranges:
            db_hash = hash_path(shard_range.account, shard_range.container)
            expected_shard_dbs.append(
                os.path.join(self.tempdir, 'sda', 'containers', '0',
                             db_hash[-3:], db_hash, db_hash + '.db'))

        broker.merge_shard_ranges(shard_ranges[:3])
        self.assertTrue(broker.set_sharding_state())

        # run cleave - first batch is cleaved
        with self._mock_sharder() as sharder:
            self.assertFalse(sharder._cleave(broker))
        self.assertEqual(
            {'cursor': str(shard_ranges[1].upper), 'ref': context_ref,
             'misplaced_done': True}, broker.load_cleave_context())

        self.assertEqual(SHARDING, broker.get_db_state())
        sharder._replicate_object.assert_has_calls(
            [mock.call(0, db, 0) for db in expected_shard_dbs[:2]]
        )

        updated_shard_ranges = broker.get_shard_ranges()
        self.assertEqual(3, len(updated_shard_ranges))

        # first 2 shard ranges should have updated object count, bytes used and
        # meta_timestamp
        shard_ranges[0].bytes_used = 23
        shard_ranges[0].object_count = 4
        shard_ranges[0].state = ShardRange.CLEAVED
        self._check_shard_range(shard_ranges[0], updated_shard_ranges[0])
        shard_ranges[1].bytes_used = 103
        shard_ranges[1].object_count = 2
        shard_ranges[1].state = ShardRange.CLEAVED
        self._check_shard_range(shard_ranges[1], updated_shard_ranges[1])
        self._check_objects(objects[:4], expected_shard_dbs[0])
        self._check_objects(objects[4:7], expected_shard_dbs[1])
        self.assertFalse(os.path.exists(expected_shard_dbs[2]))

        # third shard range should be unchanged - not yet cleaved
        self.assertEqual(dict(shard_ranges[2]),
                         dict(updated_shard_ranges[2]))

        self.assertEqual(
            {'cursor': str(shard_ranges[1].upper), 'ref': context_ref,
             'misplaced_done': True}, broker.load_cleave_context())

        # now change the shard ranges so that third consumes second
        shard_ranges[1].set_deleted()
        shard_ranges[2].lower = 'd'
        shard_ranges[2].timestamp = Timestamp.now()

        broker.merge_shard_ranges(shard_ranges[1:3])

        # run cleave - should process the extended third (final) range
        with self._mock_sharder() as sharder:
            self.assertTrue(sharder._cleave(broker))

        self.assertEqual(SHARDING, broker.get_db_state())
        sharder._replicate_object.assert_called_once_with(
            0, expected_shard_dbs[2], 0)
        updated_shard_ranges = broker.get_shard_ranges()
        self.assertEqual(2, len(updated_shard_ranges))
        self._check_shard_range(shard_ranges[0], updated_shard_ranges[0])
        # third shard range should now have updated object count, bytes used,
        # including objects previously in the second shard range
        shard_ranges[2].bytes_used = 1103
        shard_ranges[2].object_count = 3
        shard_ranges[2].state = ShardRange.CLEAVED
        self._check_shard_range(shard_ranges[2], updated_shard_ranges[1])
        self._check_objects(objects[4:8], expected_shard_dbs[2])

        self.assertEqual(
            {'cursor': str(shard_ranges[2].upper), 'ref': context_ref,
             'cleaving_done': True, 'misplaced_done': True},
            broker.load_cleave_context())

    def test_cleave_shard(self):
        broker = self._make_broker(account='.sharded_a', container='shard_c')
        own_shard_range = ShardRange(
            broker.path, Timestamp.now(), 'here', 'where',
            state=ShardRange.SHARDING, epoch=Timestamp.now())
        broker.merge_shard_ranges([own_shard_range])
        broker.update_sharding_info({'Root': 'a/c'})
        self.assertFalse(broker.is_root_container())  # sanity check

        objects = [
            ('m', self.ts_encoded(), 1, 'text/plain', 'etag_m', 0),
            ('n', self.ts_encoded(), 2, 'text/plain', 'etag_n', 0),
            ('there', self.ts_encoded(), 3, 'text/plain', 'etag_there', 0),
            ('where', self.ts_encoded(), 100, 'text/plain', 'etag_where', 0),
        ]
        misplaced_objects = [
            ('a', self.ts_encoded(), 1, 'text/plain', 'etag_a', 0),
            ('z', self.ts_encoded(), 100, 'text/plain', 'etag_z', 1),
        ]
        for obj in objects + misplaced_objects:
            broker.put_object(*obj)
        context_ref = broker._get_context_ref()

        shard_bounds = (('here', 'there'),
                        ('there', 'where'))
        shard_ranges = self._make_shard_ranges(
            shard_bounds, state=ShardRange.CREATED)
        expected_shard_dbs = []
        for shard_range in shard_ranges:
            db_hash = hash_path(shard_range.account, shard_range.container)
            expected_shard_dbs.append(
                os.path.join(self.tempdir, 'sda', 'containers', '0',
                             db_hash[-3:], db_hash, db_hash + '.db'))

        misplaced_bounds = (('', 'here'),
                            ('where', ''))
        misplaced_ranges = self._make_shard_ranges(
            misplaced_bounds, state=ShardRange.ACTIVE)
        misplaced_dbs = []
        for shard_range in misplaced_ranges:
            db_hash = hash_path(shard_range.account, shard_range.container)
            misplaced_dbs.append(
                os.path.join(self.tempdir, 'sda', 'containers', '0',
                             db_hash[-3:], db_hash, db_hash + '.db'))

        broker.merge_shard_ranges(shard_ranges)
        self.assertTrue(broker.set_sharding_state())

        # run cleave - first range is cleaved but move of misplaced objects is
        # not successful
        sharder_conf = {'shard_batch_size': 1}
        with self._mock_sharder(sharder_conf) as sharder:
            with mock.patch.object(
                    sharder, '_make_shard_range_fetcher',
                    return_value=lambda: iter(misplaced_ranges)):
                # cause misplaced objects replication to not succeed
                quorum = quorum_size(sharder.ring.replica_count)
                successes = [True] * (quorum - 1)
                fails = [False] * (sharder.ring.replica_count - len(successes))
                responses = successes + fails
                random.shuffle(responses)
                bad_result = (False, responses)
                ok_result = (True, [True] * sharder.ring.replica_count)
                sharder._replicate_object = mock.MagicMock(
                    # result for misplaced, misplaced, cleave
                    side_effect=(bad_result, ok_result, ok_result))
                self.assertFalse(sharder._cleave(broker))

        self.assertEqual(
            {'cursor': str(shard_ranges[0].upper), 'ref': context_ref,
             'misplaced_done': False}, broker.load_cleave_context())

        self.assertEqual(SHARDING, broker.get_db_state())
        sharder._replicate_object.assert_has_calls(
            [mock.call(0, misplaced_dbs[0], 0),
             mock.call(0, misplaced_dbs[1], 0),
             mock.call(0, expected_shard_dbs[0], 0)])
        shard_broker = ContainerBroker(expected_shard_dbs[0])
        # NB cleaving a shard, state goes to CLEAVED not ACTIVE
        shard_own_sr = shard_broker.get_own_shard_range()
        self.assertEqual(ShardRange.CLEAVED, shard_own_sr.state)

        updated_shard_ranges = broker.get_shard_ranges()
        self.assertEqual(2, len(updated_shard_ranges))

        # first shard range should have updated object count, bytes used and
        # meta_timestamp
        shard_ranges[0].bytes_used = 6
        shard_ranges[0].object_count = 3
        shard_ranges[0].state = ShardRange.CLEAVED
        self._check_shard_range(shard_ranges[0], updated_shard_ranges[0])
        self._check_objects(objects[:3], expected_shard_dbs[0])
        self.assertFalse(os.path.exists(expected_shard_dbs[1]))
        self._check_objects(misplaced_objects[:1], misplaced_dbs[0])
        self._check_objects(misplaced_objects[1:], misplaced_dbs[1])
        unlink_files(expected_shard_dbs)
        unlink_files(misplaced_dbs)

        # run cleave - second (final) range is cleaved; move this range to
        # CLEAVED state and update stats to simulate another replica having
        # cleaved it and replicated its state
        shard_ranges[1].update_state(ShardRange.CLEAVED)
        shard_ranges[1].update_meta(2, 15)
        broker.merge_shard_ranges(shard_ranges[1:2])
        with self._mock_sharder(sharder_conf) as sharder:
            with mock.patch.object(
                    sharder, '_make_shard_range_fetcher',
                    return_value=lambda: iter(misplaced_ranges)):
                self.assertTrue(sharder._cleave(broker))
        self.assertEqual(
            {'cursor': str(shard_ranges[1].upper), 'ref': context_ref,
             'misplaced_done': True, 'cleaving_done': True},
            broker.load_cleave_context())

        self.assertEqual(SHARDING, broker.get_db_state())
        sharder._replicate_object.assert_has_calls(
            [mock.call(0, misplaced_dbs[0], 0),
             mock.call(0, expected_shard_dbs[1], 0)])
        shard_broker = ContainerBroker(expected_shard_dbs[1])
        shard_own_sr = shard_broker.get_own_shard_range()
        self.assertEqual(ShardRange.CLEAVED, shard_own_sr.state)

        updated_shard_ranges = broker.get_shard_ranges()
        self.assertEqual(2, len(updated_shard_ranges))

        # second shard range should have updated object count, bytes used and
        # meta_timestamp
        self.assertEqual(dict(shard_ranges[1]), dict(updated_shard_ranges[1]))
        self._check_objects(objects[3:], expected_shard_dbs[1])
        self.assertFalse(os.path.exists(expected_shard_dbs[0]))
        self._check_objects(misplaced_objects[:1], misplaced_dbs[0])
        self.assertFalse(os.path.exists(misplaced_dbs[1]))

    def test_cleave_shard_shrinking(self):
        broker = self._make_broker(account='.shards_a', container='shard_c')
        own_shard_range = ShardRange(
            broker.path, next(self.ts_iter), 'here', 'where',
            state=ShardRange.SHRINKING, epoch=next(self.ts_iter))
        broker.merge_shard_ranges([own_shard_range])
        broker.update_sharding_info({'Root': 'a/c'})
        self.assertFalse(broker.is_root_container())  # sanity check

        objects = [
            ('there', self.ts_encoded(), 3, 'text/plain', 'etag_there', 0),
            ('where', self.ts_encoded(), 100, 'text/plain', 'etag_where', 0),
        ]
        for obj in objects:
            broker.put_object(*obj)
        context_ref = broker._get_context_ref()
        acceptor_epoch = next(self.ts_iter)
        acceptor = ShardRange('.shards_a/acceptor', Timestamp.now(),
                              'here', 'yonder', '1000', '11111',
                              state=ShardRange.ACTIVE, epoch=acceptor_epoch)
        db_hash = hash_path(acceptor.account, acceptor.container)
        # NB expected cleave db includes acceptor epoch
        expected_shard_db = os.path.join(
            self.tempdir, 'sda', 'containers', '0', db_hash[-3:], db_hash,
            '%s_%s.db' % (db_hash, acceptor_epoch.internal))

        broker.merge_shard_ranges([acceptor])
        broker.set_sharding_state()

        # run cleave
        with self._mock_sharder() as sharder:
            self.assertTrue(sharder._cleave(broker))

        self.assertEqual(
            {'cursor': str(acceptor.upper), 'ref': context_ref,
             'misplaced_done': True, 'cleaving_done': True},
            broker.load_cleave_context())

        self.assertEqual(SHARDING, broker.get_db_state())
        sharder._replicate_object.assert_has_calls(
            [mock.call(0, expected_shard_db, 0)])
        shard_broker = ContainerBroker(expected_shard_db)
        # NB when cleaving a shard container to a larger acceptor namespace
        # then expect the shard broker's own shard range to reflect that of the
        # acceptor shard range rather than being set to CLEAVED.
        self.assertEqual(
            ShardRange.ACTIVE, shard_broker.get_own_shard_range().state)

        updated_shard_ranges = broker.get_shard_ranges()
        self.assertEqual(1, len(updated_shard_ranges))
        self.assertEqual(dict(acceptor), dict(updated_shard_ranges[0]))

        # shard range should have unmodified acceptor, bytes used and
        # meta_timestamp
        self._check_objects(objects, expected_shard_db)

    def test_identify_sharding_candidate(self):
        brokers = [self._make_broker(container='c%03d' % i) for i in range(6)]
        for broker in brokers:
            broker.update_sharding_info({'Root': 'a/c'})
        node = {'index': 2}
        # containers are all empty
        with self._mock_sharder() as sharder:
            for broker in brokers:
                sharder._identify_sharding_candidate(broker, node)
        expected_stats = {}
        self._assert_stats(expected_stats, sharder, 'sharding_candidates')

        objects = [
            ['obj%3d' % i, next(self.ts_iter).internal, i, 'text/plain',
             'etag%s' % i, 0] for i in range(160)]

        # one container has 100 objects, which is below the sharding threshold
        for obj in objects[:100]:
            brokers[0].put_object(*obj)
        conf = {'recon_cache_path': self.tempdir}
        with self._mock_sharder(conf=conf) as sharder:
            for broker in brokers:
                sharder._identify_sharding_candidate(broker, node)
        self._assert_stats(expected_stats, sharder, 'sharding_candidates')
        expected_recon = {
            'found': 0,
            'top': []}
        sharder._report_stats()
        with open(sharder.rcache, 'rb') as fd:
            recon = json.load(fd)
        self.assertEqual(
            expected_recon,
            recon['sharding_stats']['sharding']['sharding_candidates'])

        # reduce the sharding threshold and the container is reported
        conf = {'shard_container_size': 100,
                'recon_cache_path': self.tempdir}
        with self._mock_sharder(conf=conf) as sharder:
            with mock_timestamp_now() as now:
                for broker in brokers:
                    sharder._identify_sharding_candidate(broker, node)
        stats_0 = {'path': brokers[0].db_file,
                   'node_index': 2,
                   'account': 'a',
                   'container': 'c000',
                   'root': 'a/c',
                   'object_count': 100,
                   'meta_timestamp': now.internal,
                   'file_size': os.stat(brokers[0].db_file).st_size}
        expected_stats = {'all': [stats_0]}
        self._assert_stats(expected_stats, sharder, 'sharding_candidates')
        expected_recon = {
            'found': 1,
            'top': [stats_0]}
        sharder._report_stats()
        with open(sharder.rcache, 'rb') as fd:
            recon = json.load(fd)
        self.assertEqual(
            expected_recon,
            recon['sharding_stats']['sharding']['sharding_candidates'])

        # repeat with handoff node and db_file error
        with self._mock_sharder(conf=conf) as sharder:
            with mock.patch('os.stat', side_effect=OSError('test error')):
                with mock_timestamp_now(now):
                    for broker in brokers:
                        sharder._identify_sharding_candidate(broker, {})
        stats_0_b = {'path': brokers[0].db_file,
                     'node_index': None,
                     'account': 'a',
                     'container': 'c000',
                     'root': 'a/c',
                     'object_count': 100,
                     'meta_timestamp': now.internal,
                     'file_size': None}
        expected_stats = {'all': [stats_0_b]}
        self._assert_stats(expected_stats, sharder, 'sharding_candidates')
        expected_recon = {
            'found': 1,
            'top': [stats_0_b]}
        sharder._report_stats()
        with open(sharder.rcache, 'rb') as fd:
            recon = json.load(fd)
        self.assertEqual(
            expected_recon,
            recon['sharding_stats']['sharding']['sharding_candidates'])

        # load up another container, but not to threshold for sharding, and
        # verify it is never a candidate for sharding
        for obj in objects[:50]:
            brokers[2].put_object(*obj)
        own_sr = brokers[2].get_own_shard_range()
        expected_stats = {'all': [stats_0]}
        for state in ShardRange.STATES:
            own_sr.update_state(state, state_timestamp=Timestamp.now())
            brokers[2].merge_shard_ranges([own_sr])
            with self._mock_sharder(conf=conf) as sharder:
                with mock_timestamp_now(now):
                    for broker in brokers:
                        sharder._identify_sharding_candidate(broker, node)
            with annotate_failure(state):
                self._assert_stats(
                    expected_stats, sharder, 'sharding_candidates')

        # reduce the threshold and the second container is included
        conf = {'shard_container_size': 50,
                'recon_cache_path': self.tempdir}
        own_sr.update_state(ShardRange.ACTIVE, state_timestamp=Timestamp.now())
        brokers[2].merge_shard_ranges([own_sr])
        with self._mock_sharder(conf=conf) as sharder:
            with mock_timestamp_now(now):
                for broker in brokers:
                    sharder._identify_sharding_candidate(broker, node)
        stats_2 = {'path': brokers[2].db_file,
                   'node_index': 2,
                   'account': 'a',
                   'container': 'c002',
                   'root': 'a/c',
                   'object_count': 50,
                   'meta_timestamp': now.internal,
                   'file_size': os.stat(brokers[2].db_file).st_size}
        expected_stats = {'all': [stats_0, stats_2]}
        self._assert_stats(expected_stats, sharder, 'sharding_candidates')
        expected_recon = {
            'found': 2,
            'top': [stats_0, stats_2]}
        sharder._report_stats()
        with open(sharder.rcache, 'rb') as fd:
            recon = json.load(fd)
        self.assertEqual(
            expected_recon,
            recon['sharding_stats']['sharding']['sharding_candidates'])

        # a broker not in active state is not included
        own_sr = brokers[0].get_own_shard_range()
        expected_stats = {'all': [stats_2]}
        for state in ShardRange.STATES:
            if state == ShardRange.ACTIVE:
                continue
            own_sr.update_state(state, state_timestamp=Timestamp.now())
            brokers[0].merge_shard_ranges([own_sr])
            with self._mock_sharder(conf=conf) as sharder:
                with mock_timestamp_now(now):
                    for broker in brokers:
                        sharder._identify_sharding_candidate(broker, node)
            with annotate_failure(state):
                self._assert_stats(
                    expected_stats, sharder, 'sharding_candidates')

        own_sr.update_state(ShardRange.ACTIVE, state_timestamp=Timestamp.now())
        brokers[0].merge_shard_ranges([own_sr])

        # load up a third container with 150 objects
        for obj in objects[:150]:
            brokers[5].put_object(*obj)
        with self._mock_sharder(conf=conf) as sharder:
            with mock_timestamp_now(now):
                for broker in brokers:
                    sharder._identify_sharding_candidate(broker, node)
        stats_5 = {'path': brokers[5].db_file,
                   'node_index': 2,
                   'account': 'a',
                   'container': 'c005',
                   'root': 'a/c',
                   'object_count': 150,
                   'meta_timestamp': now.internal,
                   'file_size': os.stat(brokers[5].db_file).st_size}
        expected_stats = {'all': [stats_0, stats_2, stats_5]}
        self._assert_stats(expected_stats, sharder, 'sharding_candidates')
        # note recon top list is sorted by size
        expected_recon = {
            'found': 3,
            'top': [stats_5, stats_0, stats_2]}
        sharder._report_stats()
        with open(sharder.rcache, 'rb') as fd:
            recon = json.load(fd)
        self.assertEqual(
            expected_recon,
            recon['sharding_stats']['sharding']['sharding_candidates'])

        # restrict the number of reported candidates
        conf = {'shard_container_size': 50,
                'recon_cache_path': self.tempdir,
                'recon_candidates_limit': 2}
        with self._mock_sharder(conf=conf) as sharder:
            with mock_timestamp_now(now):
                for broker in brokers:
                    sharder._identify_sharding_candidate(broker, node)
        self._assert_stats(expected_stats, sharder, 'sharding_candidates')
        expected_recon = {
            'found': 3,
            'top': [stats_5, stats_0]}
        sharder._report_stats()
        with open(sharder.rcache, 'rb') as fd:
            recon = json.load(fd)
        self.assertEqual(
            expected_recon,
            recon['sharding_stats']['sharding']['sharding_candidates'])

        # unrestrict the number of reported candidates
        conf = {'shard_container_size': 50,
                'recon_cache_path': self.tempdir,
                'recon_candidates_limit': -1}
        for i, broker in enumerate([brokers[1]] + brokers[3:5]):
            for obj in objects[:(151 + i)]:
                broker.put_object(*obj)
        with self._mock_sharder(conf=conf) as sharder:
            with mock_timestamp_now(now):
                for broker in brokers:
                    sharder._identify_sharding_candidate(broker, node)

        stats_4 = {'path': brokers[4].db_file,
                   'node_index': 2,
                   'account': 'a',
                   'container': 'c004',
                   'root': 'a/c',
                   'object_count': 153,
                   'meta_timestamp': now.internal,
                   'file_size': os.stat(brokers[4].db_file).st_size}
        stats_3 = {'path': brokers[3].db_file,
                   'node_index': 2,
                   'account': 'a',
                   'container': 'c003',
                   'root': 'a/c',
                   'object_count': 152,
                   'meta_timestamp': now.internal,
                   'file_size': os.stat(brokers[3].db_file).st_size}
        stats_1 = {'path': brokers[1].db_file,
                   'node_index': 2,
                   'account': 'a',
                   'container': 'c001',
                   'root': 'a/c',
                   'object_count': 151,
                   'meta_timestamp': now.internal,
                   'file_size': os.stat(brokers[1].db_file).st_size}

        expected_stats = {
            'all': [stats_0, stats_1, stats_2, stats_3, stats_4, stats_5]}
        self._assert_stats(expected_stats, sharder, 'sharding_candidates')
        expected_recon = {
            'found': 6,
            'top': [stats_4, stats_3, stats_1, stats_5, stats_0, stats_2]}
        sharder._report_stats()
        with open(sharder.rcache, 'rb') as fd:
            recon = json.load(fd)
        self.assertEqual(
            expected_recon,
            recon['sharding_stats']['sharding']['sharding_candidates'])

    def test_misplaced_objects_root_container(self):
        broker = self._make_broker()
        cleave_context = broker.load_cleave_context()
        own_sr = broker.get_own_shard_range()
        own_sr.update_state(ShardRange.SHARDING)
        own_sr.epoch = next(self.ts_iter)
        broker.merge_shard_ranges([own_sr])

        objects = [
            # misplaced objects in second and third shard ranges
            ['n', self.ts_encoded(), 2, 'text/plain', 'etag_n', 0],
            ['there', self.ts_encoded(), 3, 'text/plain', 'etag_there', 0],
            ['where', self.ts_encoded(), 100, 'text/plain', 'etag_where', 0],
            # deleted
            ['x', self.ts_encoded(), 0, '', '', 1],
        ]

        shard_bounds = (('', 'here'), ('here', 'there'),
                        ('there', 'where'), ('where', 'yonder'),
                        ('yonder', ''))
        initial_shard_ranges = self._make_shard_ranges(shard_bounds)
        expected_shard_dbs = []
        for shard_range in initial_shard_ranges:
            db_hash = hash_path(shard_range.account, shard_range.container)
            expected_shard_dbs.append(
                os.path.join(self.tempdir, 'sda', 'containers', '0',
                             db_hash[-3:], db_hash, db_hash + '.db'))
        broker.merge_shard_ranges(initial_shard_ranges)

        # unsharded
        with self._mock_sharder() as sharder:
            sharder._move_misplaced_objects(broker)
        sharder._replicate_object.assert_not_called()
        expected_stats = {'attempted': 1, 'success': 1, 'failure': 0,
                          'found': 0, 'placed': 0, 'unplaced': 0}
        self._assert_stats(expected_stats, sharder, 'misplaced')
        self.assertFalse(
            sharder.logger.get_increment_counts().get('misplaced_found'))

        # sharding - no misplaced objects
        self.assertTrue(broker.set_sharding_state())
        with self._mock_sharder() as sharder:
            sharder._move_misplaced_objects(broker)
        sharder._replicate_object.assert_not_called()
        self._assert_stats(expected_stats, sharder, 'misplaced')
        self.assertFalse(
            sharder.logger.get_increment_counts().get('misplaced_found'))

        # pretend we cleaved up to end of second shard range
        cleave_context.update({'cursor': 'there'})
        broker.dump_cleave_context(cleave_context)
        with self._mock_sharder() as sharder:
            sharder._move_misplaced_objects(broker)
        sharder._replicate_object.assert_not_called()
        self._assert_stats(expected_stats, sharder, 'misplaced')
        self.assertFalse(
            sharder.logger.get_increment_counts().get('misplaced_found'))

        # sharding - misplaced objects
        for obj in objects:
            broker.put_object(*obj)
        # pretend we have not cleaved any ranges
        cleave_context.update({'cursor': ''})
        broker.dump_cleave_context(cleave_context)
        with self._mock_sharder() as sharder:
            sharder._move_misplaced_objects(broker)
        sharder._replicate_object.assert_not_called()
        self._assert_stats(expected_stats, sharder, 'misplaced')
        self.assertFalse(
            sharder.logger.get_increment_counts().get('misplaced_found'))
        self.assertFalse(os.path.exists(expected_shard_dbs[0]))
        self.assertFalse(os.path.exists(expected_shard_dbs[1]))
        self.assertFalse(os.path.exists(expected_shard_dbs[2]))
        self.assertFalse(os.path.exists(expected_shard_dbs[3]))
        self.assertFalse(os.path.exists(expected_shard_dbs[4]))

        # pretend we cleaved up to end of second shard range
        cleave_context.update({'cursor': 'there'})
        broker.dump_cleave_context(cleave_context)
        with self._mock_sharder() as sharder:
            sharder._move_misplaced_objects(broker)

        sharder._replicate_object.assert_called_once_with(
            0, expected_shard_dbs[1], 0)
        expected_stats = {'attempted': 1, 'success': 1, 'failure': 0,
                          'found': 1, 'placed': 2, 'unplaced': 0}
        self._assert_stats(expected_stats, sharder, 'misplaced')
        self.assertEqual(
            1, sharder.logger.get_increment_counts()['misplaced_found'])
        # check misplaced objects were moved
        self._check_objects(objects[:2], expected_shard_dbs[1])
        # ... and removed from the source db
        self._check_objects(objects[2:], broker.db_file)
        # ... and nothing else moved
        self.assertFalse(os.path.exists(expected_shard_dbs[0]))
        self.assertFalse(os.path.exists(expected_shard_dbs[2]))
        self.assertFalse(os.path.exists(expected_shard_dbs[3]))
        self.assertFalse(os.path.exists(expected_shard_dbs[4]))

        # pretend we cleaved up to end of fourth shard range
        cleave_context.update({'cursor': 'yonder'})
        broker.dump_cleave_context(cleave_context)
        # and some new misplaced updates arrived in the first shard range
        new_objects = [
            ['b', self.ts_encoded(), 10, 'text/plain', 'etag_b', 0],
            ['c', self.ts_encoded(), 20, 'text/plain', 'etag_c', 0],
        ]
        for obj in new_objects:
            broker.put_object(*obj)

        with mock.patch('swift.container.sharder.CONTAINER_LISTING_LIMIT', 2):
            # check that *all* misplaced objects are moved despite exceeding
            # the listing limit
            with self._mock_sharder() as sharder:
                sharder._move_misplaced_objects(broker)
        expected_stats = {'attempted': 1, 'success': 1, 'failure': 0,
                          'found': 1, 'placed': 4, 'unplaced': 0}
        self._assert_stats(expected_stats, sharder, 'misplaced')
        sharder._replicate_object.assert_has_calls(
            [mock.call(0, db, 0) for db in expected_shard_dbs[2:4]],
            any_order=True
        )
        self._assert_stats(expected_stats, sharder, 'misplaced')
        self.assertEqual(
            1, sharder.logger.get_increment_counts()['misplaced_found'])

        # check misplaced objects were moved
        self._check_objects(new_objects, expected_shard_dbs[0])
        self._check_objects(objects[:2], expected_shard_dbs[1])
        self._check_objects(objects[2:3], expected_shard_dbs[2])
        self._check_objects(objects[3:], expected_shard_dbs[3])
        # ... and removed from the source db
        self._check_objects([], broker.db_file)
        self.assertFalse(os.path.exists(expected_shard_dbs[4]))

        # pretend we cleaved all ranges - sharded state
        cleave_context.update(
            {'cursor': '', 'cleaving_done': True, 'misplaced_done': True})
        broker.dump_cleave_context(cleave_context)
        self.assertTrue(broker.set_sharded_state())
        with self._mock_sharder() as sharder:
            sharder._move_misplaced_objects(broker)
        sharder._replicate_object.assert_not_called()
        expected_stats = {'attempted': 1, 'success': 1, 'failure': 0,
                          'found': 0, 'placed': 0, 'unplaced': 0}
        self._assert_stats(expected_stats, sharder, 'misplaced')
        self.assertFalse(
            sharder.logger.get_increment_counts().get('misplaced_found'))

        # and then more misplaced updates arrive
        newer_objects = [
            ['a', self.ts_encoded(), 51, 'text/plain', 'etag_a', 0],
            ['z', self.ts_encoded(), 52, 'text/plain', 'etag_z', 0],
        ]
        for obj in newer_objects:
            broker.put_object(*obj)
        broker.get_info()  # force updates to be committed
        # sanity check the puts landed in sharded broker
        self._check_objects(newer_objects, broker.db_file)

        with self._mock_sharder() as sharder:
            sharder._move_misplaced_objects(broker)
        sharder._replicate_object.assert_has_calls(
            [mock.call(0, db, 0)
             for db in (expected_shard_dbs[0], expected_shard_dbs[-1])],
            any_order=True
        )
        expected_stats = {'attempted': 1, 'success': 1, 'failure': 0,
                          'found': 1, 'placed': 2, 'unplaced': 0}
        self._assert_stats(expected_stats, sharder, 'misplaced')
        self.assertEqual(
            1, sharder.logger.get_increment_counts()['misplaced_found'])

        # check new misplaced objects were moved
        self._check_objects(newer_objects[:1] + new_objects,
                            expected_shard_dbs[0])
        self._check_objects(newer_objects[1:], expected_shard_dbs[4])
        # ... and removed from the source db
        self._check_objects([], broker.db_file)
        # ... and other shard dbs were unchanged
        self._check_objects(objects[:2], expected_shard_dbs[1])
        self._check_objects(objects[2:3], expected_shard_dbs[2])
        self._check_objects(objects[3:], expected_shard_dbs[3])

    def _setup_misplaced_objects(self):
        # make a broker with shard ranges, move it to sharded state and then
        # put some misplaced objects in it
        broker = self._make_broker()
        shard_bounds = (('', 'here'), ('here', 'there'),
                        ('there', 'where'), ('where', 'yonder'),
                        ('yonder', ''))
        initial_shard_ranges = [
            ShardRange('.sharded_a/%s-%s' % (lower, upper),
                       Timestamp.now(), lower, upper)
            for lower, upper in shard_bounds
        ]
        expected_dbs = []
        for shard_range in initial_shard_ranges:
            db_hash = hash_path(shard_range.account, shard_range.container)
            expected_dbs.append(
                os.path.join(self.tempdir, 'sda', 'containers', '0',
                             db_hash[-3:], db_hash, db_hash + '.db'))
        broker.merge_shard_ranges(initial_shard_ranges)
        objects = [
            # misplaced objects in second, third and fourth shard ranges
            ['n', self.ts_encoded(), 2, 'text/plain', 'etag_n', 0],
            ['there', self.ts_encoded(), 3, 'text/plain', 'etag_there', 0],
            ['where', self.ts_encoded(), 100, 'text/plain', 'etag_where', 0],
            # deleted
            ['x', self.ts_encoded(), 0, '', '', 1],
        ]
        own_sr = broker.get_own_shard_range()
        own_sr.update_state(ShardRange.SHARDING)
        own_sr.epoch = Timestamp.now()
        broker.merge_shard_ranges([own_sr])
        self.assertTrue(broker.set_sharding_state())
        cleave_context = broker.load_cleave_context()
        cleave_context['cleaving_done'] = True
        cleave_context['misplaced_done'] = True
        broker.dump_cleave_context(cleave_context)
        self.assertTrue(broker.set_sharded_state())
        for obj in objects:
            broker.put_object(*obj)
        self.assertEqual(SHARDED, broker.get_db_state())
        return broker, objects, expected_dbs

    def test_misplaced_objects_newer_objects(self):
        # verify that objects merged to the db afte rmisplaced objects have
        # been identified are not removed form the db
        broker, objects, expected_dbs = self._setup_misplaced_objects()
        newer_objects = [
            ['j', self.ts_encoded(), 51, 'text/plain', 'etag_j', 0],
            ['k', self.ts_encoded(), 52, 'text/plain', 'etag_k', 1],
        ]

        calls = []
        pre_removal_objects = []

        def mock_replicate_object(part, db, node_id):
            calls.append((part, db, node_id))
            if db == expected_dbs[1]:
                # put some new objects in the shard range that is being
                # replicated before misplaced objects are removed from that
                # range in the source db
                for obj in newer_objects:
                    broker.put_object(*obj)
                    # grab a snapshot of the db contents - a side effect is
                    # that the newer objects are now committed to the db
                    pre_removal_objects.extend(
                        broker.get_objects(include_deleted=True))
            return True, [True, True, True]

        with self._mock_sharder(replicas=3) as sharder:
            sharder._replicate_object = mock_replicate_object
            sharder._move_misplaced_objects(broker)

        # sanity check - the newer objects were in the db before the misplaced
        # object were removed
        for obj in newer_objects:
            self.assertIn(obj[0], [o['name'] for o in pre_removal_objects])
        for obj in objects[:2]:
            self.assertIn(obj[0], [o['name'] for o in pre_removal_objects])

        self.assertEqual(
            set([(0, db, 0) for db in (expected_dbs[1:4])]), set(calls))

        # check misplaced objects were moved
        self._check_objects(objects[:2], expected_dbs[1])
        self._check_objects(objects[2:3], expected_dbs[2])
        self._check_objects(objects[3:], expected_dbs[3])
        # ... but newer objects were not removed from the source db
        self._check_objects(newer_objects, broker.db_file)
        self.assertFalse(sharder.logger.get_lines_for_level('warning'))
        expected_stats = {'attempted': 1, 'success': 1, 'failure': 0,
                          'found': 1, 'placed': 4, 'unplaced': 0}
        self._assert_stats(expected_stats, sharder, 'misplaced')

        # they will be moved on next cycle
        unlink_files(expected_dbs)
        with self._mock_sharder(replicas=3) as sharder:
            sharder._move_misplaced_objects(broker)

        self._check_objects(newer_objects, expected_dbs[1])
        self._check_objects([], broker.db_file)
        expected_stats = {'attempted': 1, 'success': 1, 'failure': 0,
                          'found': 1, 'placed': 2, 'unplaced': 0}
        self._assert_stats(expected_stats, sharder, 'misplaced')

    def test_misplaced_objects_db_id_changed(self):
        broker, objects, expected_dbs = self._setup_misplaced_objects()

        pre_info = broker.get_info()
        calls = []
        expected_retained_objects = []
        expected_retained_objects_dbs = []

        def mock_replicate_object(part, db, node_id):
            calls.append((part, db, node_id))
            if len(calls) == 2:
                broker.newid('fake_remote_id')
                # grab snapshot of the objects in the broker when it changed id
                expected_retained_objects.extend(
                    self._get_raw_object_records(broker))
            if len(calls) >= 2:
                expected_retained_objects_dbs.append(db)
            return True, [True, True, True]

        with self._mock_sharder(replicas=3) as sharder:
            sharder._replicate_object = mock_replicate_object
            sharder._move_misplaced_objects(broker)

        # sanity checks
        self.assertNotEqual(pre_info['id'], broker.get_info()['id'])
        self.assertTrue(expected_retained_objects)

        self.assertEqual(
            set([(0, db, 0) for db in (expected_dbs[1:4])]), set(calls))

        # check misplaced objects were moved
        self._check_objects(objects[:2], expected_dbs[1])
        self._check_objects(objects[2:3], expected_dbs[2])
        self._check_objects(objects[3:], expected_dbs[3])
        # ... but objects were not removed after the source db id changed
        self._check_objects(expected_retained_objects, broker.db_file)
        expected_stats = {'attempted': 1, 'success': 0, 'failure': 1,
                          'found': 1, 'placed': 4, 'unplaced': 0}
        self._assert_stats(expected_stats, sharder, 'misplaced')

        lines = sharder.logger.get_lines_for_level('warning')
        self.assertIn('Refused to remove misplaced objects', lines[0])
        self.assertIn('Refused to remove misplaced objects', lines[1])
        self.assertFalse(lines[2:])

        # they will be moved again on next cycle
        unlink_files(expected_dbs)
        sharder.logger.clear()
        with self._mock_sharder(replicas=3) as sharder:
            sharder._move_misplaced_objects(broker)

        self.assertEqual(2, len(set(expected_retained_objects_dbs)))
        for db in expected_retained_objects_dbs:
            if db == expected_dbs[1]:
                self._check_objects(objects[:2], expected_dbs[1])
            if db == expected_dbs[2]:
                self._check_objects(objects[2:3], expected_dbs[2])
            if db == expected_dbs[3]:
                self._check_objects(objects[3:], expected_dbs[3])
        self._check_objects([], broker.db_file)
        self.assertFalse(sharder.logger.get_lines_for_level('warning'))
        expected_stats = {'attempted': 1, 'success': 1, 'failure': 0,
                          'found': 1, 'placed': len(expected_retained_objects),
                          'unplaced': 0}
        self._assert_stats(expected_stats, sharder, 'misplaced')

    def test_misplaced_objects_sufficient_replication(self):
        broker, objects, expected_dbs = self._setup_misplaced_objects()

        with self._mock_sharder(replicas=3) as sharder:
            sharder._replicate_object.return_value = (True, [True, True, True])
            sharder._move_misplaced_objects(broker)

        sharder._replicate_object.assert_has_calls(
            [mock.call(0, db, 0) for db in (expected_dbs[2:4])],
            any_order=True)
        expected_stats = {'attempted': 1, 'success': 1, 'failure': 0,
                          'found': 1, 'placed': 4, 'unplaced': 0}
        self._assert_stats(expected_stats, sharder, 'misplaced')
        self.assertEqual(
            1, sharder.logger.get_increment_counts()['misplaced_found'])
        # check misplaced objects were moved
        self._check_objects(objects[:2], expected_dbs[1])
        self._check_objects(objects[2:3], expected_dbs[2])
        self._check_objects(objects[3:], expected_dbs[3])
        # ... and removed from the source db
        self._check_objects([], broker.db_file)
        # ... and nothing else moved
        self.assertFalse(os.path.exists(expected_dbs[0]))
        self.assertFalse(os.path.exists(expected_dbs[4]))

    def test_misplaced_objects_insufficient_replication_3_replicas(self):
        broker, objects, expected_dbs = self._setup_misplaced_objects()

        returns = {expected_dbs[1]: (True, [True, True, True]),  # ok
                   expected_dbs[2]: (False, [True, False, False]),  # < quorum
                   expected_dbs[3]: (False, [False, True, True])}  # ok
        calls = []

        def mock_replicate_object(part, db, node_id):
            calls.append((part, db, node_id))
            return returns[db]

        with self._mock_sharder(replicas=3) as sharder:
            sharder._replicate_object = mock_replicate_object
            sharder._move_misplaced_objects(broker)

        self.assertEqual(
            set([(0, db, 0) for db in (expected_dbs[1:4])]), set(calls))
        expected_stats = {'attempted': 1, 'success': 0, 'failure': 1,
                          'placed': 4, 'unplaced': 0}
        self._assert_stats(expected_stats, sharder, 'misplaced')
        self.assertEqual(
            1, sharder.logger.get_increment_counts()['misplaced_found'])
        # check misplaced objects were moved to shard dbs
        self._check_objects(objects[:2], expected_dbs[1])
        self._check_objects(objects[2:3], expected_dbs[2])
        self._check_objects(objects[3:], expected_dbs[3])
        # ... but only removed from the source db if sufficiently replicated
        self._check_objects(objects[2:3], broker.db_file)
        # ... and nothing else moved
        self.assertFalse(os.path.exists(expected_dbs[0]))
        self.assertFalse(os.path.exists(expected_dbs[4]))

    def test_misplaced_objects_insufficient_replication_2_replicas(self):
        broker, objects, expected_dbs = self._setup_misplaced_objects()

        returns = {expected_dbs[1]: (True, [True, True]),  # ok
                   expected_dbs[2]: (False, [True, False]),  # ok
                   expected_dbs[3]: (False, [False, False])}  # < quorum>
        calls = []

        def mock_replicate_object(part, db, node_id):
            calls.append((part, db, node_id))
            return returns[db]

        with self._mock_sharder(replicas=2) as sharder:
            sharder._replicate_object = mock_replicate_object
            sharder._move_misplaced_objects(broker)

        self.assertEqual(
            set([(0, db, 0) for db in (expected_dbs[1:4])]), set(calls))
        expected_stats = {'attempted': 1, 'success': 0, 'failure': 1,
                          'placed': 4, 'unplaced': 0}
        self._assert_stats(expected_stats, sharder, 'misplaced')
        self.assertEqual(
            1, sharder.logger.get_increment_counts()['misplaced_found'])
        # check misplaced objects were moved to shard dbs
        self._check_objects(objects[:2], expected_dbs[1])
        self._check_objects(objects[2:3], expected_dbs[2])
        self._check_objects(objects[3:], expected_dbs[3])
        # ... but only removed from the source db if sufficiently replicated
        self._check_objects(objects[3:], broker.db_file)
        # ... and nothing else moved
        self.assertFalse(os.path.exists(expected_dbs[0]))
        self.assertFalse(os.path.exists(expected_dbs[4]))

    def test_misplaced_objects_insufficient_replication_4_replicas(self):
        broker, objects, expected_dbs = self._setup_misplaced_objects()

        returns = {expected_dbs[1]: (False, [True, False, False, False]),
                   expected_dbs[2]: (True, [True, False, False, True]),
                   expected_dbs[3]: (False, [False, False, False, False])}
        calls = []

        def mock_replicate_object(part, db, node_id):
            calls.append((part, db, node_id))
            return returns[db]

        with self._mock_sharder(replicas=4) as sharder:
            sharder._replicate_object = mock_replicate_object
            sharder._move_misplaced_objects(broker)

        self.assertEqual(
            set([(0, db, 0) for db in (expected_dbs[1:4])]), set(calls))
        expected_stats = {'attempted': 1, 'success': 0, 'failure': 1,
                          'placed': 4, 'unplaced': 0}
        self._assert_stats(expected_stats, sharder, 'misplaced')
        self.assertEqual(
            1, sharder.logger.get_increment_counts()['misplaced_found'])
        # check misplaced objects were moved to shard dbs
        self._check_objects(objects[:2], expected_dbs[1])
        self._check_objects(objects[2:3], expected_dbs[2])
        self._check_objects(objects[3:], expected_dbs[3])
        # ... but only removed from the source db if sufficiently replicated
        self._check_objects(objects[:2] + objects[3:], broker.db_file)
        # ... and nothing else moved
        self.assertFalse(os.path.exists(expected_dbs[0]))
        self.assertFalse(os.path.exists(expected_dbs[4]))

    def _check_misplaced_objects_shard_container_unsharded(self):
        broker = self._make_broker(account='.sharded_a', container='.shard_c')
        ts_shard = next(self.ts_iter)
        own_sr = ShardRange(broker.path, ts_shard, 'here', 'where')
        broker.merge_shard_ranges([own_sr])
        broker.update_sharding_info({'Root': 'a/c'})
        self.assertEqual(own_sr, broker.get_own_shard_range())  # sanity check
        self.assertEqual(UNSHARDED, broker.get_db_state())

        objects = [
            # some of these are misplaced objects
            ['b', self.ts_encoded(), 2, 'text/plain', 'etag_b', 0],
            ['here', self.ts_encoded(), 2, 'text/plain', 'etag_here', 0],
            ['n', self.ts_encoded(), 2, 'text/plain', 'etag_n', 0],
            ['there', self.ts_encoded(), 3, 'text/plain', 'etag_there', 0],
            ['x', self.ts_encoded(), 0, '', '', 1],  # deleted
            ['y', self.ts_encoded(), 10, 'text/plain', 'etag_y', 0],
        ]

        shard_bounds = (('', 'here'), ('here', 'there'),
                        ('there', 'where'), ('where', ''))
        root_shard_ranges = self._make_shard_ranges(shard_bounds)
        expected_shard_dbs = []
        for sr in root_shard_ranges:
            db_hash = hash_path(sr.account, sr.container)
            expected_shard_dbs.append(
                os.path.join(self.tempdir, 'sda', 'containers', '0',
                             db_hash[-3:], db_hash, db_hash + '.db'))

        # no objects
        with self._mock_sharder() as sharder:
            sharder._move_misplaced_objects(broker)
        sharder._replicate_object.assert_not_called()
        expected_stats = {'attempted': 1, 'success': 1, 'failure': 0,
                          'found': 0, 'placed': 0, 'unplaced': 0}
        self._assert_stats(expected_stats, sharder, 'misplaced')
        self.assertFalse(
            sharder.logger.get_increment_counts().get('misplaced_found'))
        self.assertFalse(sharder.logger.get_lines_for_level('warning'))

        # now put objects
        for obj in objects:
            broker.put_object(*obj)
        self._check_objects(objects, broker.db_file)  # sanity check

        # NB final shard range not available
        with self._mock_sharder() as sharder:
            sharder._fetch_shard_ranges = (lambda *a, **k:
                                           root_shard_ranges[:-1])
            sharder._move_misplaced_objects(broker)
        sharder._replicate_object.assert_called_with(
            0, expected_shard_dbs[0], 0),

        expected_stats = {'attempted': 1, 'success': 0, 'failure': 1,
                          'found': 1, 'placed': 2, 'unplaced': 2}
        self._assert_stats(expected_stats, sharder, 'misplaced')
        self.assertEqual(
            1, sharder.logger.get_increment_counts()['misplaced_found'])
        # some misplaced objects could not be moved...
        warning_lines = sharder.logger.get_lines_for_level('warning')
        self.assertIn(
            'Failed to find destination for at least 2 misplaced objects',
            warning_lines[0])
        self.assertFalse(warning_lines[1:])
        sharder.logger.clear()

        # check misplaced objects were moved
        self._check_objects(objects[:2], expected_shard_dbs[0])
        # ... and removed from the source db
        self._check_objects(objects[2:], broker.db_file)
        # ... and nothing else moved
        self.assertFalse(os.path.exists(expected_shard_dbs[1]))
        self.assertFalse(os.path.exists(expected_shard_dbs[2]))
        self.assertFalse(os.path.exists(expected_shard_dbs[3]))

        # repeat with final shard range available
        with self._mock_sharder() as sharder:
            sharder._fetch_shard_ranges = lambda *a, **k: root_shard_ranges
            sharder._move_misplaced_objects(broker)

        sharder._replicate_object.assert_called_with(
            0, expected_shard_dbs[-1], 0),

        expected_stats = {'attempted': 1, 'success': 1, 'failure': 0,
                          'found': 1, 'placed': 2, 'unplaced': 0}
        self._assert_stats(expected_stats, sharder, 'misplaced')
        self.assertEqual(
            1, sharder.logger.get_increment_counts()['misplaced_found'])
        self.assertFalse(sharder.logger.get_lines_for_level('warning'))

        # check misplaced objects were moved
        self._check_objects(objects[:2], expected_shard_dbs[0])
        self._check_objects(objects[4:], expected_shard_dbs[3])
        # ... and removed from the source db
        self._check_objects(objects[2:4], broker.db_file)
        # ... and nothing else moved
        self.assertFalse(os.path.exists(expected_shard_dbs[1]))
        self.assertFalse(os.path.exists(expected_shard_dbs[2]))

        # repeat - no work remaining
        with self._mock_sharder() as sharder:
            sharder._move_misplaced_objects(broker)
        sharder._replicate_object.assert_not_called()
        expected_stats = {'attempted': 1, 'success': 1, 'failure': 0,
                          'found': 0, 'placed': 0, 'unplaced': 0}
        self._assert_stats(expected_stats, sharder, 'misplaced')
        self.assertFalse(
            sharder.logger.get_increment_counts().get('misplaced_found'))
        self.assertFalse(sharder.logger.get_lines_for_level('warning'))

        # and then more misplaced updates arrive
        new_objects = [
            ['a', self.ts_encoded(), 51, 'text/plain', 'etag_a', 0],
            ['z', self.ts_encoded(), 52, 'text/plain', 'etag_z', 0],
        ]
        for obj in new_objects:
            broker.put_object(*obj)
        # sanity check the puts landed in sharded broker
        self._check_objects(new_objects[:1] + objects[2:4] + new_objects[1:],
                            broker.db_file)

        with self._mock_sharder() as sharder:
            sharder._fetch_shard_ranges = lambda *a, **k: root_shard_ranges
            sharder._move_misplaced_objects(broker)
        sharder._replicate_object.assert_has_calls(
            [mock.call(0, db, 0)
             for db in (expected_shard_dbs[0], expected_shard_dbs[3])],
            any_order=True
        )
        expected_stats = {'attempted': 1, 'success': 1, 'failure': 0,
                          'found': 1, 'placed': 2, 'unplaced': 0}
        self._assert_stats(expected_stats, sharder, 'misplaced')
        self.assertEqual(
            1, sharder.logger.get_increment_counts()['misplaced_found'])
        self.assertFalse(sharder.logger.get_lines_for_level('warning'))

        # check new misplaced objects were moved
        self._check_objects(new_objects[:1] + objects[:2],
                            expected_shard_dbs[0])
        self._check_objects(objects[4:] + new_objects[1:],
                            expected_shard_dbs[3])
        # ... and removed from the source db
        self._check_objects(objects[2:4], broker.db_file)
        # ... and nothing else moved
        self.assertFalse(os.path.exists(expected_shard_dbs[1]))
        self.assertFalse(os.path.exists(expected_shard_dbs[2]))

    def test_misplaced_objects_shard_container_unsharded(self):
        self._check_misplaced_objects_shard_container_unsharded()

    def test_misplaced_objects_shard_container_unsharded_limit_two(self):
        with mock.patch('swift.container.sharder.CONTAINER_LISTING_LIMIT', 2):
            self._check_misplaced_objects_shard_container_unsharded()

    def test_misplaced_objects_shard_container_unsharded_limit_one(self):
        with mock.patch('swift.container.sharder.CONTAINER_LISTING_LIMIT', 1):
            self._check_misplaced_objects_shard_container_unsharded()

    def test_misplaced_objects_shard_container_sharding(self):
        broker = self._make_broker(account='.sharded_a', container='shard_c')
        ts_shard = next(self.ts_iter)
        # note that own_sr spans two root shard ranges
        own_sr = ShardRange(broker.path, ts_shard, 'here', 'where')
        own_sr.update_state(ShardRange.SHARDING)
        own_sr.epoch = next(self.ts_iter)
        broker.merge_shard_ranges([own_sr])
        broker.update_sharding_info({'Root': 'a/c'})
        self.assertEqual(own_sr, broker.get_own_shard_range())  # sanity check
        self.assertEqual(UNSHARDED, broker.get_db_state())

        objects = [
            # some of these are misplaced objects
            ['b', self.ts_encoded(), 2, 'text/plain', 'etag_b', 0],
            ['here', self.ts_encoded(), 2, 'text/plain', 'etag_here', 0],
            ['n', self.ts_encoded(), 2, 'text/plain', 'etag_n', 0],
            ['there', self.ts_encoded(), 3, 'text/plain', 'etag_there', 0],
            ['v', self.ts_encoded(), 10, 'text/plain', 'etag_v', 0],
            ['y', self.ts_encoded(), 10, 'text/plain', 'etag_y', 0],
        ]

        shard_bounds = (('', 'here'), ('here', 'there'),
                        ('there', 'where'), ('where', ''))
        root_shard_ranges = self._make_shard_ranges(shard_bounds)
        expected_shard_dbs = []
        for sr in root_shard_ranges:
            db_hash = hash_path(sr.account, sr.container)
            expected_shard_dbs.append(
                os.path.join(self.tempdir, 'sda', 'containers', '0',
                             db_hash[-3:], db_hash, db_hash + '.db'))

        # pretend broker is sharding but not yet cleaved a shard
        cleave_context = broker.load_cleave_context()
        self.assertTrue(broker.set_sharding_state())
        broker.merge_shard_ranges([dict(sr) for sr in root_shard_ranges[1:3]])
        # then some updates arrive
        for obj in objects:
            broker.put_object(*obj)
        broker.get_info()
        self._check_objects(objects, broker.db_file)  # sanity check

        # first destination is not available
        with self._mock_sharder() as sharder:
            sharder._fetch_shard_ranges = lambda *a, **k: root_shard_ranges[1:]
            sharder._move_misplaced_objects(broker)

        sharder._replicate_object.assert_has_calls(
            [mock.call(0, expected_shard_dbs[-1], 0)],
        )
        expected_stats = {'attempted': 1, 'success': 0, 'failure': 1,
                          'found': 1, 'placed': 1, 'unplaced': 2}
        self._assert_stats(expected_stats, sharder, 'misplaced')
        self.assertEqual(
            1, sharder.logger.get_increment_counts()['misplaced_found'])
        warning_lines = sharder.logger.get_lines_for_level('warning')
        self.assertIn(
            'Failed to find destination for at least 2 misplaced objects',
            warning_lines[0])
        self.assertFalse(warning_lines[1:])
        sharder.logger.clear()

        # check some misplaced objects were moved
        self._check_objects(objects[5:], expected_shard_dbs[3])
        # ... and removed from the source db
        self._check_objects(objects[:5], broker.db_file)
        self.assertFalse(os.path.exists(expected_shard_dbs[0]))
        self.assertFalse(os.path.exists(expected_shard_dbs[1]))
        self.assertFalse(os.path.exists(expected_shard_dbs[2]))

        # normality resumes and all destinations are available
        with self._mock_sharder() as sharder:
            sharder._fetch_shard_ranges = lambda *a, **k: root_shard_ranges
            sharder._move_misplaced_objects(broker)

        sharder._replicate_object.assert_has_calls(
            [mock.call(0, expected_shard_dbs[0], 0)],
        )
        expected_stats = {'attempted': 1, 'success': 1, 'failure': 0,
                          'found': 1, 'placed': 2, 'unplaced': 0}
        self._assert_stats(expected_stats, sharder, 'misplaced')
        self.assertEqual(
            1, sharder.logger.get_increment_counts()['misplaced_found'])
        self.assertFalse(sharder.logger.get_lines_for_level('warning'))

        # check misplaced objects were moved
        self._check_objects(objects[:2], expected_shard_dbs[0])
        self._check_objects(objects[5:], expected_shard_dbs[3])
        # ... and removed from the source db
        self._check_objects(objects[2:5], broker.db_file)
        self.assertFalse(os.path.exists(expected_shard_dbs[1]))
        self.assertFalse(os.path.exists(expected_shard_dbs[2]))

        # pretend first shard has been cleaved
        cleave_context.update({'cursor': 'there'})
        broker.dump_cleave_context(cleave_context)
        # and then more misplaced updates arrive
        new_objects = [
            ['a', self.ts_encoded(), 51, 'text/plain', 'etag_a', 0],
            # this one is in the now cleaved shard range...
            ['k', self.ts_encoded(), 52, 'text/plain', 'etag_k', 0],
            ['z', self.ts_encoded(), 53, 'text/plain', 'etag_z', 0],
        ]
        for obj in new_objects:
            broker.put_object(*obj)
        broker.get_info()  # force updates to be committed
        # sanity check the puts landed in sharded broker
        self._check_objects(sorted(new_objects + objects[2:5]), broker.db_file)
        with self._mock_sharder() as sharder:
            sharder._fetch_shard_ranges = lambda *a, **k: root_shard_ranges
            sharder._move_misplaced_objects(broker)

        sharder._replicate_object.assert_has_calls(
            [mock.call(0, db, 0) for db in (expected_shard_dbs[0],
                                            expected_shard_dbs[1],
                                            expected_shard_dbs[-1])],
            any_order=True
        )

        expected_stats = {'attempted': 1, 'success': 1, 'failure': 0,
                          'found': 1, 'placed': 5, 'unplaced': 0}
        self._assert_stats(expected_stats, sharder, 'misplaced')
        self.assertEqual(
            1, sharder.logger.get_increment_counts()['misplaced_found'])
        self.assertFalse(sharder.logger.get_lines_for_level('warning'))

        # check *all* the misplaced objects were moved
        self._check_objects(new_objects[:1] + objects[:2],
                            expected_shard_dbs[0])
        self._check_objects(new_objects[1:2] + objects[2:4],
                            expected_shard_dbs[1])
        self._check_objects(objects[5:] + new_objects[2:],
                            expected_shard_dbs[3])
        # ... and removed from the source db
        self._check_objects(objects[4:5], broker.db_file)
        self.assertFalse(os.path.exists(expected_shard_dbs[2]))

    def test_misplaced_objects_deleted_and_updated(self):
        # setup
        broker = self._make_broker()
        own_sr = broker.get_own_shard_range()
        own_sr.update_state(ShardRange.SHARDING)
        own_sr.epoch = next(self.ts_iter)
        broker.merge_shard_ranges([own_sr])

        shard_bounds = (('', 'here'), ('here', ''))
        root_shard_ranges = self._make_shard_ranges(shard_bounds)
        expected_shard_dbs = []
        for sr in root_shard_ranges:
            db_hash = hash_path(sr.account, sr.container)
            expected_shard_dbs.append(
                os.path.join(self.tempdir, 'sda', 'containers', '0',
                             db_hash[-3:], db_hash, db_hash + '.db'))
        broker.merge_shard_ranges(root_shard_ranges)
        cleave_context = broker.load_cleave_context()
        self.assertTrue(broker.set_sharding_state())

        ts_older_internal = self.ts_encoded()  # used later
        # put deleted objects into source
        objects = [
            ['b', self.ts_encoded(), 0, '', '', 1],
            ['x', self.ts_encoded(), 0, '', '', 1]
        ]
        for obj in objects:
            broker.put_object(*obj)
        broker.get_info()
        self._check_objects(objects, broker.db_file)  # sanity check
        # pretend we cleaved all ranges - sharded state
        cleave_context.update(
            {'cursor': '', 'cleaving_done': True, 'misplaced_done': True})
        broker.dump_cleave_context(cleave_context)
        self.assertTrue(broker.set_sharded_state())

        with self._mock_sharder() as sharder:
            sharder._move_misplaced_objects(broker)

        sharder._replicate_object.assert_has_calls(
            [mock.call(0, db, 0) for db in (expected_shard_dbs[0],
                                            expected_shard_dbs[1])],
            any_order=True
        )
        expected_stats = {'attempted': 1, 'success': 1, 'failure': 0,
                          'found': 1, 'placed': 2, 'unplaced': 0}
        self._assert_stats(expected_stats, sharder, 'misplaced')
        self.assertEqual(
            1, sharder.logger.get_increment_counts()['misplaced_found'])

        # check new misplaced objects were moved
        self._check_objects(objects[:1], expected_shard_dbs[0])
        self._check_objects(objects[1:], expected_shard_dbs[1])
        # ... and removed from the source db
        self._check_objects([], broker.db_file)

        # update source db with older undeleted versions of same objects
        old_objects = [
            ['b', ts_older_internal, 2, 'text/plain', 'etag_b', 0],
            ['x', ts_older_internal, 4, 'text/plain', 'etag_x', 0]
        ]
        for obj in old_objects:
            broker.put_object(*obj)
        broker.get_info()
        self._check_objects(old_objects, broker.db_file)  # sanity check
        with self._mock_sharder() as sharder:
            sharder._move_misplaced_objects(broker)

        sharder._replicate_object.assert_has_calls(
            [mock.call(0, db, 0) for db in (expected_shard_dbs[0],
                                            expected_shard_dbs[1])],
            any_order=True
        )
        self._assert_stats(expected_stats, sharder, 'misplaced')
        self.assertEqual(
            1, sharder.logger.get_increment_counts()['misplaced_found'])

        # check older misplaced objects were not merged to shard brokers
        self._check_objects(objects[:1], expected_shard_dbs[0])
        self._check_objects(objects[1:], expected_shard_dbs[1])
        # ... and removed from the source db
        self._check_objects([], broker.db_file)

        # the destination shard dbs for misplaced objects may already exist so
        # check they are updated correctly when overwriting objects
        # update source db with newer deleted versions of same objects
        new_objects = [
            ['b', self.ts_encoded(), 0, '', '', 1],
            ['x', self.ts_encoded(), 0, '', '', 1]
        ]
        for obj in new_objects:
            broker.put_object(*obj)
        broker.get_info()
        self._check_objects(new_objects, broker.db_file)  # sanity check
        shard_broker = ContainerBroker(
            expected_shard_dbs[0], account=root_shard_ranges[0].account,
            container=root_shard_ranges[0].container)
        # update one shard container with even newer version of object
        timestamps = [next(self.ts_iter) for i in range(7)]
        ts_newer = encode_timestamps(
            timestamps[1], timestamps[3], timestamps[5])
        newer_object = ('b', ts_newer, 10, 'text/plain', 'etag_b', 0)
        shard_broker.put_object(*newer_object)

        with self._mock_sharder() as sharder:
            sharder._move_misplaced_objects(broker)

        sharder._replicate_object.assert_has_calls(
            [mock.call(0, db, 0) for db in (expected_shard_dbs[0],
                                            expected_shard_dbs[1])],
            any_order=True
        )
        self._assert_stats(expected_stats, sharder, 'misplaced')
        self.assertEqual(
            1, sharder.logger.get_increment_counts()['misplaced_found'])

        # check only the newer misplaced object was moved
        self._check_objects([newer_object], expected_shard_dbs[0])
        self._check_objects(new_objects[1:], expected_shard_dbs[1])
        # ... and removed from the source db
        self._check_objects([], broker.db_file)

        # update source with a version of 'b' that has newer data
        # but older content-type and metadata relative to shard object
        ts_update = encode_timestamps(
            timestamps[2], timestamps[3], timestamps[4])
        update_object = ('b', ts_update, 20, 'text/ignored', 'etag_newer', 0)
        broker.put_object(*update_object)

        with self._mock_sharder() as sharder:
            sharder._move_misplaced_objects(broker)

        ts_expected = encode_timestamps(
            timestamps[2], timestamps[3], timestamps[5])
        expected = ('b', ts_expected, 20, 'text/plain', 'etag_newer', 0)
        self._check_objects([expected], expected_shard_dbs[0])
        self._check_objects([], broker.db_file)

        # update source with a version of 'b' that has older data
        # and content-type but newer metadata relative to shard object
        ts_update = encode_timestamps(
            timestamps[1], timestamps[3], timestamps[6])
        update_object = ('b', ts_update, 999, 'text/ignored', 'etag_b', 0)
        broker.put_object(*update_object)

        with self._mock_sharder() as sharder:
            sharder._move_misplaced_objects(broker)

        ts_expected = encode_timestamps(
            timestamps[2], timestamps[3], timestamps[6])
        expected = ('b', ts_expected, 20, 'text/plain', 'etag_newer', 0)
        self._check_objects([expected], expected_shard_dbs[0])
        self._check_objects([], broker.db_file)

        # update source with a version of 'b' that has older data
        # but newer content-type and metadata
        ts_update = encode_timestamps(
            timestamps[2], timestamps[6], timestamps[6])
        update_object = ('b', ts_update, 999, 'text/newer', 'etag_b', 0)
        broker.put_object(*update_object)

        with self._mock_sharder() as sharder:
            sharder._move_misplaced_objects(broker)

        ts_expected = encode_timestamps(
            timestamps[2], timestamps[6], timestamps[6])
        expected = ('b', ts_expected, 20, 'text/newer', 'etag_newer', 0)
        self._check_objects([expected], expected_shard_dbs[0])
        self._check_objects([], broker.db_file)

    def _setup_find_ranges(self, account, cont, lower, upper):
        broker = self._make_broker(account=account, container=cont)
        own_sr = ShardRange('%s/%s' % (account, cont), Timestamp.now(),
                            lower, upper)
        broker.merge_shard_ranges([own_sr])
        broker.update_sharding_info({'Root': 'a/c'})
        objects = [
            # some of these are misplaced objects
            ['obj%3d' % i, self.ts_encoded(), i, 'text/plain', 'etag%s' % i, 0]
            for i in range(100)]
        for obj in objects:
            broker.put_object(*obj)
        return broker, objects

    def _check_find_shard_ranges_none_found(self, broker, objects):
        with self._mock_sharder() as sharder:
            last_found, num_found = sharder._find_shard_ranges(broker)
        self.assertGreater(sharder.split_size, len(objects))
        self.assertEqual(0, num_found)
        self.assertFalse(last_found)
        self.assertFalse(broker.get_shard_ranges())
        expected_stats = {'attempted': 1, 'success': 0, 'failure': 1,
                          'found': 0, 'min_time': mock.ANY,
                          'max_time': mock.ANY}
        stats = self._assert_stats(expected_stats, sharder, 'scanned')
        self.assertGreaterEqual(stats['max_time'], stats['min_time'])

        with self._mock_sharder(conf={'shard_container_size': 200}) as sharder:
            last_found, num_found = sharder._find_shard_ranges(broker)
        self.assertEqual(sharder.split_size, len(objects))
        self.assertEqual(0, num_found)
        self.assertFalse(last_found)
        self.assertFalse(broker.get_shard_ranges())
        expected_stats = {'attempted': 1, 'success': 0, 'failure': 1,
                          'found': 0, 'min_time': mock.ANY,
                          'max_time': mock.ANY}
        stats = self._assert_stats(expected_stats, sharder, 'scanned')
        self.assertGreaterEqual(stats['max_time'], stats['min_time'])

    def test_find_shard_ranges_none_found_root(self):
        broker, objects = self._setup_find_ranges('a', 'c', '', '')
        self._check_find_shard_ranges_none_found(broker, objects)

    def test_find_shard_ranges_none_found_shard(self):
        broker, objects = self._setup_find_ranges(
            '.shards_a', 'c', 'lower', 'upper')
        self._check_find_shard_ranges_none_found(broker, objects)

    def _check_find_shard_ranges_finds_two(self, account, cont, lower, upper):
        def check_ranges():
            self.assertEqual(2, len(broker.get_shard_ranges()))
            expected_ranges = [
                ShardRange(
                    ShardRange.make_path('.int_shards_a', 'c', cont, now, 0),
                    now, lower, objects[98][0], 99),
                ShardRange(
                    ShardRange.make_path('.int_shards_a', 'c', cont, now, 1),
                    now, objects[98][0], upper, 1),
            ]
            self._assert_shard_ranges_equal(expected_ranges,
                                            broker.get_shard_ranges())
            self.assertEqual('True', broker.get_sharding_info('Scan-Done'))

        broker, objects = self._setup_find_ranges(
            account, cont, lower, upper)
        with self._mock_sharder(conf={'shard_container_size': 199,
                                      'auto_create_account_prefix': '.int_'}
                                ) as sharder:
            with mock_timestamp_now() as now:
                last_found, num_found = sharder._find_shard_ranges(broker)
        self.assertEqual(99, sharder.split_size)
        self.assertEqual(2, num_found)
        self.assertTrue(last_found)
        check_ranges()
        expected_stats = {'attempted': 1, 'success': 1, 'failure': 0,
                          'found': 2, 'min_time': mock.ANY,
                          'max_time': mock.ANY}
        stats = self._assert_stats(expected_stats, sharder, 'scanned')
        self.assertGreaterEqual(stats['max_time'], stats['min_time'])

        with self._mock_sharder(conf={'shard_container_size': 199,
                                      'auto_create_account_prefix': '.int_'}
                                ) as sharder:
            last_found, num_found = sharder._find_shard_ranges(broker)
        self.assertEqual(0, num_found)
        self.assertTrue(last_found)
        self.assertEqual(2, len(broker.get_shard_ranges()))
        check_ranges()
        expected_stats = {'attempted': 1, 'success': 1, 'failure': 0,
                          'found': 0, 'min_time': mock.ANY,
                          'max_time': mock.ANY}
        stats = self._assert_stats(expected_stats, sharder, 'scanned')
        self.assertGreaterEqual(stats['max_time'], stats['min_time'])

    def test_find_shard_ranges_finds_two_root(self):
        self._check_find_shard_ranges_finds_two('a', 'c', '', '')

    def test_find_shard_ranges_finds_two_shard(self):
        self._check_find_shard_ranges_finds_two('.shards_a', 'c_', 'l', 'u')

    def _check_find_shard_ranges_finds_three(self, account, cont, lower,
                                             upper):
        broker, objects = self._setup_find_ranges(
            account, cont, lower, upper)
        now = Timestamp.now()
        expected_ranges = [
            ShardRange(
                ShardRange.make_path('.shards_a', 'c', cont, now, 0),
                now, lower, objects[44][0], 45),
            ShardRange(
                ShardRange.make_path('.shards_a', 'c', cont, now, 1),
                now, objects[44][0], objects[89][0], 45),
            ShardRange(
                ShardRange.make_path('.shards_a', 'c', cont, now, 2),
                now, objects[89][0], upper, 10),
        ]
        # first invocation finds 2 ranges
        with self._mock_sharder(
                conf={'shard_container_size': 90,
                      'shard_scanner_batch_size': 2}) as sharder:
            with mock_timestamp_now(now):
                last_found, num_found = sharder._find_shard_ranges(broker)
        self.assertEqual(45, sharder.split_size)
        self.assertEqual(2, num_found)
        self.assertFalse(last_found)
        self.assertEqual(2, len(broker.get_shard_ranges()))
        self._assert_shard_ranges_equal(expected_ranges[:2],
                                        broker.get_shard_ranges())
        self.assertIsNone(broker.get_sharding_info('Scan-Done'))
        expected_stats = {'attempted': 1, 'success': 1, 'failure': 0,
                          'found': 2, 'min_time': mock.ANY,
                          'max_time': mock.ANY}
        stats = self._assert_stats(expected_stats, sharder, 'scanned')
        self.assertGreaterEqual(stats['max_time'], stats['min_time'])

        # second invocation finds third shard range
        with self._mock_sharder(conf={'shard_container_size': 199,
                                      'shard_scanner_batch_size': 2}
                                ) as sharder:
            with mock_timestamp_now(now):
                last_found, num_found = sharder._find_shard_ranges(broker)
        self.assertEqual(1, num_found)
        self.assertTrue(last_found)
        self.assertEqual(3, len(broker.get_shard_ranges()))
        self._assert_shard_ranges_equal(expected_ranges,
                                        broker.get_shard_ranges())
        self.assertEqual('True', broker.get_sharding_info('Scan-Done'))
        expected_stats = {'attempted': 1, 'success': 1, 'failure': 0,
                          'found': 1, 'min_time': mock.ANY,
                          'max_time': mock.ANY}
        stats = self._assert_stats(expected_stats, sharder, 'scanned')
        self.assertGreaterEqual(stats['max_time'], stats['min_time'])

        # third invocation finds none
        with self._mock_sharder(conf={'shard_container_size': 199,
                                      'shard_scanner_batch_size': 2}
                                ) as sharder:
            sharder._send_shard_ranges = mock.MagicMock(return_value=True)
            last_found, num_found = sharder._find_shard_ranges(broker)
        self.assertEqual(0, num_found)
        self.assertTrue(last_found)
        self.assertEqual(3, len(broker.get_shard_ranges()))
        self._assert_shard_ranges_equal(expected_ranges,
                                        broker.get_shard_ranges())
        self.assertEqual('True', broker.get_sharding_info('Scan-Done'))
        expected_stats = {'attempted': 1, 'success': 1, 'failure': 0,
                          'found': 0, 'min_time': mock.ANY,
                          'max_time': mock.ANY}
        stats = self._assert_stats(expected_stats, sharder, 'scanned')
        self.assertGreaterEqual(stats['max_time'], stats['min_time'])

    def test_find_shard_ranges_finds_three_root(self):
        self._check_find_shard_ranges_finds_three('a', 'c', '', '')

    def test_find_shard_ranges_finds_three_shard(self):
        self._check_find_shard_ranges_finds_three('.shards_a', 'c_', 'l', 'u')

    def test_sharding_enabled(self):
        broker = self._make_broker()
        self.assertFalse(sharding_enabled(broker))
        broker.update_metadata(
            {'X-Container-Sysmeta-Sharding':
             ('yes', Timestamp.now().internal)})
        self.assertTrue(sharding_enabled(broker))
        # deleting broker clears sharding sysmeta
        broker.delete_db(Timestamp.now().internal)
        self.assertFalse(sharding_enabled(broker))
        # but if broker has a shard range then sharding is enabled
        broker.update_shard_range(
            ShardRange('acc/a_shard', Timestamp.now(), 'l', 'u'))
        self.assertTrue(sharding_enabled(broker))

    def test_send_shard_ranges(self):
        shard_ranges = self._make_shard_ranges((('', 'h'), ('h', '')))

        def do_test(replicas, *resp_codes):
            sent_data = defaultdict(str)

            def on_send(fake_conn, data):
                sent_data[fake_conn] += data

            with self._mock_sharder(replicas=replicas) as sharder:
                with mocked_http_conn(*resp_codes, give_send=on_send) as conn:
                    with mock_timestamp_now() as now:
                        res = sharder._send_shard_ranges(
                            'a', 'c', shard_ranges)

            self.assertEqual(sharder.ring.replica_count, len(conn.requests))
            expected_body = json.dumps([dict(sr) for sr in shard_ranges])
            expected_headers = {'Content-Type': 'application/json',
                                'Content-Length': str(len(expected_body)),
                                'X-Timestamp': now.internal,
                                'X-Backend-Record-Type': 'shard',
                                'User-Agent': mock.ANY}
            for data in sent_data.values():
                self.assertEqual(expected_body, data)
            hosts = set()
            for req in conn.requests:
                path_parts = req['path'].split('/')[1:]
                hosts.add('%s:%s/%s' % (req['ip'], req['port'], path_parts[0]))
                # FakeRing only has one partition
                self.assertEqual('0', path_parts[1])
                self.assertEqual('PUT', req['method'])
                self.assertEqual(['a', 'c'], path_parts[-2:])
                req_headers = req['headers']
                for k, v in expected_headers.items():
                    self.assertEqual(v, req_headers[k])
                self.assertTrue(
                    req_headers['User-Agent'].startswith('container-sharder'))
            self.assertEqual(sharder.ring.replica_count, len(hosts))
            return res, sharder

        replicas = 3
        res, sharder = do_test(replicas, 202, 202, 202)
        self.assertTrue(res)
        self.assertFalse(sharder.logger.get_lines_for_level('warning'))
        self.assertFalse(sharder.logger.get_lines_for_level('error'))
        res, sharder = do_test(replicas, 202, 202, 404)
        self.assertTrue(res)
        self.assertEqual([True], [
            'Failed to put shard ranges' in line for line in
            sharder.logger.get_lines_for_level('warning')])
        self.assertFalse(sharder.logger.get_lines_for_level('error'))
        res, sharder = do_test(replicas, 202, 202, Exception)
        self.assertTrue(res)
        self.assertFalse(sharder.logger.get_lines_for_level('warning'))
        self.assertEqual([True], [
            'Failed to put shard ranges' in line for line in
            sharder.logger.get_lines_for_level('error')])
        res, sharder = do_test(replicas, 202, 404, 404)
        self.assertFalse(res)
        self.assertEqual([True, True], [
            'Failed to put shard ranges' in line for line in
            sharder.logger.get_lines_for_level('warning')])
        self.assertFalse(sharder.logger.get_lines_for_level('error'))
        res, sharder = do_test(replicas, 500, 500, 500)
        self.assertFalse(res)
        self.assertEqual([True, True, True], [
            'Failed to put shard ranges' in line for line in
            sharder.logger.get_lines_for_level('warning')])
        self.assertFalse(sharder.logger.get_lines_for_level('error'))
        res, sharder = do_test(replicas, Exception, Exception, 202)
        self.assertEqual([True, True], [
            'Failed to put shard ranges' in line for line in
            sharder.logger.get_lines_for_level('error')])
        res, sharder = do_test(replicas, Exception, eventlet.Timeout(), 202)
        self.assertFalse(sharder.logger.get_lines_for_level('warning'))
        self.assertEqual([True, True], [
            'Failed to put shard ranges' in line for line in
            sharder.logger.get_lines_for_level('error')])

        replicas = 2
        res, sharder = do_test(replicas, 202, 202)
        self.assertTrue(res)
        self.assertFalse(sharder.logger.get_lines_for_level('warning'))
        self.assertFalse(sharder.logger.get_lines_for_level('error'))
        res, sharder = do_test(replicas, 202, 404)
        self.assertTrue(res)
        self.assertEqual([True], [
            'Failed to put shard ranges' in line for line in
            sharder.logger.get_lines_for_level('warning')])
        self.assertFalse(sharder.logger.get_lines_for_level('error'))
        res, sharder = do_test(replicas, 202, Exception)
        self.assertTrue(res)
        self.assertFalse(sharder.logger.get_lines_for_level('warning'))
        self.assertEqual([True], [
            'Failed to put shard ranges' in line for line in
            sharder.logger.get_lines_for_level('error')])
        res, sharder = do_test(replicas, 404, 404)
        self.assertFalse(res)
        self.assertEqual([True, True], [
            'Failed to put shard ranges' in line for line in
            sharder.logger.get_lines_for_level('warning')])
        self.assertFalse(sharder.logger.get_lines_for_level('error'))
        res, sharder = do_test(replicas, Exception, Exception)
        self.assertFalse(res)
        self.assertFalse(sharder.logger.get_lines_for_level('warning'))
        self.assertEqual([True, True], [
            'Failed to put shard ranges' in line for line in
            sharder.logger.get_lines_for_level('error')])
        res, sharder = do_test(replicas, eventlet.Timeout(), Exception)
        self.assertFalse(res)
        self.assertFalse(sharder.logger.get_lines_for_level('warning'))
        self.assertEqual([True, True], [
            'Failed to put shard ranges' in line for line in
            sharder.logger.get_lines_for_level('error')])

        replicas = 4
        res, sharder = do_test(replicas, 202, 202, 202, 202)
        self.assertFalse(sharder.logger.get_lines_for_level('warning'))
        self.assertFalse(sharder.logger.get_lines_for_level('error'))
        self.assertTrue(res)
        res, sharder = do_test(replicas, 202, 202, 404, 404)
        self.assertTrue(res)
        self.assertEqual([True, True], [
            'Failed to put shard ranges' in line for line in
            sharder.logger.get_lines_for_level('warning')])
        self.assertFalse(sharder.logger.get_lines_for_level('error'))
        res, sharder = do_test(replicas, 202, 202, Exception, Exception)
        self.assertTrue(res)
        self.assertFalse(sharder.logger.get_lines_for_level('warning'))
        self.assertEqual([True, True], [
            'Failed to put shard ranges' in line for line in
            sharder.logger.get_lines_for_level('error')])
        res, sharder = do_test(replicas, 202, 404, 404, 404)
        self.assertFalse(res)
        self.assertEqual([True, True, True], [
            'Failed to put shard ranges' in line for line in
            sharder.logger.get_lines_for_level('warning')])
        self.assertFalse(sharder.logger.get_lines_for_level('error'))
        res, sharder = do_test(replicas, 500, 500, 500, 202)
        self.assertFalse(res)
        self.assertEqual([True, True, True], [
            'Failed to put shard ranges' in line for line in
            sharder.logger.get_lines_for_level('warning')])
        self.assertFalse(sharder.logger.get_lines_for_level('error'))
        res, sharder = do_test(replicas, Exception, Exception, 202, 404)
        self.assertFalse(res)
        self.assertEqual([True], [
            all(msg in line for msg in ('Failed to put shard ranges', '404'))
            for line in sharder.logger.get_lines_for_level('warning')])
        self.assertEqual([True, True], [
            'Failed to put shard ranges' in line for line in
            sharder.logger.get_lines_for_level('error')])
        res, sharder = do_test(
            replicas, eventlet.Timeout(), eventlet.Timeout(), 202, 404)
        self.assertFalse(res)
        self.assertEqual([True], [
            all(msg in line for msg in ('Failed to put shard ranges', '404'))
            for line in sharder.logger.get_lines_for_level('warning')])
        self.assertEqual([True, True], [
            'Failed to put shard ranges' in line for line in
            sharder.logger.get_lines_for_level('error')])

    def test_process_broker_not_sharding_no_others(self):
        # verify that sharding process will not start when own shard range is
        # missing or in wrong state even when other shard ranges are in the db
        broker = self._make_broker()
        node = {'ip': '1.2.3.4', 'port': 6040, 'device': 'sda5', 'id': '2',
                'index': 0}
        # sanity check
        self.assertIsNone(broker.get_own_shard_range(no_default=True))
        self.assertEqual(UNSHARDED, broker.get_db_state())

        # no shard ranges
        with self._mock_sharder() as sharder:
            sharder._process_broker(broker, node, 99)
        self.assertIsNone(broker.get_own_shard_range(no_default=True))
        self.assertEqual(UNSHARDED, broker.get_db_state())
        self.assertFalse(broker.logger.get_lines_for_level('warning'))
        self.assertFalse(broker.logger.get_lines_for_level('error'))
        broker.logger.clear()

        # no own shard range
        with self._mock_sharder() as sharder:
            sharder._process_broker(broker, node, 99)
        self.assertIsNone(broker.get_own_shard_range(no_default=True))
        self.assertEqual(UNSHARDED, broker.get_db_state())
        self.assertFalse(broker.logger.get_lines_for_level('warning'))
        self.assertFalse(broker.logger.get_lines_for_level('error'))
        broker.logger.clear()

        # now add own shard range
        for state in ShardRange.STATES:
            if state in (ShardRange.SHARDING,
                         ShardRange.SHRINKING):
                epoch = None
            else:
                epoch = Timestamp.now()
            own_sr = broker.get_own_shard_range()  # returns the default
            own_sr.update_state(state)
            own_sr.epoch = epoch
            broker.merge_shard_ranges([own_sr])
            with self._mock_sharder() as sharder:
                with mock_timestamp_now() as now:
                    with mock.patch.object(sharder, '_audit_container'):
                        sharder._process_broker(broker, node, 99)
                        own_shard_range = broker.get_own_shard_range(
                            no_default=True)
            self.assertEqual(dict(own_sr, meta_timestamp=now),
                             dict(own_shard_range))
            self.assertEqual(UNSHARDED, broker.get_db_state())
            if epoch:
                self.assertFalse(broker.logger.get_lines_for_level('warning'))
            else:
                self.assertIn('missing epoch',
                              broker.logger.get_lines_for_level('warning')[0])
            self.assertFalse(broker.logger.get_lines_for_level('error'))
            broker.logger.clear()

    def _check_process_broker_sharding_no_others(self, state):
        # verify that when existing own_shard_range has given state then the
        # sharding process will begin
        broker = self._make_broker(hash_='hash%s' % state)
        node = {'ip': '1.2.3.4', 'port': 6040, 'device': 'sda5', 'id': '2',
                'index': 0}
        own_sr = broker.get_own_shard_range()
        self.assertTrue(own_sr.update_state(state))
        epoch = Timestamp.now()
        own_sr.epoch = epoch
        broker.merge_shard_ranges([own_sr])

        with self._mock_sharder() as sharder:
            with mock_timestamp_now() as now:
                sharder._audit_container = mock.MagicMock()
                sharder._process_broker(broker, node, 99)
                own_shard_range = broker.get_own_shard_range(no_default=True)

        self.assertEqual(dict(own_sr, meta_timestamp=now),
                         dict(own_shard_range))
        self.assertEqual(SHARDING, broker.get_db_state())
        self.assertEqual(epoch.normal, parse_db_filename(broker.db_file)[1])
        if epoch:
            self.assertFalse(broker.logger.get_lines_for_level('warning'))
        else:
            self.assertIn('missing epoch',
                          broker.logger.get_lines_for_level('warning')[0])
        self.assertFalse(broker.logger.get_lines_for_level('error'))

    def test_process_broker_sharding_with_own_shard_range_no_others(self):
        self._check_process_broker_sharding_no_others(ShardRange.SHARDING)
        self._check_process_broker_sharding_no_others(ShardRange.SHRINKING)

    def test_process_broker_not_sharding_others(self):
        # verify that sharding process will not start when own shard range is
        # missing or in wrong state even when other shard ranges are in the db
        broker = self._make_broker()
        node = {'ip': '1.2.3.4', 'port': 6040, 'device': 'sda5', 'id': '2',
                'index': 0}
        # sanity check
        self.assertIsNone(broker.get_own_shard_range(no_default=True))
        self.assertEqual(UNSHARDED, broker.get_db_state())

        # add shard ranges - but not own
        shard_ranges = self._make_shard_ranges((('', 'h'), ('h', '')))
        broker.merge_shard_ranges(shard_ranges)

        with self._mock_sharder() as sharder:
            sharder._process_broker(broker, node, 99)
        self.assertIsNone(broker.get_own_shard_range(no_default=True))
        self.assertEqual(UNSHARDED, broker.get_db_state())
        self.assertFalse(broker.logger.get_lines_for_level('warning'))
        self.assertFalse(broker.logger.get_lines_for_level('error'))
        broker.logger.clear()

        # now add own shard range
        for state in ShardRange.STATES:
            if state in (ShardRange.SHARDING,
                         ShardRange.SHRINKING,
                         ShardRange.SHARDED):
                epoch = None
            else:
                epoch = Timestamp.now()

            own_sr = broker.get_own_shard_range()  # returns the default
            own_sr.update_state(state)
            own_sr.epoch = epoch
            broker.merge_shard_ranges([own_sr])
            with self._mock_sharder() as sharder:
                with mock_timestamp_now() as now:
                    sharder._process_broker(broker, node, 99)
                    own_shard_range = broker.get_own_shard_range(
                        no_default=True)
            self.assertEqual(dict(own_sr, meta_timestamp=now),
                             dict(own_shard_range))
            self.assertEqual(UNSHARDED, broker.get_db_state())
            if epoch:
                self.assertFalse(broker.logger.get_lines_for_level('warning'))
            else:
                self.assertIn('missing epoch',
                              broker.logger.get_lines_for_level('warning')[0])
            self.assertFalse(broker.logger.get_lines_for_level('error'))
            broker.logger.clear()

    def _check_process_broker_sharding_others(self, state):
        # verify states in which own_shard_range will cause sharding
        # process to start when other shard ranges are in the db
        broker = self._make_broker(hash_='hash%s' % state)
        node = {'ip': '1.2.3.4', 'port': 6040, 'device': 'sda5', 'id': '2',
                'index': 0}
        # add shard ranges - but not own
        shard_ranges = self._make_shard_ranges((('', 'h'), ('h', '')))
        broker.merge_shard_ranges(shard_ranges)
        # sanity check
        self.assertIsNone(broker.get_own_shard_range(no_default=True))
        self.assertEqual(UNSHARDED, broker.get_db_state())

        # now set own shard range to given state and persist it
        own_sr = broker.get_own_shard_range()  # returns the default
        self.assertTrue(own_sr.update_state(state))
        epoch = Timestamp.now()
        own_sr.epoch = epoch
        broker.merge_shard_ranges([own_sr])
        with self._mock_sharder() as sharder:
            with mock_timestamp_now() as now:
                # we're not testing rest of the process here so prevent any
                # attempt to progress shard range states
                sharder._create_shard_containers = lambda *args: 0
                sharder._process_broker(broker, node, 99)
                own_shard_range = broker.get_own_shard_range(no_default=True)

        self.assertEqual(dict(own_sr, meta_timestamp=now),
                         dict(own_shard_range))
        self.assertEqual(SHARDING, broker.get_db_state())
        self.assertEqual(epoch.normal, parse_db_filename(broker.db_file)[1])
        self.assertFalse(broker.logger.get_lines_for_level('warning'))
        self.assertFalse(broker.logger.get_lines_for_level('error'))

    def test_process_broker_sharding_with_own_shard_range_and_others(self):
        self._check_process_broker_sharding_others(ShardRange.SHARDING)
        self._check_process_broker_sharding_others(ShardRange.SHRINKING)
        self._check_process_broker_sharding_others(ShardRange.SHARDED)

    def check_shard_ranges_sent(self, broker, expected_sent):
        bodies = []

        def capture_send(conn, data):
            bodies.append(data)

        with self._mock_sharder() as sharder:
            with mocked_http_conn(204, 204, 204,
                                  give_send=capture_send) as mock_conn:
                sharder._update_root_container(broker)

        for req in mock_conn.requests:
            self.assertEqual('PUT', req['method'])
        self.assertEqual([expected_sent] * 3,
                         [json.loads(b) for b in bodies])

    def test_update_root_container_own_range(self):
        broker = self._make_broker()

        # nothing to send
        with self._mock_sharder() as sharder:
            with mocked_http_conn() as mock_conn:
                sharder._update_root_container(broker)
        self.assertFalse(mock_conn.requests)

        def check_only_own_shard_range_sent(state):
            own_shard_range = broker.get_own_shard_range()
            self.assertTrue(own_shard_range.update_state(
                state, state_timestamp=next(self.ts_iter)))
            broker.merge_shard_ranges([own_shard_range])
            # add an object, expect to see it reflected in the own shard range
            # that is sent
            broker.put_object(str(own_shard_range.object_count + 1),
                              next(self.ts_iter).internal, 1, '', '')
            with mock_timestamp_now() as now:
                # force own shard range meta updates to be at fixed timestamp
                expected_sent = [
                    dict(own_shard_range,
                         meta_timestamp=now.internal,
                         object_count=own_shard_range.object_count + 1,
                         bytes_used=own_shard_range.bytes_used + 1)]
                self.check_shard_ranges_sent(broker, expected_sent)

        for state in ShardRange.STATES:
            with annotate_failure(state):
                check_only_own_shard_range_sent(state)

    def test_update_root_container_all_ranges(self):
        broker = self._make_broker()
        other_shard_ranges = self._make_shard_ranges((('', 'h'), ('h', '')))
        self.assertTrue(other_shard_ranges[0].set_deleted())
        broker.merge_shard_ranges(other_shard_ranges)

        # own range missing - send nothing
        with self._mock_sharder() as sharder:
            with mocked_http_conn() as mock_conn:
                sharder._update_root_container(broker)
        self.assertFalse(mock_conn.requests)

        def check_all_shard_ranges_sent(state):
            own_shard_range = broker.get_own_shard_range()
            self.assertTrue(own_shard_range.update_state(
                state, state_timestamp=next(self.ts_iter)))
            broker.merge_shard_ranges([own_shard_range])
            # add an object, expect to see it reflected in the own shard range
            # that is sent
            broker.put_object(str(own_shard_range.object_count + 1),
                              next(self.ts_iter).internal, 1, '', '')
            with mock_timestamp_now() as now:
                shard_ranges = broker.get_shard_ranges(include_deleted=True)
                expected_sent = sorted([
                    own_shard_range.copy(
                        meta_timestamp=now.internal,
                        object_count=own_shard_range.object_count + 1,
                        bytes_used=own_shard_range.bytes_used + 1)] +
                    shard_ranges,
                    key=lambda r: (r.lower, r.upper))
                self.check_shard_ranges_sent(
                    broker, [dict(sr) for sr in expected_sent])

        for state in ShardRange.STATES.keys():
            with annotate_failure(state):
                check_all_shard_ranges_sent(state)

    def test_audit_root_container(self):
        broker = self._make_broker()

        expected_stats = {'attempted': 1, 'success': 1, 'failure': 0}
        with self._mock_sharder() as sharder:
            with mock.patch.object(
                    sharder, '_audit_shard_container') as mocked:
                sharder._audit_container(broker)
        self._assert_stats(expected_stats, sharder, 'audit_root')
        self.assertFalse(sharder.logger.get_lines_for_level('warning'))
        self.assertFalse(sharder.logger.get_lines_for_level('error'))
        mocked.assert_not_called()

        def assert_overlap_warning(line, state_text):
            self.assertIn(
                'Audit failed for root %s' % broker.db_file, line)
            self.assertIn(
                'overlapping ranges in state %s: k-t s-z' % state_text,
                line)

        expected_stats = {'attempted': 1, 'success': 0, 'failure': 1}
        shard_bounds = (('a', 'j'), ('k', 't'), ('s', 'z'))
        for state, state_text in ShardRange.STATES.items():
            shard_ranges = self._make_shard_ranges(shard_bounds, state)
            broker.merge_shard_ranges(shard_ranges)
            with self._mock_sharder() as sharder:
                with mock.patch.object(
                        sharder, '_audit_shard_container') as mocked:
                    sharder._audit_container(broker)
            lines = sharder.logger.get_lines_for_level('warning')
            assert_overlap_warning(lines[0], state_text)
            self.assertFalse(lines[1:])
            self.assertFalse(sharder.logger.get_lines_for_level('error'))
            self._assert_stats(expected_stats, sharder, 'audit_root')
            mocked.assert_not_called()

        def assert_missing_warning(line):
            self.assertIn(
                'Audit failed for root %s' % broker.db_file, line)
            self.assertIn('missing range(s): -a j-k z-', line)

        own_shard_range = broker.get_own_shard_range()
        states = (ShardRange.SHARDING, ShardRange.SHARDED)
        for state in states:
            own_shard_range.update_state(
                state, state_timestamp=next(self.ts_iter))
            broker.merge_shard_ranges([own_shard_range])
            with self._mock_sharder() as sharder:
                with mock.patch.object(
                        sharder, '_audit_shard_container') as mocked:
                    sharder._audit_container(broker)
            lines = sharder.logger.get_lines_for_level('warning')
            assert_missing_warning(lines[0])
            assert_overlap_warning(lines[0], state_text)
            self.assertFalse(lines[1:])
            self.assertFalse(sharder.logger.get_lines_for_level('error'))
            self._assert_stats(expected_stats, sharder, 'audit_root')
            mocked.assert_not_called()

    def test_audit_shard_container(self):
        broker = self._make_broker(account='.shards_a', container='shard_c')
        broker.update_sharding_info({'Root': 'a/c'})
        shard_bounds = (('a', 'j'), ('k', 't'), ('s', 'z'))
        shard_ranges = self._make_shard_ranges(shard_bounds, ShardRange.ACTIVE)
        shard_ranges[1].name = broker.path
        expected_stats = {'attempted': 1, 'success': 0, 'failure': 1}

        def call_audit_container():
            with self._mock_sharder() as sharder:
                with mock.patch.object(
                        sharder, '_audit_root_container') as mocked:
                    with mock.patch.object(sharder, 'swift') as mock_swift:
                        mock_response = mock.MagicMock()
                        mock_response.body = json.dumps(
                            [dict(sr) for sr in shard_ranges])
                        mock_swift.make_request.return_value = mock_response
                        sharder.reclaim_age = 0
                        sharder._audit_container(broker)
            mocked.assert_not_called()
            return sharder

        broker.account = 'bad_account'
        sharder = call_audit_container()
        lines = sharder.logger.get_lines_for_level('warning')
        self._assert_stats(expected_stats, sharder, 'audit_shard')
        self.assertIn('Audit warnings for shard %s' % broker.db_file, lines[0])
        self.assertIn('account not in shards namespace', lines[0])
        self.assertIn('root has no matching shard range', lines[0])
        self.assertIn('Audit failed for shard %s' % broker.db_file, lines[1])
        self.assertIn('missing own shard range', lines[1])
        self.assertFalse(lines[2:])
        self.assertFalse(sharder.logger.get_lines_for_level('error'))
        self.assertFalse(broker.is_deleted())

        broker.get_info()
        sharder = call_audit_container()
        lines = sharder.logger.get_lines_for_level('warning')
        self._assert_stats(expected_stats, sharder, 'audit_shard')
        self.assertIn('Audit warnings for shard %s' % broker.db_file, lines[0])
        self.assertNotIn('account not in shards namespace', lines[0])
        self.assertIn('root has no matching shard range', lines[0])
        self.assertIn('Audit failed for shard %s' % broker.db_file, lines[1])
        self.assertIn('missing own shard range', lines[1])
        self.assertFalse(lines[2:])
        self.assertFalse(sharder.logger.get_lines_for_level('error'))
        self.assertFalse(broker.is_deleted())

        # create own shard range, no match in root
        expected_stats = {'attempted': 1, 'success': 1, 'failure': 0}
        own_shard_range = broker.get_own_shard_range()  # get the default
        own_shard_range.lower = 'j'
        own_shard_range.upper = 'k'
        broker.merge_shard_ranges([own_shard_range])
        sharder = call_audit_container()
        lines = sharder.logger.get_lines_for_level('warning')
        self.assertIn('Audit warnings for shard %s' % broker.db_file, lines[0])
        self.assertNotIn('account not in shards namespace', lines[0])
        self.assertNotIn('missing own shard range', lines[0])
        self.assertIn('root has no matching shard range', lines[0])
        self._assert_stats(expected_stats, sharder, 'audit_shard')
        self.assertFalse(lines[1:])
        self.assertFalse(sharder.logger.get_lines_for_level('error'))
        self.assertFalse(broker.is_deleted())

        def assert_ok():
            sharder = call_audit_container()
            self.assertFalse(sharder.logger.get_lines_for_level('warning'))
            self.assertFalse(sharder.logger.get_lines_for_level('error'))
            self._assert_stats(expected_stats, sharder, 'audit_shard')

        # make own shard range match one in root
        own_shard_range = broker.get_own_shard_range()
        own_shard_range.lower = 'k'
        own_shard_range.upper = 't'
        own_shard_range.timestamp = Timestamp.now()
        broker.merge_shard_ranges([own_shard_range])
        assert_ok()
        self.assertFalse(broker.is_deleted())

        own_shard_range.update_state(ShardRange.SHARDED,
                                     state_timestamp=Timestamp.now())
        broker.merge_shard_ranges([own_shard_range])
        assert_ok()

        own_shard_range.deleted = 1
        own_shard_range.timestamp = Timestamp.now()
        broker.merge_shard_ranges([own_shard_range])
        assert_ok()
        self.assertTrue(broker.is_deleted())