"""Owner-supplied garment measurements and body sizing, stored separately per SQL product."""

import copy
import json
import re
import time

from app import AppError
from catalog import SIZES, normalize
from product_registry import words, EDITABLE_FACTS
from shop_facts import measurements_reply

SIZE_PATTERN = r'XXL|ХХЛ|XL|ХЛ|XS|ХС|S|С|M|М|L|Л'
LABELS = {'length_cm': 'довжина', 'width_cm': 'ширина', 'waist_cm': 'пояс',
          'leg_width_cm': 'ширина штанини'}


def size_code(value):
    return value.upper().translate(str.maketrans({'С': 'S', 'М': 'M', 'Л': 'L', 'Х': 'X'}))


def rows(text):
    text = text.strip()
    if not text or len(text) > 1500:
        raise AppError('Надішли таблицю текстом до 1500 символів: один розмір у кожному рядку.')
    # Allow headings and both the owner's existing "Заміри S — ..." and plain "S: ...".
    text = re.sub(r';(?!\s*(?:' + SIZE_PATTERN + r')(?!\w))\s*',', ',text,flags=re.I)
    matches = list(re.finditer(r'(?im)(?:^|[\n;])\s*[•*\-]?\s*(?:заміри\s*|замеры\s*|розмір\s*|размер\s*)?('
                               + SIZE_PATTERN + r')(?!\w)\s*[:|—–-]?\s*([^\n;]+)', text))
    if not matches:
        raise AppError('Не бачу рядків із розмірами. Наприклад:\nS: довжина 64, ширина 43\nM: довжина 65, ширина 45')
    result = {}
    covered = list(text)
    for match in matches:
        code = size_code(match[1])
        if code in result:
            raise AppError('Розмір %s повторюється. Залиши один рядок для кожного розміру.' % code)
        result[code] = normalize(match[2])
        covered[match.start():match.end()] = ' ' * (match.end() - match.start())
    # Never silently drop a typo such as an unsupported XXLX row or a seventh size.
    remainder = ''.join(covered)
    if re.search(r'\d', remainder) or len(result) > 6:
        raise AppError('Частину таблиці не вдалося розібрати. Надішли лише рядки S, M, L тощо без інших чисел у заголовку.')
    return result


def parse_measurements(text):
    values = rows(text)
    measures, raw = {}, False
    labels = {'leg_width_cm': r'ширин\w*\s+(?:в\s+)?штанин\w*',
              'waist_cm': r'(?:ширин\w*\s+(?:в\s+)?пояс\w*|пояс\w*|тал[іи]\w*)',
              'width_cm': r'ширин\w*', 'length_cm': r'(?:довжин\w*|длин\w*)'}
    for size, body in values.items():
        measured, remaining = {}, body
        for field, label in labels.items():
            pattern = r'(?:' + label + r')\s*[:—–-]?\s*(\d{1,3}(?:[.,]\d+)?)\s*(?:см)?'
            matches = list(re.finditer(pattern, remaining))
            if len(matches) > 1:
                raise AppError('У рядку %s кілька значень для «%s». Уточни одне.' % (size, LABELS[field]))
            if matches:
                value = float(matches[0][1].replace(',', '.'))
                if not 0 < value <= 300:
                    raise AppError('Значення замірів мають бути більші за 0 й не перевищувати 300 см.')
                measured[field] = value
                remaining = re.sub(pattern, ' ', remaining)
        if not measured or re.search(r'\d', remaining):
            # A raw measurement table is useful too; don't guess unlabeled width/length order.
            if re.search(r'зріст|зрост|ріст|рост|ваг|вес|кг', body):
                raise AppError('Це параметри людини. Додай їх у «Розмірна сітка», а тут — заміри самого одягу.')
            if not re.search(r'довжин|длин|ширин|пояс|рукав|груд|плеч|стег|тал[іи]|см', body):
                raise AppError('Підпиши, що виміряно: довжина, ширина, пояс тощо. Сам порядок чисел бот не вгадуватиме.')
            raw = True
        measures[size] = measured
    if raw:
        return {'measurements':text.strip(), 'garment_measurements':{}}
    return {'measurements': None, 'garment_measurements': measures}


