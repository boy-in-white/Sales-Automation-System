"""Compact owner navigation; no network requests and no customer-facing controls."""

import json
import re
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def short(value, limit=110):
    value = re.sub(r'[\x00-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2060-\u206f]', ' ', str(value))
    value = re.sub(r'\s+', ' ', value).strip()
    return value[:limit] + ('…' if len(value) > limit else '')


def local_time(value):
    return datetime.fromtimestamp(value, ZoneInfo('Europe/Kyiv')).strftime('%H:%M') if type(value) in (int, float) and 0 < value < 32503680000 else '—'


def bank_status(store):
    """A local diagnostic view, never a bank call or a reason to contact customers."""
    from checkout import payment_settings
    from monobank_payments import configured, scope
    private = Path(store.path).parent
    try:
        settings = configured(private, payment_settings(private, with_bank=False))
    except (ValueError, TypeError, OSError):
        settings = None
    title = '💳 Передоплати mono\n\n'
    if not settings:
        return title + ('Підключення потребує налаштування.' if settings is None else 'Автоперевірка ще не підключена.') + '\nВідкрий «Налаштувати monobank.command», встав токен і вибери окремий рахунок. До підключення перевіряй оплату в Ledgerly.'
    health = store.meta('mono-health')
    same = health.get('scope') == scope(settings) and health.get('activation_id') == settings['activation_id']
    state = health.get('state') if same or health.get('state') == 'manual' else 'starting'
    if same and health.get('updated') and time.time() - health['updated'] > 180:
        state = 'stopped'
    states = {'idle': 'Очікує нову передоплату; зайвих запитів до банку немає.',
              'ok': 'Зв’язок із mono працює. Очікувані передоплати перевіряються.',
              'waiting': 'Чекає наступного дозволеного запиту до банку.',
              'retry': 'Є тимчасовий збій. Наступна перевірка відбудеться автоматично.',
              'starting': 'Налаштовано. Перевірка почнеться, коли клієнту надішлються реквізити.',
              'manual': 'Налаштовано, але спільна черга замовлень недоступна. Перевір підключення Ledgerly.',
              'stopped': 'Немає свіжого стану перевірки. Перевір, чи запущено Repliq.'}
    text = title + states.get(state, states['starting'])
    if same and health.get('last_success'):
        text += '\nОстання успішна перевірка: ' + datetime.fromtimestamp(health['last_success'],ZoneInfo('Europe/Kyiv')).strftime('%d.%m %H:%M') + '.'
    if same and str(health.get('code')) in ('401', '403'):
        text += '\nТокен не приймається банком: онови його через налаштування mono.'
    if same and health.get('next_check') and state == 'retry':
        text += '\nНаступна спроба — не раніше ' + local_time(health['next_check']) + '.'
    if same and type(health.get('matched_payments')) is int:
        text += '\nЗіставлено переказів: %s.' % health['matched_payments']
    if same and health.get('awaiting_receipts'):
        text += '\nПлатіж знайдено, очікуємо квитанцію: %s.' % health['awaiting_receipts']
    text += '\n\nТільки вибраний рахунок і передоплати 94–105 грн. Оплата нових замовлень підтверджується автоматично після надходження коштів і збереження квитанції. Фото саме по собі оплату не підтверджує. Повна оплата — у Ledgerly.'
    return text


