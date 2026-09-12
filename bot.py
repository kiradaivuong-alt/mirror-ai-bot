"""Business rules, durable inbox/outbox, and per-customer conversation state."""
import hashlib
import hmac
import json
import re
import sqlite3
import time
import unicodedata
import uuid
from contextlib import contextmanager
from pathlib import Path
from ai import SHOP, FIELDS, understand, validate_decision


def folded(s):
    return ''.join(c for c in unicodedata.normalize('NFD', s.lower().replace('đ', 'd'))
                   if unicodedata.category(c) != 'Mn')


def other_size(text):
    # Semantic cases ("loại lớn hơn") are also handled by AI.
    matches = re.findall(r'(\d+(?:[.,]\d+)?)\s*(cm|m)?\s*[x×*]\s*(\d+(?:[.,]\d+)?)\s*(cm|m)?', text.lower())
    for a, au, b, bu in matches:
        unit_a, unit_b = au or bu or 'cm', bu or au or 'cm'
        a, b = float(a.replace(',', '.')), float(b.replace(',', '.'))
        dimensions = sorted([round(a * (100 if unit_a == 'm' else 1), 2),
                             round(b * (100 if unit_b == 'm' else 1), 2)])
        if dimensions != [30, 100]:
            return True
    return False


def phone_number(text):
    number = re.sub(r'[\s().-]', '', text)
    if number.startswith('+84'):
        number = '0' + number[3:]
    return number if re.fullmatch(r'0[35789]\d{8}', number) else ''


def money(quantity):
    return f'{quantity * SHOP["unit_price_vnd"]:,}'.replace(',', '.') + 'đ'


def signature_ok(raw, supplied, secret):
    expected = 'sha256=' + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return bool(secret) and hmac.compare_digest(expected, supplied)


def fresh_state():
    return {'order': dict.fromkeys(FIELDS, ''), 'address_complete': False,
            'collecting': False, 'paused': False, 'quote': None, 'offered': None,
            'confirmed': None, 'history': [], 'last_customer': 0}


