# Airchive — environment setup

Follow this document top to bottom the first time you stand the collector up.
It assumes no prior context about this project.

Setup is deliberately ordered, and two steps are **gates**: the collector must
not be built past them until they pass.

| Step | What it establishes | Gate |
|---|---|---|
| 1 | Firebase project + Firestore database | — |
| 2 | Client access locked down | — |
| 3 | Local Google credentials | — |
| 4 | `airchive check-firestore` round trip | **Gate A** — storage proven before collector logic |
| 5 | ThinQ PAT, country code, client ID | — |
| 6 | `airchive discover` | — |
| 7 | `airchive validate-counter` | **Gate B** — the daily counter must advance intraday |

---

## 1. Create the Firebase project and Firestore database

The collector needs its own project. Do not reuse a project that serves an
application: this one exists only to hold telemetry, and its access rules are
locked shut.

1. Open the [Firebase Console](https://console.firebase.google.com/) and choose
   **Add project**.
2. Name it `lg-ac-telemetry` (suggested — any name works, but the rest of these
   docs use that one). Note the **project ID** Firebase assigns; it may get a
   random suffix, e.g. `lg-ac-telemetry-4f2a1`. The project ID, not the display
   name, is what goes into `FIREBASE_PROJECT_ID`.
3. Google Analytics is not used by this system. Decline it.
4. In the left nav choose **Build → Firestore Database**, then **Create
   database**.
5. Choose **Standard edition** (Native mode). This is the default for new
   databases and is what `google-cloud-firestore` talks to. Datastore mode will
   not work with this collector.
6. Choose a location.

   > **The database location is permanent.** Firestore does not allow a database
   > to be moved or its location changed after creation. Changing it later means
   > creating a new database and copying the data across. Pick deliberately:
   > choose the region closest to where the collector will run
   > (`asia-southeast1`, Singapore, is the nearest region to a Philippines
   > deployment). A regional location is cheaper than multi-region and is the
   > right choice for a single-writer telemetry series.

7. Start in **production mode** when offered the rules choice. Step 2 replaces
   the rules anyway, but production mode fails closed in the meantime.

Record the project ID in your local `.env` as `FIREBASE_PROJECT_ID`.

### Confirm

The database is created when the Firestore **Data** tab renders an empty
collection browser for the project. Keep this browser tab open — step 4 checks a
document appears here.

---

## 2. Lock client access shut

The collector uses server-side Admin credentials, which bypass Firestore
security rules altogether. Rules therefore exist here only to close the *client*
surface — and since no frontend exists in this system, that surface should be
shut completely.

The repository ships the rules in [`firestore.rules`](../firestore.rules):

```
match /{document=**} {
  allow read, write: if false;
}
```

Apply them either way:

- **Console:** Firestore → **Rules** tab → paste the file's contents →
  **Publish**.
- **CLI:** `npm i -g firebase-tools && firebase login && firebase deploy --only firestore:rules --project <your-project-id>`

### Confirm

- In the console's **Rules Playground**, simulate an unauthenticated `get` on
  `telemetry/anything`. It must be **denied**.
- Server-side access is unaffected — step 4 proves this by writing a document
  with Admin credentials while the rules stay closed. Both facts together are
  the check: clients denied, collector still able to write.

---

## 3. Local Google credentials

Prefer **Application Default Credentials**. Do not create a service-account key
file for local work; a long-lived key on a laptop is the single most common way
these projects leak.

```bash
gcloud auth application-default login
gcloud config set project <your-project-id>
```

That writes credentials to your user profile, outside this repository:

- Linux/macOS: `~/.config/gcloud/application_default_credentials.json`
- Windows: `%APPDATA%\gcloud\application_default_credentials.json`

The Google client libraries pick these up with no configuration. Nothing needs
to be set in `.env`.

Your account needs the **Cloud Datastore User** role (`roles/datastore.user`) on
the project — being the project Owner covers it.

### Escape hatch: `GOOGLE_APPLICATION_CREDENTIALS`

Only if ADC genuinely cannot be used (no `gcloud`, a locked-down CI runner):

1. Create a service account with **only** `roles/datastore.user`.
2. Create a JSON key for it and save it **outside this repository** — e.g.
   `~/.secrets/airchive-key.json`. Never inside the working tree; the ignore
   rules are a safety net, not a place to keep keys.
3. Point at it: `GOOGLE_APPLICATION_CREDENTIALS=/absolute/path/outside/repo/airchive-key.json`

**Revoking and replacing a key**

```bash
gcloud iam service-accounts keys list --iam-account=<sa>@<project>.iam.gserviceaccount.com
gcloud iam service-accounts keys delete <KEY_ID> --iam-account=<sa>@<project>.iam.gserviceaccount.com
```

Deleting the key invalidates it immediately. Create a replacement with
`gcloud iam service-accounts keys create`, update the path, and delete the old
file from disk. If a key was ever committed or pasted anywhere, treat it as
compromised: delete it first, investigate second.

The deployed collector never uses a key file at all — it uses the attached
service account (see [operations.md](operations.md)).

### Confirm

```bash
git status --ignored --short | grep -E '\.env|key.*\.json'   # placeholders show as ignored
git ls-files | grep -iE 'credential|key.*\.json|\.env$'      # must print nothing
```

The second command printing nothing is the check: no credential file is tracked
in the repository.

---

## 4. Gate A — prove the round trip

```bash
airchive check-firestore
```

It writes a scratch document to `_airchiveCheck/check-<random>`, reads it back
and compares a nonce, prints a console link, waits for you to press Enter, then
deletes it and verifies the deletion. Exit code 0 means pass.

For a non-interactive run: `airchive check-firestore --pause-seconds 30`, or
`--keep` to leave the document in place and remove it yourself.

### Confirm

1. The command exits 0 and prints `check-firestore: PASS`.
2. While it waits, the document is visible in the Firestore **Data** tab.
3. After it completes, the document is gone.

> **Gate A.** Do not build or run collector polling logic until this passes.
> The first poller run must not also be the first test of connectivity.

**Result — fill this in when you run it:**

- [ ] `check-firestore` passed on ______ (date) against project ______

---

## 5. ThinQ access: token, country code, client ID

Three values identify the collector to LG. They go in `.env` locally and in
Secret Manager / environment variables when deployed.

### 5.1 Personal Access Token — `LG_THINQ_PAT`

1. Sign in at **<https://connect-pat.lgthinq.com>** with the **same LG account
   the air conditioner is registered to**. A PAT issued from a different account
   cannot see the device.
2. Choose to create a new PAT, name it something you will recognise later
   (`airchive-collector`), and grant it the **device read** scopes — at minimum
   permission to view the device list, device state, and energy usage. This
   collector never issues control commands, so control scopes are not needed
   and should not be granted.
3. Copy the token **once** — the portal will not show it again — and put it in
   `.env` as `LG_THINQ_PAT`.

The token is a bearer credential: anyone holding it can read your devices. It
never belongs in a commit, a log line, a screenshot, or a chat message. The
collector's SDK boundary is built specifically to keep it out of logs (see
[operations.md](operations.md), *Credential hygiene*).

