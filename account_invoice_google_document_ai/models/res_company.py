# Copyright 2023 CreuBlanca
# Copyright 2023 ForgeFlow
# Copyright 2026 Roetz
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import fields, models


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