class Bot:
    def __init__(self, path, ai=understand):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path, self.ai = str(path), ai
        with self.db() as db:
            db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS sessions (sender TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS inbox (seq INTEGER PRIMARY KEY AUTOINCREMENT,
              mid TEXT UNIQUE NOT NULL, sender TEXT NOT NULL, payload TEXT NOT NULL,
              done INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS outbox (id TEXT PRIMARY KEY, sender TEXT NOT NULL,
              channel TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
              attempts INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS orders (id TEXT PRIMARY KEY, sender TEXT NOT NULL, data TEXT NOT NULL);
            ''')
            # A interrupted send may already have reached the recipient: do not blindly resend.
            db.execute("UPDATE outbox SET status='uncertain' WHERE status='sending'")

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def state(self, db, sender):
        row = db.execute('SELECT data FROM sessions WHERE sender=?', (sender,)).fetchone()
        return json.loads(row['data']) if row else fresh_state()

    def save(self, db, sender, s):
        db.execute('INSERT OR REPLACE INTO sessions VALUES (?,?)',
                   (sender, json.dumps(s, ensure_ascii=False)))

    def enqueue(self, mid, sender, payload):
        with self.db() as db:
            db.execute('INSERT OR IGNORE INTO inbox(mid,sender,payload) VALUES (?,?,?)',
                       (mid, sender, json.dumps(payload, ensure_ascii=False)))

    def emit(self, db, sender, channel, text, quote=None, allow_paused=False):
        oid = uuid.uuid4().hex
        payload = {'text': text, 'quote': quote, 'allow_paused': allow_paused}
        db.execute('INSERT INTO outbox(id,sender,channel,payload) VALUES (?,?,?,?)',
                   (oid, sender, channel, json.dumps(payload, ensure_ascii=False)))
        return oid

    def reply(self, db, sender, s, text, quote=None, allow_paused=False):
        self.emit(db, sender, 'facebook', text, quote, allow_paused)
        s['history'].append({'role': 'assistant', 'content': text})

    def handoff(self, db, sender, s, reason, text, say=True):
        already = s['paused']
        s['paused'], s['quote'], s['offered'] = True, None, None
        self.emit(db, sender, 'telegram',
                  ('📩 KHÁCH BỔ SUNG' if already else '🔔 CẦN CHỦ SHOP HỖ TRỢ')
                  + f'\nMã khách Messenger: {sender}\nLý do: {reason}\nTin nhắn: {text[:1200]}'
                  + '\nThông tin đã có: ' + json.dumps(s['order'], ensure_ascii=False)
                  + '\nMở hộp thư Fanpage để tiếp nhận. Bot đã tạm dừng với khách này.')
        if say and not already:
            wording = ('Dạ hiện shop có gương 30 × 100 cm. Với kích thước anh/chị cần, '
                       'em xin chuyển chủ shop hỗ trợ trực tiếp ạ.' if reason == 'other_size'
                       else 'Dạ em xin chuyển chủ shop hỗ trợ trực tiếp vấn đề này cho anh/chị ạ.')
            self.reply(db, sender, s, wording, allow_paused=True)

    def confirm(self, db, sender, s, token):
        if not token or token != s['quote'] or token != s['offered'] or s['confirmed']:
            return False
        order_id = 'G-' + uuid.uuid4().hex[:10].upper()
        s['confirmed'] = order_id
        o = s['order']
        data = {**o, 'product': SHOP['name'], 'size': SHOP['size'],
                'unit_price_vnd': SHOP['unit_price_vnd'], 'total_vnd': int(o['quantity']) * SHOP['unit_price_vnd'],
                'payment': 'COD', 'shipping_vnd': 0, 'created_at': time.time()}
        db.execute('INSERT INTO orders VALUES (?,?,?)', (order_id, sender, json.dumps(data, ensure_ascii=False)))
        self.emit(db, sender, 'telegram', f'🛒 ĐƠN MỚI ĐÃ XÁC NHẬN — {order_id}\n'
                  f'Gương acrylic tráng bạc 30 × 100 cm\nSố lượng: {o["quantity"]}\n'
                  f'Thu COD: {money(int(o["quantity"]))} — miễn phí ship\n'
                  f'Người nhận: {o["name"]}\nSĐT: {o["phone"]}\nĐịa chỉ: {o["address"]}\n'
                  f'Mã khách Messenger: {sender}')
        self.reply(db, sender, s, f'Dạ em đã ghi nhận đơn {order_id}, tổng tiền '
                   f'{money(int(o["quantity"]))}, miễn phí vận chuyển, thanh toán khi nhận hàng ạ.')
        s['quote'], s['offered'] = None, None
        return True

    def handle(self, db, sender, s, event):
        text = event.get('text', '')[:8000]
        timestamp = min(float(event.get('timestamp', time.time())), time.time())
        if event.get('manual'):
            s['paused'], s['quote'], s['offered'] = True, None, None
            return
        s['last_customer'] = max(s['last_customer'], timestamp)
        previous_history = list(s['history'])
        s['history'].append({'role': 'user', 'content': text or '[Tệp đính kèm / nút bấm]'})
        if s['paused']:
            self.handoff(db, sender, s, 'Khách đang chờ hỗ trợ', text, say=False)
            return
        if time.time() - s['last_customer'] >= 86400:
            self.handoff(db, sender, s, 'Tin nhắn đã ngoài cửa sổ 24 giờ', text, say=False)
            return
        if s['confirmed']:
            self.handoff(db, sender, s, f'Theo dõi/sửa/hủy/mua thêm sau đơn {s["confirmed"]}', text)
            return
        if event.get('attachment'):
            self.handoff(db, sender, s, 'Ảnh/âm thanh/tệp cần người xem trong Messenger', text)
            return
        if other_size(text):
            self.handoff(db, sender, s, 'other_size', text)
            return
        token = event.get('confirm_token')
        explicit = folded(text).strip(' .!') in {'xac nhan', 'xac nhan dat hang', 'chot don', 'dong y dat hang'}
        if token or explicit:
            if self.confirm(db, sender, s, token or s['offered']):
                return
            self.reply(db, sender, s, 'Dạ đơn chưa sẵn sàng hoặc thông tin đã thay đổi. '
                       'Anh/chị gửi lại thông tin cần đặt để em kiểm tra nhé.')
            return
        d = validate_decision(self.ai(previous_history, s, text))
        if d['handoff'] != 'none':
            self.handoff(db, sender, s, d['handoff'] + ': ' + d['reason'] if d['handoff'] != 'other_size' else 'other_size', text)
            return
        if d['intent'] == 'cancel':
            s.update(order=dict.fromkeys(FIELDS, ''), address_complete=False, collecting=False, quote=None, offered=None)
            self.reply(db, sender, s, 'Dạ em đã dừng ghi nhận đơn này ạ.')
            return
        changed = False
        for f in FIELDS:
            value, evidence = d['patch'][f].strip(), d['evidence'][f].strip()
            if not value:
                continue
            if not evidence or evidence not in text:
                raise ValueError('Extraction missing customer evidence')
            if f == 'quantity':
                if not re.fullmatch(r'[1-9]\d{0,2}', value) or int(value) > 100:
                    self.handoff(db, sender, s, 'Số lượng lớn/không hợp lệ cần kiểm tra', text)
                    return
            if f == 'phone':
                value = phone_number(evidence)
                if not value:
                    s['quote'], s['offered'] = None, None
                    self.reply(db, sender, s, 'Anh/chị kiểm tra và gửi lại số điện thoại di động 10 số giúp em nhé.')
                    return
            if f == 'name' and value not in text:
                raise ValueError('Name not in customer text')
            if (f == 'name' and len(value) > 100) or (f == 'address' and len(value) > 500):
                raise ValueError('Customer field too long for a readable quote')
            if f == 'address':
                # Require every word of a merged address to be from customer-provided data.
                available = set(re.findall(r'\w+', folded(s['order']['address'] + ' ' + text)))
                if not set(re.findall(r'\w+', folded(value))).issubset(available):
                    raise ValueError('Invented address')
                s['address_complete'] = d['address_complete']
            if value != s['order'][f]:
                s['order'][f], changed = value, True
        if changed:
            s['quote'], s['offered'] = None, None
        s['collecting'] = s['collecting'] or d['intent'] == 'order' or changed
        if not s['collecting']:
            self.reply(db, sender, s, d['reply'] or 'Anh/chị muốn tìm hiểu thêm điều gì về gương ạ?')
            return
        o = s['order']
        missing = [label for key, label in [('quantity', 'số lượng'), ('name', 'tên người nhận'),
                   ('phone', 'số điện thoại'), ('address', 'địa chỉ đầy đủ')] if not o[key]]
        if o['address'] and not s['address_complete']:
            missing.append('địa chỉ đủ đường/thôn/ấp, phường/xã và tỉnh/thành')
        if missing:
            prefix = d['reply'].strip()[:450]
            self.reply(db, sender, s, (prefix + '\n' if prefix else '')
                       + 'Anh/chị cho em xin ' + ', '.join(missing) + ' để ghi nhận đơn nhé.')
        else:
            if not s['quote']:
                s['quote'] = uuid.uuid4().hex
            prefix = d['reply'].strip()[:450]
            self.reply(db, sender, s, (prefix + '\n' if prefix else '') + f'Dạ em xin xác nhận:\nGương acrylic tráng bạc 30 × 100 cm: '
                       f'{o["quantity"]} chiếc\nTổng tiền: {money(int(o["quantity"]))}, miễn phí ship\n'
                       f'Thanh toán khi nhận hàng (COD)\nNgười nhận: {o["name"]}\n'
                       f'SĐT: {o["phone"]}\nĐịa chỉ: {o["address"]}\n'
                       'Anh/chị bấm “Xác nhận đặt hàng” hoặc nhắn “xác nhận đặt hàng” nếu thông tin đúng nhé.', s['quote'])

    def process_one(self):
        # Called by a single worker under the application's shared lock.
        with self.db() as db:
            row = db.execute('SELECT * FROM inbox WHERE done=0 ORDER BY seq LIMIT 1').fetchone()
            if not row:
                return False
            sender = row['sender']
            db.execute('SAVEPOINT business')
            s = self.state(db, sender)
            event = json.loads(row['payload'])
            try:
                self.handle(db, sender, s, event)
            except Exception:
                # Roll back partial orders/outbox before escalating. Do not log customer data or tokens.
                db.execute('ROLLBACK TO business')
                s = self.state(db, sender)
                s['last_customer'] = max(s['last_customer'], min(float(event.get('timestamp', time.time())), time.time()))
                s['history'].append({'role': 'user', 'content': event.get('text', '')[:8000]})
                self.handoff(db, sender, s, 'AI lỗi hoặc chưa hiểu chắc; cần người kiểm tra', event.get('text', ''))
            s['history'] = s['history'][-24:]
            self.save(db, sender, s)
            db.execute('UPDATE inbox SET done=1 WHERE seq=?', (row['seq'],))
        return True

    def delivery_one(self, send):
        with self.db() as db:
            row = db.execute("SELECT rowid,* FROM outbox WHERE status='pending' AND next_try<=? ORDER BY rowid LIMIT 1",
                             (time.time(),)).fetchone()
            if not row:
                return False
            p, s = json.loads(row['payload']), self.state(db, row['sender'])
            skip = row['channel'] == 'facebook' and (
                time.time() - s['last_customer'] >= 86400 or
                (s['paused'] and not p['allow_paused']) or
                (p['quote'] and p['quote'] != s['quote']))
            if skip:
                db.execute("UPDATE outbox SET status='skipped' WHERE id=?", (row['id'],))
                return True
            db.execute("UPDATE outbox SET status='sending',attempts=attempts+1 WHERE id=?", (row['id'],))
        try:
            result = send(dict(row), p)  # sent / retry / uncertain / failed
        except Exception:
            result = 'uncertain'
        if result not in {'sent', 'retry', 'uncertain', 'failed'}:
            result = 'failed'
        if result == 'retry' and row['attempts'] >= 4:
            result = 'failed'
        with self.db() as db:
            db.execute('UPDATE outbox SET status=?,next_try=? WHERE id=?',
                       ('pending' if result == 'retry' else result,
                        time.time() + min(300, 10 * 2 ** row['attempts']), row['id']))
            s = self.state(db, row['sender'])
            if result == 'sent' and p['quote'] == s['quote'] and p['quote']:
                s['offered'] = p['quote']
            if result in {'failed', 'uncertain'}:
                s['paused'], s['offered'] = True, None
                if row['channel'] == 'facebook':
                    self.emit(db, row['sender'], 'telegram',
                              f'⚠️ CẦN KIỂM TRA GỬI MESSENGER\nMã khách: {row["sender"]}\n'
                              f'Trạng thái: {result}\nBot đã tạm dừng. Mã gửi: {row["id"]}')
            self.save(db, row['sender'], s)
        return True
