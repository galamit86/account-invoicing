# Copyright 2026 Roetz
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import base64
import json
from unittest.mock import MagicMock, patch

from google.api_core import exceptions as google_exceptions
from google.cloud import documentai_v1 as documentai

from odoo import Command, fields
from odoo.exceptions import UserError, ValidationError
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
        self.assertEqual(move.invoice_ocr_document_type, "invoice")

    def test_vat_on_contacts_resolves_to_one_commercial_supplier(self):
        self.env["res.partner"].create(
            {
                "name": "OCR Vendor Contact 1",
                "parent_id": self.vendor.id,
                "vat": self.vendor.vat,
                "supplier_rank": 1,
            }
        )
        self.env["res.partner"].create(
            {
                "name": "OCR Vendor Contact 2",
                "parent_id": self.vendor.id,
                "vat": self.vendor.vat,
                "supplier_rank": 1,
            }
        )
        move, attachment = self._create_move_and_attachment()
        entities = move._group_ocr_entities(
            [self._entity("supplier_tax_id", self.vendor.vat)]
        )

        supplier, strong_match = move._match_ocr_supplier(entities)

        self.assertEqual(supplier, self.vendor)
        self.assertTrue(strong_match)

    def test_unique_iban_matches_supplier(self):
        bank_contact = self.env["res.partner"].create(
            {
                "name": "OCR Vendor Bank Contact",
                "parent_id": self.vendor.id,
            }
        )
        bank = self.env["res.partner.bank"].create(
            {
                "partner_id": bank_contact.id,
                "acc_number": "NL91 ABNA 0417 1643 00",
            }
        )
        move, attachment = self._create_move_and_attachment()
        entities = move._group_ocr_entities(
            [self._entity("supplier_iban", "NL91ABNA0417164300")]
        )

        supplier, strong_match = move._match_ocr_supplier(entities)
        warnings = []
        move._match_ocr_partner_bank(entities, supplier, warnings)

        self.assertEqual(supplier, self.vendor)
        self.assertTrue(strong_match)
        self.assertEqual(move.partner_bank_id, bank)
        self.assertFalse(warnings)

    def test_name_fallback_rejects_customer_only_partner(self):
        customer = self.env["res.partner"].create(
            {"name": "OCR Customer Only", "supplier_rank": 0}
        )
        move, _attachment = self._create_move_and_attachment()
        entities = move._group_ocr_entities(
            [self._entity("supplier_name", customer.name)]
        )

        supplier, strong_match = move._match_ocr_supplier(entities)

        self.assertFalse(supplier)
        self.assertFalse(strong_match)

    def test_complementary_line_fragments_are_merged(self):
        move, attachment = self._create_move_and_attachment()
        result = self._result()
        result["entities"] = [
            entity for entity in result["entities"] if entity["type"] != "line_item"
        ]
        result["entities"] += [
            self._entity(
                "line_item",
                "Subscription 1",
                properties=[
                    self._entity("line_item/description", "Subscription"),
                    self._entity("line_item/quantity", 1),
                ],
            ),
            self._entity(
                "line_item",
                "100.00",
                properties=[self._entity("line_item/amount", 100)],
            ),
            self._entity(
                "line_item",
                "PO123",
                properties=[self._entity("line_item/purchase_order", "PO123")],
            ),
        ]
        move.write(
            {
                "invoice_ocr_state": "extracted",
                "invoice_ocr_attachment_id": attachment.id,
                "invoice_ocr_result": json.dumps(result),
            }
        )

        move._apply_invoice_ocr_result()

        self.assertEqual(len(move.invoice_line_ids), 1)
        self.assertEqual(move.invoice_line_ids.name, "Subscription")
        self.assertEqual(move.invoice_line_ids.quantity, 1)
        self.assertEqual(move.invoice_line_ids.price_unit, 100)

    def test_proforma_is_kept_without_lines_and_cannot_be_posted(self):
        move, attachment = self._create_move_and_attachment()
        result = self._result()
        result["text"] = "PRO FORMA FACTUUR"
        result["entities"] = [
            entity
            for entity in result["entities"]
            if entity["type"] not in ("invoice_id", "net_amount", "total_tax_amount")
        ]
        move.write(
            {
                "invoice_ocr_state": "extracted",
                "invoice_ocr_attachment_id": attachment.id,
                "invoice_ocr_result": json.dumps(result),
            }
        )

        move._apply_invoice_ocr_result()

        self.assertEqual(move.invoice_ocr_document_type, "proforma")
        self.assertEqual(move.invoice_ocr_state, "review")
        self.assertFalse(move.invoice_line_ids)
        with self.assertRaisesRegex(UserError, "pro-forma"):
            move._post()

    def test_reference_to_proforma_does_not_block_final_invoice(self):
        move, _attachment = self._create_move_and_attachment()
        result = self._result()
        result["text"] = "FINAL INVOICE\nThis invoice replaces pro forma PF-001."
        entities = move._group_ocr_entities(result["entities"])

        document_type = move._classify_ocr_document(result, entities)

        self.assertEqual(document_type, "invoice")

    def test_existing_bill_for_purchase_order_is_linked_as_duplicate(self):
        expense_account = self.env["account.account"].search(
            [
                ("company_ids", "in", self.company.id),
                ("account_type", "=", "expense"),
            ],
            limit=1,
        )
        product = self.env["product.product"].create(
            {
                "name": "OCR Purchase Product",
                "is_storable": True,
            }
        )
        purchase_order = self.env["purchase.order"].create(
            {
                "partner_id": self.vendor.id,
                "currency_id": self.company.currency_id.id,
                "order_line": [
                    Command.create(
                        {
                            "name": product.name,
                            "product_id": product.id,
                            "product_qty": 1,
                            "product_uom": product.uom_po_id.id,
                            "price_unit": 100,
                            "taxes_id": [Command.clear()],
                            "date_planned": fields.Datetime.now(),
                        }
                    )
                ],
            }
        )
        purchase_order.button_confirm()
        existing_bill = self.env["account.move"].create(
            {
                "move_type": "in_invoice",
                "company_id": self.company.id,
                "journal_id": self.purchase_journal.id,
                "partner_id": self.vendor.id,
                "currency_id": self.company.currency_id.id,
                "invoice_line_ids": [
                    Command.create(
                        {
                            "name": product.name,
                            "product_id": product.id,
                            "account_id": expense_account.id,
                            "quantity": 1,
                            "price_unit": 100,
                            "purchase_line_id": purchase_order.order_line.id,
                            "tax_ids": [Command.clear()],
                        }
                    )
                ],
            }
        )
        move, attachment = self._create_move_and_attachment()
        result = self._result()
        result["entities"].append(self._entity("purchase_order", purchase_order.name))
        move.write(
            {
                "invoice_ocr_state": "extracted",
                "invoice_ocr_attachment_id": attachment.id,
                "invoice_ocr_result": json.dumps(result),
            }
        )

        move._apply_invoice_ocr_result()

        self.assertEqual(move.invoice_ocr_duplicate_move_id, existing_bill)
        self.assertFalse(move.invoice_line_ids)
        self.assertEqual(move.invoice_ocr_state, "review")
        self.assertIn("already represented", move.invoice_ocr_warnings)

    def test_po_reference_only_match_does_not_replace_ocr_lines(self):
        product = self.env["product.product"].create(
            {
                "name": "OCR Mismatched Purchase Product",
                "is_storable": True,
            }
        )
        purchase_order = self.env["purchase.order"].create(
            {
                "partner_id": self.vendor.id,
                "currency_id": self.company.currency_id.id,
                "order_line": [
                    Command.create(
                        {
                            "name": product.name,
                            "product_id": product.id,
                            "product_qty": 1,
                            "product_uom": product.uom_po_id.id,
                            "price_unit": 200,
                            "date_planned": fields.Datetime.now(),
                        }
                    )
                ],
            }
        )
        purchase_order.button_confirm()
        move, attachment = self._create_move_and_attachment()
        result = self._result()
        result["entities"].append(self._entity("purchase_order", purchase_order.name))
        move.write(
            {
                "invoice_ocr_state": "extracted",
                "invoice_ocr_attachment_id": attachment.id,
                "invoice_ocr_result": json.dumps(result),
            }
        )

        move._apply_invoice_ocr_result()

        self.assertEqual(len(move.invoice_line_ids), 1)
        self.assertFalse(move.invoice_line_ids.purchase_line_id)
        self.assertIn("open lines do not match", move.invoice_ocr_warnings)

    def test_supplier_history_disambiguates_account_and_tax(self):
        historical_vendor = self.env["res.partner"].create(
            {"name": "Historical OCR Vendor", "supplier_rank": 1}
        )
        expense_account = self.env["account.account"].search(
            [
                ("company_ids", "in", self.company.id),
                ("account_type", "=", "expense"),
            ],
            limit=1,
        )
        selected_tax = self.env["account.tax"].create(
            {
                "name": "OCR purchase tax selected",
                "company_id": self.company.id,
                "type_tax_use": "purchase",
                "amount_type": "percent",
                "amount": 17.5,
            }
        )
        self.env["account.tax"].create(
            {
                "name": "OCR purchase tax duplicate",
                "company_id": self.company.id,
                "type_tax_use": "purchase",
                "amount_type": "percent",
                "amount": 17.5,
            }
        )
        historical_bill = self.env["account.move"].create(
            {
                "move_type": "in_invoice",
                "company_id": self.company.id,
                "journal_id": self.purchase_journal.id,
                "partner_id": historical_vendor.id,
                "invoice_date": fields.Date.today(),
                "ref": "HISTORY-001",
                "invoice_line_ids": [
                    Command.create(
                        {
                            "name": "Historical service",
                            "account_id": expense_account.id,
                            "quantity": 1,
                            "price_unit": 100,
                            "tax_ids": [Command.set(selected_tax.ids)],
                        }
                    )
                ],
            }
        )
        historical_bill.action_post()
        move, attachment = self._create_move_and_attachment()
        entities = move._group_ocr_entities(
            [
                self._entity(
                    "vat",
                    "17.5",
                    properties=[self._entity("vat/tax_rate", 17.5)],
                ),
                self._entity(
                    "line_item",
                    "Historical service 100",
                    properties=[
                        self._entity("line_item/description", "Historical service"),
                        self._entity("line_item/amount", 100),
                    ],
                ),
            ]
        )
        warnings = []
        historical_defaults = move._get_ocr_historical_line_defaults(historical_vendor)

        with patch.object(
            type(move), "_get_ocr_historical_line_defaults", return_value={}
        ):
            commands = move._prepare_ocr_line_commands(
                entities, warnings, supplier=historical_vendor
            )

        line_values = commands[0][2]
        self.assertEqual(historical_defaults["account_id"], expense_account.id)
        self.assertEqual(line_values["tax_ids"], [(6, 0, selected_tax.ids)])
        self.assertNotIn("Several purchase taxes", "\n".join(warnings))

        result = {
            "text": "invoice",
            "entities": [
                self._entity("supplier_name", historical_vendor.name),
                self._entity("receiver_tax_id", self.company.vat),
                self._entity("invoice_id", "HISTORY-002"),
                self._entity("invoice_date", fields.Date.today()),
                self._entity("currency", self.company.currency_id.name),
                self._entity("net_amount", 100),
                self._entity("total_tax_amount", 17.5),
                self._entity("total_amount", 117.5),
                self._entity(
                    "vat",
                    "17.5",
                    properties=[self._entity("vat/tax_rate", 17.5)],
                ),
                self._entity(
                    "line_item",
                    "Historical service 100",
                    properties=[
                        self._entity("line_item/description", "Historical service"),
                        self._entity("line_item/amount", 100),
                    ],
                ),
            ],
        }
        move.write(
            {
                "invoice_ocr_state": "extracted",
                "invoice_ocr_attachment_id": attachment.id,
                "invoice_ocr_result": json.dumps(result, default=str),
            }
        )

        move._apply_invoice_ocr_result()

        self.assertNotEqual(move.invoice_ocr_state, "error")
        self.assertEqual(move.invoice_line_ids.account_id, expense_account)
        self.assertEqual(move.invoice_line_ids.tax_ids, selected_tax)
        self.assertEqual(move.amount_total, 117.5)

    def test_zero_rate_does_not_reuse_historical_nonzero_tax(self):
        nonzero_tax = self.env["account.tax"].search(
            [
                ("company_id", "=", self.company.id),
                ("type_tax_use", "=", "purchase"),
                ("amount", "!=", 0),
            ],
            limit=1,
        )
        move, _attachment = self._create_move_and_attachment()
        entities = move._group_ocr_entities(
            [
                self._entity(
                    "vat",
                    "0",
                    properties=[self._entity("vat/tax_rate", 0)],
                ),
                self._entity(
                    "line_item",
                    "Exempt service 100",
                    properties=[
                        self._entity("line_item/description", "Exempt service"),
                        self._entity("line_item/amount", 100),
                    ],
                ),
            ]
        )
        warnings = []

        with patch.object(
            type(move),
            "_get_ocr_historical_line_defaults",
            return_value={"tax_ids": nonzero_tax.ids},
        ):
            commands = move._prepare_ocr_line_commands(
                entities, warnings, supplier=self.vendor
            )

        tax_command = commands[0][2].get("tax_ids")
        self.assertNotEqual(tax_command, [(6, 0, nonzero_tax.ids)])

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
        invoice_user = self.env["res.users"].create(
            {
                "name": "Invoice OCR user",
                "login": "invoice-ocr-user",
                "groups_id": [Command.set([self.env.ref("base.group_user").id])],
            }
        )
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
                service.with_user(invoice_user)._get_client(
                    self.company.with_user(invoice_user)
                )

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
        self.assertEqual(test_connection.call_args.args[0], self.company)
        self.assertEqual(action["tag"], "display_notification")
        self.assertEqual(action["params"]["type"], "success")

    def test_connection_processes_test_image_with_configured_processor(self):
        service = self.env["account.invoice.google.document.ai"]
        client = MagicMock()
        client.processor_path.return_value = "processors/test-processor"

        with patch.object(type(service), "_get_client", return_value=client):
            service._test_connection(self.company)

        client.processor_path.assert_called_once_with(
            "test-project", "eu", "test-processor"
        )
        request = client.process_document.call_args.kwargs["request"]
        self.assertEqual(request.name, "processors/test-processor")
        self.assertEqual(request.raw_document.mime_type, "image/png")
        self.assertTrue(request.raw_document.content.startswith(b"\x89PNG"))

    def test_connection_uses_pinned_processor_version(self):
        self.company.invoice_ocr_google_processor_version = "test-version"
        service = self.env["account.invoice.google.document.ai"]
        client = MagicMock()
        client.processor_version_path.return_value = "processorVersions/test-version"

        with patch.object(type(service), "_get_client", return_value=client):
            service._test_connection(self.company)

        client.processor_version_path.assert_called_once_with(
            "test-project", "eu", "test-processor", "test-version"
        )
        request = client.process_document.call_args.kwargs["request"]
        self.assertEqual(request.name, "processorVersions/test-version")

    def test_connection_permission_error_is_actionable(self):
        service = self.env["account.invoice.google.document.ai"]
        client = MagicMock()
        client.processor_path.return_value = "processors/test-processor"
        client.process_document.side_effect = google_exceptions.PermissionDenied(
            "documentai.processors.processOnline denied"
        )

        with (
            patch.object(type(service), "_get_client", return_value=client),
            self.assertRaisesRegex(UserError, "roles/documentai.apiUser"),
        ):
            service._test_connection(self.company)

    def test_connection_missing_processor_error_is_actionable(self):
        service = self.env["account.invoice.google.document.ai"]
        client = MagicMock()
        client.processor_path.return_value = "processors/test-processor"
        client.process_document.side_effect = google_exceptions.NotFound(
            "processor not found"
        )

        with (
            patch.object(type(service), "_get_client", return_value=client),
            self.assertRaisesRegex(UserError, "could not find"),
        ):
            service._test_connection(self.company)
