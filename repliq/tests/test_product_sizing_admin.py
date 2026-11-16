import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app import AppError
from catalog import Catalog
from chat_store import Store
from dialogue import Dialogue
from product_registry import Registry, runtime_catalog
from product_sizing import parse_measurements, parse_guide, process, input_message
from shop_facts import measurements_reply
import test_consultation_flow as consultation

ROOT=Path(__file__).resolve().parents[1]


class SizingAdminTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Store(Path(self.temp.name)/'state.sqlite3');self.addCleanup(self.store.close_current)
        self.registry=Registry(self.store,'account')
        self.base=Catalog.load(ROOT/'shop.json')
        self.records=[{'id':'shirt1','canonical_name':'Футболки Gallery Dept','aliases':[]},
                      {'id':'shirt2','canonical_name':'Футболка Other Brand','aliases':[]}]
        self.bindings=[{'advert':'102','product_id':'shirt1','active':1,'title':'Gallery','price':650}]
        self.registry.snapshot(self.records)
        self.worker=SimpleNamespace(registry=self.registry)
        self.worker.compose_products=self.compose
        self.compose()

    def compose(self):
        stable=self.store.meta('keys')
        self.worker.catalog=runtime_catalog(self.base,self.registry.products(),self.bindings,stable,self.registry.sizing_profiles())
        self.store.set_meta('keys',{p['ledgerly_product_id']:p['key'] for p in self.worker.catalog.products.values()})

    def job(self,action,**payload):
        self.count=getattr(self,'count',0)+1
        process(self.worker,{'owner':7,'event_key':'job'+str(self.count),'action':action,'payload':payload})

    def outbox(self):
        with self.store.db() as db:
            return [dict(r,keyboard=json.loads(r['keyboard'])) for r in db.execute('SELECT * FROM admin_outbox ORDER BY id')]

    def save(self,mode,text,product='shirt1'):
        self.job('sizing_edit',product_id=product,mode=mode)
        version=self.registry.sizing_profile(product)[0]
        self.job('sizing_preview',product_id=product,mode=mode,revision=version,text=text)
        button=self.outbox()[-1]['keyboard']['inline_keyboard'][0][0]
        self.assertTrue(self.registry.select(7,button['callback_data'].split(':',1)[1],'confirm'+str(self.count)))
        job=self.registry.next_job();process(self.worker,job);self.registry.finish(job['event_key'])

    def test_existing_owner_measurements_are_parsed_as_garment_dimensions(self):
        value=parse_measurements('Заміри S - ширина 43, довжина 64\nЗаміри М - ширина 45, довжина 65\nЗаміри L - ширина 47, довжина 67')
        self.assertEqual(value['garment_measurements']['M'],{'width_cm':45,'length_cm':65})
        self.assertNotIn('height_guidance',value)

    def test_pants_waist_and_leg_are_not_misclassified_as_shirt_width(self):
        value=parse_measurements('S: довжина — 104 см, ширина в поясі — 68 см, ширина штанини — 25 см.')
        self.assertEqual(value['garment_measurements']['S'],{'length_cm':104,'waist_cm':68,'leg_width_cm':25})

    def test_raw_labeled_measurements_can_be_kept_without_guessing_fields(self):
        value=parse_measurements('S: плечі 40 см, рукав 60 см\nM: плечі 42 см, рукав 62 см')
        self.assertEqual(value['garment_measurements'],{})
        self.assertIn('рукав 60',value['measurements'])

    def test_garment_and_body_tables_cannot_be_swapped_or_silently_truncated(self):
        for text in ('S: зріст 150-160, вага 40-50','S: 43/64','S: ширина 43\nS: ширина 45','S: довжина 0'):
            with self.subTest(text=text),self.assertRaises(AppError):parse_measurements(text)
        for text in ('S: довжина 64, ширина 43','S: 150-160\nM: 160-170 99','S: вага 50-60'):
            with self.subTest(text=text),self.assertRaises(AppError):parse_guide(text)

    def test_height_and_weight_charts_are_distinct_from_measurements(self):
        guide=parse_guide('S: зріст 1.50–1.60\nM: зріст 160-170\nL: зріст 170–180')
        self.assertEqual(guide['height_guidance']['S'],[150,160])
        self.assertEqual(guide['sizing_basis'],'height')
        body=parse_guide('S: зріст 150-160, вага 40-55\nM: зріст 160-170, вага 50-70')
        self.assertTrue(body['size_chart_verified']);self.assertEqual(body['sizing_basis'],'body')

    def test_common_table_punctuation_and_open_height_range(self):
        measured=parse_measurements('• S: довжина 104; пояс 68; ширина штанини 25\n• M: довжина 106; пояс 72')
        self.assertEqual(measured['garment_measurements']['S']['waist_cm'],68)
        guide=parse_guide('S: 174–180\nM: 180–185\nL: 185+')
        self.assertEqual(guide['height_guidance']['L'],[185,230])
        self.assertEqual(parse_guide('S: 150-160; M: 160-170')['height_guidance']['M'],[160,170])

    def test_preview_does_not_change_customer_facts_until_confirmation(self):
        self.job('sizing_edit',product_id='shirt1',mode='measurements')
        self.job('sizing_preview',product_id='shirt1',mode='measurements',revision=0,text='M: ширина 50, довжина 70')
        self.assertEqual(self.registry.sizing_profile('shirt1'),(0,{}))
        keyboard=self.outbox()[-1]['keyboard']['inline_keyboard']
        self.assertEqual(keyboard[0][0]['text'],'✅ Зберегти')

    def test_each_field_is_saved_independently_and_survives_sql_refresh_and_restart(self):
        self.save('measurements','M: довжина 70, ширина 50')
        self.save('guide','S: 150-160\nM: 160-170\nL: 170-180')
        self.registry.snapshot(self.records);self.compose()
        p=self.worker.catalog.products[self.worker.catalog.adverts['102']]
        self.assertEqual(p['garment_measurements']['M']['width_cm'],50)
        self.assertEqual(p['height_guidance']['M'],[160,170])
        reopened=Registry(self.store,'account')
        self.assertEqual(reopened.sizing_profile('shirt1')[0],2)
        self.assertNotIn('shirt1',Registry(self.store,'another-account').sizing_profiles())
        self.assertNotIn('shirt2',self.registry.sizing_profiles())

    def test_saved_facts_answer_the_matching_customer_question(self):
        self.save('measurements','S: довжина 64, ширина 43\nM: довжина 65, ширина 45\nL: довжина 67, ширина 47')
        self.save('guide','S: 150-160\nM: 160-170\nL: 170-180')
        bot=Dialogue(self.worker.catalog,consultation.Stock())
        measured=bot.plan(['Які заміри M?'],{},'102')
        self.assertIn('довжина — 65',measured.text);self.assertIn('ширина — 45',measured.text)
        advice=bot.plan(['Який розмір порадите на зріст 180 і вагу 80?'],{},'102')
        self.assertIn('Вам підійде L',advice.text)
        self.assertNotIn('ширина — 45',advice.text)
        self.assertFalse(advice.on_send.get('handoff_now'))

    def test_stale_confirmation_and_cancel_cannot_overwrite_newer_data(self):
        self.job('sizing_edit',product_id='shirt1',mode='guide')
        patch=parse_guide('M: 160-170')
        self.assertTrue(self.registry.save_sizing('shirt1','guide',patch,0))
        with self.assertRaises(AppError):self.job('sizing_save',product_id='shirt1',mode='guide',revision=0,patch=patch)
        self.job('sizing_edit',product_id='shirt1',mode='guide')
        self.registry.private_message(7,'↩️ Скасувати','cancel')
        with self.assertRaises(AppError):self.job('sizing_save',product_id='shirt1',mode='guide',revision=1,patch=patch)
        self.assertEqual(self.registry.sizing_profile('shirt1')[0],1)

    def test_input_step_always_has_a_real_cancel_button(self):
        for label in ('➕ Додати оголошення','✏️ Змінити оголошення','⛔ Вимкнути оголошення'):
            self.registry.private_message(7,label,'prompt'+label)
            self.assertIn(['↩️ Скасувати'],self.outbox()[-1]['keyboard']['keyboard'])
        self.job('sizing_edit',product_id='shirt1',mode='guide')
        self.assertIn(['↩️ Скасувати'],self.outbox()[-1]['keyboard']['keyboard'])
        self.registry.private_message(7,'↩️ Скасувати','cancel')
        self.assertNotIn(['↩️ Скасувати'],self.outbox()[-1]['keyboard']['keyboard'])

    def test_profile_keeps_stable_product_identity_after_plural_to_singular_rename(self):
        before=self.worker.catalog.adverts['102']
        self.records[0]['canonical_name']='Футболка Gallery Dept'
        self.registry.snapshot(self.records);self.compose()
        self.assertEqual(self.worker.catalog.adverts['102'],before)

    def test_clear_one_field_does_not_clear_the_other(self):
        self.save('guide','M: 160-170')
        self.save('measurements','M: ширина 50, довжина 70')
        self.save('measurements','видалити')
        p=self.worker.catalog.products[self.worker.catalog.adverts['102']]
        self.assertEqual(p['height_guidance'],{'M':[160,170]})
        self.assertFalse(measurements_reply(p)[0])

    def test_cannot_add_excluded_xl_or_steal_another_owners_choice(self):
        self.job('sizing_edit',product_id='shirt1',mode='guide')
        with self.assertRaises(AppError):self.job('sizing_preview',product_id='shirt1',mode='guide',revision=0,text='XL: 180-190')
        token=self.registry.choice(7,{'_action':'sizing_open','product_id':'shirt1'}).split(':',1)[1]
        self.assertFalse(self.registry.select(8,token,'wrong-owner'))

