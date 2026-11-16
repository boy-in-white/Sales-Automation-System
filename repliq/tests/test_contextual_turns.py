"""Full multi-turn regressions inspired by anonymized buyer questions."""
import copy
import json
import unittest

from conversation_turn import history_excerpt, inferred_topic, safe_excerpt, MAX_TURNS, prepare
from customer_language import parameters
import test_consultation_flow as consultation


class ContextualTurns(unittest.TestCase):
    def setUp(self):
        self.s = consultation.ConsultationTests()
        self.s.setUp()

    def test_material_detour_keeps_pending_weight_and_previous_height(self):
        self.s.turn('Який розмір порадите?')
        self.s.turn('зріст 180')
        p = self.s.turn('Яка тканинка?')
        self.assertIn('60%', p.text)
        self.assertEqual(self.s.context['awaiting'], 'weight')
        p = self.s.turn('78')
        self.assertIn('Вам підійде M', p.text)
        self.assertEqual(self.s.context['height'], 180)
        self.assertEqual(self.s.context['weight'], 78)

    def test_price_and_material_detours_preserve_color_choice(self):
        self.s.turn('Мені M')
        before = self.s.context['size']
        for question in ('Який матеріал?', 'Яка ціна?'):
            self.s.turn(question)
            self.assertEqual(self.s.context['awaiting'], 'color')
            self.assertEqual(self.s.context['size'], before)
        p = self.s.turn('блек')
        self.assertEqual(self.s.context['color'], 'BLACK')
        self.assertNotIn('Який розмір', p.text)

    def test_measurement_ellipsis_is_not_a_new_size_selection(self):
        self.s.turn('Мені S')
        self.s.turn('Які заміри M?')
        p = self.s.turn('А для L?')
        self.assertIn('108', p.text)
        self.assertEqual(self.s.context['size'], 'S')

    def test_measurement_ellipsis_after_manual_history(self):
        state = {'product':'suvene-pants','size':'S','greeted':True,
                 'recent_history':[{'speaker':'customer','text':'Які заміри M?'},
                                   {'speaker':'seller','text':'M: довжина 106 см, пояс 72 см.'}]}
        p = self.s.bot.plan(['А для L?'], state, '101')
        self.assertIn('108', p.text)
        self.assertEqual(p.context['size'], 'S')

    def test_ellipsis_without_previous_measurement_does_not_invent_one(self):
        self.assertEqual(prepare(['А для L?'], {}), ['А для L?'])

    def test_bare_size_after_measurements_can_still_select_a_size(self):
        self.s.turn('Які заміри M?')
        self.s.turn('L')
        self.assertEqual(self.s.context['size'],'L')

    def test_price_question_preserves_the_already_issued_invoice_price(self):
        self.s.order(); self.s.turn('Післяплата')
        self.s.catalog.products['suvene-pants']['price_uah']=999
        p=self.s.turn('Яка ціна?')
        self.assertIn('800',p.text); self.assertNotIn('999',p.text)

    def test_measurement_ellipsis_during_checkout_does_not_edit_invoice_items(self):
        self.s.order(); self.s.turn('Післяплата')
        items=copy.deepcopy(self.s.context['invoice_items'])
        self.s.turn('Які заміри M?')
        p=self.s.turn('А для L?')
        self.assertIn('108',p.text)
        self.assertEqual(self.s.context['invoice_items'],items)

    def test_delivery_method_after_consultation_places_the_correct_order(self):
        self.s.turn('Який розмір мені підійде?')
        self.s.turn('180 78')
        self.s.turn('чорний')
        p = self.s.turn('Післяплатою')
        self.assertEqual(self.s.context['checkout_method'], 'cod')
        self.assertTrue(self.s.context['invoice_issued'])
        self.assertIn('квитанці', p.text)
        self.assertEqual(self.s.context['size'], 'M')
        self.assertFalse(self.s.context.get('payment_verified'))

    def test_payment_question_is_not_an_invoice_or_confirmation(self):
        self.s.turn('Мені M чорні')
        p = self.s.turn('Можна у відділенні розрахуватись?')
        self.assertIn('післяплатою', p.text)
        self.assertIn('100', p.text)
        self.assertFalse(self.s.context.get('invoice_issued'))
        self.assertFalse(self.s.context.get('payment_verified'))
        self.s.turn('так')
        self.assertTrue(self.s.context['invoice_issued'])

    def test_facts_during_address_collection_do_not_turn_into_address(self):
        self.s.order(); self.s.turn('Післяплата')
        before = copy.deepcopy(self.s.context)
        p = self.s.turn('А на зиму підходять?')
        self.assertIn('на зиму', p.text)
        for key in ('invoice_id','checkout_amount','invoice_items','shipping_awaiting'):
            self.assertEqual(self.s.context.get(key), before.get(key))
        self.assertFalse(self.s.context.get('shipping_details',{}).get('name'))
        self.s.turn('Коваленко Олег Петрович\n0671234567\nЛьвів\n12')
        self.assertIn('Львів', self.s.context['shipping_details']['place'])

    def test_side_question_cannot_confirm_pending_shipping_review(self):
        self.s.order(); self.s.turn('Післяплата')
        self.s.turn('Коваленко Олег Петрович\n0671234567\nЛьвів\n12')
        self.s.turn('А який склад тканини?')
        self.assertFalse(self.s.context.get('customer_confirmation'))
        self.s.turn('так')
        self.assertTrue(self.s.context['customer_confirmation'])

    def test_two_topics_answer_both_without_restarting_order(self):
        p = self.s.turn('Який матеріал і чи відправляєте олх доставкою?')
        self.assertIn('60%', p.text)
        self.assertIn('OLX', p.text)
        self.assertFalse(self.s.context.get('invoice_issued'))

    def test_no_inventory_guesses_in_combined_availability_delivery_question(self):
        self.s.stock.values.update({('suvene-pants','M',c):0 for c in ('BLACK','GRAY','WHITE')})
        p = self.s.turn('Чи є сірі M і чи можна олх доставкою?')
        self.assertIn('немає', p.text)
        self.assertIn('OLX', p.text)
        self.assertNotIn('Так, обраний варіант є в наявності', p.text)

    def test_separate_clients_do_not_share_recent_memory(self):
        self.s.turn('Які заміри M?')
        other = self.s.bot.plan(['L'], {}, '102')
        self.assertNotIn('108', other.text)
        self.assertEqual(other.context['product'], 'gallery-tee')

    def test_new_memory_is_applied_only_after_actual_send(self):
        state = {'product':'suvene-pants','greeted':True}
        p = self.s.bot.plan(['Який матеріал?'], state, '101')
        self.assertNotIn('conversation_memory', p.context)
        self.assertNotIn('conversation_memory', state)
        self.assertIn('conversation_memory', p.on_send)

    def test_memory_is_bounded_and_redacts_contact_information(self):
        for _ in range(12):
            self.s.turn('Які заміри M?')
        memory = self.s.context['conversation_memory']
        self.assertEqual(len(memory['turns']), MAX_TURNS)
        self.assertLess(len(json.dumps(memory).encode()), 12000)
        self.assertNotIn('0671234567', safe_excerpt('0671234567 https://example.org/private'))
        self.assertNotIn('https://', safe_excerpt('0671234567 https://example.org/private'))

    def test_history_is_reused_without_unanswered_input_or_other_roles(self):
        rows = [{'_id':'old','type':'sent','text':'Які заміри?'},
                {'_id':'new','type':'received','text':'M'}, {'type':'system','text':'notice'}]
        self.assertEqual(history_excerpt(rows, ['new']), [{'speaker':'seller','text':'Які заміри?'}])

    def test_meter_space_and_inflected_weight_are_understood(self):
        for raw, h, w in [('зріст 1 61 вага 46',161,46), ('зростом 1,85 вагою 78',185,78), ('1 80 78',180,78)]:
            values, errors = parameters(raw, {}, weight_for_fit=False)
            self.assertEqual(values, {'height':h,'weight':w}, raw)
            self.assertFalse(errors)

    def test_unknown_and_payment_phrases_never_trigger_retrieved_actions(self):
        for raw in ('оплатив 100 грн', 'скасуй замовлення', 'так', '0671234567', 'https://example.org'):
            self.assertIsNone(inferred_topic(raw))
        self.assertEqual(inferred_topic('Пояс регулюється?'), 'waist')
        p = self.s.turn('Не закатуються?')
        self.assertNotIn('власник', p.text.lower())

    def test_explicit_owner_request_survives_a_material_question(self):
        p = self.s.turn('Який матеріал, поклич власника')
        self.assertTrue(p.on_send.get('handoff_now'))

    def test_fabric_question_about_another_brand_cannot_borrow_current_facts(self):
        p = self.s.turn('Який матеріал у Broken Planet?')
        self.assertNotIn('60%',p.text)
        self.assertFalse(p.on_send.get('handoff_now'))

    def test_two_people_parameters_are_not_combined(self):
        values, errors = parameters('я зріст 180 вага 78, а брат зріст 170 вага 60',{})
        self.assertFalse(values); self.assertTrue(errors)

    def test_colloquial_olx_preference_is_answered_without_generic_menu(self):
        p=self.s.turn('якщо можна то було би добре по олх якщо вам не накладно бо я знаю там бувають що не хочуть відпрявляти по олх')
        self.assertIn('Так, відправляємо OLX',p.text)
        self.assertFalse(p.on_send.get('handoff_now'))

    def test_arrival_deadline_does_not_become_a_shipping_guarantee(self):
        p=self.s.turn('Мені просто потрібно щоб завтра вони приїхали')
        self.assertIn('не можу гарантувати',p.text)
        self.assertFalse(p.on_send.get('handoff_now'))

    def test_restock_ellipsis_uses_previous_availability_question(self):
        self.s.stock.values.update({('suvene-pants','M',c):0 for c in ('BLACK','GRAY','WHITE')})
        self.s.turn('Є сірі M?')
        p=self.s.turn('А буде?')
        self.assertIn('дати надходження',p.text)
        self.assertNotIn('Що бажаєте уточнити',p.text)

    def test_measurement_method_is_not_guessed(self):
        p=self.s.turn('Ви в самому верху міряли?')
        self.assertIn('не уточнено',p.text)
        self.assertFalse(p.on_send.get('handoff_now'))

    def test_questions_can_change_subject_without_changing_the_basket(self):
        self.s.turn('Хочу чорні штани M')
        items=copy.deepcopy(self.s.context)
        first=self.s.turn('Який матеріал футболки Gallery Dept?')
        self.assertIn('100%',first.text)
        second=self.s.turn('А просвічується?')
        self.assertIn('не просвіч',second.text)
        self.assertEqual(self.s.context['product'],items['product'])
        self.assertEqual(self.s.context['size'],'M')
        self.assertEqual(self.s.context['color'],'BLACK')

    def test_ambiguous_selection_after_another_product_question_is_clarified_once(self):
        self.s.turn('Мені чорні штани M')
        self.s.turn('Який матеріал футболки Gallery Dept?')
        clarification=self.s.turn('Тоді L білу')
        self.assertIn('Уточню, щоб правильно записати',clarification.text)
        self.assertEqual(self.s.context['product'],'suvene-pants')
        self.s.turn('Футболку')
        self.assertEqual(self.s.context['product'],'gallery-tee')
        self.assertEqual(self.s.context['size'],'L')
        self.assertEqual(self.s.context['color'],'WHITE')
        self.assertNotIn('pending_subject_choice',self.s.context)

    def test_resolving_the_ambiguity_to_original_product_does_not_repeat_old_choice(self):
        self.s.turn('Мені чорні штани M')
        self.s.turn('Який матеріал футболки Gallery Dept?')
        self.s.turn('Тоді L білу')
        self.s.turn('Штани')
        self.assertEqual(self.s.context['product'],'suvene-pants')
        self.assertEqual(self.s.context['size'],'L')
        self.assertNotIn('pending_subject_choice',self.s.context)

    def test_multi_product_fact_subject_stays_with_the_item_just_discussed(self):
        self.s.turn('Хочу чорні штани M і білу футболку L')
        before=copy.deepcopy(self.s.context['cart'])
        self.s.turn('Який матеріал футболки?')
        p=self.s.turn('А просвічується?')
        self.assertIn('не просвіч',p.text)
        self.assertEqual(self.s.context['cart'],before)


if __name__ == '__main__':
    unittest.main()
