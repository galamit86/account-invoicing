1.  Enable the Document AI API in a Google Cloud project.
2.  Create an Invoice Parser processor, preferably in the `eu`
    multi-region.
3.  Create a dedicated service account and grant it
    `roles/documentai.apiUser`.
4.  Create a JSON key for that service account.
5.  Load `queue_job` as a server-wide module and configure its job
    runner.
6.  In *Accounting \> Configuration \> Settings*, configure the project,
    location, processor, optional pinned processor version, confidence
    threshold, and upload the service-account JSON.
7.  Click **Test Google Connection** before enabling extraction.
8.  Keep automatic posting disabled until representative invoices have
    been validated.

The service-account private key is stored in the Odoo database and its
backups. Restrict administrator and backup access, use a dedicated
least-privilege service account, and rotate the key when access changes.
