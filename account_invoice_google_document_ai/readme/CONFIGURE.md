1.  Enable the Document AI API in a Google Cloud project.
2.  Create an Invoice Parser processor, preferably in the `eu`
    multi-region.
3.  Grant the Odoo runtime identity `roles/documentai.apiUser`.
4.  Configure Application Default Credentials for the Odoo process.
    Service-account private keys are intentionally not stored in Odoo.
5.  Load `queue_job` as a server-wide module and configure its job
    runner.
6.  In *Accounting \> Configuration \> Settings*, configure the project,
    location, processor, optional pinned processor version, and
    confidence threshold.
7.  Keep automatic posting disabled until representative invoices have
    been validated.
