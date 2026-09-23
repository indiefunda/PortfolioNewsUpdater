# 📱 Reading your news from a phone (hosted, read-only)

The panel in `cloud_manager.py` is a **control plane** — it runs the updater,
uploads config, and deletes rows. It must never be the thing exposed to the
internet. This sets up a separate, read-only view of the archive behind a Google
sign-in, on a free Firebase Hosting URL.

```
  VM (cron)                     Firebase                      your phone
  ─────────                     ────────                      ──────────
  news.db ──► news_web.py ──►  Firestore  ──► Hosting page ──► Google sign-in
              --sync-firebase   (rules:        (index.html +    then read /
                                auth-only)      firebase-boot.js) filter
```

Nothing is exposed on the VM: no open port, no firewall change, no public URL
pointing at your server. The VM only makes **outbound** writes to Firestore.

## What already works (verified)

```bash
# A standalone page you can open locally right now, no Firebase at all:
python3 news_web.py --out=/tmp/newsite --embed
# -> /tmp/newsite/index.html  (open it directly; --embed makes file:// work)
```

Verified against the real 1,396-row archive: 8 tickers in the dropdown, ticker +
importance + sentiment + pushed filters, full-text search, "show more"
pagination, empty state on no match, and rows that expand to show the summary,
the **Chinese original title**, and *Original* / *Translate* links.

## Step-by-step (written for someone who has never opened Firebase)

Firebase is Google's app platform that bolts onto a Google Cloud project. You
use three parts of it, all with free tiers:

| Piece | What it does here |
|---|---|
| **Hosting** | serves `index.html` at `https://<project>.web.app` |
| **Authentication** | the "Sign in with Google" button |
| **Firestore** | the database the page reads your news from |

> Console labels drift over time. If a button is named slightly differently,
> look for the same idea — the *order* and the *decisions* below are what matter.

### Step 0 — Try the page first, without Firebase (2 minutes)

Do this before investing any time. It proves the page is what you want:

```bash
python news_web.py --out=web_local --embed
start web_local\index.html          # Windows; or just double-click the file
```

You get the full view — filters, search, expandable rows. No account, no cloud.

### Step 1 — Add Firebase to your EXISTING Google Cloud project

Go to <https://console.firebase.google.com/> and click **Create a project**.

The first field is the project name and it has a **dropdown**. Open it and
**select `keen-wavelet-275120`** — do not type a new name.

This is the whole reason the setup is simple: Firebase attaches to the project
your VM already lives in, so the VM's own service account can write to Firestore
and **no key file ever exists on the server**. Create a separate project and
you'd have to download and guard a credentials file instead.

Next: Google Analytics → **turn it off** (not needed). Then **Create project**.

**Check it worked:** the project picker in the top bar says
`keen-wavelet-275120`.

### Step 2 — Create the Firestore database

Left sidebar → **Build** → **Firestore Database** → **Create database**.

- **Location:** choose **`us-east1`** (same region as your VM).
  ⚠️ **This is permanent.** It cannot be changed later without deleting the
  database.
- **Security rules:** choose **Production mode**. Do *not* choose "test mode" —
  test mode leaves the database open to anyone for 30 days.

**Check it worked:** the Firestore page shows an empty **Data** tab.

### Step 3 — Turn on Google sign-in

Left sidebar → **Build** → **Authentication** → **Get started**.

Go to the **Sign-in method** tab → click **Google** → toggle **Enable** → pick a
support email → **Save**.

**Check it worked:** Google shows as *Enabled* in the list.

### Step 4 — Register a web app and copy its config

1. Gear icon (top-left) → **Project settings** → **General** tab.
2. Scroll to **Your apps** → click the **`</>`** (Web) button.
3. Nickname: `news-reader`. **Leave "Also set up Firebase Hosting" UNCHECKED** —
   we deploy from a config file instead, and ticking it creates a setup we don't
   want.
4. **Register app**.
5. It shows a block of code beginning `const firebaseConfig = {`. Copy **just
   the `{ ... }` object** into a new file `firebase-config.json` in this folder:

