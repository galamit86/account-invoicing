# Copyright 2023 CreuBlanca
# Copyright 2023 ForgeFlow
# Copyright 2026 Roetz
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import api, fields, models
from odoo.exceptions import ValidationError


class ResCompany(models.Model):
    _inherit = "res.company"

    invoice_ocr_google_mode = fields.Selection(
        [
            ("disabled", "Disabled"),
            ("manual", "Manual"),
            ("automatic", "Automatic"),
        ],
        default="disabled",
        required=True,
    )
    invoice_ocr_google_project = fields.Char()
    invoice_ocr_google_location = fields.Selection(
        [("eu", "Europe"), ("us", "United States")],
        default="eu",
        required=True,
    )
    invoice_ocr_google_processor = fields.Char()
    invoice_ocr_google_processor_version = fields.Char(
        help="Pin a processor version to keep extraction behavior stable.",
    )
    invoice_ocr_google_credentials = fields.Binary(
        string="Service Account JSON",
        attachment=False,
        copy=False,
        groups="base.group_system",
        help=(
            "Google service-account JSON used only for invoice extraction. "
            "The private key is stored in the database and its backups."
        ),
    )
    invoice_ocr_google_credentials_filename = fields.Char(
        string="Service Account Filename",
        copy=False,
        groups="base.group_system",
    )
    invoice_ocr_confidence_threshold = fields.Float(
        default=0.80,
        help="Critical fields below this confidence require review.",
    )
    invoice_ocr_auto_post = fields.Boolean(
        help=(
            "Allow native Odoo auto-posting after all OCR controls pass. The "
            "company and supplier native auto-post settings must also permit it."
        ),
    )

    @api.constrains("invoice_ocr_google_credentials")
    def _check_invoice_ocr_google_credentials(self):
        service = self.env["account.invoice.google.document.ai"]
        for company in self.filtered("invoice_ocr_google_credentials"):
            try:
                service._get_credentials(company)
            except ValueError as error:
                raise ValidationError(str(error)) from error