def parse_guide(text):
    values = rows(text)
    heights, weights = {}, {}
    for size, body in values.items():
        if re.search(r'довжин|длин|ширин|пояс|груд|тал[іи]', body):
            raise AppError('Це заміри одягу. Додай їх у «Заміри виробу». Тут потрібен зріст людини й, за бажанням, вага.')
        # Accept 1.70–1.80 as well as 170–180, but never guess the meaning of four bare numbers.
        body = re.sub(r'([12])[.,](\d{1,2})(?!\d)', lambda m: str(round(float(m[0].replace(',', '.')) * 100)), body)
        body = re.sub(r'(?<!\d)(\d{3})\s*\+',r'\1–230',body)
        pattern = r'(\d{2,3})\s*[-–—…]\s*(\d{2,3})'
        found = list(re.finditer(pattern, body))
        if len(found) not in (1, 2):
            raise AppError('Для %s потрібен діапазон зросту: наприклад «M: 160–170» або «M: зріст 160–170, вага 50–70».' % size)
        labeled_height = re.search(r'(?:зріст|зрост|ріст|рост)\w*\s*[:—–-]?\s*' + pattern, body)
        labeled_weight = re.search(r'(?:ваг|вес)\w*\s*[:—–-]?\s*' + pattern, body)
        if len(found) == 2 and not (labeled_height and labeled_weight):
            raise AppError('Для %s підпиши обидва діапазони: «зріст ... , вага ...».' % size)
        height = [int(v) for v in (labeled_height.groups() if labeled_height else found[0].groups())]
        if not 120 <= height[0] <= height[1] <= 230 or (labeled_weight and not labeled_height):
            raise AppError('Перевір зріст для %s: підтримується діапазон від 120 до 230 см.' % size)
        heights[size] = height
        if labeled_weight:
            weight = [int(v) for v in labeled_weight.groups()]
            if not 35 <= weight[0] <= weight[1] <= 200:
                raise AppError('Перевір вагу для %s: підтримується діапазон від 35 до 200 кг.' % size)
            weights[size] = weight
        remainder = re.sub(pattern, '', body)
        if re.search(r'\d', remainder):
            raise AppError('У рядку %s є зайві числа. Перевір діапазони.' % size)
    if weights and set(weights) != set(heights):
        raise AppError('Додай вагу для кожного розміру або залиш лише діапазони зросту.')
    if weights:
        return {'sizing_basis': 'body', 'height_guidance': {}, 'size_chart_verified': True,
                'sizes': [{'size': s, 'height_cm': heights[s], 'weight_kg': weights[s]} for s in SIZES if s in heights]}
    return {'sizing_basis': 'height', 'height_guidance': heights, 'size_chart_verified': False, 'sizes': []}


def guide_text(product):
    if product.get('sizing_basis') == 'height' and product.get('height_guidance'):
        return '\n'.join('%s: зріст %g–%g' % (s, *product['height_guidance'][s]) for s in SIZES if s in product['height_guidance'])
    if product.get('size_chart_verified') and product.get('sizes'):
        return '\n'.join('%s: зріст %g–%g, вага %g–%g' % (r['size'], *r['height_cm'], *r['weight_kg']) for r in product['sizes'])
    return 'Ще не додано.'


def session_keyboard(back=False):
    return {'keyboard': ([['⬅️ До товару']] if back else []) + [['↩️ Скасувати'], ['🏠 Головне меню']], 'resize_keyboard': True,
            'input_field_placeholder': 'Надішли текст або скасуй…'}


def input_message(registry, owner, text, key):
    """Queue long text intact only while the owner explicitly edits a sizing field."""
    with registry.store.db() as db:
        row = db.execute('SELECT action,updated FROM admin_sessions WHERE account=? AND owner=?', (registry.account,owner)).fetchone()
    if text == '⬅️ До товару' and (not row or not row['action'].startswith('sizing:')):
        last = registry.store.meta('product-editor:%s:%s' % (registry.account,owner)).get('product_id')
        if last:
            registry.job(owner,key,'sizing_open',{'product_id':last})
            return True
    if not row or not row['action'].startswith('sizing:') or time.time() - row['updated'] > 600:
        return False
    if text == '⬅️ До товару':
        payload = json.loads(row['action'][7:])
        registry.job(owner, key, 'sizing_open', {'product_id': payload['product_id']})
        return True
    if text.startswith('/') or text in ('↩️ Скасувати','🏠 Головне меню','⚙️ Керування каталогом','📏 Заміри та розміри',
                                       '📦 Товари','📋 Оголошення','➕ Додати оголошення','✏️ Змінити оголошення',
                                       '⛔ Вимкнути оголошення','🔄 Оновити каталог'):
        return False
    payload = json.loads(row['action'][7:])
    registry.job(owner,key,'sizing_preview',dict(payload,text=text))
    return True


