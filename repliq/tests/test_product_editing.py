"""Product edits are previewed, versioned and scoped to a single SQL identity."""
from app import AppError
import unittest
from dialogue import Dialogue
from product_sizing import process, input_message
import test_product_sizing_admin as sizing
import test_consultation_flow as consultation


class ProductEditing(unittest.TestCase):
    setUp = sizing.SizingAdminTests.setUp
    compose = sizing.SizingAdminTests.compose
    job = sizing.SizingAdminTests.job
    outbox = sizing.SizingAdminTests.outbox
    save = sizing.SizingAdminTests.save
    def test_editing_one_fact_keeps_other_facts_and_sizing(self):
        self.save('measurements','M: ширина 50, довжина 70')
        self.save('fact:material','70% бавовни, 30% поліестеру.')
        self.save('fact:care','Прати за рекомендацією на бірці.')
        self.registry.snapshot(self.records); self.compose()
        product = self.worker.catalog.products[self.worker.catalog.adverts['102']]
        self.assertEqual(product['garment_measurements']['M']['width_cm'],50)
        self.assertEqual(product['facts']['material'],'70% бавовни, 30% поліестеру.')
        self.assertIn('бірці',product['facts']['care'])
        self.assertTrue(product['facts'].get('fit'))

    def test_updated_fact_is_used_in_an_already_open_conversation(self):
        stock=consultation.Stock()
        bot=Dialogue(self.worker.catalog,stock)
        first=bot.plan(['Який матеріал?'],{},'102')
        state=dict(first.context,**first.on_send)
        self.save('fact:material','70% бавовни, 30% поліестеру.')
        bot.catalog=self.worker.catalog
        second=bot.plan(['Який матеріал?'],state,'102')
        self.assertIn('70%',second.text); self.assertNotIn('100%',second.text)

    def test_clear_fact_does_not_restore_outdated_base_value(self):
        self.save('fact:material','видалити')
        product=self.worker.catalog.products[self.worker.catalog.adverts['102']]
        self.assertNotIn('material',product['facts'])

    def test_old_preview_does_not_overwrite_another_edit(self):
        self.job('sizing_edit',product_id='shirt1',mode='fact:material')
        self.job('sizing_preview',product_id='shirt1',mode='fact:material',revision=0,text='100% льон.')
        self.assertTrue(self.registry.save_sizing('shirt1','description',{'description':'Новий опис.'},0))
        with self.assertRaises(AppError):
            self.job('sizing_save',product_id='shirt1',mode='fact:material',revision=0,patch={'facts':{'material':'100% льон.'}})
        self.assertNotIn('facts',self.registry.sizing_profile('shirt1')[1])

    def test_back_button_is_a_navigation_action_and_not_a_new_fact(self):
        self.job('sizing_edit',product_id='shirt1',mode='fact:material')
        self.assertTrue(input_message(self.registry,7,'⬅️ До товару','back'))
        job=self.registry.next_job(); self.assertEqual(job['action'],'sizing_open')
        process(self.worker,job)
        self.assertEqual(self.registry.sizing_profile('shirt1'),(0,{}))

    def test_arbitrary_profile_fields_cannot_change_inventory_or_identity(self):
        self.assertFalse(self.registry.save_sizing('shirt1','fact:price_uah',{'facts':{'price_uah':'1'}},0))
        self.assertFalse(self.registry.save_sizing('shirt1','fact:material',{'facts':{'care':'wrong field'}},0))
        self.assertFalse(self.registry.save_sizing('shirt1','description',{'description':'x'*601},0))
