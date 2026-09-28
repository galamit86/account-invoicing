# Copyright 2023 CreuBlanca
# Copyright 2023 ForgeFlow
# Copyright 2026 Roetz
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    invoice_ocr_google_mode = fields.Selection(
        related="company_id.invoice_ocr_google_mode",
        readonly=False,
    )
    invoice_ocr_google_project = fields.Char(
        related="company_id.invoice_ocr_google_project",
        readonly=False,
    )
    invoice_ocr_google_location = fields.Selection(
        related="company_id.invoice_ocr_google_location",
        readonly=False,
    )
    invoice_ocr_google_processor = fields.Char(
        related="company_id.invoice_ocr_google_processor",
        readonly=False,
    )
    invoice_ocr_google_processor_version = fields.Char(
        related="company_id.invoice_ocr_google_processor_version",
        readonly=False,
    )
    invoice_ocr_confidence_threshold = fields.Float(
        related="company_id.invoice_ocr_confidence_threshold",
        readonly=False,
    )
    invoice_ocr_auto_post = fields.Boolean(
        related="company_id.invoice_ocr_auto_post",
        readonly=False,
    )
