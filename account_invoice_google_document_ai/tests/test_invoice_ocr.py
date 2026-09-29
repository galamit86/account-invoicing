# Copyright 2026 Roetz
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import base64
import json
from unittest.mock import MagicMock, patch

from google.cloud import documentai_v1 as documentai

from odoo.exceptions import ValidationError
from odoo.tests import TransactionCase, tagged

from odoo.addons.queue_job.tests.common import trap_jobs


@tagged("post_install", "-at_install")
class TestInvoiceGoogleDocumentAI(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.purchase_journal = cls.env["account.journal"].search(
            [
                ("company_id", "=", cls.company.id),
                ("type", "=", "purchase"),
            ],
            limit=1,
        )
        if not cls.purchase_journal:
            cls.purchase_journal = cls.env["account.journal"].create(
                {
                    "name": "Vendor Bills",
                    "code": "BILL",
                    "type": "purchase",
                    "company_id": cls.company.id,
                }
            )
        cls.company.invoice_ocr_google_mode = "automatic"
        cls.company.invoice_ocr_google_project = "test-project"
        cls.company.invoice_ocr_google_location = "eu"
        cls.company.invoice_ocr_google_processor = "test-processor"
        cls.company.vat = "NL004495445B01"
        cls.vendor = cls.env["res.partner"].create(
            {
                "name": "OCR Vendor",
                "vat": "NL123456782B01",
                "supplier_rank": 1,
            }
        )

    def _create_move_and_attachment(self):
        move = self.env["account.move"].create(
            {
                "move_type": "in_invoice",
                "company_id": self.company.id,
                "journal_id": self.purchase_journal.id,
            }
        )
        attachment = self.env["ir.attachment"].create(
            {
                "name": "invoice.pdf",
                "mimetype": "application/pdf",
                "datas": base64.b64encode(b"%PDF-1.4 test invoice"),
                "res_model": move._name,
                "res_id": move.id,
            }
        )
        move.message_main_attachment_id = attachment
        return move, attachment

    def _entity(self, entity_type, value, confidence=0.99, properties=None):
        return {
            "type": entity_type,
            "text": str(value),
            "normalized": str(value),
            "confidence": confidence,
            "properties": properties or [],
        }

    def _result(self):
        return {
            "text": "invoice",
            "entities": [
                self._entity("supplier_tax_id", self.vendor.vat),
                self._entity("supplier_name", self.vendor.name),
                self._entity("receiver_tax_id", self.company.vat),
                self._entity("invoice_id", "OCR-001"),
                self._entity("invoice_date", "2026-09-01"),
                self._entity("due_date", "2026-10-01"),
                self._entity("currency", self.company.currency_id.name),
                self._entity("net_amount", 100),
                self._entity("total_tax_amount", 0),
                self._entity("total_amount", 100),
                self._entity(
                    "line_item",
                    "Consulting",
                    properties=[
                        self._entity("line_item/description", "Consulting"),
                        self._entity("line_item/quantity", 1),
                        self._entity("line_item/unit_price", 100),
                        self._entity("line_item/amount", 100),
                    ],
                ),
            ],
        }

    def test_pdf_decoder_queues_one_job(self):
        move, attachment = self._create_move_and_attachment()
        with trap_jobs() as trap:
            decoder = move._get_edi_decoder(
                {"type": "pdf", "attachment": attachment},
                new=True,
            )
            self.assertTrue(decoder(move, {"attachment": attachment}, True))
            trap.assert_enqueued_job(
                move._extract_invoice_with_google,
                args=(attachment.id,),
            )
        self.assertEqual(move.invoice_ocr_state, "pending")

    def test_apply_result_matches_vendor_and_creates_line(self):
        move, attachment = self._create_move_and_attachment()
        move.write(
            {
                "invoice_ocr_state": "extracted",
                "invoice_ocr_attachment_id": attachment.id,
                "invoice_ocr_result": json.dumps(self._result()),
            }
        )
        move._apply_invoice_ocr_result()
        self.assertEqual(move.partner_id, self.vendor)
        self.assertEqual(move.ref, "OCR-001")
        self.assertEqual(len(move.invoice_line_ids), 1)
        self.assertTrue(move.invoice_line_ids.is_imported)
        self.assertEqual(move.invoice_ocr_state, "done")

    def test_manual_line_is_not_deleted_or_duplicated(self):
        move, attachment = self._create_move_and_attachment()
        move.write(
            {
                "partner_id": self.vendor.id,
                "invoice_line_ids": [
                    (
                        0,
                        0,
                        {
                            "name": "Manual line",
                            "quantity": 1,
                            "price_unit": 100,
                        },
                    )
                ],
                "invoice_ocr_state": "extracted",
                "invoice_ocr_attachment_id": attachment.id,
                "invoice_ocr_result": json.dumps(self._result()),
            }
        )
        move._apply_invoice_ocr_result()
        self.assertEqual(len(move.invoice_line_ids), 1)
        self.assertEqual(move.invoice_line_ids.name, "Manual line")
        self.assertEqual(move.invoice_ocr_state, "review")

    def test_low_confidence_line_requires_review(self):
        move, attachment = self._create_move_and_attachment()
        result = self._result()
        next(entity for entity in result["entities"] if entity["type"] == "line_item")[
            "confidence"
        ] = 0.2
        move.write(
            {
                "invoice_ocr_state": "extracted",
                "invoice_ocr_attachment_id": attachment.id,
                "invoice_ocr_result": json.dumps(result),
            }
        )
        move._apply_invoice_ocr_result()
        self.assertEqual(move.invoice_ocr_state, "review")
        self.assertIn("low extraction confidence", move.invoice_ocr_warnings)

    def test_manual_action_does_not_bypass_duplicate_control(self):
        original_move, original_attachment = self._create_move_and_attachment()
        original_move.write(
            {
                "invoice_ocr_state": "done",
                "invoice_ocr_attachment_checksum": original_attachment.checksum,
            }
        )
        duplicate_move, _attachment = self._create_move_and_attachment()

        duplicate_move.action_send_to_invoice_ocr()

        self.assertEqual(duplicate_move.invoice_ocr_state, "review")
        self.assertEqual(duplicate_move.invoice_ocr_duplicate_move_id, original_move)

    def test_european_amount_is_parsed_without_scaling(self):
        move, _attachment = self._create_move_and_attachment()

        self.assertEqual(move._ocr_float("1.234,56"), 1234.56)

    def test_receiver_mismatch_prevents_automatic_completion(self):
        move, _attachment = self._create_move_and_attachment()
        move.company_id.invoice_ocr_auto_post = False
        warnings = []
        move._check_ocr_receiver(
            {"receiver_tax_id": [self._entity("receiver_tax_id", "NL999999999B01")]},
            warnings,
        )

        self.assertIn(
            "does not match this company",
            "\n".join(warnings),
        )

    def test_invalid_amount_requires_review(self):
        move, _attachment = self._create_move_and_attachment()
        warnings = []

        self.assertEqual(
            move._ocr_float("N/A", warnings=warnings, label="total"),
            0.0,
        )
        self.assertTrue(warnings)

    def test_ambiguous_text_amount_requires_review(self):
        move, _attachment = self._create_move_and_attachment()
        warnings = []

        self.assertEqual(
            move._ocr_float("1,234", warnings=warnings, label="total"),
            0.0,
        )
        self.assertTrue(warnings)

    def test_structured_money_value_is_preserved(self):
        entity = documentai.Document.Entity(
            type_="total_amount",
            mention_text="1,234.56",
            normalized_value={
                "money_value": {
                    "currency_code": "EUR",
                    "units": 1234,
                    "nanos": 560000000,
                }
            },
        )
        service = self.env["account.invoice.google.document.ai"]

        normalized = service._normalize_entity(entity)
        self.assertEqual(normalized["normalized"], "1234.56")
        self.assertEqual(normalized["currency"], "EUR")

    def test_missing_currency_requires_review(self):
        move, attachment = self._create_move_and_attachment()
        result = self._result()
        result["entities"] = [
            entity for entity in result["entities"] if entity["type"] != "currency"
        ]
        move.write(
            {
                "invoice_ocr_state": "extracted",
                "invoice_ocr_attachment_id": attachment.id,
                "invoice_ocr_result": json.dumps(result),
            }
        )

        move._apply_invoice_ocr_result()

        self.assertEqual(move.invoice_ocr_state, "review")
        self.assertIn("invoice currency", move.invoice_ocr_warnings)

    def test_missing_tax_total_requires_review(self):
        move, attachment = self._create_move_and_attachment()
        result = self._result()
        result["entities"] = [
            entity
            for entity in result["entities"]
            if entity["type"] != "total_tax_amount"
        ]
        move.write(
            {
                "invoice_ocr_state": "extracted",
                "invoice_ocr_attachment_id": attachment.id,
                "invoice_ocr_result": json.dumps(result),
            }
        )

        move._apply_invoice_ocr_result()

        self.assertEqual(move.invoice_ocr_state, "review")
        self.assertIn("total_tax_amount", move.invoice_ocr_warnings)

    def test_service_account_json_is_used_for_module_client(self):
        credential_values = {
            "type": "service_account",
            "project_id": "test-project",
            "client_email": "invoice-ocr@test-project.iam.gserviceaccount.com",
            "private_key": "test-private-key",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
        encoded = base64.b64encode(json.dumps(credential_values).encode())
        credentials = MagicMock()
        service = self.env["account.invoice.google.document.ai"]
        credentials_path = (
            "odoo.addons.account_invoice_google_document_ai.models."
            "google_document_ai.service_account.Credentials."
            "from_service_account_info"
        )
        client_path = (
            "odoo.addons.account_invoice_google_document_ai.models."
            "google_document_ai.documentai.DocumentProcessorServiceClient"
        )

        with patch(credentials_path, return_value=credentials) as from_info:
            self.company.invoice_ocr_google_credentials = encoded
            with patch(client_path) as client:
                service._get_client(self.company)

        from_info.assert_called()
        self.assertEqual(from_info.call_args.args[0], credential_values)
        self.assertEqual(client.call_args.kwargs["credentials"], credentials)

    def test_invalid_service_account_json_is_rejected(self):
        with self.assertRaises(ValidationError):
            with self.env.cr.savepoint():
                self.company.invoice_ocr_google_credentials = base64.b64encode(
                    b'{"type": "authorized_user"}'
                )

    def test_missing_service_account_json_marks_job_failed(self):
        move, attachment = self._create_move_and_attachment()
        self.company.invoice_ocr_google_credentials = False
        move.write(
            {
                "invoice_ocr_state": "pending",
                "invoice_ocr_attachment_id": attachment.id,
            }
        )

        move._extract_invoice_with_google(attachment.id)

        self.assertEqual(move.invoice_ocr_state, "error")
        self.assertIn("service-account JSON", move.invoice_ocr_error)

    def test_settings_connection_action_checks_company_processor(self):
        settings = self.env["res.config.settings"].create(
            {"company_id": self.company.id}
        )
        service = self.env["account.invoice.google.document.ai"]

        with patch.object(type(service), "_test_connection") as test_connection:
            action = settings.action_test_invoice_ocr_google_connection()

        test_connection.assert_called_once()
        self.assertEqual(test_connection.call_args.args[1], self.company)
        self.assertEqual(action["tag"], "display_notification")
        self.assertEqual(action["params"]["type"], "success")
