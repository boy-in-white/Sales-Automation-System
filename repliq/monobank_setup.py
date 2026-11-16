#!/usr/bin/env python3
"""Interactive setup only: read mono accounts, then save an explicit selection."""

from getpass import getpass
from pathlib import Path
import re
import time
import uuid

from app import ROOT, private_lock, read_json, write_private
from checkout import payment_settings, valid_iban
from monobank_payments import MonoClient, MonoError, destination, fingerprint, matches_account


def personal_accounts(info):
    if not isinstance(info, dict) or not info.get('clientId'):
        raise ValueError('mono не повернув профіль власника.')
    return [account for account in info.get('accounts', []) if
            isinstance(account, dict) and isinstance(account.get('id'), str) and account['id'] != '0' and
            account.get('currencyCode') == 980 and account.get('type') != 'fop' and
            isinstance(account.get('iban'), str) and valid_iban(account['iban'])]


def save_selection(private_dir, token, info, selected, merchant, confirmed, now=None):
    if not confirmed or selected not in personal_accounts(info) or not matches_account(selected, merchant):
        raise ValueError('Рахунок і реквізити не збігаються або рахунок не вибрано.')
    if not isinstance(token, str) or not token.strip() or any(c.isspace() for c in token):
        raise ValueError('Перевір токен mono.')
    moment = time.time() if now is None else now
    config = {'enabled':True, 'token':token, 'client_id':info['clientId'], 'account_id':selected['id'],
              'activation_id':uuid.uuid4().hex, 'enabled_at':moment, 'setup_info_at':moment,
              'destination_signature':destination(merchant), 'dedicated_account':True,
              'account_label': 'mono · ' + (merchant['account'][-4:]),
              'own_iban_hashes': sorted({fingerprint(''.join(a['iban'].split()).upper()) for a in info.get('accounts', [])
                                         if isinstance(a,dict) and isinstance(a.get('iban'),str) and a['iban']})}
    private_dir = Path(private_dir)
    with private_lock(private_dir / 'monobank-setup.lock'):
        previous = read_json(private_dir / 'monobank.json')
        payments = read_json(private_dir / 'payments.json')
        if previous.get('enabled') and previous.get('account_id') == selected['id'] and previous.get('client_id') == info['clientId'] and previous.get('destination_signature') == config['destination_signature']:
            # Token rotation must not make existing invoices lose their activation.
            config['activation_id'] = previous['activation_id']
            config['enabled_at'] = previous['enabled_at']
        if previous or payments:
            write_private(private_dir / 'monobank-setup-backup.json', {'monobank':previous, 'payments':payments})
        # A failure leaves auto-check disabled by destination mismatch, never enabled on the wrong card.
        write_private(private_dir / 'payments.json', merchant)
        write_private(private_dir / 'monobank.json', config)
    return config


def main():
    private_dir = ROOT / 'private'
    print('Налаштування mono для Repliq\nТокен не показується під час введення.')
    existing = read_json(private_dir / 'monobank.json')
    token = getpass('Встав токен mono та натисни Enter (введення приховане): ').strip() or existing.get('token','')
    if not token:
        raise ValueError('Токен не введено.')
    info = MonoClient(token).info()
    accounts = personal_accounts(info)
    if not accounts:
        raise ValueError('Не знайшов особистого гривневого рахунку. Перевір доступ токена до рахунків.')
    print('\nВибери окремий рахунок для оплат. Інші рахунки чекер не читатиме.')
    for index, account in enumerate(accounts, 1):
        cards = ', '.join('•••• ' + str(pan)[-4:] for pan in account.get('maskedPan', [])) or 'IBAN …' + account['iban'][-4:]
        print('%s. %s · %s' % (index, account.get('type','mono'), cards))
    choice = input('Номер рахунку зі списку: ').strip()
    if not choice.isdigit() or not 1 <= int(choice) <= len(accounts):
        raise ValueError('Неправильний номер. Жоден рахунок не вибрано.')
    selected = accounts[int(choice)-1]
    current = payment_settings(private_dir, with_bank=False)
    if current and matches_account(selected, current):
        merchant = dict(current)
        print('Поточні реквізити …%s належать вибраному рахунку.' % merchant['account'][-4:])
    else:
        print('Номер картки потрібен тільки для реквізитів клієнту. Enter — використовувати особистий IBAN цього рахунку.')
        number = re.sub(r'\s','',input('Номер картки або Enter: '))
        merchant = {'enabled':True, 'kind':'card' if number else 'iban',
                    'account':number or selected['iban'], 'recipient':info.get('name',''), 'bank_name':'monobank'}
        if not matches_account(selected, merchant):
            raise ValueError('Ця картка не належить вибраному рахунку. Налаштування не збережені.')
    print('\nНа цей рахунок мають приходити тільки оплати замовлень. Картки одного рахунку API не розділяє.')
    print('94–105 грн зараховуються як передоплата 100 грн. Решта за товар у ТТН — ціна мінус 100 грн.')
    print('Перевірка банку працює лише під час очікування такої передоплати. Повна оплата — вручну в Ledgerly.')
    print('Платежі без номера замовлення розподіляються між замовленнями групою; платника API може не визначати.')
    confirmed = input('Це окремий рахунок для оплат замовлень? Напиши «так»: ').strip().lower() == 'так'
    if not confirmed:
        raise ValueError('Налаштування не збережені.')
    config = save_selection(private_dir, token, info, selected, merchant, True)
    print('\nГотово: %s. Перезапусти Repliq. Ledgerly також потрібна оновлена версія.' % config['account_label'])
    print('Чекер працюватиме разом із Repliq. Банківські виписки не публікуються в Telegram.')


if __name__ == '__main__':
    try:
        main()
    except (EOFError, KeyboardInterrupt):
        print('\nНалаштування перервано.')
    except MonoError as error:
        print('Не вдалося прочитати рахунки mono (%s). Перевір токен або повтори через хвилину.' % error.code)
    except (ValueError, OSError) as error:
        print(str(error))
