# Copyright 2023 CreuBlanca
# Copyright 2023 ForgeFlow
# Copyright 2026 Roetz
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

{
    "name": "Account Invoice OCR Google Document AI",
    "summary": "Extract vendor bills with Google Document AI",
    "version": "18.0.1.0.0",
    "category": "Accounting/Accounting",
    "license": "AGPL-3",
    "author": "CreuBlanca, ForgeFlow, Roetz, Odoo Community Association (OCA)",
    "website": "https://github.com/OCA/account-invoicing",
    "depends": ["account", "purchase", "queue_job"],
    "external_dependencies": {"python": ["google-cloud-documentai"]},
    "data": [
        "data/queue_job_data.xml",
        "views/res_config_settings_views.xml",
        "views/account_move_views.xml",
    ],
    "installable": True,
    "application": False,
}