def process(worker, job):
    registry, owner, event = worker.registry, job['owner'], job['event_key']
    action, payload = job['action'], job['payload']
    if action == 'sizing_cancel':
        registry.private_message(owner,'/cancel',event)
        return
    records = registry.products()
    if action == 'sizing_list':
        query = words(payload.get('argument',''))
        records = [r for r in records if not query or query in words(r['canonical_name'])]
        if not records:
            raise AppError('Спочатку додай товар у Ledgerly й онови каталог. Залишки залишаються в Ledgerly.')
        page = max(0,min(int(payload.get('page',0)),(len(records)-1)//20))
        buttons = [[{'text': r['canonical_name'][:60], 'callback_data': registry.choice(owner,{'_action':'sizing_open','product_id':str(r['id'])})}] for r in records[page*20:(page+1)*20]]
        navigation = []
        for label, target in (('← Назад',page-1),('Далі →',page+1)):
            if 0 <= target <= (len(records)-1)//20:
                navigation.append({'text':label,'callback_data':registry.choice(owner,{'_action':'sizing_list','page':target,'argument':payload.get('argument','')})})
        if navigation:
            buttons.append(navigation)
        registry.tell(owner,event,'📦 Налаштування товарів\n\nОбери товар, щоб змінити заміри, розмірну сітку або характеристики.\nЦіни й залишки оновлюються з Ledgerly.',{'inline_keyboard':buttons})
        return
    product_id = str(payload.get('product_id',''))
    record = next((r for r in records if str(r['id']) == product_id),None)
    product = next((p for p in worker.catalog.products.values() if str(p.get('ledgerly_product_id')) == product_id),None)
    if not record or not product:
        raise AppError('Товар більше неактивний. Онови каталог і вибери його знову.')
    version, profile = registry.sizing_profile(product_id)
    def button(label, action, **extra):
        return {'text':label, 'callback_data':registry.choice(owner,dict(_action=action,product_id=product_id,**extra))}
    if action == 'sizing_open':
        registry.store.set_meta('product-editor:%s:%s' % (registry.account,owner), {'product_id': product_id})
        with registry.store.db() as db:
            db.execute('DELETE FROM admin_sessions WHERE account=? AND owner=?',(registry.account,owner))
        measured, _ = measurements_reply(product)
        registry.tell(owner,event,'📏 '+record['canonical_name']+'\n\n📐 Заміри виробу\n'+(measured or 'Ще не додано.')+
                      '\n\n📊 Розмірна сітка для підбору\n'+guide_text(product)+
                      '\n\nНа «які заміри?» бот надсилає заміри одягу. На «який розмір підійде?» — підбирає за сіткою та перевіряє наявність.',
                      {'inline_keyboard':[[button('📐 Заміри виробу','sizing_edit',mode='measurements')],
                                          [button('📊 Розмірна сітка','sizing_edit',mode='guide')],
                                          [button('🧵 Характеристики','sizing_facts')],
                                          [button('📝 Опис товару','sizing_edit',mode='description')],
                                          [{'text':'← До товарів','callback_data':registry.choice(owner,{'_action':'sizing_list'})}]]})
        return
    if action == 'sizing_facts':
        current = product.get('facts', {})
        lines = ['🧵 '+record['canonical_name'], 'Обери характеристику для редагування.']
        for field, label in EDITABLE_FACTS.items():
            if current.get(field):
                lines.append(label+': '+str(current[field])[:110])
        registry.tell(owner,event,'\n\n'.join(lines)[:3300],
                      {'inline_keyboard':[[button(('✓ ' if current.get(field) else '＋ ')+label,'sizing_edit',mode='fact:'+field)]
                                           for field,label in EDITABLE_FACTS.items()]+[[button('← До товару','sizing_open')]]})
        return
    mode = payload.get('mode')
    is_fact = isinstance(mode, str) and mode.startswith('fact:') and mode[5:] in EDITABLE_FACTS
    if mode not in ('measurements','guide','description') and not is_fact:
        raise AppError('Вибери, що саме хочеш змінити: заміри чи розмірну сітку.')
    if action in ('sizing_preview','sizing_save'):
        with registry.store.db() as db:
            session = db.execute('SELECT action,updated FROM admin_sessions WHERE account=? AND owner=?',(registry.account,owner)).fetchone()
        active = json.loads(session['action'][7:]) if session and session['action'].startswith('sizing:') else {}
        if (not session or time.time()-session['updated']>600 or active.get('product_id') != product_id or
                active.get('mode') != mode or active.get('revision') != payload.get('revision')):
            raise AppError('Це редагування завершено або скасовано. Відкрий товар знову; збережені дані не змінено.')
    if action == 'sizing_edit':
        state = json.dumps({'product_id':product_id,'mode':mode,'revision':version},ensure_ascii=False)
        with registry.store.db() as db:
            db.execute('INSERT OR REPLACE INTO admin_sessions VALUES(?,?,?,?)',(registry.account,owner,'sizing:'+state,time.time()))
        example = ('Це заміри самого одягу. Надішли текст, наприклад:\nS: довжина 64, ширина 43\nM: довжина 65, ширина 45\nL: довжина 67, ширина 47\n\nМожна також надіслати свої заміри текстом із підписаними величинами.' if mode == 'measurements' else
                   'Це параметри людини для підбору. Надішли текст, наприклад:\nS: зріст 150–160\nM: зріст 160–170\nL: зріст 170–180\n\nЗа потреби для кожного рядка додай вагу: «M: зріст 160–170, вага 50–70». Без ваги бот орієнтується на зріст. Ці приклади не зберігаються автоматично.')
        if is_fact or mode == 'description':
            label = EDITABLE_FACTS[mode[5:]] if is_fact else 'Опис товару'
            current = product.get('facts',{}).get(mode[5:]) if is_fact else product.get('description')
            example = label+'\n\nЗараз: '+(current or 'Ще не додано.')+'\n\nНадішли новий текст до '+('400' if is_fact else '600')+' символів. Він повністю замінить це поле.'
        registry.tell(owner,event,'📏 '+record['canonical_name']+'\n\n'+example+'\n\nПокажу результат перед збереженням. Для очищення поля надішли «видалити».',session_keyboard(back=True))
        return
    if action == 'sizing_preview':
        if payload.get('revision') != version:
            raise AppError('Ці дані вже змінено. Відкрий товар і повтори редагування, щоб не стерти нові заміри.')
        text = payload.get('text','').strip()
        if is_fact or mode == 'description':
            limit = 400 if is_fact else 600
            if not text or len(text) > limit:
                raise AppError('Надішли текст від 1 до %s символів. Для очищення поля — «видалити».' % limit)
            value = None if normalize(text) in ('видалити','очистити') else text
            patch = {'facts':{mode[5:]:value}} if is_fact else {'description':value}
        elif normalize(text) in ('видалити','очистити'):
            patch = ({'measurements':None,'garment_measurements':{}} if mode=='measurements' else
                     {'sizing_basis':'body','height_guidance':{},'size_chart_verified':False,'sizes':[]})
        else:
            patch = parse_measurements(text) if mode=='measurements' else parse_guide(text)
        excluded = set(product.get('excluded_sizes',[]))
        codes = set(patch.get('garment_measurements',{})) | set(patch.get('height_guidance',{})) | {r['size'] for r in patch.get('sizes',[])}
        if codes & excluded:
            raise AppError('Для цього товару розмір %s вимкнено. Не додавай його в сітку.' % ', '.join(sorted(codes & excluded)))
        preview = copy.deepcopy(product); preview.update(patch)
        body = ((value or 'Поле буде очищено.') if is_fact or mode == 'description' else
                (measurements_reply(preview)[0] or 'Заміри буде очищено.') if mode=='measurements' else guide_text(preview))
        label = EDITABLE_FACTS[mode[5:]] if is_fact else 'опис товару' if mode=='description' else 'заміри виробу' if mode=='measurements' else 'розмірну сітку'
        registry.tell(owner,event,'Перевір '+label+' · '+record['canonical_name']+'\n\n'+body+
                      '\n\nЗберегти? Можеш надіслати виправлений текст замість цього.',
                      {'inline_keyboard':[[button('✅ Зберегти','sizing_save',mode=mode,revision=version,patch=patch)],
                                          [button('↩️ Не зберігати','sizing_open')]]})
        return
    if action == 'sizing_save':
        patch = payload.get('patch',{})
        if not registry.save_sizing(product_id,mode,patch,payload.get('revision')):
            raise AppError('Сітку вже змінено. Відкрий товар знову: старий перегляд не перезаписав нові дані.')
        with registry.store.db() as db:
            db.execute('DELETE FROM admin_sessions WHERE account=? AND owner=?',(registry.account,owner))
        worker.compose_products()
        registry.tell(owner,event,'✅ Збережено для «'+record['canonical_name']+'». Бот уже використовує ці дані.',
                      {'inline_keyboard':[[button('📦 До товару','sizing_open')],
                                          [button('🧵 Характеристики','sizing_facts')],
                                          [{'text':'← До товарів','callback_data':registry.choice(owner,{'_action':'sizing_list'})}]]})
        return
    raise AppError('Невідомий крок редагування замірів.')
