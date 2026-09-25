# Kaskad backend API — Functionalities

Complete list of what the backend (FastAPI + MongoDB) does. This file is updated every time a feature is added or changed. Endpoint details are in [README.md](README.md#api-reference).

The backend serves two clients: the **client app** (mobile + desktop, public API) and the **console** (developer accounts and platform admin).

---

## 1. Public catalog (client app)

- **Categories** in the order defined by the platform admin.
- **Home:** featured, new and popular apps; compatible apps first for the given platform.
- **List and search:** keywords (every word must match the name or a description), category, platform, developer, sort (popular, recent, name — accent-insensitive), pagination, batch fetch by ids.
- **App details:** listing, screenshots, categories, developer `{id, name}`, published versions (production; plus beta for testers), rating (average, count, 1–5 distribution) and `share_url` (public web page). Summaries in lists include the average rating and count.
- **Developer page:** `GET /developers/{id}` (name, number of published apps) and its apps.
- **Languages:** listings (short / long description) and release notes are returned in the `Accept-Language` language (French / English), falling back to the listing's main language.
- **Visibility rules:** an app is public when published and its developer account is not suspended; a version is public when published, stored, security scan passed, and in the production channel (beta: testers only).
- **Downloads:** `GET /versions/{id}/download` counts the download then redirects to a temporary signed URL (resumed downloads with a `Range` header are not counted twice). Beta downloads require a signed token (24 h) given only to testers.
- **Update check:** for installed apps, the newest published version (same platform), beta included for testers.
- **Media:** icons and screenshots served through `/media/...` (redirect to storage, cacheable).

---

## 2. Ratings, reviews and reports (client app)

- **Reviews list:** visible reviews of a published app, sorted by most recent (last edit), highest or lowest rating, filter by stars, pagination; returns the rating summary; `is_mine` flags the caller's review.
- **Own review:** get / create or update (1–5 stars, text up to 2,000 characters, version, language of the review) / delete. The author name always comes from the account (name, otherwise the part of the email before "@"); no name is accepted from the client. **Email account required** (`403 email_account_required` for anonymous accounts). One review per account and app (unique index).
- **Rating aggregates** stored on the app (average, count, distribution) and recomputed on every create / update / delete / hide / restore; hidden reviews don't count.
- **Report a review:** reason; one report per person (account, or hashed IP address without an account); rate-limited.
- **Report an app:** reason (malware, abusive, copyright, misleading, broken, other) and details, with or without an account; one open report per person and app; the platform admin is emailed ("app reported", in their language, link to Moderation › Reports); rate-limited.

## 3. Public web pages

- `GET /a/{id}` (outside the API prefix): server-rendered HTML page of a published app — icon, name, developer, rating stars, platforms, latest version, downloads, categories, screenshots, description — in French or English (`Accept-Language`).
- Open Graph / Twitter meta tags (title, description, icon) for link previews.
- **"Open in Kaskad"** button: `kaskad://app/<id>`; on Android, replaced by an `intent://` URL for package `ANDROID_PACKAGE_ID` with a browser fallback; optional **"Get Kaskad"** button when `KASKAD_DOWNLOAD_URL` is set.
- Localized 404 page for unknown, unpublished or suspended apps. The page address (`PUBLIC_WEB_URL`, default `PUBLIC_BASE_URL`) is the app's `share_url`.

---

## 4. End-user accounts (client app)

- Anonymous account (device id), email registration (with an optional public **name**) and sign-in, upgrade from anonymous to email.
- **Account name:** `PATCH /me {name}` (email accounts); propagated to all the user's reviews.
- JWT access token + single-use refresh token (rotation, revocation).
- **Library:** favorites, followed apps (with notification preference), installed apps (version) — get / replace; "my apps" with installed version, latest version and `update_available`; updates only.
- **Push tokens:** register / remove FCM or APNs tokens with platform and language.
- Sign out (refresh token revoked); **account deletion** with all data (reviews deleted and ratings recomputed).
- Login rate limiting per account and IP.

---

## 5. Console authentication and developer accounts

- **Roles:** `admin` (platform admin, unique: `ADMIN_EMAIL`, never assignable), `owner`, `developer`, `viewer`.
- **Sign up:** creates a developer account and its owner; confirmation email (48 h link) in the console language; sign-in refused until confirmed; resend confirmation (same answer for unknown addresses).
- **Sign in / refresh / sign out**, profile update, password change, member language remembered (used for emails).
- **Forgot / reset password:** emailed link (1 h, single use), other sessions revoked, returns a session.
- **Team:** list members, change role (developer / viewer), deactivate / reactivate (sessions revoked); the owner and the platform admin can't be modified (except suspension of an owner by the platform admin).
- **Invitations:** invite by email with a role; email in the console language; resend (new link), revoke; public lookup and acceptance (creates the member and returns a session); one account per person.
- **Account:** rename (propagated to the apps' developer name).
- **Developer accounts (platform admin):** list with owner, members, apps and suspension; **suspend / reactivate** an account (apps hidden from the store, members' sessions refused and revoked, invitations blocked, owner emailed with the reason).
- **API keys:** create (key shown once, only the SHA-256 is stored), list, revoke; a key authenticates as a developer of its account (`Authorization: Bearer ksk_…`), last use tracked.
- **Isolation:** every console route is scoped to the member's account (apps, versions, statistics, moderation, activity); the platform admin sees everything and can filter by account.
- Startup bootstrap: creates the platform admin and platform account; migrates older data (apps, activity, former roles).

---

## 6. Apps (console)

- Create (in the member's account), list (filters, search, pending versions, developer account for the admin), details, preview as displayed in the client app (live or with the listing draft).
- Listing fields: name, short / long description, **main language** and **translations** (FR / EN), categories, target platforms, featured, Android package.
- Icon and screenshots upload (PNG / JPEG / WebP checked by content, 10 MB, 12 screenshots), reorder / remove; unused files deleted.
- Status: draft / published / unpublished (archived) — direct for the platform admin, by request for members.
- **Beta testers:** list of client-account emails; each new tester receives an invitation email (console language, `kaskad://app/<id>` link).
- Viewers are read-only (`403 read_only` on any change).

---

## 7. Versions and releases

- **Upload** (multipart): version name (semantic version), version code, platform, format (must match the platform and the file extension), release notes (`changelog` or `changelog_fr` / `changelog_en`), channel (`production` / `beta`), optional `submit=true` with note and release date. SHA-256 and size computed while streaming; size limit; duplicate (code + format) refused.
- **Security scan** (background queue, resumed after restart): see section 10.
- **Edit:** version name, release notes and translations, channel (before going live only); published versions are editable by the platform admin only.
- **Channels:** production (everyone) and beta (testers only; at least `MIN_BETA_TESTERS` testers, default 3, to submit or publish a beta).
- **Scheduled releases:** requested date at submission and/or chosen by the admin; `scheduled` status; a background scheduler publishes due versions every 30 s (atomic claim); cancel schedule; publish now.
- **Promotion:** a live beta is promoted to production by the platform admin (directly or by approving a promotion request).
- **Publication effects:** app catalog fields recomputed (available platforms, latest version, last publication — production only); push notification to followers when it's the newest production version of its platform.
- **Archive** (versions are never deleted), **rescan**, admin download link (not counted).
- **Automatic submission** after a passed scan for uploads with `submit=true` (blocked and logged for a beta without enough testers).

---

## 8. Review workflow

- Nothing goes live without the platform admin.
- **Versions:** submit (note, requested date) → approve (publish now or schedule) / reject (reason) / withdraw; editing a submitted version cancels the submission.
- **Status requests** (publish / unpublish / draft): request → approve / reject / withdraw.
- **Listing drafts:** edits of a live app are stored as a draft (texts, languages, media, categories…), submitted, then published (applied to the live listing) or rejected; the draft can be discarded; media cleanup aware of live + draft.
- **Promotion requests** for live betas.
- **Moderation endpoints** (platform admin): review requests (pending / rejected), the security scan queue (including scheduled versions), **app reports** (open / closed) and **reported / hidden user reviews**.
- **Follow-up emails:** new request → platform admin; approval / rejection / scheduling / publication → author; in each recipient's language.

---

## 9. User reviews in the console

- **App reviews** for the developer account (hidden ones included, with report count and reasons): filter by stars and replied / unanswered, pagination.
- **Developer reply:** public reply signed with the developer account name (members except viewers); the review author is emailed on the first reply, in the language of the review; reply can be edited or deleted.
- **Hide / restore** a review (platform admin): hidden reviews disappear from the client app and the rating, their reports are cleared; restoring also dismisses reports of a visible review.
- **Resolve an app report** (platform admin): dismissed or handled, internal note, optional unpublishing of the app; every open report of the same app is closed together.
- **Summary** for the dashboard: average rating, number of reviews, unanswered reviews of the member's apps.
- Overview counters for the platform admin: open app reports and reported reviews.
- Activity log entries: `review.replied`, `review.reply_deleted`, `review.hidden`, `review.restored`, `report.dismissed`, `report.resolved` (and `app.archived` when a report unpublishes the app).

---

## 10. Security scanning

- Antivirus: ClamAV (INSTREAM) and/or VirusTotal (v3); optional requirement of at least one antivirus.
- Format checks by content (magic bytes) for every installer type.
- **APK:** manifest analysis (androguard): package, version, permissions, signature; package must match the app; **signing certificate continuity** with previous versions.
- **Windows (EXE / MSI):** Authenticode verification with verdicts valid / tampered / untrusted / unverifiable (optional strict mode).
- No file is ever executed; report stored with errors, warnings and engine results; system entry in the activity log.

---

## 11. Categories (platform admin)

- Create, edit (name, icon), reorder, move apps to another category (live listings and drafts), delete with mandatory reassignment when used; app counts scoped to the account for members.

---

## 12. Statistics

- Overview (apps per status, total downloads, last 30 days, pending versions, requests to review, latest releases).
- Downloads over time (day / week / month), breakdown by platform, format, version or app, top apps, CSV export (account, app, version, platform, format).
- Scoped to the member's account; the platform admin sees everything or one account.

---

## 13. Activity log

- Every console action with actor (member, "System" or "API · key name"), account, target and details (reasons, requesters, channels, dates…).
- Account attribution: actions on an app are logged in the app's account (so owners see the platform admin's decisions).
- Filters: action prefix, member, account (platform admin); pagination. Available to owners and the platform admin.

---

## 14. Emails (Brevo)

- Transports: Brevo SMTP relay (STARTTLS / TLS) or Brevo HTTP API; without configuration, emails are written to the logs (development).
- HTML + text emails with the Kaskad layout, in French or English: email confirmation, password reset, team invitation, beta tester invitation, "to review", version published / rejected / scheduled, status request approved / rejected, listing published / rejected, account suspended / reactivated, reply to a user review (to the reviewer), app reported (to the platform admin).
- Sent in the background; failures are logged without breaking the request.

---

## 15. Push notifications

- FCM HTTP v1 (Android) and APNs (iOS), localized title and body per token language, only to followers with notifications enabled; invalid tokens removed.

---

## 16. Storage

- Local storage (development) with HMAC-signed URLs and `Range` support, or S3-compatible storage (MinIO, Backblaze B2…) with presigned URLs.
- Binaries never stored in MongoDB.
- Cleanup task: abandoned uploads, orphan files (live and draft media kept), incomplete multipart uploads.

---

## 17. Platform and operations

- Localized errors (`{detail, code}`) in French or English from `Accept-Language`, with parameters (e.g. required number of testers).
- CORS configuration, environment-based settings (`.env`), production safety checks (JWT secret).
- MongoDB indexes created at startup; seed script for demo data.
- Docker / docker-compose files.
- Test suite with a real in-memory MongoDB, S3 (moto), fake email sender and fake scanners.
