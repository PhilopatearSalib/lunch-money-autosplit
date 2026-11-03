# Changelog

## 2.0.0 (2026-11-02)

Moved to the Lunch Money v2 API (`https://api.lunchmoney.dev/v2`). Your `config.toml` and token keep working; nothing to change.

- Splits use the new `POST /transactions/split/{id}` endpoint.
- Already-split charges are detected the v2 way: the API returns the parts of a split (with `split_parent_id`) instead of the original, and those parts are skipped. Splits made by Lunch Money's own rules are skipped too.
- Transactions are fetched page by page, so long lookback periods return everything.
- Account names for `account_contains` come from the accounts endpoints, since v2 transactions carry only account IDs.
- Archived categories are no longer offered or matched.
- Clearer error messages, using the v2 error format.
- Charges with more than two decimal places are skipped with a warning instead of risking a rejected split.
- New tests for the v2 client: pagination, account names, the split request and error handling.

## 1.1.0 (2026-11-02)

Changes suggested in a review by Lunch Money's developer advocate.

- The `LUNCHMONEY_TOKEN` environment variable is now the recommended way to supply the token. Setup offers it first and sets it automatically on Windows; the plain-text token file stays as an optional convenience, with a clear warning.
- On Windows, setup offers to move an existing token file into the environment variable.
- `schedule` on Mac/Linux shows how to give cron the token.
- New `--no-log` option for `run` and `schedule`.
- README: new sections on keeping the token safe, what the log contains, Lunch Money's built-in split rules, and running the tests.
- Automated tests for matching, rounding, split calculations, the config file and the run loop, running on Windows, Mac and Linux via GitHub Actions.

## 1.0.1 (2026-11-02)

- Category search instead of printing every category; exact names win.
- The setup preview marks transactions that are already split.

## 1.0.0 (2026-11-02)

- First release.
