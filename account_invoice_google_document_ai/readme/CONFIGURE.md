1.  Enable the Document AI API in a Google Cloud project.
2.  Create an Invoice Parser processor, preferably in the `eu`
    multi-region.
3.  Create a dedicated service account and grant it
    `roles/documentai.apiUser`.
4.  Create a JSON key for that service account.
5.  Load `queue_job` as a server-wide module and configure its job
    runner.
6.  In *Accounting \> Configuration \> Settings*, select the company and
    configure the project, location, processor, exact processor-version ID,
    confidence threshold, and service-account JSON. The settings are
    company-specific. Pin a processor version that is available for the
    configured processor instead of relying on Google's default version.
7.  Click **Test Google Connection** before enabling extraction. The test
    sends a small one-page image to the configured processor and can incur
    one page of Document AI processing charges. Repeat this test whenever
    the processor version or credentials change.
8.  Keep automatic posting disabled until representative invoices have
    been validated.

The service-account private key is stored in the Odoo database and its
backups. Restrict administrator and backup access, use a dedicated
least-privilege service account, and rotate the key when access changes.
Record the accepted processor version in deployment documentation so every
environment uses the same extraction model deliberately.
