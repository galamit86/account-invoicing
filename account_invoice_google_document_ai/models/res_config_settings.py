# Copyright 2023 CreuBlanca
# Copyright 2023 ForgeFlow
# Copyright 2026 Roetz
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import _, fields, models


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
    invoice_ocr_google_credentials = fields.Binary(
        related="company_id.invoice_ocr_google_credentials",
        readonly=False,
        groups="base.group_system",
    )
    invoice_ocr_google_credentials_filename = fields.Char(
        related="company_id.invoice_ocr_google_credentials_filename",
        readonly=False,
        groups="base.group_system",
    )
    invoice_ocr_confidence_threshold = fields.Float(
        related="company_id.invoice_ocr_confidence_threshold",
        readonly=False,
    )
    invoice_ocr_auto_post = fields.Boolean(
        related="company_id.invoice_ocr_auto_post",
        readonly=False,
    )

    def action_test_invoice_ocr_google_connection(self):
        self.ensure_one()
        self.env["account.invoice.google.document.ai"]._test_connection(self.company_id)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Google Document AI"),
                "message": _("The invoice processor connection succeeded."),
                "type": "success",
                "sticky": False,
            },
        }
