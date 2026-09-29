Attach a PDF to a draft vendor bill and click **Extract PDF** in manual mode.
Automatic mode queues eligible incoming vendor-bill PDFs without requiring
the button. Structured UBL, CII, and Factur-X documents continue to use
Odoo's native decoder before Google Document AI is considered.

Extraction runs asynchronously. Use the **OCR Processing**, **OCR Review
Required**, and **OCR Failed** filters in the Bills list to monitor it. Review
the supplier, invoice reference and date, currency, purchase-order match,
bank account, lines, accounts, taxes, and totals before posting.

Documents with low-confidence or inconsistent data remain in draft with an
explanation. A detected pro-forma remains line-free and cannot be posted until
it is replaced by the final invoice or explicitly reclassified after review.
If the referenced purchase order is already represented by another active
vendor bill, the possible duplicate is linked and OCR lines are not created.

Keep automatic posting disabled during acceptance testing. Enable it only
after representative suppliers, tax rates, purchase orders, credit notes,
pro-formas, and duplicate documents have been tested with the pinned processor
version.
