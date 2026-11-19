import copy
import unittest
import test_repliq_inbox as fixtures
import repliq_inbox as inbox
from parcel_manifest import prepare,rows
import business_db as business

class SizeMappingInboxTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.InboxTests();self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def test_requested_l_physical_m_uses_m_variant_in_manifest(self):
        data=copy.deepcopy(self.f.data)
        data['items'][0].update(requested_size='L',size_substituted=True)
        inbox.validate({"data":data,"ttn":fixtures.TTN})
        self.assertIn('L (відправити M)',inbox.description(data))
        prepare(fixtures.TTN,data['items'])
        with business._LOCK,business._transaction() as session:
            found=rows(session,{'ttn':fixtures.TTN})
        self.assertEqual(found[0]['variant_id'],self.f.pv['id'])
        self.assertEqual(found[0]['size_code'],'M')

    def test_reversed_and_skipped_sizes_cannot_enter_ledgerly(self):
        for requested in ['S','XL']:
            with self.subTest(requested=requested):
                data=copy.deepcopy(self.f.data)
                data['items'][0].update(requested_size=requested,size_substituted=True)
                with self.assertRaises(ValueError):inbox.validate({"data":data,"ttn":fixtures.TTN})
