# Where to start — deployment guide

You will run **two small services** on Railway from one GitHub repo:
1. **flask** (this folder's root) — the brain: timers, Groq, Zoho, Telegram, admin panel.
2. **bridge** (`bridge/` folder) — the WhatsApp link (a linked device on your old WhatsApp Business number).

Keep the bot **STOPPED** and **dry-run ON** until step 8. Nothing is created in Zoho and nothing is posted to WhatsApp in dry-run.

## 0. Before anything: two things in other apps
1. **Zoho:** create a customer named exactly `DEFAULT-CUSTOMER` (or choose another name and set `DEFAULT_CUSTOMER_NAME`). When the bot cannot tell which store a quote is for, it creates the quote under this customer, with a note on the quote. Give it the Place of Supply you want those quotes to carry (it decides CGST+SGST vs IGST).
2. **WhatsApp:** create your **sample group** (see step 4b) so you can test with a few people without touching the real 25-member group.

## 1. GitHub
1. Create a new empty **private** repo.
2. Unzip the project and upload everything (GitHub web: *Add file → Upload files*; drag the whole folder content). Do **not** upload any file containing real keys; the repo only has `.env.example`.

## 2. Railway — Flask service
1. Railway → *New Project → Deploy from GitHub repo* → pick the repo.
2. This first service uses the repo root. Railway reads the `Procfile` (gunicorn, **one worker** — keep it at one, the timer loop lives in it).
3. *Variables* tab: add every variable under "Flask service" in `.env.example`. `REDIS_URL` (your Upstash URL) is **required**: the app refuses to start without it, so the waiting queue can never be silently lost. Use long random values for `ADMIN_PIN`, `FLASK_SECRET_KEY`, `BRIDGE_SECRET`.
   - `OWNER_NUMBERS` = your own number (10 digits) so everything you post is ignored.
4. *Settings → Networking → Generate domain*. Open `https://<domain>/health` — you should see `{"ok": true}`.
5. Open `https://<domain>/admin`, log in with your PIN. It shows **STOPPED** (the safe default).

## 3. Railway — Bridge service
1. In the same project: *New → GitHub repo → same repo*, then in this service's *Settings* set **Root Directory = `bridge`**.
2. *Variables*: `FLASK_URL` (the Flask domain from step 2), `BRIDGE_SECRET` (same value as Flask), `AUTH_DIR=/data/auth`. Leave `WHATSAPP_GROUP_ID` empty for now.
3. *Settings → Volumes → add a volume mounted at `/data`* (keeps the WhatsApp login across restarts). If volumes are not available on your plan, tell me — there is a Redis alternative.
4. Generate a domain for the bridge too, then set `BRIDGE_URL` in the **Flask** service to it.

## 4. Link WhatsApp (old Business number)
1. On the phone with that number: *WhatsApp Business → Settings → Linked devices → Link a device*.
2. Railway → bridge service → *Deploy logs*: a **QR code** is printed. Scan it. The log says "WhatsApp connected".
3. The log then lists all groups with their ids. Copy the id of your 25-member group (looks like `1203630…@g.us`) into the bridge variable `WHATSAPP_GROUP_ID` and redeploy.
4. Keep the phone online (connected to the internet) — WhatsApp unlinks devices if the phone is offline for about two weeks.

## 4b. Use a sample group first
1. In WhatsApp create a new group, for example "Sample Quotation Group", and add the bot number (the old Business number) and a few testers. You can keep your own number in it too: your messages are ignored.
2. The bridge prints your groups in its logs: copy the **sample group's** id into `WHATSAPP_GROUP_ID` (not the real group's). The bot only ever reads and posts in that one group.
3. Ask the testers to send realistic messages: normal quotes, a store with no code, a store the bot cannot know, a PHD store without saying PHD, a total-only message, a message with measures ("10 meters"), a payment text, an edit, a delete, a photo/PDF (must be ignored).
4. Later, to switch to the real group: change `WHATSAPP_GROUP_ID`, redeploy the bridge, and go through steps 6 to 8 again.

**Careful when you move past dry-run in the sample group:** quotes are then created in your real Zoho and emails really go out if a message contains an email address. Ask testers to use only your own email address, and delete the test estimates in Zoho afterwards.

## 5. Check the link
- In `/admin` the line "WhatsApp bridge" should say **connected** after a minute.
- Post a test text in the group from **your own number**: it must be ignored (nothing appears in "Waiting").
- If it is NOT ignored, WhatsApp is hiding your number: add bridge variable `LOG_SENDERS=1`, post again, copy the `jid:` shown in the logs into the Flask variable `OWNER_JIDS`, redeploy, and remove `LOG_SENDERS`.

## 6. Start in dry-run
In `/admin`: confirm **Dry run: ON** and **Post PDF to WhatsApp group: OFF**, then tap **START bot**. From that moment every quote-like message gets a 3-minute timer; after it, Telegram shows what *would* be created.

## 7. What to watch during the first week (dry-run)
- Does each Telegram preview show the right customer, items and prices? Flags should be rare and sensible.
- Do edited / deleted messages cancel or reset pending quotes? (Unverified until real messages arrive.)
- Do "moosapet store"-type messages continue with the sender's brand?
- When the store is unclear, is the quote created under `DEFAULT-CUSTOMER` with the notice and the store name at the end of the notes, and does Telegram say so?
- Are new items added to the Zoho item list (live mode) with sensible HSN/SAC codes?
- Is your own number really ignored? Are attachments ignored?

## 8. Go live in stages
1. **Dry run OFF** (posting still OFF): quotes are created in Zoho and reported on Telegram. Check the first few in Zoho: the line description (e.g. "10 meters"), CGST+SGST vs IGST on one non-Telangana customer, the email.
2. **Post to group ON**: the PDF is posted in the group, one at a time with a small delay. Also Telegram always gets the result, so a WhatsApp problem never loses a quote.

## Costs and limits
- Railway: Hobby plan about $5/month; two always-on services should fit in or slightly above that. The trial credit is one-time.
- Upstash: the timer loop checks every 15 seconds (about 170k commands a month), within the free tier.
- This bridge uses an unofficial WhatsApp connection. It can break when WhatsApp changes, and the number could be banned. Telegram gets every result, so nothing is lost silently.