If it leaks or expires, revoke it in the same portal and issue a replacement.
The collector classifies an invalid token as a **fatal** condition and stops
retrying, so a revoked token shows up as a loud failure in `airchive health`
rather than as silent data loss.

### 5.2 Country code — `LG_COUNTRY_CODE`

The two-letter ISO 3166-1 code of the **LG account's** country, not necessarily
where you are now. It selects the ThinQ API region; the wrong one authenticates
against a region your devices do not exist in.

The Philippines (`PH`) routes to the `KIC` region — `api-kic.lgthinq.com`. Check
any other country with:

```bash
python -c "from thinqconnect.country import get_region_from_country as r; print(r('PH'))"
```

### 5.3 Client ID — `LG_CLIENT_ID`

Generate it **exactly once**:

```bash
python -c "import uuid; print(uuid.uuid4())"
```

Put the result in `.env` as `LG_CLIENT_ID` and never change it. It is a stable
identity for this collector, persisted as configuration and reused across every
invocation, restart, and redeploy.

Do not generate one per run. LG associates client identity with API usage and
subscription state; a new identity on every invocation looks like a new client
several hundred times a day. The collector refuses to invent one for you — a
missing `LG_CLIENT_ID` fails startup validation with instructions rather than
silently generating an ephemeral value.

### Confirm

```bash
grep -c . .env     # the file exists and has content
git check-ignore .env && echo ".env is ignored"
```

---
