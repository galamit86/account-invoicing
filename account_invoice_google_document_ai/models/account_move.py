# Copyright 2023 CreuBlanca
# Copyright 2023 ForgeFlow
# Copyright 2026 Roetz
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import json
import logging
import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import float_compare

from odoo.addons.queue_job.exception import RetryableJobError

_logger = logging.getLogger(__name__)


class AccountMove(models.Model):
    _inherit = "account.move"

    invoice_ocr_state = fields.Selection(
        [
            ("pending", "Pending"),
            ("processing", "Processing"),
            ("extracted", "Extracted"),
            ("done", "Done"),
            ("review", "Review Required"),
            ("error", "Failed"),
        ],
        copy=False,
        tracking=True,
    )
    invoice_ocr_provider = fields.Char(copy=False, readonly=True)
    invoice_ocr_attachment_id = fields.Many2one(
        "ir.attachment",
        copy=False,
        readonly=True,
        ondelete="set null",
    )
    invoice_ocr_attachment_checksum = fields.Char(copy=False, readonly=True)
    invoice_ocr_result = fields.Text(copy=False, readonly=True)
    invoice_ocr_warnings = fields.Text(copy=False, readonly=True)
    invoice_ocr_error = fields.Text(copy=False, readonly=True)
    invoice_ocr_min_confidence = fields.Float(copy=False, readonly=True)
    invoice_ocr_duplicate_move_id = fields.Many2one(
        "account.move",
        copy=False,
        readonly=True,
        ondelete="set null",
    )
    invoice_ocr_document_type = fields.Selection(
        [
            ("invoice", "Invoice"),
            ("credit_note", "Credit Note"),
            ("proforma", "Pro-forma"),
            ("unknown", "Unknown"),
        ],
        copy=False,
        tracking=True,
    )
    can_send_to_invoice_ocr = fields.Boolean(
        compute="_compute_can_send_to_invoice_ocr",
    )

    @api.depends("state", "move_type", "company_id.invoice_ocr_google_mode")
    def _compute_can_send_to_invoice_ocr(self):
        for move in self:
            move.can_send_to_invoice_ocr = (
                move.state == "draft"
                and move.is_purchase_document(include_receipts=True)
                and move.company_id.invoice_ocr_google_mode != "disabled"
            )

    def _get_edi_decoder(self, file_data, new=False):
        decoder = super()._get_edi_decoder(file_data, new=new)
        if decoder:
            return decoder
        company = self.company_id if self and self.company_id else self.env.company
        if (
            file_data.get("type") == "pdf"
            and company.invoice_ocr_google_mode == "automatic"
        ):
            return self._decode_google_document_ai
        return None

    def _post(self, soft=True):
        proformas = self.filtered(
            lambda move: move.invoice_ocr_document_type == "proforma"
        )
        if proformas:
            raise UserError(
                _(
                    "A pro-forma document is not a vendor bill and cannot be posted. "
                    "Replace it with the final invoice or explicitly reclassify it "
                    "after review."
                )
            )
        return super()._post(soft=soft)

    def _decode_google_document_ai(self, invoice, file_data, new=False):
        attachment = file_data.get("attachment")
        if not attachment:
            return False
        invoice._enqueue_invoice_ocr(attachment)
        return True

    def action_send_to_invoice_ocr(self):
        self.ensure_one()
        if not self.can_send_to_invoice_ocr:
            raise UserError(_("This document cannot be sent for invoice extraction."))
        attachment = self.message_main_attachment_id
        if not attachment or attachment.mimetype != "application/pdf":
            attachment = self.attachment_ids.filtered(
                lambda item: item.mimetype == "application/pdf"
            )[:1]
        if not attachment:
            raise UserError(_("Attach a PDF invoice before starting extraction."))
        self._enqueue_invoice_ocr(attachment)

    def _enqueue_invoice_ocr(self, attachment):
        self.ensure_one()
        checksum = attachment.checksum
        duplicate = self.search(
            [
                ("id", "!=", self.id),
                ("company_id", "=", self.company_id.id),
                ("invoice_ocr_attachment_checksum", "=", checksum),
                (
                    "invoice_ocr_state",
                    "in",
                    (
                        "pending",
                        "processing",
                        "extracted",
                        "done",
                        "review",
                    ),
                ),
            ],
            limit=1,
        )
        if duplicate:
            warning = _("The same PDF was already processed on %(move)s.") % {
                "move": duplicate.display_name,
            }
            self.write(
                {
                    "invoice_ocr_state": "review",
                    "invoice_ocr_provider": "google_document_ai",
                    "invoice_ocr_attachment_id": attachment.id,
                    "invoice_ocr_attachment_checksum": checksum,
                    "invoice_ocr_duplicate_move_id": duplicate.id,
                    "invoice_ocr_result": False,
                    "invoice_ocr_warnings": warning,
                    "invoice_ocr_error": False,
                    "invoice_ocr_min_confidence": 0.0,
                }
            )
            self.message_post(body=warning)
            return
        self.write(
            {
                "invoice_ocr_state": "pending",
                "invoice_ocr_provider": "google_document_ai",
                "invoice_ocr_attachment_id": attachment.id,
                "invoice_ocr_attachment_checksum": checksum,
                "invoice_ocr_duplicate_move_id": False,
                "invoice_ocr_document_type": False,
                "invoice_ocr_result": False,
                "invoice_ocr_warnings": False,
                "invoice_ocr_error": False,
                "invoice_ocr_min_confidence": 0.0,
            }
        )
        identity_key = f"invoice-ocr-google-{self.id}-{checksum}"
        self.with_delay(
            channel="root.invoice_ocr",
            identity_key=identity_key,
            description=_("Extract invoice %(invoice)s with Google Document AI")
            % {"invoice": self.display_name},
            max_retries=5,
        )._extract_invoice_with_google(attachment.id)

    def _extract_invoice_with_google(self, attachment_id):
        self.ensure_one()
        if self.state != "draft" or self.invoice_ocr_state not in (
            "pending",
            "processing",
        ):
            return
        attachment = self.env["ir.attachment"].browse(attachment_id).exists()
        if not attachment:
            self.write(
                {
                    "invoice_ocr_state": "error",
                    "invoice_ocr_error": _("The source PDF no longer exists."),
                }
            )
            return
        company = self.company_id
        missing = [
            label
            for value, label in (
                (company.invoice_ocr_google_project, _("Google project")),
                (company.invoice_ocr_google_location, _("Google location")),
                (company.invoice_ocr_google_processor, _("Google processor")),
                (
                    company.sudo().invoice_ocr_google_credentials,
                    _("Google service-account JSON"),
                ),
            )
            if not value
        ]
        if missing:
            self.write(
                {
                    "invoice_ocr_state": "error",
                    "invoice_ocr_error": _("Missing configuration: %s")
                    % ", ".join(missing),
                }
            )
            return
        self.invoice_ocr_state = "processing"
        service = self.env["account.invoice.google.document.ai"]
        try:
            result = service._process_document(attachment, company)
        except Exception as error:  # Google exposes several transport subclasses.
            if service._is_retryable_error(error):
                raise RetryableJobError(str(error)) from error
            _logger.exception("Google invoice extraction failed for move %s", self.id)
            self.write(
                {
                    "invoice_ocr_state": "error",
                    "invoice_ocr_error": str(error),
                }
            )
            return
        self.write(
            {
                "invoice_ocr_state": "extracted",
                "invoice_ocr_result": json.dumps(result, ensure_ascii=True),
                "invoice_ocr_error": False,
            }
        )
        self.with_delay(
            channel="root.invoice_ocr",
            identity_key=f"invoice-ocr-apply-{self.id}-{attachment.checksum}",
            description=_("Apply extracted values to invoice %(invoice)s")
            % {"invoice": self.display_name},
        )._apply_invoice_ocr_result()

    def _invoice_ocr_job_failed(self, **fail_values):
        for move in self.exists().filtered(lambda item: item.state == "draft"):
            message = fail_values.get("exc_message") or _(
                "Invoice extraction failed after all retry attempts."
            )
            move.write(
                {
                    "invoice_ocr_state": "error",
                    "invoice_ocr_error": message,
                }
            )
            move.message_post(body=message)

    def _apply_invoice_ocr_result(self):
        self.ensure_one()
        if self.state != "draft" or self.invoice_ocr_state != "extracted":
            return
        try:
            with self.env.cr.savepoint():
                result = json.loads(self.invoice_ocr_result)
                warnings, minimum_confidence = self._apply_normalized_invoice_ocr(
                    result
                )
        except Exception as error:
            _logger.exception("Applying invoice extraction failed for move %s", self.id)
            self.write(
                {
                    "invoice_ocr_state": "error",
                    "invoice_ocr_error": str(error),
                }
            )
            return
        values = {
            "invoice_ocr_state": "review" if warnings else "done",
            "invoice_ocr_warnings": "\n".join(warnings) or False,
            "invoice_ocr_error": False,
            "invoice_ocr_min_confidence": minimum_confidence,
            "checked": not warnings,
        }
        self.write(values)
        if warnings:
            self.message_post(body="<br/>".join(warnings))
        elif self.company_id.invoice_ocr_auto_post:
            try:
                with self.env.cr.savepoint():
                    self._autopost_bill()
            except Exception as error:
                _logger.exception("Auto-posting OCR invoice %s failed", self.id)
                warning = _("Automatic posting failed: %s") % error
                self.write(
                    {
                        "invoice_ocr_state": "review",
                        "invoice_ocr_warnings": warning,
                        "checked": False,
                    }
                )
                self.message_post(body=warning)

    def _apply_normalized_invoice_ocr(self, result):  # noqa: C901
        entities = self._group_ocr_entities(result.get("entities", []))
        warnings = []
        document_type = self._classify_ocr_document(result, entities)
        self.invoice_ocr_document_type = document_type
        is_proforma = document_type == "proforma"
        critical = (
            ("invoice_date", "total_amount")
            if is_proforma
            else (
                "invoice_id",
                "invoice_date",
                "net_amount",
                "total_tax_amount",
                "total_amount",
            )
        )
        confidences = [
            entities[key][0].get("confidence", 0.0)
            for key in critical
            if entities.get(key)
        ]
        threshold = self.company_id.invoice_ocr_confidence_threshold
        for key in critical:
            if not entities.get(key):
                warnings.append(
                    _("Google did not recognize the required field: %s") % key
                )
            elif entities[key][0].get("confidence", 0.0) < threshold:
                warnings.append(_("Low confidence for field: %s") % key)

        supplier_entities = (
            entities.get("supplier_tax_id")
            or entities.get("supplier_iban")
            or entities.get("supplier_name", [])
        )
        if not supplier_entities:
            warnings.append(_("Google did not recognize the supplier identity."))
        elif supplier_entities[0].get("confidence", 0.0) < threshold:
            warnings.append(_("Low confidence for the supplier identity."))
        else:
            confidences.append(supplier_entities[0].get("confidence", 0.0))

        supplier, strong_supplier_match = self._match_ocr_supplier(entities)
        if supplier:
            self.partner_id = supplier
            if not strong_supplier_match:
                warnings.append(
                    _(
                        "The supplier was matched without a unique VAT number; "
                        "review it."
                    )
                )
        else:
            warnings.append(_("No unambiguous existing supplier could be matched."))

        self._check_ocr_receiver(entities, warnings)
        currency, currency_confidence = self._match_ocr_currency(entities, warnings)
        if currency_confidence is not None:
            confidences.append(currency_confidence)

        values = self._prepare_ocr_invoice_values(entities, currency, warnings)
        existing_product_lines = self.invoice_line_ids.filtered(
            lambda line: line.display_type == "product"
        )
        if values:
            self.write(values)

        total = self._ocr_float(
            self._first_ocr_value(entities, "total_amount"),
            warnings=warnings,
            label=_("total amount"),
        )
        po_references = [
            self._ocr_entity_value(entity)
            for entity in entities.get("purchase_order", [])
            if self._ocr_entity_value(entity)
        ]
        purchase_orders = self._match_ocr_purchase_orders(
            po_references, supplier, currency, total, warnings
        )
        duplicate = self._find_ocr_po_bill_duplicate(
            purchase_orders, supplier, currency, total, warnings
        )
        if duplicate:
            self.invoice_ocr_duplicate_move_id = duplicate
            warnings.append(
                _(
                    "Purchase order %(purchase_order)s is already represented by "
                    "vendor bill %(bill)s. OCR lines were not created."
                )
                % {
                    "purchase_order": ", ".join(purchase_orders.mapped("name")),
                    "bill": duplicate.display_name,
                }
            )

        if is_proforma:
            warnings.append(
                _(
                    "This document is marked as pro-forma. It was retained for "
                    "review, but accounting lines were not created and posting is "
                    "blocked."
                )
            )
        elif duplicate:
            pass
        elif existing_product_lines:
            warnings.append(
                _("Existing invoice lines were kept; OCR lines were not added.")
            )
        else:
            if hasattr(self, "_find_and_set_purchase_orders") and (
                purchase_orders or (not po_references and supplier)
            ):
                matched_po_references = purchase_orders.mapped("name")
                method, _po_lines, _invoice_lines = self._match_purchase_orders(
                    matched_po_references,
                    supplier.id if supplier else False,
                    total,
                    True,
                    10,
                )
                if method in ("total_match", "subset_total_match"):
                    self._find_and_set_purchase_orders(
                        matched_po_references,
                        supplier.id if supplier else False,
                        total,
                        from_ocr=True,
                    )
                elif method == "po_match":
                    warnings.append(
                        _(
                            "The purchase order reference matches, but its open lines "
                            "do not match the extracted total. OCR lines were kept."
                        )
                    )
            if not self.invoice_line_ids.filtered(
                lambda line: line.display_type == "product"
            ):
                line_commands = self._prepare_ocr_line_commands(
                    entities, warnings, supplier=supplier
                )
                if line_commands:
                    with self._get_edi_creation() as invoice:
                        invoice.invoice_line_ids = line_commands
                else:
                    warnings.append(_("Google did not return usable invoice lines."))
        self._match_ocr_partner_bank(entities, supplier, warnings)
        if not is_proforma and not duplicate:
            self._check_ocr_totals(entities, warnings)
        if self.duplicated_ref_ids:
            warnings.append(
                _("Odoo found another bill with the same supplier reference.")
            )
        return warnings, min(confidences) if confidences else 0.0

    def _classify_ocr_document(self, result, entities):
        invoice_type = str(
            self._first_ocr_value(entities, "invoice_type") or ""
        ).lower()
        document_text = result.get("text") or ""
        if "proforma" in invoice_type or re.search(
            r"^\s*pro[\s-]*forma(?:\s+(?:invoice|factuur))?\s*$",
            document_text,
            flags=re.IGNORECASE | re.MULTILINE,
        ):
            return "proforma"
        total = self._ocr_float(self._first_ocr_value(entities, "total_amount"))
        if "credit" in invoice_type or total < 0:
            return "credit_note"
        if entities.get("invoice_id") or entities.get("invoice_date"):
            return "invoice"
        return "unknown"

    def _match_ocr_purchase_orders(
        self, po_references, supplier, currency, total, warnings
    ):
        if not po_references:
            return self.env["purchase.order"]
        references = list(
            dict.fromkeys(str(ref).strip() for ref in po_references if ref)
        )
        orders = self.env["purchase.order"].search(
            [
                ("company_id", "=", self.company_id.id),
                ("state", "in", ("purchase", "done")),
                "|",
                ("name", "in", references),
                ("partner_ref", "in", references),
            ]
        )
        if supplier:
            orders = orders.filtered(
                lambda order: order.partner_id.commercial_partner_id
                == supplier.commercial_partner_id
            )
        if currency:
            orders = orders.filtered(lambda order: order.currency_id == currency)
        if len(orders) > 1:
            exact_total_orders = orders.filtered(
                lambda order: not float_compare(
                    abs(order.amount_total),
                    abs(total),
                    precision_rounding=order.currency_id.rounding,
                )
            )
            if len(exact_total_orders) == 1:
                return exact_total_orders
            warnings.append(
                _("Several purchase orders match the extracted reference and total.")
            )
            return self.env["purchase.order"]
        if not orders:
            warnings.append(
                _(
                    "No purchase order matches the extracted reference, supplier, "
                    "currency, and total."
                )
            )
        return orders

    def _find_ocr_po_bill_duplicate(
        self, purchase_orders, supplier, currency, total, warnings
    ):
        if len(purchase_orders) != 1:
            return self.env["account.move"]
        candidates = self.search(
            [
                ("id", "!=", self.id),
                ("company_id", "=", self.company_id.id),
                ("state", "!=", "cancel"),
                ("move_type", "in", self.get_purchase_types(include_receipts=True)),
                (
                    "invoice_line_ids.purchase_line_id.order_id",
                    "=",
                    purchase_orders.id,
                ),
            ]
        )
        if supplier:
            candidates = candidates.filtered(
                lambda move: move.commercial_partner_id
                == supplier.commercial_partner_id
            )
        if currency:
            candidates = candidates.filtered(lambda move: move.currency_id == currency)
        if total:
            candidates = candidates.filtered(
                lambda move: not float_compare(
                    abs(move.amount_total),
                    abs(total),
                    precision_rounding=move.currency_id.rounding,
                )
            )
        if len(candidates) == 1:
            return candidates
        if len(candidates) > 1:
            warnings.append(
                _(
                    "Several existing bills match the extracted purchase order "
                    "and total."
                )
            )
        return self.env["account.move"]

    def _group_ocr_entities(self, entities):
        grouped = {}
        for entity in entities:
            grouped.setdefault(entity.get("type"), []).append(entity)
        return grouped

    def _first_ocr_value(self, entities, entity_type):
        records = entities.get(entity_type, [])
        return self._ocr_entity_value(records[0]) if records else False

    def _ocr_entity_value(self, entity):
        return entity.get("normalized") or entity.get("text") or False

    def _ocr_float(self, value, default=0.0, warnings=None, label=None):
        if value in (False, None, ""):
            return default
        if isinstance(value, int | float):
            return float(value)
        cleaned = re.sub(r"[^0-9,.-]", "", str(value))
        if "," in cleaned and "." in cleaned:
            decimal_separator = "," if cleaned.rfind(",") > cleaned.rfind(".") else "."
            thousands_separator = "." if decimal_separator == "," else ","
            cleaned = cleaned.replace(thousands_separator, "")
            cleaned = cleaned.replace(decimal_separator, ".")
        elif "," in cleaned:
            cleaned = self._normalize_single_separator_number(
                cleaned, ",", default, warnings, label, value
            )
            if not isinstance(cleaned, str):
                return cleaned
        elif "." in cleaned:
            cleaned = self._normalize_single_separator_number(
                cleaned, ".", default, warnings, label, value
            )
            if not isinstance(cleaned, str):
                return cleaned
        try:
            return float(cleaned)
        except (TypeError, ValueError):
            if warnings is not None:
                warnings.append(
                    _("The extracted %(label)s is not a valid number: %(value)s")
                    % {
                        "label": label or _("value"),
                        "value": value,
                    }
                )
            return default

    def _normalize_single_separator_number(
        self, cleaned, separator, default, warnings, label, original_value
    ):
        unsigned = cleaned.lstrip("-")
        parts = unsigned.split(separator)
        if len(parts) > 2 and all(len(part) == 3 for part in parts[1:]):
            return cleaned.replace(separator, "")
        if len(parts) == 2 and len(parts[1]) in (1, 2):
            return cleaned.replace(separator, ".")
        if warnings is not None:
            warnings.append(
                _("The extracted %(label)s has ambiguous separators: %(value)s")
                % {
                    "label": label or _("value"),
                    "value": original_value,
                }
            )
        return default

    def _check_ocr_receiver(self, entities, warnings):
        receiver_entities = entities.get("receiver_tax_id", [])
        if not receiver_entities:
            warnings.append(
                _("The invoice recipient VAT number was not recognized; review it.")
            )
            return
        receiver = receiver_entities[0]
        if (
            receiver.get("confidence", 0.0)
            < self.company_id.invoice_ocr_confidence_threshold
        ):
            warnings.append(_("Low confidence for the invoice recipient identity."))
            return
        extracted_vat = re.sub(
            r"[^A-Z0-9]", "", str(self._ocr_entity_value(receiver)).upper()
        )
        company_vat = re.sub(r"[^A-Z0-9]", "", (self.company_id.vat or "").upper())
        if not company_vat:
            warnings.append(
                _("The company has no VAT number configured to verify the recipient.")
            )
        elif extracted_vat != company_vat:
            warnings.append(
                _("The extracted invoice recipient does not match this company.")
            )

    def _match_ocr_supplier(self, entities):
        vat = self._first_ocr_value(entities, "supplier_tax_id")
        iban = self._first_ocr_value(entities, "supplier_iban")
        name = self._first_ocr_value(entities, "supplier_name")
        email = self._first_ocr_value(entities, "supplier_email")
        phone = self._first_ocr_value(entities, "supplier_phone")
        company_domain = [
            ("company_id", "in", (False, self.company_id.id)),
            ("supplier_rank", ">", 0),
        ]
        if vat:
            exact_vat = self.env["res.partner"].search(
                company_domain + [("vat", "=ilike", vat)],
            )
            commercial_partners = exact_vat.commercial_partner_id
            if len(commercial_partners) == 1:
                return commercial_partners, True
        if iban:
            sanitized_iban = re.sub(r"[^A-Z0-9]", "", str(iban).upper())
            banks = self.env["res.partner.bank"].search(
                [("sanitized_acc_number", "=", sanitized_iban)]
            )
            commercial_partners = banks.partner_id.commercial_partner_id.filtered(
                lambda partner: (
                    not partner.company_id or partner.company_id == self.company_id
                )
                and partner.supplier_rank > 0
            )
            if len(commercial_partners) == 1:
                return commercial_partners, True
        partner = self.env["res.partner"]._retrieve_partner(
            name=name,
            email=email,
            phone=phone,
            vat=vat,
            company=self.company_id,
        )
        commercial_partner = partner.commercial_partner_id
        if partner and (
            partner.supplier_rank > 0 or commercial_partner.supplier_rank > 0
        ):
            return commercial_partner, False
        return self.env["res.partner"], False

    def _match_ocr_currency(self, entities, warnings):
        candidates = []

        def collect(entity):
            currency = entity.get("currency")
            if currency:
                candidates.append((currency.upper(), entity.get("confidence", 0.0)))
            for property_entity in entity.get("properties", []):
                collect(property_entity)

        for grouped_entities in entities.values():
            for entity in grouped_entities:
                collect(entity)
        for entity in entities.get("currency", []):
            value = self._ocr_entity_value(entity)
            if value:
                candidates.append((str(value).upper(), entity.get("confidence", 0.0)))

        currency_names = {name for name, _confidence in candidates}
        if not currency_names:
            warnings.append(_("Google did not recognize the invoice currency."))
            return self.env["res.currency"], None
        if len(currency_names) > 1:
            warnings.append(_("Extracted monetary values use inconsistent currencies."))
            return self.env["res.currency"], min(
                confidence for _name, confidence in candidates
            )
        currency_name = currency_names.pop()
        confidence = min(
            item_confidence
            for name, item_confidence in candidates
            if name == currency_name
        )
        if confidence < self.company_id.invoice_ocr_confidence_threshold:
            warnings.append(_("Low confidence for the invoice currency."))
        currency = self.env["res.currency"].search(
            [("name", "=", currency_name)],
            limit=1,
        )
        if not currency:
            warnings.append(_("Unknown extracted currency: %s") % currency_name)
        return currency, confidence

    def _prepare_ocr_invoice_values(self, entities, currency, warnings):
        values = {}
        field_map = {
            "invoice_id": "ref",
            "invoice_date": "invoice_date",
            "due_date": "invoice_date_due",
            "supplier_payment_ref": "payment_reference",
        }
        for source, target in field_map.items():
            value = self._first_ocr_value(entities, source)
            if value:
                values[target] = value
        if currency:
            values["currency_id"] = currency.id
        po_reference = self._first_ocr_value(entities, "purchase_order")
        if po_reference:
            values["invoice_origin"] = po_reference
        invoice_type = (self._first_ocr_value(entities, "invoice_type") or "").lower()
        total = self._ocr_float(
            self._first_ocr_value(entities, "total_amount"),
            warnings=warnings,
            label=_("total amount"),
        )
        if "credit" in invoice_type or total < 0:
            values["move_type"] = "in_refund"
        return values

    def _prepare_ocr_line_commands(  # noqa: C901
        self, entities, warnings, supplier=False
    ):
        invoice_tax_rates = self._get_ocr_invoice_tax_rates(entities, warnings)
        historical_defaults = self._get_ocr_historical_line_defaults(supplier)
        commands = []
        for line in self._normalize_ocr_lines(entities, warnings):
            entity = line["entity"]
            properties = line["properties"]
            if (
                entity.get("confidence", 0.0)
                < self.company_id.invoice_ocr_confidence_threshold
            ):
                warnings.append(_("An invoice line has low extraction confidence."))
            for property_type in (
                "line_item/quantity",
                "line_item/unit_price",
                "line_item/amount",
                "line_item/product_code",
            ):
                property_entities = properties.get(property_type, [])
                if property_entities and (
                    property_entities[0].get("confidence", 0.0)
                    < self.company_id.invoice_ocr_confidence_threshold
                ):
                    warnings.append(
                        _("Low confidence for invoice line field: %s")
                        % property_type.removeprefix("line_item/")
                    )
            description = line["description"] or _("OCR invoice line")
            quantity = line["quantity"]
            unit_price = self._ocr_float(
                self._first_ocr_value(properties, "line_item/unit_price"),
                warnings=warnings,
                label=_("line unit price"),
            )
            if not unit_price:
                amount = self._ocr_float(
                    self._first_ocr_value(properties, "line_item/amount"),
                    warnings=warnings,
                    label=_("line amount"),
                )
                unit_price = amount / quantity if quantity else amount
            line_values = {
                "name": description,
                "quantity": quantity,
                "price_unit": unit_price,
                "is_imported": True,
            }
            product_code = self._first_ocr_value(properties, "line_item/product_code")
            if product_code:
                products = (
                    self.env["product.product"]
                    .with_company(self.company_id)
                    .search(
                        [
                            ("company_id", "in", (False, self.company_id.id)),
                            "|",
                            ("default_code", "=", product_code),
                            ("barcode", "=", product_code),
                        ],
                        limit=2,
                    )
                )
                if len(products) == 1:
                    line_values["product_id"] = products.id
                elif len(products) > 1:
                    warnings.append(
                        _("Several products match extracted code %s.") % product_code
                    )
            if "product_id" not in line_values and historical_defaults.get(
                "account_id"
            ):
                line_values["account_id"] = historical_defaults["account_id"]
            tax_rate_value = self._first_ocr_value(properties, "line_item/tax_rate")
            tax_rate = self._ocr_float(tax_rate_value, default=None)
            if tax_rate is None and len(invoice_tax_rates) == 1:
                tax_rate = invoice_tax_rates[0]
            if tax_rate is not None:
                taxes = self.env["account.tax"].search(
                    [
                        ("company_id", "=", self.company_id.id),
                        ("type_tax_use", "=", "purchase"),
                        ("amount_type", "=", "percent"),
                        ("amount", "=", tax_rate),
                    ]
                )
                if len(taxes) == 1:
                    line_values["tax_ids"] = [(6, 0, taxes.ids)]
                elif len(taxes) > 1:
                    historical_taxes = self.env["account.tax"]
                    if "product_id" not in line_values:
                        historical_taxes = taxes.filtered(
                            lambda tax: tax.id in historical_defaults.get("tax_ids", [])
                        )
                        if not historical_taxes:
                            historical_taxes = self._get_ocr_historical_taxes(
                                taxes, supplier
                            )
                    if historical_taxes:
                        line_values["tax_ids"] = [(6, 0, historical_taxes.ids)]
                    else:
                        warnings.append(
                            _("Several purchase taxes match rate %s%%.") % tax_rate
                        )
                else:
                    warnings.append(_("No purchase tax matches rate %s%%.") % tax_rate)
            elif "product_id" not in line_values and historical_defaults.get("tax_ids"):
                line_values["tax_ids"] = [(6, 0, historical_defaults["tax_ids"])]
            commands.append((0, 0, line_values))
        return commands

    def _normalize_ocr_lines(self, entities, warnings):
        lines = []
        for entity in entities.get("line_item", []):
            properties = self._group_ocr_entities(entity.get("properties", []))
            description = self._first_ocr_value(properties, "line_item/description")
            product_code = self._first_ocr_value(properties, "line_item/product_code")
            quantity_value = self._first_ocr_value(properties, "line_item/quantity")
            unit_price_value = self._first_ocr_value(properties, "line_item/unit_price")
            amount_value = self._first_ocr_value(properties, "line_item/amount")
            if not any(
                value not in (False, None, "")
                for value in (
                    description,
                    product_code,
                    quantity_value,
                    unit_price_value,
                    amount_value,
                )
            ):
                continue
            lines.append(
                {
                    "entity": entity,
                    "properties": properties,
                    "description": description,
                    "product_code": product_code,
                    "quantity_value": quantity_value,
                    "unit_price_value": unit_price_value,
                    "amount_value": amount_value,
                }
            )

        merged = []
        for line in lines:
            pricing_only = (
                not line["description"]
                and not line["product_code"]
                and line["quantity_value"] in (False, None, "")
                and (
                    line["unit_price_value"] not in (False, None, "")
                    or line["amount_value"] not in (False, None, "")
                )
            )
            if (
                pricing_only
                and merged
                and merged[-1]["unit_price_value"] in (False, None, "")
                and merged[-1]["amount_value"] in (False, None, "")
            ):
                previous = merged[-1]
                previous["unit_price_value"] = line["unit_price_value"]
                previous["amount_value"] = line["amount_value"]
                for key, values in line["properties"].items():
                    previous["properties"].setdefault(key, []).extend(values)
                continue
            merged.append(line)

        usable_lines = [
            line
            for line in merged
            if line["unit_price_value"] not in (False, None, "")
            or line["amount_value"] not in (False, None, "")
        ]
        for line in usable_lines:
            line["quantity"] = (
                self._ocr_float(
                    line["quantity_value"],
                    default=1.0,
                    warnings=warnings,
                    label=_("line quantity"),
                )
                or 1.0
            )
        return usable_lines

    def _get_ocr_historical_line_defaults(self, supplier):
        if not supplier:
            return {}
        lines = self.env["account.move.line"].search(
            [
                ("move_id.company_id", "=", self.company_id.id),
                ("move_id.state", "=", "posted"),
                ("move_id.move_type", "in", ("in_invoice", "in_refund")),
                (
                    "move_id.commercial_partner_id",
                    "=",
                    supplier.commercial_partner_id.id,
                ),
                ("display_type", "=", "product"),
                ("account_id", "!=", False),
            ],
            order="date desc, id desc",
            limit=100,
        )
        patterns = {
            (line.account_id.id, tuple(sorted(line.tax_ids.ids))) for line in lines
        }
        if len(patterns) != 1:
            return {}
        account_id, tax_ids = patterns.pop()
        return {"account_id": account_id, "tax_ids": list(tax_ids)}

    def _get_ocr_historical_taxes(self, candidate_taxes, supplier):
        if not supplier or not candidate_taxes:
            return self.env["account.tax"]
        lines = self.env["account.move.line"].search(
            [
                ("move_id.company_id", "=", self.company_id.id),
                ("move_id.state", "=", "posted"),
                ("move_id.move_type", "in", ("in_invoice", "in_refund")),
                (
                    "move_id.commercial_partner_id",
                    "=",
                    supplier.commercial_partner_id.id,
                ),
                ("display_type", "=", "product"),
                ("tax_ids", "in", candidate_taxes.ids),
            ]
        )
        historical_taxes = lines.tax_ids & candidate_taxes
        return (
            historical_taxes if len(historical_taxes) == 1 else self.env["account.tax"]
        )

    def _get_ocr_invoice_tax_rates(self, entities, warnings):
        rates = []
        for vat_entity in entities.get("vat", []):
            properties = self._group_ocr_entities(vat_entity.get("properties", []))
            rate_value = self._first_ocr_value(properties, "vat/tax_rate")
            rate = self._ocr_float(rate_value, default=None)
            if rate is not None and rate not in rates:
                rates.append(rate)
        extracted_tax = self._ocr_float(
            self._first_ocr_value(entities, "total_tax_amount")
        )
        if extracted_tax and not rates:
            warnings.append(
                _("Tax was extracted, but no VAT rate could be recognized.")
            )
        elif len(rates) > 1:
            warnings.append(
                _(
                    "The invoice contains several VAT rates; "
                    "review their line allocation."
                )
            )
        return rates

    def _match_ocr_partner_bank(self, entities, supplier, warnings):
        iban = self._first_ocr_value(entities, "supplier_iban")
        if not iban or not supplier:
            return
        normalized = re.sub(r"[^A-Z0-9]", "", str(iban).upper())
        banks = self.env["res.partner.bank"].search(
            [
                ("partner_id.commercial_partner_id", "=", supplier.id),
                ("sanitized_acc_number", "=", normalized),
            ]
        )
        if len(banks) == 1:
            self.partner_bank_id = banks
        else:
            warnings.append(
                _(
                    "The extracted IBAN is not uniquely registered for the matched "
                    "supplier."
                )
            )

    def _check_ocr_totals(self, entities, warnings):
        checks = (
            ("total_amount", self.amount_total, _("Total")),
            ("net_amount", self.amount_untaxed, _("Untaxed total")),
            ("total_tax_amount", self.amount_tax, _("Tax total")),
        )
        for entity_type, actual, label in checks:
            extracted_value = self._first_ocr_value(entities, entity_type)
            if extracted_value is False:
                continue
            extracted = abs(
                self._ocr_float(
                    extracted_value,
                    warnings=warnings,
                    label=label,
                )
            )
            if float_compare(
                extracted,
                abs(actual),
                precision_rounding=self.currency_id.rounding,
            ):
                warnings.append(
                    _(
                        "%(label)s differs: extracted %(extracted)s, "
                        "calculated %(actual)s."
                    )
                    % {"label": label, "extracted": extracted, "actual": abs(actual)}
                )
