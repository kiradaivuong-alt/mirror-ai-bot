"""One-process service. Run behind HTTPS reverse proxy for Messenger webhooks."""
import argparse
import hmac
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from flask import Flask, request, jsonify
from bot import Bot, signature_ok

BASE = Path(__file__).resolve().parent


def load_env():
    path = BASE / '.env'
    if path.exists():
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                os.environ.setdefault(key.strip(), value.strip().strip('\"\''))


def post_json(url, data, headers=None):
    req = urllib.request.Request(url, data=json.dumps(data).encode(),
        headers={'Content-Type': 'application/json', **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            result = json.load(response)
        return 'failed' if result.get('ok') is False or 'error' in result else 'sent'
    except urllib.error.HTTPError as e:
        # Only an explicit throttling rejection is automatically retried.
        return 'retry' if e.code == 429 else ('uncertain' if e.code >= 500 else 'failed')
    except Exception:
        return 'uncertain'


def sender(row, payload):
    if os.environ.get('LIVE_SEND', 'false').lower() != 'true':
        return 'sent'  # Dry-run stores the would-be message in local outbox only.
    if row['channel'] == 'telegram':
        return post_json('https://api.telegram.org/bot' + os.environ['TELEGRAM_BOT_TOKEN'] + '/sendMessage',
                         {'chat_id': os.environ['TELEGRAM_CHAT_ID'], 'text': payload['text'][:4000]})
    message = {'text': payload['text'][:1900], 'metadata': 'mirror-ai-v1'}
    if payload['quote']:
        message['quick_replies'] = [{'content_type': 'text', 'title': 'Xác nhận đặt hàng',
                                     'payload': 'confirm:' + payload['quote']}]
    return post_json('https://graph.facebook.com/' + os.environ['META_GRAPH_VERSION'] + '/'
                     + os.environ['META_PAGE_ID'] + '/messages',
                     {'recipient': {'id': row['sender']}, 'messaging_type': 'RESPONSE', 'message': message},
                     {'Authorization': 'Bearer ' + os.environ['META_PAGE_ACCESS_TOKEN']})


def check_config(server=True):
    required = ['OPENAI_API_KEY']
    if server:
        required += ['META_PAGE_ID', 'META_APP_SECRET', 'META_VERIFY_TOKEN', 'ADMIN_TOKEN']
    if os.environ.get('LIVE_SEND', 'false').lower() == 'true':
        required += ['META_PAGE_ACCESS_TOKEN', 'META_GRAPH_VERSION', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID']
    missing = [key for key in required if not os.environ.get(key)]
    if missing:
        raise SystemExit('Chưa cấu hình: ' + ', '.join(missing))
    if server and len(os.environ['ADMIN_TOKEN']) < 24:
        raise SystemExit('ADMIN_TOKEN phải dài ít nhất 24 ký tự ngẫu nhiên.')
    if os.environ.get('LIVE_SEND', 'false').lower() == 'true' and not re.fullmatch(r'v\d+\.\d+', os.environ['META_GRAPH_VERSION']):
        raise SystemExit('META_GRAPH_VERSION chưa hợp lệ.')


def create_app(bot):
    app = Flask(__name__)
    app.config['MAX_CONTENT_LENGTH'] = 1024 * 1024
    lock = threading.RLock()
    app.extensions['bot_lock'] = lock

    @app.get('/health')
    def health():
        return {'status': 'ok'}

    @app.get('/webhook')
    def verify():
        supplied, expected = request.args.get('hub.verify_token', ''), os.environ.get('META_VERIFY_TOKEN', '')
        if request.args.get('hub.mode') == 'subscribe' and expected and hmac.compare_digest(supplied.encode(), expected.encode()):
            return request.args.get('hub.challenge', ''), 200, {'Content-Type': 'text/plain'}
        return 'Forbidden', 403

    @app.post('/webhook')
    def webhook():
        raw = request.get_data()
        if not signature_ok(raw, request.headers.get('X-Hub-Signature-256', ''), os.environ.get('META_APP_SECRET', '')):
            return 'Forbidden', 403
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or data.get('object') != 'page' or not isinstance(data.get('entry'), list):
            return 'Invalid payload', 400
        pending = []
        try:
            for entry in data['entry']:
                if str(entry.get('id')) != os.environ.get('META_PAGE_ID'):
                    continue
                for e in entry.get('messaging', []):
                    m, p = e.get('message', {}), e.get('postback', {})
                    if not m and not p:
                        continue  # Delivery receipts, read receipts and unrelated events.
                    if m.get('is_echo') and m.get('metadata') == 'mirror-ai-v1':
                        continue
                    customer = str(e.get('recipient' if m.get('is_echo') else 'sender', {}).get('id', ''))
                    if not customer.isdigit():
                        continue
                    mid = m.get('mid') or p.get('mid')
                    if not mid:
                        continue
                    if m.get('is_echo'):
                        event = {'manual': True}
                    else:
                        payload = m.get('quick_reply', {}).get('payload', '') or p.get('payload', '')
                        event = {'text': m.get('text', '') or p.get('title', ''),
                                 'timestamp': float(e.get('timestamp', time.time() * 1000)) / 1000,
                                 'attachment': bool(m.get('attachments')),
                                 'confirm_token': payload[8:] if payload.startswith('confirm:') else None}
                        if not isinstance(event['text'], str):
                            return 'Invalid message', 400
                    pending.append((str(mid), customer, event))
        except (AttributeError, TypeError, ValueError):
            return 'Invalid payload', 400
        # Persist before returning 200. AI and external sends happen in background.
        for mid, customer, event in pending:
            bot.enqueue(mid, customer, event)
        return 'EVENT_RECEIVED', 200

    @app.before_request
    def admin_auth():
        if request.path.startswith('/admin/'):
            expected = os.environ.get('ADMIN_TOKEN', '')
            provided = request.headers.get('Authorization', '')
            if not expected or not hmac.compare_digest(provided.encode(), ('Bearer ' + expected).encode()):
                return jsonify(error='Unauthorized'), 401

    @app.get('/admin/status')
    def status():
        with bot.db() as db:
            counts = {r['status']: r['n'] for r in db.execute('SELECT status,COUNT(*) n FROM outbox GROUP BY status')}
            issues = [dict(r) for r in db.execute("SELECT id,sender,channel,status FROM outbox WHERE status IN ('failed','uncertain') ORDER BY rowid DESC LIMIT 50")]
        return {'outbox': counts, 'needs_review': issues,
                'live_send': os.environ.get('LIVE_SEND', 'false').lower() == 'true'}

    @app.post('/admin/conversations/<customer>/<action>')
    def control(customer, action):
        if action not in {'pause', 'resume'} or not customer.isdigit():
            return {'error': 'Invalid request'}, 400
        with lock, bot.db() as db:
            s = bot.state(db, customer)
            s['paused'] = action == 'pause'
            s['quote'], s['offered'] = None, None
            # Previously queued replies must not reappear on resume.
            db.execute("UPDATE outbox SET status='skipped' WHERE sender=? AND channel='facebook' AND status='pending'", (customer,))
            bot.save(db, customer, s)
        return {'paused': s['paused']}

    return app


def worker(bot, lock, stop):
    while not stop.is_set():
        try:
            with lock:
                # Process a burst of customer edits before sending a possibly stale quote.
                for _ in range(30):
                    if not bot.process_one():
                        break
                for _ in range(50):
                    if not bot.delivery_one(sender):
                        break
        except Exception:
            print('Lỗi worker; dữ liệu đang chờ được giữ lại. Kiểm tra dịch vụ.', flush=True)
        stop.wait(0.5)


def chat(bot):
    print('Thử AI thật; KHÔNG gửi Messenger/Telegram. Gõ /quit để thoát. Có phí API khi gửi câu hỏi.')
    customer = 'demo-' + uuid.uuid4().hex[:8]
    def show(row, payload):
        print('\n[' + row['channel'] + '] ' + payload['text'] + '\n')
        return 'sent'
    while True:
        text = input('Bạn: ').strip()
        if text == '/quit':
            return
        if not text:
            continue
        bot.enqueue(uuid.uuid4().hex, customer, {'text': text, 'timestamp': time.time()})
        bot.process_one()
        while bot.delivery_one(show):
            pass


def main():
    load_env()
    parser = argparse.ArgumentParser()
    parser.add_argument('--chat', action='store_true')
    args = parser.parse_args()
    if args.chat:
        check_config(server=False)
        # Isolate testing from production inbox/outbox.
        chat(Bot(BASE / 'data' / 'demo.sqlite3'))
        return
    check_config()
    bot = Bot(BASE / os.environ.get('DB_PATH', 'data/bot.sqlite3'))
    app = create_app(bot)
    stop = threading.Event()
    thread = threading.Thread(target=worker, args=(bot, app.extensions['bot_lock'], stop), daemon=True)
    thread.start()
    from waitress import serve
    print('Chế độ: ' + ('GỬI THẬT' if os.environ.get('LIVE_SEND', 'false').lower() == 'true' else 'THỬ NGHIỆM, KHÔNG GỬI THẬT'))
    try:
        serve(app, host=os.environ.get('HOST', '127.0.0.1'), port=int(os.environ.get('PORT', '8080')))
    finally:
        stop.set()
        thread.join(timeout=5)


if __name__ == '__main__':
    main()
