# Copyright 2023 CreuBlanca
# Copyright 2023 ForgeFlow
# Copyright 2026 Roetz
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from decimal import Decimal

from google.api_core import exceptions as google_exceptions
from google.api_core.client_options import ClientOptions
from google.cloud import documentai_v1 as documentai

from odoo import models


class AccountInvoiceGoogleDocumentAI(models.AbstractModel):
    _name = "account.invoice.google.document.ai"
    _description = "Google Document AI invoice extraction service"

    def _process_document(self, attachment, company):
        endpoint = f"{company.invoice_ocr_google_location}-documentai.googleapis.com"
        client = documentai.DocumentProcessorServiceClient(
            client_options=ClientOptions(api_endpoint=endpoint),
        )
        if company.invoice_ocr_google_processor_version:
            processor_name = client.processor_version_path(
                company.invoice_ocr_google_project,
                company.invoice_ocr_google_location,
                company.invoice_ocr_google_processor,
                company.invoice_ocr_google_processor_version,
            )
        else:
            processor_name = client.processor_path(
                company.invoice_ocr_google_project,
                company.invoice_ocr_google_location,
                company.invoice_ocr_google_processor,
            )
        request = documentai.ProcessRequest(
            name=processor_name,
            raw_document=documentai.RawDocument(
                content=attachment.raw,
                mime_type=attachment.mimetype,
            ),
        )
        response = client.process_document(request=request, timeout=120)
        return self._normalize_document(response.document)

    def _normalize_document(self, document):
        return {
            "text": document.text or "",
            "entities": [
                self._normalize_entity(entity) for entity in document.entities
            ],
        }

    def _normalize_entity(self, entity):
        normalized = entity.normalized_value
        normalized_value = self._normalized_value(normalized) if normalized else False
        money = (
            normalized.money_value
            if normalized
            and normalized._pb.WhichOneof("structured_value") == "money_value"
            else False
        )
        return {
            "type": getattr(entity, "type_", False) or getattr(entity, "type", False),
            "text": entity.mention_text or "",
            "normalized": normalized_value,
            "currency": money.currency_code if money else "",
            "confidence": entity.confidence or 0.0,
            "properties": [
                self._normalize_entity(property_entity)
                for property_entity in entity.properties
            ],
        }

    def _normalized_value(self, normalized):
        value_type = normalized._pb.WhichOneof("structured_value")
        if value_type == "money_value":
            money = normalized.money_value
            value = Decimal(money.units) + Decimal(money.nanos) / Decimal(10**9)
            return format(value, "f")
        if value_type == "integer_value":
            return normalized.integer_value
        if value_type == "float_value":
            return normalized.float_value
        if value_type == "date_value":
            date = normalized.date_value
            return f"{date.year:04d}-{date.month:02d}-{date.day:02d}"
        return normalized.text or ""

    def _is_retryable_error(self, error):
        return isinstance(
            error,
            google_exceptions.DeadlineExceeded
            | google_exceptions.InternalServerError
            | google_exceptions.ResourceExhausted
            | google_exceptions.ServiceUnavailable,
        )
