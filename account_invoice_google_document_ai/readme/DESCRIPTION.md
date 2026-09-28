This module extracts vendor bill data from PDF attachments with Google
Document AI.

It integrates with Odoo's native invoice attachment decoder so
structured UBL/CII/Factur-X documents keep using the native decoder
first. Ordinary PDFs are processed asynchronously with `queue_job`.
Extracted supplier, company, currency, tax, totals, invoice lines,
purchase order references, and bank details are checked before a bill is
considered ready.

Uncertain or inconsistent results remain in draft for review. Existing
invoice lines are never replaced by a repeated extraction.