class OperatorMenu:
    def __init__(self, handoffs, registry, account, group):
        self.handoffs, self.registry = handoffs, registry
        self.store, self.account, self.group = handoffs.store, account, group

    def publish(self, owner, key, text, buttons):
        # Navigation replaces one personal panel, instead of filling the chat with menus.
        with self.store.db() as db:
            if db.execute('SELECT 1 FROM admin_outbox WHERE event_key=?', ('operator-panel:' + key + ':0',)).fetchone():
                return
            db.execute("UPDATE admin_outbox SET status='cancelled' WHERE account=? AND owner=? AND status='pending' AND event_key LIKE 'operator-panel:%'", (self.account, owner))
            self.registry.tell(owner, 'operator-panel:' + key, text, {'inline_keyboard': buttons})

    def show(self, owner, key, section='all', page=0):
        if section == 'mono':
            # A reply-keyboard action must answer below the request. Editing an
            # old navigation panel looked as though the bank button did nothing.
            return self.registry.tell(owner, 'mono-status:' + key, bank_status(self.store),
                                      {'inline_keyboard': [[{'text': '↻ Оновити', 'callback_data': 'operator:mono:0'},
                                                            {'text': '← Діалоги', 'callback_data': 'operator:all:0'}]]})
        section = section if section in ('all', 'needs', 'human', 'payment') else 'all'
        page = max(0, min(int(page), 100))
        clauses = ['h.account=?', '(h.group_id=? OR h.group_id IS NULL)', "h.mode!='closed'",
                   "(h.mode IN ('human','waiting','opening','paused','blocked') OR EXISTS(SELECT 1 FROM chats x WHERE x.account=h.account AND x.thread=h.thread))"]
        args = [self.account, self.group]
        if section == 'needs':
            clauses.append("((h.attention=1 AND h.mode!='human') OR h.mode='blocked')")
        elif section == 'human':
            clauses.append("h.mode='human'")
        elif section == 'payment':
            clauses.extend(["c.order_id!=''", 'c.verified=0'])
        with self.store.db() as db:
            rows = db.execute("SELECT h.*,c.snapshot,c.verified,(SELECT o.text FROM bridge_outbox o WHERE o.account=h.account AND o.thread=h.thread AND o.kind='customer' ORDER BY o.id DESC LIMIT 1) AS last_customer "
                              "FROM handoffs h JOIN effective_ads a ON a.account=h.account AND a.advert=h.advert LEFT JOIN order_cards c ON c.account=h.account AND c.thread=h.thread WHERE " +
                              ' AND '.join(clauses) + ' ORDER BY h.attention DESC,h.updated DESC LIMIT 1000', args).fetchall()
        cards = []
        for row in rows:
            item = dict(row)
            try:
                item['order'] = json.loads(item['snapshot']) if item.get('snapshot') else {}
            except (ValueError, TypeError):
                item['order'] = {}  # A damaged order card must not hide the customer.
            if section == 'payment' and not (item['order'].get('issued') and item['order'].get('method') in ('cod', 'full')):
                continue
            cards.append(item)
        page = min(page, max(0, (len(cards) - 1) // 6))
        selected = cards[page * 6:(page + 1) * 6]
        heading = {'all': '💬 Діалоги', 'needs': '🙋 Потрібна твоя увага', 'human': '✍️ Відповідаю я', 'payment': '💳 Оплата у Ledgerly'}[section]
        lines, buttons = [heading + ' · %s' % len(cards)], []
        for item in selected:
            order = item['order']
            public = item['title'].split(' · ID ', 1)[0]
            name = short(public, 40) if ' · ID ' in item['title'] else ''
            name = name or 'Клієнт'
            mode = {'bot': 'auto', 'human': 'я · auto о ' + local_time(item['human_until']), 'waiting': 'auto · потрібна допомога',
                    'paused': 'пауза', 'blocked': 'перевірити надсилання', 'returning': 'повертається в auto'}.get(item['mode'], 'очікує')
            lines.append('\n%s · ID %s\n%s' % (name, short(item['buyer'], 32), mode))
            if item.get('last_customer'):
                body = item['last_customer'].split('\n', 1)[-1]
                lines.append('Клієнт: ' + short(body))
            goods = ', '.join('%s %s' % (i.get('name', 'товар'), i.get('size') or '') for i in order.get('items', [])[:2])
            if goods:
                lines.append(short(goods))
            if order.get('id'):
                lines.append('№' + short(order['id'], 40))
            group = str(item['group_id'])
            if group.startswith('-100') and item.get('topic_id'):
                target = item.get('control_message_id') or item['topic_id']
                url = 'https://t.me/c/%s/%s?thread=%s' % (group[4:], target, item['topic_id'])
                buttons.append([{'text': 'Відкрити · ' + name + ' · ' + short(item['buyer'], 12), 'url': url}])
            else:
                buttons.append([{'text': 'Відкрити · ' + name + ' · ' + short(item['buyer'], 12), 'callback_data': 'openchat:' + self.handoffs.link_token(item)}])
        if not selected:
            lines.append('\nЗараз таких діалогів немає.' if section != 'all' else '\nТут з’являться нові клієнти.')
        buttons.append([{'text': ('✓ ' if section == value else '') + label, 'callback_data': 'operator:%s:0' % value}
                        for label, value in (('Усі', 'all'), ('Потрібен я', 'needs'), ('Мої', 'human'))])
        navigation = []
        if page:
            navigation.append({'text': '← Назад', 'callback_data': 'operator:%s:%s' % (section, page - 1)})
        if (page + 1) * 6 < len(cards):
            navigation.append({'text': 'Далі →', 'callback_data': 'operator:%s:%s' % (section, page + 1)})
        if navigation:
            buttons.append(navigation)
        buttons.append([{'text': '↻ Оновити', 'callback_data': 'operator:%s:%s' % (section, page)},
                        {'text': '💳 Стан mono', 'callback_data': 'operator:mono:0'}])
        lines.append('\nНапиши у темі клієнта — підключишся особисто. auto повертає бота; після 40 хв без твоїх відповідей він повертається сам. Оплата й відправлення — у Ledgerly.')
        self.publish(owner, key, '\n'.join(lines), buttons)
