from __future__ import annotations

import unittest

import mongoengine

from dropzone_ticketing.model import mongoengine_alias
from dropzone_ticketing.model.ticket import Ticket


class StorageTicketDocumentTest(unittest.TestCase):
    def test_document_defines_the_expected_fields(self) -> None:
        self.assertEqual(
            sorted(name for name in Ticket._fields if name != "id"),
            ["code", "owner", "redeemed"],
        )

    def test_primary_key_is_the_generated_object_id(self) -> None:
        self.assertIsInstance(Ticket._fields["id"], mongoengine.ObjectIdField)
        self.assertEqual(Ticket._meta["id_field"], "id")

    def test_field_types_and_constraints(self) -> None:
        fields = Ticket._fields

        self.assertIsInstance(fields["code"], mongoengine.StringField)
        self.assertTrue(fields["code"].required)
        self.assertTrue(fields["code"].unique)

        self.assertIsInstance(fields["owner"], mongoengine.StringField)
        self.assertTrue(fields["owner"].required)

        self.assertIsInstance(fields["redeemed"], mongoengine.DateTimeField)
        self.assertFalse(fields["redeemed"].required)

    def test_collection_metadata(self) -> None:
        self.assertEqual(Ticket._meta["collection"], "tickets")
        self.assertIn("code", Ticket._meta["indexes"])
        self.assertIs(Ticket._meta["db_alias"], mongoengine_alias)


if __name__ == "__main__":
    unittest.main()
