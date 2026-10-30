# Security policy

## Reporting a vulnerability

Please do not open a public issue for a security problem.

Report it privately through GitHub: the repository's **Security** tab → **Report a vulnerability**. If that is not available to you, contact the maintainer on [LinkedIn](https://www.linkedin.com/in/rameesraja1992/).

You will get a reply within 7 days. Please include the affected file or component, what an attacker could do, and steps to reproduce. Leave out real tenant data.

## Supported versions

Only the `main` branch is supported. Fixes are not backported.

## Design notes

- **No secrets in Azure.** The deployed Function App authenticates with its managed identity, both to Fabric and to Azure resources. No client secret is stored in app settings or Key Vault.
- **The client secret is local only.** The service principal secret exists only in `research/.env` on your laptop, for local runs and research scripts. That file is git-ignored. Do not commit it or copy it into Azure.
- **Contributor on enrich workspaces.** The managed identity needs Contributor on the workspaces it enriches, so it can read item and run details. Grant it only on the workspaces you need.
- **Harvested data is sensitive.** The tables in Log Analytics and the raw files in storage include user names and SQL query text. Restrict access to the Log Analytics workspace and the storage account to the people who need it.
