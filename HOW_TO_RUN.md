# How to run this app

Takes **one image of a purchase order** and produces a **saved, verified Order + linked
Invoice inside Fakturama**, creating the customer, products and tax rates it needs
along the way.

Everything below has been run on this machine. Where something is fragile, it says so.

---

## 1. What you need first

| Requirement | Notes |
|---|---|
| **Windows** | The automation drives Fakturama's real UI through Windows UI Automation. It cannot run on macOS or Linux. |
| **Fakturama installed** | Tested against **1.6.9** at `C:\Program Files (x86)\Fakturama\Fakturama.exe`. If yours lives elsewhere, set `FIC_FAKTURAMA_EXE` in `.env`. |
| **Python 3.10+** | This venv runs 3.10.2. |
| **A Gemini API key** | Free tier is fine — see the model note in step 3. |

---

## 2. One-time setup

This project uses **[uv](https://docs.astral.sh/uv/)**. One command builds the whole
environment from `pyproject.toml` + `uv.lock`:

```bash
cd C:\Users\hp\Downloads\tjm\fakturama-image-to-cash

uv sync
```

That's it — uv creates `.venv`, installs every dependency at the exact locked version,
and installs this project so the **`fic`** command exists. Check it:

```bash
uv run fic --help
```

From here on, **prefix every command with `uv run`**. You never need to activate the
venv or install anything by hand.

> **If `uv` hangs or prints nothing:** your uv install is broken, not your project.
> That happened here — `uv.exe` was a truncated **538 KB** instead of ~41 MB, so
> `uv --version` hung forever and left zombie processes behind. Fix it:
>
> ```powershell
> # kill any stuck uv first, or the installer can't replace the file
> taskkill /F /IM uv.exe /T
> powershell -ExecutionPolicy Bypass -c "irm https://astral.sh/uv/install.ps1 | iex"
> uv --version      # should answer instantly
> ```
>
> A healthy `uv sync` on this project finishes in seconds.

<details>
<summary>Without uv (fallback)</summary>

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -e .
uv run fic --help
```

Note the pip route needs a recent pip — the old 21.x that ships with some Python
installs can't do editable installs with this project's build backend.
</details>

---

## 3. Configure `.env`

Copy the template and fill in your key:

```bash
copy .env.example .env
```

The settings that matter:

```ini
GEMINI_API_KEY=your-key-here      # required
FIC_PROVIDER=gemini               # which model reads the image: gemini | claude
FIC_GEMINI_MODEL=gemini-flash-latest
```

### Choosing the model — read this, it will save you time

Three real failures hit while setting this up, each with a different fix:

| What you'll see | Why | Fix |
|---|---|---|
| `404 NOT_FOUND ... no longer available` | Pinned Gemini versions get **retired**. `gemini-2.5-pro` already was. | Use a `-latest` alias. |
| `429 RESOURCE_EXHAUSTED` | The **pro** models exhaust the free tier almost immediately — one image was enough. | `FIC_GEMINI_MODEL=gemini-flash-latest` |
| `503 UNAVAILABLE ... high demand` | Google-side overload — the request never reached the model. Nothing is wrong with your image, key or config. | **Handled for you** — see below. |

The app prints a one-line explanation and a hint for each of these instead of a raw
stack trace.

### About the 503s specifically

`gemini-flash-latest` is a shared free-tier model and refuses requests when demand
spikes. The app now **retries automatically** with exponential backoff (2s, 4s, 8s,
16s) and only gives up after that. You'll see:

```
Gemini returned a transient error (attempt 1/5); retrying in 2s -- this is Google-side load, not your request
```

That's informational; let it run. A real measurement from this machine: four
consecutive 503s, then success on the fifth attempt — and the extraction was still
46/46 fields correct. Without the retry that would have been five manual re-runs.

Tune with `FIC_GEMINI_RETRIES` in `.env` (default `4`, set `0` to disable). Only
transient errors are retried — a bad key or a retired model still fails immediately
with its proper message, rather than making you wait through four pointless backoffs.

If it still fails after all attempts, Google is having a bad day: either wait, or
switch model with `FIC_GEMINI_MODEL=gemini-3.5-flash`.

### If it seems to hang on "reading the image"

It won't any more. Each request has a **90s ceiling** (`FIC_GEMINI_TIMEOUT`);
the SDK's own default is *unlimited*, which used to let a stalled request sit
silently forever. A hang is now treated as a transient failure and retried like
any other. You'll see the timing either way:

```
  reading the image with gemini-flash-latest (up to 90s per attempt, 5 attempts) ...
  attempt 1/5 failed after 91s (ReadTimeout); retrying in 2s -- transient
  image read in 63s
```

A vision call on a full-page order normally lands in 10-30s.

### If retries keep failing: it is probably the model, not you

`-latest` aliases track whatever Google currently points them at, and that target can
be badly overloaded while other models are completely fine. Measured on this machine,
minutes apart, using a trivial **text-only** request with no image at all:

| Model | Result |
|---|---|
| `gemini-flash-latest` | **503 UNAVAILABLE** after 10s |
| `gemini-3.5-flash` | OK in 1.0s |
| `gemini-3.1-flash-lite` | OK in 1.7s |

Because that test sends no image, a failure there rules out your picture, your key and
this project entirely. Diagnose it the same way:

```bash
uv run python -c "from dotenv import load_dotenv; load_dotenv(); from google import genai; print(genai.Client().models.generate_content(model='gemini-flash-latest', contents='Say OK').text)"
```

If that fails but another model succeeds, set the working one:

```ini
FIC_GEMINI_MODEL=gemini-3.5-flash
```

Switching took the same order from *three consecutive failures* to `image read in 17s`
on the first attempt.

**`gemini-flash-latest` is the default and is the right choice on a free key.** On the
sample order it extracted **46 of 46 fields identical** to the hand-checked reference,
so there's no accuracy penalty on this kind of document.

To switch models, edit `FIC_GEMINI_MODEL` in `.env` — no code change. Useful values:
`gemini-flash-latest`, `gemini-3.5-flash`, `gemini-pro-latest` (paid keys).

---

## 4. Where to put your invoice image

Put it in **`data/input/`**:

```
data/
  input/
    order_001.png        <-- the sample
    my_invoice.png       <-- yours goes here
```

`.png`, `.jpg` and `.jpeg` all work. The folder is only a convention — you can pass any
path — but keeping images there keeps runs tidy.

---

## 5. Run it

Start with a dry run. It calls the model and validates the arithmetic, but **touches
Fakturama not at all**:

```bash
uv run fic run data/input/my_invoice.png --dry-run
```

If that's clean, do the real thing:

```bash
uv run fic run data/input/my_invoice.png
```

Success looks like:

```
done — Order + Invoice saved and verified
```

### Other commands

```bash
# just read the image, write the JSON, touch nothing else
uv run fic extract data/input/my_invoice.png -o data/golden/my_invoice.json

# run from an already-extracted JSON — no API key, no model variance
uv run fic run --from-json data/golden/my_invoice.json

# use Claude instead of Gemini for one run
uv run fic run data/input/my_invoice.png --provider claude

# read the image with BOTH models and halt if they disagree on any money field
uv run fic run data/input/my_invoice.png --cross-check

# dump Fakturama's live UI tree (for debugging selectors)
uv run fic probe
```

`--from-json` replaces **only** the model call. The JSON still goes through the same
validation gate, so it skips the LLM, not the checks.

---

## 6. What actually happens

```
your image
  └─ Gemini reads it            → structured JSON
     └─ reconcile()             → arithmetic gate: lines, VAT, discount, shipping,
        │                         totals must all agree, or it STOPS here
        └─ Phase 1  open a New Order, set date / Cust.Ref / Net / With VAT
           └─ Phase 2  find the customer — create it if missing (incl. a separate
              │        delivery address), then re-select to prove the save
              └─ Phase 3  per item: find the product, create it (and its tax rate)
                 │        if missing, then set quantity and discount
                 └─ Phase 4  set shipping, verify Total Net / VAT / Total
                    │        against your document, then Save
                    └─ Phase 5  create the linked Invoice from the Order, set the
                                payment method, mark it paid if the document says
                                so, and Save
```

**Nothing advances on an assumption.** Every value written is read back from the UI and
compared. If Fakturama's totals disagree with your document, the run stops and tells
you both numbers instead of saving something wrong.

---

## 7. Where the output goes

Each run creates `runs/<timestamp>/` containing:

- `extraction.json` — exactly what the model read from your image
- `state.json` — every step taken, the order number, what was created vs reused
- `report.md` — human-readable summary

If a run fails, **read `state.json` first** — it shows precisely how far it got.

---

## 8. When it stops instead of finishing

Stopping is a feature here, not a crash. The most common reasons:

**"extraction failed to reconcile against printed totals"**
The model misread a number, or the document's own arithmetic doesn't add up. Compare
`runs/<ts>/extraction.json` against your image. Never "fixed" automatically — a
plausible-looking wrong number written into a financial record is the worst outcome.

**"order totals do not match the source document"**
Fakturama computed something different from your document. Both numbers are printed.
Usually a real data problem worth looking at.

**"There is already a document with the number: PO0000NN"**
Fakturama's number counter drifted out of step with its data. **This happens when
Fakturama is force-killed instead of closed properly.** Always close it with
**File → Exit**. If it's already happened, open Fakturama and fix the next number
under its number-range settings.

**"more than one existing contact matching this debtor"**
Two customer records genuinely match. A human has to pick, so the run stops rather
than guess which one to bill.

**Fakturama's own "Duplicate Contact" popup**
Its internal check (name + street) fired. The run stops rather than click through —
only you can tell whether it's the same customer or a coincidence.

---

## 9. Things that will bite you

1. **Close Fakturama with File → Exit.** Force-killing it desyncs its document-number
   counter and the *next* run fails on save. This cost real time to diagnose.
2. **Let Fakturama finish starting before running.** A cold start takes well over 30
   seconds; the window title appears long before the UI is usable. The app waits, but
   attaching to an already-running Fakturama is the reliable path.
3. **Don't use the mouse during a run.** It drives the real cursor and keyboard. A
   stray click can land a keystroke in the wrong field.
4. **One editor tab at a time.** The flow manages its own tabs; leaving unrelated
   editors open can confuse field lookups.

---

## 10. Known gaps

- **`Alias name` and `Discount`** on the customer's Miscellaneous tab aren't set — the
  fields weren't located in this build. Logged each run, never silently skipped.
- **Delivery address name handling:** the document gives one combined name, and it goes
  into the delivery *Company* field. Splitting it into first/last would be inventing
  structure the document doesn't have.
- **`--cross-check` needs both keys** (`ANTHROPIC_API_KEY` and `GEMINI_API_KEY`).

---

## 11. Quick reference

```bash
# setup, once
uv sync
copy .env.example .env          # then add GEMINI_API_KEY

# every run
uv run fic run data/input/my_invoice.png --dry-run   # check first
uv run fic run data/input/my_invoice.png             # do it

# tests (no GUI, no network, ~2s)
uv run pytest -q
```

**Want to know what actually happens during a run?**
[HOW_IT_WORKS.md](HOW_IT_WORKS.md) walks the code from `uv run fic run <image>` to the
last saved record — the two validation tiers, the five UI phases, and where each
safety check sits.

For the engineering detail — every bug found against the real app and why the code is
shaped the way it is — see [README.md](README.md).