```json
{
  "apiKey": "AIza...",
  "authDomain": "keen-wavelet-275120.firebaseapp.com",
  "projectId": "keen-wavelet-275120",
  "appId": "1:1234567890:web:abcdef"
}
```

The other keys it shows (`storageBucket`, `messagingSenderId`) are harmless —
include them or not.

This config is **public by design**; it ships inside the page. Access is
controlled by the Firestore rules, not by hiding these values.

⚠️ Do **not** paste a **service-account** key here. That is a different file with
`"type": "service_account"` and a `private_key`, and putting it in a web page
would leak write access to your database. The build now refuses it.

### Step 5 — Let the VM write to Firestore

**There are TWO gates here, and the second one is the one that actually
blocks you.** This section originally covered only the first, which cost real
debugging time.

**Gate 1 — IAM (what the identity may do).**

```bash
gcloud compute instances describe stock-monitor --zone=us-east1-b \
  --format="value(serviceAccounts[0].email)"
```

On this project the answer is `609593941215-compute@developer.gserviceaccount.com`
— the **default Compute Engine service account**. Check what it already has:

```bash
gcloud projects get-iam-policy keen-wavelet-275120 \
  --flatten="bindings[].members" \
  --filter="bindings.members:609593941215-compute@developer.gserviceaccount.com" \
  --format="value(bindings.role)"
```

It already holds `roles/editor`, which **includes Firestore read/write**, so no
role change is needed here. (If yours comes back empty, grant
`roles/datastore.user` as originally written.)

**Gate 2 — OAuth access scopes (what the token may request).** This is the one
that bites. A GCE instance gets a fixed set of scopes at creation, and IAM being
permissive means nothing if the token cannot carry the scope. The default set is:

```
devstorage.read_only, logging.write, monitoring.write, pubsub,
service.management.readonly, servicecontrol, trace.append
```

**No `datastore`.** So every Firestore call fails with:

```
reason: "ACCESS_TOKEN_SCOPE_INSUFFICIENT"
```

Fix it by changing the instance's scopes, which **requires stopping the VM**:

```bash
gcloud compute instances stop stock-monitor --zone=us-east1-b --quiet

gcloud compute instances set-service-account stock-monitor --zone=us-east1-b \
  --service-account=609593941215-compute@developer.gserviceaccount.com \
  --scopes=https://www.googleapis.com/auth/datastore,https://www.googleapis.com/auth/devstorage.read_only,https://www.googleapis.com/auth/logging.write,https://www.googleapis.com/auth/monitoring.write,https://www.googleapis.com/auth/pubsub,https://www.googleapis.com/auth/service.management.readonly,https://www.googleapis.com/auth/servicecontrol,https://www.googleapis.com/auth/trace.append

gcloud compute instances start stock-monitor --zone=us-east1-b --quiet
```

Notes:

- Add `datastore` to the existing list rather than switching to `cloud-platform`.
  `cloud-platform` is broader than this job needs.
- **The external IP changes** on restart (it is ephemeral). Nothing in this
  project depends on the IP — the panel and these docs reach the VM by name — but
  note the new one if you had it written down.
- **SSH will prompt about an unknown host key** after the restart. Accept it, or
  pass `--strict-host-key-checking=no` to `gcloud compute ssh`.
- Check the time first: the news cron jobs run `1-5` (Mon–Fri) at 13:15/21:00 UTC
  in summer. A weekend is a free window.

### Step 6 — Push the archive to Firestore

