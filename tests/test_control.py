"""All power operations are synthetic; no requests are sent to Alibaba Cloud."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from app import create_app
from cloud import Cloud
from tests.fakes import FakeCloud, FakeNotifier, SETUP, setup_copy


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cloud, self.notifier = FakeCloud(), FakeNotifier()
        self.app = create_app(self.tmp.name, self.cloud, self.notifier, scheduler=False)
        self.client = self.app.test_client()
        token = (Path(self.tmp.name) / 'setup.token').read_text()
        self.assertEqual(self.client.post('/api/setup', json=setup_copy(), headers={'X-Setup-Token': token}).status_code, 200)
        self.csrf = self.client.get('/api/status').json['csrf']
        self.store, self.engine = self.app.extensions['store'], self.app.extensions['engine']
        self.item = self.store.rows('instances')[0]
        self.path = '/api/instances/' + self.item['id'] + '/control'

    def tearDown(self):
        self.tmp.cleanup()

    def post(self, action='stop', **overrides):
        return self.client.post(self.path, json={'action': action, 'confirmed': True,
                                'confirm_instance_id': self.item['instance_id'], **overrides},
                                headers={'X-CSRF-Token': self.csrf})

    def update(self, **values):
        self.item = {**self.store.get('instances', self.item['id']), **values}
        self.store.put('instances', self.item)

    def enable(self):
        settings = self.store.meta('settings')
        self.store.set_meta('settings', {**settings, 'dry_run': False})

    def spot(self, **values):
        self.cloud.metadata['i-test001'] = {'spot_strategy': 'SpotAsPriceGo', 'spot_interruption': 'Stop',
                                          'stopped_mode': 'StopCharging', 'lock_reasons': [], **values}
        self.update(spot_auto_restore=True)

    def observed_reclaim(self):
        self.spot(lock_reasons=['Recycling'])
        self.engine.launch('check', background=False)
        self.assertTrue(self.store.get('instances', self.item['id'])['spot_recovery_pending'])
        self.cloud.metadata['i-test001']['lock_reasons'] = []
        self.cloud.states['i-test001'] = 'Stopped'

    def test_manual_controls_require_login_csrf_confirmation_and_exact_id(self):
        for values in ({'confirmed': False}, {'confirmed': 'true'}, {'confirm_instance_id': 'i-wrong'}, {'action': 'delete'}):
            self.assertEqual(self.post(**values).status_code, 400)
        self.assertEqual(self.client.post(self.path, json={}).status_code, 403)
        self.client.post('/api/logout', json={}, headers={'X-CSRF-Token': self.csrf})
        self.assertEqual(self.post().status_code, 401)
        self.assertEqual(self.cloud.actions, [])

    def test_manual_stop_start_and_hold_survive_restart(self):
        response = self.post()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.json['submitted'])
        self.assertTrue(self.store.get('instances', self.item['id'])['manual_hold'])
        self.enable()
        restarted = create_app(self.tmp.name, self.cloud, self.notifier, scheduler=False)
        restarted.extensions['engine'].launch('check', background=False)
        self.assertEqual(self.cloud.actions, [('i-test001', 'StopInstance')])
        response = self.post('start')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(self.store.get('instances', self.item['id'])['manual_hold'])
        self.assertEqual(self.cloud.actions[-1], ('i-test001', 'StartInstance'))

    def test_paused_manual_operation_is_explicit_not_automatic(self):
        self.update(paused=True)
        self.assertEqual(self.post().status_code, 200)
        self.engine.launch('check', background=False)
        self.assertEqual(len(self.cloud.actions), 1)

    def test_no_duplicate_power_request_when_already_in_target_state(self):
        self.assertFalse(self.post('start').json['submitted'])
        self.cloud.states['i-test001'] = 'Stopped'
        self.assertFalse(self.post('stop').json['submitted'])
        self.assertTrue(self.store.get('instances', self.item['id'])['manual_hold'])
        self.assertEqual(self.cloud.actions, [])

    def test_manual_transition_unknown_lock_or_missing_instance_never_acts(self):
        for status in ('Starting', 'Stopping', 'Unknown'):
            self.cloud.states['i-test001'] = status
            self.assertEqual(self.post().status_code, 400)
        self.cloud.states['i-test001'] = 'Running'
        self.cloud.metadata['i-test001'] = {'lock_reasons': ['financial']}
        self.assertEqual(self.post().status_code, 400)
        self.cloud.error = ValueError('实例不存在或已释放')
        self.assertEqual(self.post().status_code, 400)
        self.assertEqual(self.cloud.actions, [])

    def test_manual_start_refuses_overlimit_or_failed_traffic_query(self):
        self.enable()
        self.cloud.states['i-test001'] = 'Stopped'
        self.cloud.usage = 190
        self.assertEqual(self.post('start').status_code, 400)
        with patch.object(self.cloud, 'traffic', side_effect=ValueError('CDT 403')):
            self.assertEqual(self.post('start').status_code, 400)
        self.assertEqual(self.cloud.actions, [])

    def test_failed_manual_stop_persists_hold_and_redacts_credentials(self):
        self.update(stopped_by_monitor=True, spot_recovery_pending=True)
        self.cloud.action_error = ValueError('timeout ' + SETUP['account']['sk'])
        r = self.post()
        self.assertEqual(r.status_code, 400)
        item = self.store.get('instances', self.item['id'])
        self.assertTrue(item['manual_hold'])
        self.assertFalse(item['stopped_by_monitor'])
        self.assertFalse(item['spot_recovery_pending'])
        self.assertNotIn(SETUP['account']['sk'], r.text + json.dumps(self.store.events()))
        self.assertEqual(self.post().status_code, 400)  # failed-request cooldown

    def test_busy_control_is_rejected(self):
        self.engine.lock.acquire()
        try:
            self.assertEqual(self.post().status_code, 409)
        finally:
            self.engine.lock.release()

    def test_spot_observed_reclaim_restores_original_instance(self):
        self.enable()
        self.observed_reclaim()
        restarted = create_app(self.tmp.name, self.cloud, self.notifier, scheduler=False)
        restarted.extensions['engine'].launch('check', background=False)
        self.assertEqual(self.cloud.actions, [('i-test001', 'StartInstance')])
        self.engine.launch('check', background=False)
        self.assertFalse(self.store.get('instances', self.item['id'])['spot_recovery_pending'])

    def test_unobserved_reclaim_is_not_guessed(self):
        self.enable()
        self.spot()
        self.cloud.states['i-test001'] = 'Stopped'
        self.engine.launch('check', background=False)
        self.assertEqual(self.cloud.actions, [])

    def test_spot_restore_blocked_by_optout_dry_run_pause_manual_hold_or_overlimit(self):
        for barrier in ('optout', 'dry_run', 'paused', 'manual_hold', 'overlimit'):
            with self.subTest(barrier=barrier):
                self.cloud.states['i-test001'] = 'Running'
                self.cloud.usage = 10
                self.enable()
                self.update(paused=False, manual_hold=False, stopped_by_monitor=False, spot_auto_restore=True,
                            spot_recovery_pending=False, start_requested_at=0)
                self.observed_reclaim()
                if barrier == 'optout': self.update(spot_auto_restore=False)
                if barrier == 'dry_run': self.store.set_meta('settings', {**self.store.meta('settings'), 'dry_run': True})
                if barrier == 'paused': self.update(paused=True)
                if barrier == 'manual_hold': self.update(manual_hold=True)
                if barrier == 'overlimit': self.cloud.usage = 190
                self.engine.launch('check', background=False)
                self.assertEqual(self.cloud.actions, [])

    def test_spot_restore_requires_correct_type_mode_and_no_cloud_locks(self):
        self.enable()
        self.update(spot_auto_restore=True, spot_recovery_pending=True)
        self.cloud.states['i-test001'] = 'Stopped'
        for changed in ({'spot_strategy': 'NoSpot'}, {'spot_interruption': 'Terminate'}, {'stopped_mode': 'KeepCharging'},
                        {'lock_reasons': ['financial']}, {'lock_reasons': ['security']}, {'lock_reasons': ['Recycling']}):
            self.cloud.metadata['i-test001'] = {'spot_strategy': 'SpotAsPriceGo', 'spot_interruption': 'Stop',
                                              'stopped_mode': 'StopCharging', 'lock_reasons': [], **changed}
            self.engine.launch('check', background=False)
            self.assertEqual(self.cloud.actions, [])

    def test_failed_stock_restore_has_persisted_five_minute_cooldown(self):
        self.enable()
        self.observed_reclaim()
        self.cloud.action_error = ValueError('OperationDenied.NoStock')
        with patch.object(self.cloud, 'action', wraps=self.cloud.action) as action:
            self.engine.launch('check', background=False)
            self.engine.launch('check', background=False)
            self.assertEqual(action.call_count, 1)
            item = self.store.get('instances', self.item['id'])
            self.assertTrue(item['start_requested_at'] > 0)
            self.assertTrue(item['spot_recovery_pending'])
            self.update(start_requested_at=time.time() - 301)
            self.engine.launch('check', background=False)
            self.assertEqual(action.call_count, 2)

    def test_reclaim_marker_expires_when_running_and_warning_is_withdrawn(self):
        self.spot()
        self.update(status='Running', spot_recovery_pending=True, spot_notice_at=time.time() - 601)
        self.engine.launch('check', background=False)
        self.assertFalse(self.store.get('instances', self.item['id'])['spot_recovery_pending'])

    def test_identity_edit_clears_old_hold_and_pending_reclaim(self):
        self.update(manual_hold=True, spot_recovery_pending=True)
        response = self.client.put('/api/instances/' + self.item['id'], json={'instance_id': 'i-another'},
                                   headers={'X-CSRF-Token': self.csrf})
        self.assertEqual(response.status_code, 200)
        item = self.store.get('instances', self.item['id'])
        self.assertFalse(item.get('manual_hold'))
        self.assertFalse(item.get('spot_recovery_pending'))

    def test_old_closed_setup_endpoint_remains_blocked_but_normal_notification_test_succeeds(self):
        self.assertEqual(self.client.post('/api/setup/test', json={'kind': 'notification'}).status_code, 409)
        response = self.client.post('/api/notifications/test', json={'channel': 'wecom', 'url':
                                    'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=fake-test-only'},
                                    headers={'X-CSRF-Token': self.csrf})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.notifier.messages), 1)


class PowerRequestTests(unittest.TestCase):
    def test_every_stop_uses_keep_charging_and_graceful_shutdown(self):
        cloud = Cloud()
        with patch.object(cloud, 'request', return_value={'RequestId': 'synthetic'}) as request:
            cloud.action(SETUP['account'], SETUP['instance'], 'StopInstance')
            params = request.call_args.args[5]
            self.assertEqual(params['StoppedMode'], 'KeepCharging')
            self.assertEqual(params['ForceStop'], 'false')
            self.assertEqual(params['RegionId'], SETUP['instance']['region'])
            cloud.action(SETUP['account'], SETUP['instance'], 'StartInstance')
            self.assertNotIn('StoppedMode', request.call_args.args[5])
        for action in ('DeleteInstance', 'DeleteDisk', 'RebootInstance'):
            with self.assertRaises(ValueError): cloud.action(SETUP['account'], SETUP['instance'], action)

    def test_cloud_metadata_parsing_and_missing_instance_fail_closed(self):
        cloud = Cloud()
        item = {'InstanceId': 'i-test001', 'Status': 'Stopped', 'SpotStrategy': 'SpotAsPriceGo',
                'SpotInterruptionBehavior': 'Stop', 'StoppedMode': 'StopCharging',
                'OperationLocks': {'LockReason': [{'LockReason': 'Recycling'}]}}
        with patch.object(cloud, 'request', return_value={'Instances': {'Instance': [item]}}):
            snapshot = cloud.status(SETUP['account'], SETUP['instance'])
            self.assertEqual(snapshot['spot_interruption'], 'Stop')
            self.assertEqual(snapshot['lock_reasons'], ['Recycling'])
        with patch.object(cloud, 'request', return_value={'Instances': {'Instance': []}}), self.assertRaises(ValueError):
            cloud.status(SETUP['account'], SETUP['instance'])


if __name__ == '__main__': unittest.main()
