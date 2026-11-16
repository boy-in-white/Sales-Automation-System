import unittest

from tracking_service import classify_tracking_doc, snapshot_hash


class TrackingTests(unittest.TestCase):
    def test_received_is_terminal(self):
        status, terminal, code = classify_tracking_doc(
            {"Status": "Відправлення отримано", "StatusCode": "11"}
        )
        self.assertEqual(status, "Отримано")
        self.assertTrue(terminal)
        self.assertEqual(code, "11")

    def test_refusal_is_terminal(self):
        status, terminal, _ = classify_tracking_doc(
            {"StatusDescription": "Відмова одержувача"}
        )
        self.assertEqual(status, "Відмова")
        self.assertTrue(terminal)

    def test_hash_ignores_irrelevant_api_fields(self):
        base = {
            "status_label": "Відправлено",
            "status_code": "5",
            "place": "Київ",
            "doc": {"ActualDeliveryDate": "", "TransientField": "one"},
        }
        changed_noise = {
            **base,
            "doc": {"ActualDeliveryDate": "", "TransientField": "two"},
        }
        self.assertEqual(snapshot_hash(base), snapshot_hash(changed_noise))


if __name__ == "__main__":
    unittest.main()

