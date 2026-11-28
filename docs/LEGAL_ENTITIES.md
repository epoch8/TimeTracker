# Legal entities: who bills whom

Two optional modules for businesses whose invoices are not "one company bills one client":

- **Legal entities** (`billing_entities`): the legal entities you invoice **from**. Each has its own details, payment accounts, invoice defaults and invoice numbering.
- **Client legal entities** (`client_entities`): the legal entities a client is billed **to** (Bill to), for clients that pay from several entities or switch entities over time.

Each module works on its own; together they fill the invoice's **Who bills whom** block: From, Currency, Pay to, Bill to.

With both modules off, invoices work as before: the seller is the company from Settings, the buyer is the client, and numbers come from Settings.

## Turning the modules on

**Admin → Modules**, section Finance:

| Module | Id | Needs |
|---|---|---|
| Legal entities | `billing_entities` | Invoices |
| Client legal entities | `client_entities` | Clients, Invoices |

Both are off in the setup presets except **Everything**. Switching a module off hides its pages and fields. Saved data stays, and invoices that already have an entity keep printing it.

## Our legal entities (the seller)

**Admin → Legal entities** lists the entities on the left and opens one on the right. Managing them needs the admin role or the `manage_settings` permission.

### Add an entity

**Add entity** opens its own page:

- **Short name**: shown in lists and in the invoice form.
- **Legal name**: printed on invoices.
- Country (two letters, ISO 3166), registration number, VAT number, address, e-mail, phone, website.
- **Logo**: printed instead of the logo from Settings.
- **Number format**: the format of the entity's default series (see [Invoice numbering](#invoice-numbering)).

After the entity is created, a step offers to add its first payment account. **Skip, add later** is fine.

### Payment accounts

One account is one IBAN in one currency. If one IBAN takes USD and EUR, add two accounts.

- **Bank** is a short label for lists.
- **Pay to, as printed** is the payment text typed by hand (bank, SWIFT, beneficiary, intermediary banks). `{IBAN}` in it is filled in from the IBAN field.

An account can be archived. It can be deleted while no invoice uses it.

### Invoice defaults

Filled into a new invoice from this entity, and still editable on the invoice:

- VAT category, tax rate, VAT exemption code and reason;
- terms, printed at the bottom of the invoice;
- **Invoice template**: by default the entity follows the template for all entities (Settings → Invoice Defaults). Pick one only if this entity needs its own.

**Save changes** at the bottom of the page saves the details and invoice defaults. The **Invoice numbering** block has its own **Save numbering** button.

### Archive and delete

An entity can be deleted until an invoice uses it. After that, archive it: an archived entity is not offered for new invoices, and its invoices keep it.

## Client legal entities (Bill to)

With `client_entities` on, the client page has a **Legal entities** card. Each entity has:

- legal name, registration number, VAT number, country, address;
- **E-mail for invoices**;
- a status (active or archived).

Like the seller's entities, a client entity can be deleted until an invoice uses it, and archived after that. A client without legal entities is billed with its own details, as before.

## On the invoice: Who bills whom

Step 1 of a new invoice and the draft show **Who bills whom**:

- **From**: one of our active legal entities, or the company from Settings. It defaults to the entity of the client's last invoice.
- **Pay to**: the active accounts of the From entity. Picking one sets the invoice currency, and changing the currency picks the only account in it.
- **Bill to**: the client's active legal entities, or "Client details". It defaults to the Bill to of the client's latest invoice, else the client's only entity. Picking an entity fills the client name, e-mail and address (still editable).

The invoice keeps a copy of both sides:

- the seller's details and payment text (`seller_snapshot`, `payment_details_text`);
- the Bill to entity (`buyer_snapshot`, also offered in the PDF template editor).

A draft refreshes both copies each time it is saved. Once the invoice leaves draft, it keeps its copies, so editing an entity later does not change issued invoices. The built-in PDF layouts print the Bill to registration number and VAT number under the client name.

## Invoice numbering

With `billing_entities` on, each of our legal entities numbers its invoices by **series**. The **Invoice numbering** block is at the bottom of the entity's page.

### Two numbers

Each invoice from an entity has two numbers:

- **Number** (`invoice_number`) is unique across all invoices. E-invoices, exports, the PDF file name and the API use it, e.g. `ACME-2026-20`.
- **Printed as** (`printed_number`) is what the client sees on the PDF, in e-mails, in the client portal and on the pay page, e.g. just `20`. Without one, the number is printed.

`Invoice.display_number` gives the printed number, else the number. Invoice templates use `invoice.display_number`.

### Which series an invoice takes

1. The client's series for the invoice's Bill to (with `client_entities` on).
2. Else the client's series for any legal entity.
3. Else the entity's default series.

Only active series are picked. In the **Series by client** table, the **Used for** column says in words which invoices each series takes.

With `client_entities` off, invoices do not name the client's legal entity, so each client has one series in use. A series made for one legal entity of a client waits until the module is on again.

### Tokens

Example: an invoice for December 2026, issued on 05/01/2027, the 20th invoice of its series.

| Token | What it puts in | Example |
|---|---|---|
| `{SEQ}` | The running number of the series: 1, 2, 3. Each series counts on its own | `20` |
| `{YYYY}` | The year of the month the invoice bills. With **Restart every year**: the year of the issue date | `2026` (`2027` with a restart) |
| `{YY}` | The same year, two digits | `26` |
| `{MM}` | The month the invoice bills | `12` |
| `{DD}` | The day of the issue date | `05` |
| `{NUMBER}` | **Printed as** only: the whole invoice number | `ACME-2026-20` |

The month the invoice bills is the invoice's period, else the month before the issue date.

