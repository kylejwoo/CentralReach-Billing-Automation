# Security and privacy

This repository must not contain real client, provider, claim, authorization, account, browser-session, or organization-identifying configuration data.

Production values belong only in the gitignored local files:

- `provider_location_mapping.json`
- `bulk_merge_config.json`

Before publishing a change, run `git status` and verify that screenshots, logs, browser profiles, spreadsheets, and production configuration files are not staged.

If sensitive information is ever committed and pushed, deleting it in a later commit is not enough because it remains in Git history. Rewrite the repository history or recreate the repository from a clean sanitized snapshot.
