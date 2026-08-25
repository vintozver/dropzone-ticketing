from __future__ import annotations

from datetime import datetime, timezone
import unittest

import mongoengine

from dropzone_ticketing.model import mongoengine_alias
from dropzone_ticketing.model.auth import Fido2Credential, GoogleCredential, User
from dropzone_ticketing.model.ticket import Redemption, Ticket, UserRef


class StorageTicketDocumentTest(unittest.TestCase):
    def test_document_defines_the_expected_fields(self) -> None:
        self.assertEqual(
            sorted(name for name in Ticket._fields if name != "id"),
            ["code", "issued_by", "issued_to", "payment", "purpose", "redeemed"],
        )

    def test_primary_key_is_the_generated_object_id(self) -> None:
        self.assertIsInstance(Ticket._fields["id"], mongoengine.ObjectIdField)
        self.assertEqual(Ticket._meta["id_field"], "id")

    def test_field_types_and_constraints(self) -> None:
        fields = Ticket._fields

        self.assertIsInstance(fields["code"], mongoengine.StringField)
        self.assertTrue(fields["code"].required)
        self.assertTrue(fields["code"].unique)

        self.assertIsInstance(fields["issued_to"], mongoengine.EmbeddedDocumentField)
        self.assertTrue(fields["issued_to"].required)
        self.assertIs(fields["issued_to"].document_type_obj, UserRef)

        self.assertIsInstance(fields["payment"], mongoengine.StringField)
        self.assertTrue(fields["payment"].required)

        self.assertIsInstance(fields["purpose"], mongoengine.StringField)
        self.assertTrue(fields["purpose"].required)

        self.assertIsInstance(fields["issued_by"], mongoengine.EmbeddedDocumentField)
        self.assertTrue(fields["issued_by"].required)
        self.assertIs(fields["issued_by"].document_type_obj, UserRef)

        self.assertIsInstance(fields["redeemed"], mongoengine.EmbeddedDocumentField)
        self.assertFalse(fields["redeemed"].required)
        self.assertIs(fields["redeemed"].document_type_obj, Redemption)

        redeemed_fields = Redemption._fields
        self.assertIsInstance(redeemed_fields["dt"], mongoengine.DateTimeField)
        self.assertTrue(redeemed_fields["dt"].required)
        self.assertIsInstance(redeemed_fields["by_user"], mongoengine.StringField)
        self.assertFalse(redeemed_fields["by_user"].required)
        self.assertIsInstance(redeemed_fields["reason"], mongoengine.StringField)
        self.assertFalse(redeemed_fields["reason"].required)

        user_ref_fields = UserRef._fields
        self.assertIsInstance(user_ref_fields["id"], mongoengine.ObjectIdField)
        self.assertFalse(user_ref_fields["id"].required)
        self.assertIsInstance(user_ref_fields["display_name"], mongoengine.StringField)
        self.assertFalse(user_ref_fields["display_name"].required)

    def test_redemption_requires_datetime_and_omits_absent_reason(self) -> None:
        with self.assertRaises(mongoengine.ValidationError):
            Redemption().validate()

        redemption = Redemption(dt=datetime(2026, 8, 22, tzinfo=timezone.utc))
        self.assertNotIn("by_user", redemption.to_mongo())
        self.assertNotIn("reason", redemption.to_mongo())

    def test_collection_metadata(self) -> None:
        self.assertEqual(Ticket._meta["collection"], "tickets")
        self.assertIn("code", Ticket._meta["indexes"])
        self.assertIn("issued_to.id", Ticket._meta["indexes"])
        self.assertIn("issued_to.display_name", Ticket._meta["indexes"])
        self.assertIs(Ticket._meta["db_alias"], mongoengine_alias)

    def test_fido2_credential_embedded_document_fields(self) -> None:
        fields = Fido2Credential._fields
        self.assertIsInstance(fields["id"], mongoengine.BinaryField)
        self.assertTrue(fields["id"].primary_key)
        self.assertEqual(fields["id"].db_field, "_id")
        self.assertIsInstance(fields["data"], mongoengine.BinaryField)
        self.assertTrue(fields["data"].required)
        self.assertIsInstance(fields["dt"], mongoengine.DateTimeField)
        self.assertTrue(fields["dt"].required)

    def test_user_indexes_credentials_by_embedded_identifier(self) -> None:
        self.assertEqual(
            User._meta["indexes"],
            [
                {"fields": ["display_name"]},
                {"fields": ["$display_name"]},
                {"fields": ["fido2_credentials._id"], "unique": True},
                {"fields": ["google_credentials.email"], "unique": True, "sparse": True},
            ],
        )

    def test_user_login_and_display_name_fields(self) -> None:
        fields = User._fields
        self.assertIsInstance(fields["id"], mongoengine.ObjectIdField)
        self.assertTrue(fields["id"].primary_key)
        self.assertIsInstance(fields["login"], mongoengine.StringField)
        self.assertTrue(fields["login"].required)
        self.assertTrue(fields["login"].unique)
        self.assertIsInstance(fields["display_name"], mongoengine.StringField)
        self.assertFalse(fields["display_name"].required)

    def test_google_credential_embedded_document_fields(self) -> None:
        fields = GoogleCredential._fields
        self.assertIsInstance(fields["email"], mongoengine.StringField)
        self.assertTrue(fields["email"].required)


if __name__ == "__main__":
    unittest.main()
