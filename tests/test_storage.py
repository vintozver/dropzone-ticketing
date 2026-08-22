from __future__ import annotations

import unittest

import mongoengine

from dropzone_ticketing.storage import Ticket


class StorageTicketDocumentTest(unittest.TestCase):
    def test_document_defines_the_expected_fields(self) -> None:
        self.assertEqual(
            sorted(name for name in Ticket._fields if name != "id"),
            ["code", "date_issued", "identifier", "owner", "redeemed", "redeemed_at"],
        )

    def test_field_types_and_constraints(self) -> None:
        fields = Ticket._fields

        self.assertIsInstance(fields["identifier"], mongoengine.StringField)
        self.assertTrue(fields["identifier"].required)
        self.assertTrue(fields["identifier"].unique)

        self.assertIsInstance(fields["code"], mongoengine.StringField)
        self.assertTrue(fields["code"].required)

        self.assertIsInstance(fields["date_issued"], mongoengine.DateTimeField)
        self.assertTrue(fields["date_issued"].required)

        self.assertIsInstance(fields["owner"], mongoengine.StringField)
        self.assertTrue(fields["owner"].required)

        self.assertIsInstance(fields["redeemed"], mongoengine.BooleanField)
        self.assertFalse(fields["redeemed"].default)

        self.assertIsInstance(fields["redeemed_at"], mongoengine.DateTimeField)
        self.assertTrue(fields["redeemed_at"].null)

    def test_collection_metadata(self) -> None:
        self.assertEqual(Ticket._meta["collection"], "tickets")
        self.assertIn("identifier", Ticket._meta["indexes"])


if __name__ == "__main__":
    unittest.main()
