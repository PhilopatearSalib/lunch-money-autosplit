# Lunch Money Auto-Split

Automatically split recurring transactions in [Lunch Money](https://lunchmoney.app) into several categories.

Some bills arrive as one charge but cover several things: a phone and internet bundle, a warehouse-club run that is part groceries and part household, a utility bill shared between two budgets. Lunch Money lets you split a transaction by hand, but you have to redo it every month. This tool does it for you.

```
2026-10-22  ROGERS ******9211  135.60  [Rogers split] -> Phone 79.10 + Internet 56.50
    split done
```

**What it does**
- Finds charges that match your rules (by payee name, and optionally account and amount)
- Splits each one into the categories you chose, by percentage or fixed amounts
- Runs every day on its own once scheduled, and never touches a transaction that is already split

**Safe by default:** every run is a preview unless you add `--apply`, refunds, income and pending charges are left alone, and your access token stays on your own computer.

> **Lunch Money's built-in split rules** can also split a transaction by percentage (Setup → Rules → split). If they cover your case, use them: they run as soon as a transaction is imported. This tool is for when you want a preview of exactly what will change before anything is touched, a guided setup that works out the proportions from a real bill, or a log of every split.

---

## Before you start

You need:
- A Lunch Money account
- **Python 3.11 or newer.** Download it from [python.org/downloads](https://www.python.org/downloads/).
  On Windows, tick **"Add python.exe to PATH"** on the first screen of the installer.

To check that Python is installed, open a terminal and type `python --version` (on a Mac: `python3 --version`).

> **Mac users:** type `python3` wherever this guide says `python`.

---

## Step 1: Download the tool

1. On this page, click the green **Code** button, then **Download ZIP**.
2. Unzip it into a folder you'll keep, for example `Documents\lunch-money-autosplit`. Don't leave it in Downloads, because the daily run needs the folder to stay where it is.

## Step 2: Open a terminal in that folder

- **Windows:** open the folder in File Explorer, right-click an empty area and choose **Open in Terminal**.
- **Mac:** open Terminal, type `cd ` (with a space), drag the folder into the Terminal window, and press Enter.

## Step 3: Install the one requirement

```
python -m pip install -r requirements.txt
```

## Step 4: Get a Lunch Money access token

1. In Lunch Money, click the **gear icon** and open the **Developers** tab.
2. Type a name such as `autosplit`, choose **"I'm developing a personal project"**, and request a new access token.
3. Copy the token. Lunch Money shows it only once.

Keep the token private. Anyone who has it can see and change your budget. Don't paste it into chats or take screenshots of it.

## Step 5: Run the guided setup

```
python autosplit.py setup
```

The setup asks a few simple questions:

1. **Your token.** Paste it and press Enter. Nothing appears while you paste; that's normal. Then choose where to keep it:
   - **Environment variable `LUNCHMONEY_TOKEN`** (recommended). On Windows the setup sets it for you; on Mac and Linux it shows the line to add. See [Keeping your token safe](#keeping-your-token-safe).
   - **A token file in the tool's folder.** Convenient, but stored as plain text.
   - **Neither**, if you'd rather set it up yourself.
2. **Which charge to split.** Type text from the payee name, for example `ROGERS`. It shows matching transactions from the last few months so you can confirm it's the right one.
3. **How many categories,** and **how to divide the charge:**
   - **Copy a past bill** (recommended): enter how one real bill splits, for example Phone 79.10 and Internet 56.50. The same proportions are used every month, even if the total changes.
   - **Percentages:** for example 60% / 40%.
   - **Fixed amounts:** for example always 40.00 to Household, with the rest going to Groceries.
4. **Which categories.** Type part of a category's name, for example `phone`. Type `?` to see the full list.

Then it does a **dry run** that shows what it would split without changing anything, and asks whether to split those transactions now.

## Step 6: Make it automatic

At the end of setup, answer **yes** to "Run automatically every day?" and pick a time.

- **Windows:** a Task Scheduler task called "Lunch Money Auto-Split" is created for you. If your computer is off at that time, it runs as soon as it's back on.
- **Mac / Linux:** the tool prints the lines to paste into your crontab (`crontab -e`), including how to give cron your token.

You're done. New matching charges are split within a day of appearing in Lunch Money.

---

## Everyday commands

| Command | What it does |
|---|---|
| `python autosplit.py setup` | Add more rules (or start over) |
| `python autosplit.py run` | Preview what would be split; changes nothing |
| `python autosplit.py run --apply` | Split the matching transactions now |
| `python autosplit.py categories` | List your categories and their IDs |
| `python autosplit.py schedule --time 09:30` | Set or change the daily run |
| `python autosplit.py unschedule` | Stop the daily run |
| `python autosplit.py run --no-log` | Run without writing `autosplit.log` (also works with `schedule`) |

Each run writes its results to `autosplit.log` in the tool's folder. See [What the log contains](#what-the-log-contains).

## Editing rules by hand

Your rules are stored in `config.toml`. You can edit it in any text editor. [`config.example.toml`](config.example.toml) explains every option, including:

- `payee_contains`: words that must all appear in the payee name
- `account_contains`: limit a rule to one account
- `min_amount` / `max_amount`: limit a rule to a range of amounts
- `split_by`: `"percent"` or `"amount"`
- `value = "rest"`: the part that gets whatever is left

If more than one rule matches a transaction, the first rule in the file is used.

## Troubleshooting

**`python` is not recognized**
Python isn't installed or isn't on your PATH. Reinstall it and tick "Add python.exe to PATH", or try `py` instead of `python` on Windows.

**Category "…" was not found**
Run `python autosplit.py categories` and copy the name exactly as shown, or re-run `setup`.

**Lunch Money rejected the access token**
The token was revoked or mistyped. Create a new one (Step 4) and run `python autosplit.py setup` again.

**Could not reach Lunch Money / Read timed out**
Lunch Money's API was slow or unavailable. The tool retries automatically. If it still fails, the next daily run will catch up.

**A charge wasn't split**
Check that its payee contains all of your `payee_contains` words, that it's within `lookback_days` (30 by default), and that the bank account has synced in Lunch Money. Pending charges are skipped until they post, because their amount can still change.

## Keeping your token safe

Your access token gives full access to your Lunch Money budget, so treat it like a password.

**Recommended: the `LUNCHMONEY_TOKEN` environment variable.** The tool always uses it first if it's set.

- **Windows:** the setup can set it for you. To do it yourself, run this in PowerShell with your token in the quotes, then open a new terminal window:
  ```
  setx LUNCHMONEY_TOKEN "your-token"
  ```
  The Task Scheduler run uses it automatically.
- **Mac (zsh):** add this line to `~/.zshrc`, then open a new terminal:
  ```
  export LUNCHMONEY_TOKEN="your-token"
  ```
- **Linux (bash):** add the same line to `~/.bashrc`.
- **cron (Mac and Linux):** cron doesn't read those files. Add this line at the top of your crontab (`crontab -e`), above the schedule line. Your crontab is readable only by you.
  ```
  LUNCHMONEY_TOKEN="your-token"
  ```

**Optional: the token file.** If you choose the token file during setup, the token is saved as **plain text** in `.lunchmoney-token` next to the script. On Mac and Linux the file is made readable only by your user account. Anyone who can read the file can use your budget, so don't share the folder or commit the file. The included `.gitignore` already excludes it. On Windows, the setup offers to move an existing token file into the environment variable.

If a token is ever exposed, revoke it in Lunch Money under **Developers** and create a new one.

## What the log contains

`autosplit.log` records each run: the date, payee, amount, rule name and categories of every transaction it matched or split, plus any errors. That's useful for checking what the daily run did, but it's personal financial information.

- Keep the log private; it's excluded by `.gitignore`.
- It's capped at about 200 KB, plus one older copy (`autosplit.log.1`).
- To turn it off, add `--no-log`: `python autosplit.py run --apply --no-log`, or `python autosplit.py schedule --no-log` for the daily run.
- You can delete the log files at any time.

## Running the tests

The tests cover matching, rounding and the split calculations, plus the config file and the run loop. They use a fake Lunch Money API, so they need no account or internet connection:

```
python -m unittest discover -s tests -v
```

They also run automatically on Windows, Mac and Linux for every push (see the Actions tab).

## Removing it

1. `python autosplit.py unschedule`
2. Delete the folder.
3. In Lunch Money, open **Developers** and revoke the token.

---

## Notes

- Tested end-to-end with a real account on Windows 11. The automated tests also run on Mac and Linux, but daily runs with cron haven't been tried there yet; reports are welcome in the Issues tab.
- Uses the [Lunch Money Developer API v2](https://lunchmoney.dev) (`https://api.lunchmoney.dev/v2`). Version 1.x of this tool used the older v1 API.
- Planned: storing the token in Windows Credential Manager or the Mac Keychain.
- Requires Python 3.11+ and the `requests` package.
- Not affiliated with Lunch Money.

## Related

- [Lunch Money Dedupe](https://github.com/PhilopatearSalib/lunch-money-dedupe): find transactions you entered by hand that came in again from your bank, and merge or delete them. Uses the same `LUNCHMONEY_TOKEN`.

## License

MIT © 2026 Philopatear Salib
