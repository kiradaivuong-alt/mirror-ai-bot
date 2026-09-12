import hashlib
import hmac
import json
import os
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch
from ai import FIELDS, understand
from app import create_app
from bot import Bot, other_size, phone_number, signature_ok


def decision(**kwargs):
    d = {'intent': 'chat', 'reply': 'Dạ gương làm bằng acrylic tráng bạc ạ.',
         'handoff': 'none', 'reason': '', 'address_complete': False,
         'patch': dict.fromkeys(FIELDS, ''), 'evidence': dict.fromkeys(FIELDS, '')}
    d.update(kwargs)
    return d


class BotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.d = decision()
        self.calls = []
        def fake(history, state, text):
            self.calls.append((history, text))
            return self.d
        self.bot = Bot(self.temp.name + '/test.sqlite3', fake)

    def tearDown(self):
        self.temp.cleanup()

    def msg(self, text='', **extra):
        mid = extra.pop('mid', uuid.uuid4().hex)
        customer = extra.pop('customer', '123')
        self.bot.enqueue(mid, customer, {'text': text, 'timestamp': time.time(), **extra})
        self.bot.process_one()

    def state(self, customer='123'):
        with self.bot.db() as db:
            return self.bot.state(db, customer)

    def rows(self, table):
        assert table in {'outbox', 'orders', 'inbox'}
        with self.bot.db() as db:
            return [dict(r) for r in db.execute('SELECT * FROM ' + table)]

    def deliver(self):
        while self.bot.delivery_one(lambda row, payload: 'sent'):
            pass

    def prepare_order(self):
        data = {'quantity': '2', 'name': 'Nguyễn An', 'phone': '0901234567',
                'address': '12 Lê Lợi, phường Bến Thành, TP Hồ Chí Minh'}
        self.d = decision(intent='order', patch=data, evidence=data, address_complete=True, reply='')
        self.msg(' | '.join(data.values()))

    def test_new_question_uses_ai(self):
        self.msg('Nó làm bằng gì á shop?')
        self.assertEqual(len(self.calls), 1)
        self.assertIn('acrylic', self.rows('outbox')[0]['payload'])

    def test_history_separate_customers(self):
        self.msg('Xin chào')
        self.msg('Chất liệu?', customer='456')
        self.assertEqual(self.calls[-1][0], [])
        self.msg('Nói rõ thêm?')
        self.assertEqual(self.calls[-1][0][0]['content'], 'Xin chào')

    def test_size_units_and_handoff(self):
        for size in ['30x100cm', '100 × 30 cm', '0,3 x 1m', '0.3m x 100cm']:
            self.assertFalse(other_size(size), size)
        self.msg('Có 40x120 không?')
        self.assertTrue(self.state()['paused'])
        self.assertFalse(self.calls)
        self.assertEqual(len([r for r in self.rows('outbox') if r['channel'] == 'telegram']), 1)

    def test_semantic_custom_size(self):
        self.d = decision(handoff='other_size', reason='Muốn loại to hơn')
        self.msg('có loại bự hơn ko')
        self.assertTrue(self.state()['paused'])

    def test_unknown_and_followup_no_bot_reply(self):
        self.d = decision(handoff='unknown', reason='Chưa có độ dày')
        self.msg('Dày bao nhiêu ly?')
        count = len([r for r in self.rows('outbox') if r['channel'] == 'facebook'])
        self.msg('Tôi cần gấp')
        self.assertEqual(len([r for r in self.rows('outbox') if r['channel'] == 'facebook']), count)
        self.assertEqual(len(self.calls), 1)

    def test_duplicate_webhook(self):
        self.msg('Hi', mid='unique')
        self.msg('Hi', mid='unique')
        self.assertEqual(len(self.calls), 1)

    def test_confirmation_requires_delivery_and_current_token(self):
        self.prepare_order()
        token = self.state()['quote']
        self.msg(confirm_token=token)
        self.assertEqual(self.rows('orders'), [])
        self.deliver()
        self.msg(confirm_token=token)
        orders = self.rows('orders')
        self.assertEqual(len(orders), 1)
        self.assertEqual(json.loads(orders[0]['data'])['total_vnd'], 398000)
        self.msg(confirm_token=token)
        self.assertEqual(len(self.rows('orders')), 1)

    def test_edit_invalidates_old_confirmation(self):
        self.prepare_order()
        old = self.state()['quote']
        self.deliver()
        self.d = decision(intent='order', patch={**dict.fromkeys(FIELDS, ''), 'quantity': '3'},
                          evidence={**dict.fromkeys(FIELDS, ''), 'quantity': '3 cái'})
        self.msg('Đổi 3 cái')
        self.assertNotEqual(self.state()['quote'], old)
        self.msg(confirm_token=old)
        self.assertFalse(self.rows('orders'))

    def test_plain_ok_does_not_confirm(self):
        self.prepare_order()
        self.deliver()
        self.d = decision()
        self.msg('ok')
        self.assertFalse(self.rows('orders'))
        self.deliver()
        self.msg('xác nhận đặt hàng')
        self.assertEqual(len(self.rows('orders')), 1)

    def test_phone_validation(self):
        self.assertEqual(phone_number('+84 901 234 567'), '0901234567')
        self.assertEqual(phone_number('12345'), '')

    def test_no_invented_fields(self):
        self.d = decision(patch={**dict.fromkeys(FIELDS, ''), 'name': 'Nguyễn An'},
                          evidence={**dict.fromkeys(FIELDS, ''), 'name': 'Nguyễn An'})
        self.msg('Tôi muốn mua')
        self.assertTrue(self.state()['paused'])
        self.assertEqual(self.state()['order']['name'], '')

    def test_ai_failure_escalates(self):
        self.bot.ai = lambda *args: (_ for _ in ()).throw(TimeoutError())
        self.msg('Có hàng không?')
        self.assertTrue(self.state()['paused'])
        self.assertTrue(any(r['channel'] == 'telegram' for r in self.rows('outbox')))

    def test_attachment_handoff(self):
        self.msg(attachment=True)
        self.assertTrue(self.state()['paused'])
        self.assertFalse(self.calls)

    def test_manual_reply_stops_bot(self):
        self.msg(manual=True)
        self.msg('Xin chào')
        self.assertFalse(self.calls)
        self.assertFalse(any(r['channel'] == 'facebook' for r in self.rows('outbox')))

    def test_stale_window_no_facebook(self):
        self.msg('Hi', timestamp=time.time() - 90000)
        self.assertFalse(any(r['channel'] == 'facebook' for r in self.rows('outbox')))

    def test_uncertain_send_not_retried(self):
        self.msg('Hi')
        self.bot.delivery_one(lambda *args: 'uncertain')
        self.assertEqual(self.rows('outbox')[0]['status'], 'uncertain')
        self.assertTrue(self.state()['paused'])
        calls = []
        while self.bot.delivery_one(lambda row, payload: calls.append(row['channel']) or 'sent'):
            pass
        self.assertNotIn('facebook', calls)

    def test_restart_preserves_dedup_and_paused_state(self):
        self.msg('40x120', mid='one')
        restored = Bot(self.bot.path)
        restored.enqueue('one', '123', {'text': '40x120'})
        self.assertFalse(restored.process_one())
        with restored.db() as db:
            self.assertTrue(restored.state(db, '123')['paused'])

    def test_webhook_auth_and_batch_echo(self):
        env = {'META_APP_SECRET': 'secret', 'META_VERIFY_TOKEN': 'verify', 'META_PAGE_ID': '9', 'ADMIN_TOKEN': 'admin'}
        with patch.dict(os.environ, env):
            client = create_app(self.bot).test_client()
            self.assertEqual(client.get('/webhook?hub.mode=subscribe&hub.verify_token=verify&hub.challenge=777').text, '777')
            self.assertEqual(client.post('/webhook', json={}).status_code, 403)
            self.assertEqual(client.get('/admin/status').status_code, 401)
            events = [{'sender': {'id': '123'}, 'recipient': {'id': '9'}, 'message': {'mid': 'm1', 'text': 'Hi'}},
                      {'sender': {'id': '9'}, 'recipient': {'id': '123'}, 'message': {'mid': 'm2', 'is_echo': True, 'metadata': 'mirror-ai-v1'}},
                      {'sender': {'id': '9'}, 'recipient': {'id': '123'}, 'message': {'mid': 'm3', 'is_echo': True, 'text': 'Người bán đây'}}]
            raw = json.dumps({'object': 'page', 'entry': [{'id': '9', 'messaging': events}]}).encode()
            sig = 'sha256=' + hmac.new(b'secret', raw, hashlib.sha256).hexdigest()
            self.assertTrue(signature_ok(raw, sig, 'secret'))
            response = client.post('/webhook', data=raw, content_type='application/json', headers={'X-Hub-Signature-256': sig})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(self.rows('inbox')), 2)

    def test_pause_and_resume_cancel_queued_replies(self):
        self.msg('hi')
        with patch.dict(os.environ, {'ADMIN_TOKEN': 'secret'}):
            client = create_app(self.bot).test_client()
            headers = {'Authorization': 'Bearer secret'}
            self.assertEqual(client.post('/admin/conversations/123/pause', headers=headers).status_code, 200)
            self.assertEqual(self.rows('outbox')[0]['status'], 'skipped')
            self.assertEqual(client.post('/admin/conversations/123/resume', headers=headers).status_code, 200)
            self.assertFalse(self.state()['paused'])

    def test_responses_contract(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self):
                return json.dumps({'status': 'completed', 'output': [{'type': 'message', 'content': [
                    {'type': 'output_text', 'text': json.dumps(decision())}]}]}).encode()
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'test'}), patch('urllib.request.urlopen', return_value=Response()) as call:
            understand([], self.state(), 'Chất liệu gì?')
            request_body = json.loads(call.call_args.args[0].data)
            self.assertFalse(request_body['store'])
            self.assertTrue(request_body['text']['format']['strict'])
            self.assertEqual(request_body['input'][-1]['content'], 'Chất liệu gì?')


if __name__ == '__main__':
    unittest.main(verbosity=2)
