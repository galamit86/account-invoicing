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

    def _apply_normalized_invoice_ocr(self, result):
        entities = self._group_ocr_entities(result.get("entities", []))
        warnings = []
        critical = (
            "invoice_id",
            "invoice_date",
            "net_amount",
            "total_tax_amount",
            "total_amount",
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

        supplier_entities = entities.get("supplier_tax_id") or entities.get(
            "supplier_name", []
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
        if existing_product_lines:
            warnings.append(
                _("Existing invoice lines were kept; OCR lines were not added.")
            )
        else:
            line_commands = self._prepare_ocr_line_commands(entities, warnings)
            if line_commands:
                values["invoice_line_ids"] = line_commands
            else:
                warnings.append(_("Google did not return usable invoice lines."))
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
        if (
            not existing_product_lines
            and hasattr(self, "_find_and_set_purchase_orders")
            and (po_references or supplier)
        ):
            self._find_and_set_purchase_orders(
                po_references,
                supplier.id if supplier else False,
                total,
                from_ocr=True,
            )
        self._match_ocr_partner_bank(entities, supplier, warnings)
        self._check_ocr_totals(entities, warnings)
        if self.duplicated_ref_ids:
            warnings.append(
                _("Odoo found another bill with the same supplier reference.")
            )
        return warnings, min(confidences) if confidences else 0.0

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
                limit=2,
            )
            if len(exact_vat) == 1:
                return exact_vat, True
        partner = self.env["res.partner"]._retrieve_partner(
            name=name,
            email=email,
            phone=phone,
            vat=vat,
            company=self.company_id,
        )
        return partner, False

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

    def _prepare_ocr_line_commands(self, entities, warnings):
        invoice_tax_rates = self._get_ocr_invoice_tax_rates(entities, warnings)
        commands = []
        for entity in entities.get("line_item", []):
            properties = self._group_ocr_entities(entity.get("properties", []))
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
            description = self._first_ocr_value(
                properties, "line_item/description"
            ) or _("OCR invoice line")
            quantity = (
                self._ocr_float(
                    self._first_ocr_value(properties, "line_item/quantity"),
                    default=1.0,
                    warnings=warnings,
                    label=_("line quantity"),
                )
                or 1.0
            )
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
            tax_rate = self._ocr_float(
                self._first_ocr_value(properties, "line_item/tax_rate")
            )
            if not tax_rate and len(invoice_tax_rates) == 1:
                tax_rate = invoice_tax_rates[0]
            if tax_rate:
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
                    warnings.append(
                        _("Several purchase taxes match rate %s%%.") % tax_rate
                    )
                else:
                    warnings.append(_("No purchase tax matches rate %s%%.") % tax_rate)
            commands.append((0, 0, line_values))
        return commands

    def _get_ocr_invoice_tax_rates(self, entities, warnings):
        rates = []
        for vat_entity in entities.get("vat", []):
            properties = self._group_ocr_entities(vat_entity.get("properties", []))
            rate = self._ocr_float(self._first_ocr_value(properties, "vat/tax_rate"))
            if rate and rate not in rates:
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
        normalized = re.sub(r"\s+", "", iban).upper()
        bank = supplier.bank_ids.filtered(
            lambda item: re.sub(r"\s+", "", item.acc_number or "").upper() == normalized
        )[:1]
        if bank:
            self.partner_bank_id = bank
        else:
            warnings.append(
                _("The extracted IBAN is not registered for the matched supplier.")
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