`ACME-{YYYY}-{SEQ}` gives `ACME-2026-20`. Printed as `{SEQ}` gives `20`; `{NUMBER}` gives `ACME-2026-20`.

### The default series

The default series numbers invoices of clients with no series of their own:

- **Number format** and **Printed as**.
- **Restart every year**: `{SEQ}` starts again at 1 every year. The year is the issue date's year, for the counter and for `{YYYY}`; `{MM}` is not allowed. The format needs `{YYYY}` or `{YY}`.
- **Next {SEQ}**: where the counter goes on. With a restart, there is one field per year: this year and the next, and in January also the last one. The years follow today's date.

Only the default series can restart every year.

### Series by client

**Add series** makes a series for one client:

- **Client**, and with `client_entities` on, **Bill to**: one of the client's legal entities, or **Any legal entity of this client**.
- **Number format** (it needs `{SEQ}`), **Printed as**, **Next {SEQ}**.
- **Status**: an archived series is not picked, and its numbers stay on their invoices.

A client may have one active series per Bill to, and one for any legal entity.

### What the forms refuse

- A format that another active series already makes, of this or another legal entity. Numbers are unique across invoices, so add something that tells them apart.
- A **Next {SEQ}** that is not above the last number the series gave on an issued invoice. The form offers **Use N**.
- A **Next {SEQ}** whose number another invoice already has, a draft with a number set by hand included. The form offers **Use N** with the next free number.

Two formats that can make the same number (for example `CL-{SEQ}` and `CL-{YYYY}-{SEQ}`) get a warning only. When that happens, the next free number is taken.

### When the number is given

A draft from an entity with series is `DRAFT-<id>`. It gets the series' next free number when it leaves draft, on every path that changes the status: Mark as issued, Send, payments, Peppol, SDI, bulk actions, approval and the API. A deleted draft leaves no gap.

- Step 1 and the draft show **Will be ACME-20 · assigned when issued**. Lists show "Draft · will be ACME-20".
- **Set number now** sets a number by hand. Its `{SEQ}` is taken when the invoice is issued; the counter only moves forward. When another draft is issued, a number held by such a draft is skipped and named.
- **Assign from the series when issued** hands a number set by hand back to the series. In the invoice list, the same action works for selected drafts, with a preview.
- Sending a draft (e-mail, Peppol, SDI) issues it and commits its number first, then sends. If sending fails, the invoice stays issued with its number.
- The client never sees `DRAFT-…`. A draft of our legal entity notifies the client when it is issued, not when it is created: with `DRAFT-`, a number set by hand or an older number alike. The notification carries the printed number. A draft deleted before that notifies nobody.

A draft made before the entity had series is issued with the number it has.

### A printed number the client already has

If another invoice of the same entity and client (not cancelled) already has this printed number, issuing asks first. The same Bill to counts when both invoices have one, and the same year for a series that restarts every year.

- The draft shows a warning.
- **Mark as issued** and **Send** ask **Issue anyway** or **Change number**. Escape or a click outside closes the question and nothing happens.
- The API (`send-email`, `send-peppol`, `send-sdi`) answers `409 duplicate_printed_number`. Send again with `confirm_duplicate_printed_number=<the printed number>` to issue anyway.
- A bulk status change skips that invoice and lists it as not issued.
- In the interface, a payment on a draft, Peppol and SDI refuse with "issue it with Mark as issued first".

A confirmed repeat is written to the invoice's audit trail.

### Cancel and reissue

The new draft keeps the printed number and the series, and gets the first free suffix of the original number: `ACME-20-2`, then `ACME-20-3`. A reissue whose Bill to takes another series gets that series' number when it is issued.

### Without series

- An entity with no series rows numbers as before: its number format and counter, when the draft is created.
- With `billing_entities` off, numbers come from Settings.
- If the module is switched off while drafts are `DRAFT-…`, they get the Settings number when issued.

## Known limits

- **E-invoices take the seller from Settings.** Peppol (UBL), FatturaPA / SDI and the Factur-X / ZUGFeRD XML inside the PDF use the company details from Settings, not the invoice's From entity. The PDF itself prints the entity.
- **E-invoices take the buyer's tax details from the client**, not from the Bill to entity.
- **Change an invoice's status through the object.** Numbers are given in a `before_flush` hook, which does not see bulk `UPDATE` statements.

## For developers

| What | Where |
|---|---|
| Seller entities, payment accounts | `app/models/billing_entity.py` (`billing_entities`, `billing_entity_payment_accounts`) |
| Client entities | `app/models/client_entity.py` (`client_entities`) |
| Number series, counters | `app/models/billing_entity_number_series.py` (`billing_entity_number_series`, `billing_entity_number_counters`) |
| Invoice columns | `billing_entity_id`, `payment_account_id`, `client_entity_id`, `seller_snapshot`, `buyer_snapshot`, `payment_details_text`, `number_series_id`, `printed_number`, `printed_number_edited` |
| Series lookup, numbering hook, form checks | `app/utils/billing_entities.py` (`resolve_series`, `numbering_before_flush`, `check_series`) |
| Bill to helpers | `app/utils/client_entities.py` |

How a number is given:

1. The hook takes the series counter row with `INSERT … ON CONFLICT DO NOTHING`, then locks it with `SELECT … FOR UPDATE` until the transaction ends. On SQLite this is best effort, and a locked database answers "try again".
2. It checks the repeated printed number before writing anything.
3. Numbering errors are `IssueNumberError` subclasses with a fixed, translated message. The exception text is never shown.

Migration `224_add_invoice_number_series` turns each entity's number format into its default series and keeps the counter. It also replaces `invoice.invoice_number` with `invoice.display_number` in saved invoice templates.