Run this **on the VM** (it reads the VM's own metadata server for credentials):

```bash
python3 news_web.py --sync-firebase --project=keen-wavelet-275120 --dry-run
```

Expect a line like `1396 row(s): 1396 to write, 0 to delete, 0 unchanged`. The
dry run needs no credentials and writes nothing. Then do it for real:

```bash
python3 news_web.py --sync-firebase --project=keen-wavelet-275120
```

**Check it worked:** Firestore → **Data** shows a `news` collection with ~1,400
documents and a `meta` collection with one `status` document.

### Step 7 — Install the Firebase CLI and deploy

On your Windows machine:

```bash
npm install -g firebase-tools
firebase login          # opens a browser; use the same Google account
firebase --version      # sanity check
```

Then, in this project folder:

```bash
python news_web.py --out=web_public --hosted --firebase-config=firebase-config.json
firebase deploy --only firestore:rules,hosting
```

`.firebaserc` is already committed, so `firebase deploy` knows which project to
use — no `firebase init` and no `firebase use` needed. The command prints your
Hosting URL when it finishes.

### Step 8 — Open it

Go to `https://keen-wavelet-275120.web.app`, click **Sign in with Google**, and
the archive loads. Bookmark it on your phone's home screen.

### Keeping it fed (optional, after the first deploy works)

Add **two** cron lines on the VM so the archive stays current. Keep them separate
from the four news cron jobs — a failure here must never affect your digest:

```bash
crontab -e
# ~15 minutes after the two news runs (EDT/UTC values):
30 13 * * 1-5 cd /home/Achilles && /usr/bin/python3 news_web.py \
  --sync-firebase --project=keen-wavelet-275120 >> news_web.log 2>&1
15 21 * * 1-5 cd /home/Achilles && /usr/bin/python3 news_web.py \
  --sync-firebase --project=keen-wavelet-275120 >> news_web.log 2>&1
```

**Two lines, not four.** Unlike `news_updater.py`, the sync has no schedule
guard, so adding the winter pair as well would make it run four times a day. It
does not need the guard: firing an hour late in winter is irrelevant for a data
sync, and a repeat sync is nearly free anyway — the manifest means unchanged rows
are never rewritten, so a no-op sync writes exactly one small document.

### If something goes wrong

| Symptom | Cause and fix |
|---|---|
| "Firebase config missing" on the page | `firebase-config.json` is empty or malformed — rebuild with step 7 |
| Blank page; console says `auth/unauthorized-domain` | Add the domain: Authentication → Settings → **Authorized domains** |
| "Missing or insufficient permissions" after signing in | Rules not deployed — `firebase deploy --only firestore:rules` |
| Sync fails with `403 PERMISSION_DENIED` | Step 5 gate 1: no Firestore role on the service account |
| **Sync fails with `ACCESS_TOKEN_SCOPE_INSUFFICIENT`** | **Step 5 gate 2: the VM's OAuth scopes lack `datastore`. IAM alone is not enough — the instance must be stopped to change scopes** |
| Sync fails: cannot reach the metadata server | You ran it off the VM. It must run on the GCP VM |
| Page shows only "Signed out." | Google provider not enabled (step 3) |
| `firebase: command not found` | Reopen the terminal after `npm install -g` |
| Firestore asks you to upgrade to Blaze | Hosting, Auth and Firestore all have free tiers and this design stays inside them. If it insists, Blaze with a **$0 budget alert** is safe — but check before adding a card |

## Cost — and why "Blaze" does not mean "you get billed"

Adding Firebase to a project that already has billing enabled forces the Blaze
(pay-as-you-go) plan; Firebase cannot put a billed project on Spark. That sounds
alarming, so here is the part that matters, quoted from the pricing page:

> **"No-cost usage from Spark plan included"** — Blaze keeps the same no-cost
> tier as Spark, calculated **daily**.

So Blaze is not "you pay per request". It is "you have the free tier, and only
usage *above* it is billed". The no-cost tier, per project per day:

| Service | No-cost allowance | What this project uses |
|---|---|---|
| Firestore document writes | 20,000 / day | ~1,400 on a cold sync, ~1 per sync after (the manifest skips unchanged rows) |
| Firestore document reads | 50,000 / day | 1 per visit (cached), ~1,400 on a cold load |
| Firestore document deletes | 20,000 / day | 0 normally |
| Firestore stored data | 1 GiB | ~1.4 MB — about 0.14% |
| Hosting data transfer | 360 MB / day | ~15 KB per page load |
| Hosting storage | 10 GB | under 100 KB |
| Authentication (Google sign-in) | 50K monthly active users | 1 |

Two things make the write figure small: the sync keeps a manifest, so it only
writes rows whose content actually changed, and a no-op sync writes just the one
tiny `meta` document. You would need more than a dozen *full* re-syncs in a day
to exhaust the write allowance.

The Firebase JavaScript SDK is served from Google's CDN (`gstatic.com`), not from
your Hosting bucket, so it does not count against the 360 MB/day transfer.

### Confirming it yourself

- **Firestore → Usage** in the Firebase console shows today's reads/writes/deletes
  against the limits, in real time.
- **GCP console → Billing → Reports**, filtered to Firestore/Hosting, shows the
  actual charge — expected to be zero.
- **Billing → Budgets & alerts**: set a small budget. Be aware this *alerts*; a
  Google Cloud budget is not a hard spending cap.

### If you would rather not have a payment method involved at all

Two alternatives, both covered earlier in this repo's discussion:

1. **A separate Firebase project with no billing** (true Spark). Free, no card —
   but the VM's service account belongs to this project, so the VM would need a
   service-account key file for the new one. That is a long-lived credential on
   the server, which is exactly what the same-project approach avoids.
2. **Skip Firebase entirely and use Tailscale** — a private network between the
   VM and your phone. No cloud database, no card, no Firebase account: Tailscale
   *is* the authentication. You lose the shareable public link and must install
   Tailscale on each device. `news_web.py` would need a small `--serve` mode.

## Status: deployed and live

`https://keen-wavelet-275120.web.app` is up. What is actually confirmed:

**Verified — the live site:**
- `index.html` and `firebase-boot.js` both serve 200 with correct content types
- the page carries the config, sets `__GL_HOSTED__`, and embeds **no** data
  (12 KB — the whole archive lives in Firestore)
- the three security headers from `firebase.json` are present
- **anonymous access is refused: `403` on both `news` and `meta`** — the rules
  really are doing the work, not just hiding the UI
- **the sign-in gate renders**: after a real 15-second wait the page reports
  `signinGate: true`, `signInButton: true`, `stats: "Signed out."` and a visible
  "Sign in with Google" button

**Verified — the data path:**
- the sync committed **850 rows + 1 meta document** (851 operations, in two
  batches of the 500 the API allows)
- read back over REST: `meta/status` says `count 850, pushed 86`, and an
  aggregate count confirms **850 documents** in `news`
- a repeat sync is a **no-op**: `0 to write, 0 to delete, 850 unchanged`, one
  operation committed
- the sync is on cron, twice daily, alongside the untouched news jobs

**Still not verified:** the Google sign-in round trip and the authenticated read,
because they need a human to complete an OAuth consent screen. Everything up to
that boundary is confirmed.

### A trap worth recording

Do **not** test this page with Chrome's `--virtual-time-budget`. Firebase Auth
resolves its initial state via IndexedDB and timers, and virtual time breaks
that: `onAuthStateChanged` never fires and the page appears permanently stuck on
"Loading…" with no error. That is a **false negative** — I chased it for a while
before switching to `_audit/cdp_probe.js`, which drives a real clock over the
DevTools protocol. Use that instead.

## Security notes

- The rules are **default-deny**: `news` and `meta` are readable when signed in,
  nothing else is reachable, and no client can ever write.
- The VM writes with an IAM service-account token, which bypasses rules by
  design. Note that this project's VM service account holds the GCP default
  `roles/editor`, which is broader than this job needs — narrowing it to
  `roles/datastore.user` would be a genuine hardening step, but check nothing
  else on the VM relies on `editor` first.
- The page is read-only. There is no delete, no "run now", no config upload —
  those stay in the local panel where they belong.
- If you later want the phone to *act* (mark read, delete), that needs new
  authenticated write rules and a different review. Do not add them casually to
  these rules.
