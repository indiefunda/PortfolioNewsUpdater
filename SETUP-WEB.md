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

## Setup

### 1. Use your EXISTING Google Cloud project

Add Firebase to the same project that runs the VM (`keen-wavelet-275120`). This
matters: it means the VM's own service account can write to Firestore, so you
never create a key file.

Firebase console → **Add project** → choose the existing project.

### 2. Create Firestore

Firestore → **Create database** → **Native mode** → pick a region near you.
(Not "Datastore mode".)

### 3. Turn on Google sign-in

Authentication → **Get started** → Sign-in method → **Google** → Enable.
Under **Settings → Authorized domains**, `localhost` and your
`*.web.app` domain are added automatically.

### 4. Register a web app and copy its config

Project settings → **Your apps** → **Web** (`</>`) → register → copy the config
object into a file, e.g. `firebase-config.json`:

```json
{
  "apiKey": "AIza...",
  "authDomain": "your-project.firebaseapp.com",
  "projectId": "your-project",
  "appId": "1:123:web:abc",
  "storageBucket": "your-project.appspot.com",
  "messagingSenderId": "123"
}
```

These values are **public by design** — they ship in the page. Access is
controlled by the Firestore rules, not by hiding the config.

### 5. Let the VM write to Firestore

```bash
# The VM's service account email:
gcloud compute instances describe stock-monitor --zone=us-east1-b \
  --format='value(serviceAccounts[0].email)'

# Grant it Firestore write access:
gcloud projects add-iam-policy-binding keen-wavelet-275120 \
  --member="serviceAccount:THE_EMAIL_FROM_ABOVE" \
  --role="roles/datastore.user"
```

### 6. Deploy the rules and the page

```bash
npm install -g firebase-tools
firebase login
firebase use keen-wavelet-275120

# Build the hosted page (no news.json — the data lives in Firestore):
python3 news_web.py --out=web_public --hosted --firebase-config=firebase-config.json

firebase deploy --only firestore:rules,hosting
```

`firebase deploy` prints your URL, something like
`https://your-project.web.app`. Open it, sign in with Google, and the archive
loads.

### 7. Keep it fed from the VM

```bash
# On the VM: push the archive to Firestore, then check it worked
python3 news_web.py --sync-firebase --project=keen-wavelet-275120 --dry-run
python3 news_web.py --sync-firebase --project=keen-wavelet-275120
```

Add one cron line so it runs shortly after each news run. **Keep this separate
from the four news cron jobs** — a failure here must not affect your digest:

```bash
crontab -e
# 15 minutes after the 09:15 and 17:00 ET runs (EDT values shown):
30 13 * * 1-5 cd /home/Achilles && /usr/bin/python3 news_web.py \
  --sync-firebase --project=keen-wavelet-275120 >> news_web.log 2>&1
15 21 * * 1-5 cd /home/Achilles && /usr/bin/python3 news_web.py \
  --sync-firebase --project=keen-wavelet-275120 >> news_web.log 2>&1
```

(These are the same DST-aware summer/winter pairs the installer uses — see
`setup_cloud.sh`. Add the winter pair too, `30 14` and `15 22`, exactly as it
does for the news jobs.)

## Cost

Firestore free tier (Spark plan): 50k document reads, 20k writes, 1 GiB/day.
A full sync is ~1,400 writes and happens twice a day, and the page reads 1 tiny
meta doc on a repeat visit because it caches the rows in `localStorage`. Comfort-
ably inside the free tier.

## What was verified, and what was not

**Verified offline:**
- the static export, filters, search, pagination and row expansion, in a real
  headless browser against the real 1,396-row archive
- the pinned Firebase CDN version resolves and exports every symbol the
  bootstrap imports (`_audit/test_hosted_build.py`)
- the hosted build contains no `news.json`, sets `__GL_HOSTED__` before the page
  script runs, and refuses to build without a config
- the sync's `--dry-run` can be run with no credentials and changes nothing

**Not verified — needs your project:** the Firestore reads and writes, and the
Google sign-in round trip. Those cannot be tested without a real Firebase
project. Once you have completed steps 1–6, run:

```bash
python3 news_web.py --sync-firebase --project=YOUR_PROJECT --dry-run   # expect N to write
python3 news_web.py --sync-firebase --project=YOUR_PROJECT             # then the real sync
```

and tell me what the page does — I'll fix whatever it needs.

## Security notes

- The rules are **default-deny**: `news` and `meta` are readable when signed in,
  nothing else is reachable, and no client can ever write.
- The VM writes with an IAM service-account token, which bypasses rules by
  design. Protect that by keeping the VM's service account scoped — `roles/
  datastore.user`, not Editor.
- The page is read-only. There is no delete, no "run now", no config upload —
  those stay in the local panel where they belong.
- If you later want the phone to *act* (mark read, delete), that needs new
  authenticated write rules and a different review. Do not add them casually to
  these rules.
