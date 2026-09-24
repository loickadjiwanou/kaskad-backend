# Kaskad — Backend API (FastAPI + MongoDB)

Backend of the Kaskad store, a publisher-only app store: it distributes **only our own applications** as installer files (APK, EXE, MSI, DMG, PKG, AppImage, DEB, RPM). Clients download the files; nothing is ever installed automatically.

The API serves three consumers:

- the **client app** (Expo mobile + Electron desktop): public catalog, downloads, update checks;
- **end users** (optional accounts): library sync, push notifications;
- the **admin console**: catalog management, uploads, security scans, publication, statistics, moderation.

---

## Table of contents

1. [Architecture](#architecture)
2. [Features](#features)
3. [Getting started](#getting-started)
4. [Running with Docker](#running-with-docker)
5. [Configuration](#configuration)
6. [Authentication and roles](#authentication-and-roles)
7. [Version lifecycle](#version-lifecycle)
8. [Security scanning](#security-scanning)
9. [File storage](#file-storage)
10. [Push notifications](#push-notifications)
11. [API reference](#api-reference)
12. [Errors](#errors)
13. [Data model](#data-model)
14. [Project structure](#project-structure)
15. [Tests](#tests)
16. [Production checklist](#production-checklist)
17. [Troubleshooting](#troubleshooting)

---

## Architecture

```
 Client app (mobile / desktop)        Admin console
            │                               │
            ▼                               ▼
 ┌──────────────────────── FastAPI (/api/v1) ─────────────────────────┐
 │ public · users · admin routers                                      │
 │ background: security scan queue · cleanup loop · push notifications │
 └──────┬───────────────────────┬──────────────────────┬───────────────┘
        ▼                       ▼                      ▼
     MongoDB           S3 storage (MinIO / B2)   ClamAV · VirusTotal
  (metadata only)        or local disk (dev)     FCM · APNs
```

- **MongoDB stores metadata only.** Binaries, icons and screenshots live in S3-compatible storage (local disk in development).
- **Downloads** go through the API: the download is counted, then the client is redirected (302) to a **signed, temporary URL**.
- **Uploads** are hashed (SHA-256) while being received, stored, then **scanned in the background**. A version can only be published after its scan has passed.
- **Stack:** Python 3.12, FastAPI, PyMongo (async API), Pydantic v2, PyJWT, Argon2, boto3, androguard, signify, httpx.

---

## Features

| Area | What the API provides |
|---|---|
| Authentication & roles | JWT for admins and end users, short-lived access tokens, single-use refresh tokens (rotation) with revocation, admin roles (full admin / content editor), login rate limiting |
| Catalog | App CRUD (name, short/long description, icon, screenshots, categories, target platforms, featured flag), status draft / published / archived, preview as displayed in the client app |
| Versions & files | Binary upload per version and format, semantic version + version code, changelog, size, SHA-256 computed on upload, complete history (archived, never deleted) |
| Storage | S3-compatible (MinIO, Backblaze B2) or local disk, signed temporary download URLs (anti-hotlinking), resumable downloads (`Range`), automatic cleanup of incomplete uploads and orphan files |
| Categories | CRUD (name, icon, order), reordering, reassigning apps between categories, safe deletion |
| Security | ClamAV and/or VirusTotal, file format checks, APK manifest analysis and signing certificate continuity, Authenticode verification for EXE, no file is ever executed |
| Statistics | Download counters per app and version, time series, breakdown (platform, format, version), top apps, CSV export |
| Moderation | Queue of versions awaiting validation, activity log (who did what, when) |
| Notifications | Push notification to followers when a new version is published (FCM for Android, APNs for iOS), localized, invalid tokens removed |
| Public API | Categories, home sections, search with filters, app details, versions, download, update check, "my apps" |
| i18n | Error messages in French or English (`Accept-Language`) |

---

## Getting started

Requirements: **Python 3.12+** and **MongoDB 6+** (or use [Docker](#running-with-docker)).

```bash
cd kaskad-backend
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env
```

Edit `.env` (at least `ADMIN_EMAIL`, `ADMIN_PASSWORD`, and the MongoDB connection), then start the API:

```bash
.venv/bin/uvicorn app.main:app --reload
```

| URL | |
|---|---|
| http://localhost:8000/api/v1 | API base URL |
| http://localhost:8000/api/v1/docs | Interactive documentation (Swagger UI) |
| http://localhost:8000/api/v1/openapi.json | OpenAPI schema |
| http://localhost:8000/api/v1/health | Health check (also checks MongoDB) |

On startup the API creates the MongoDB indexes and, **if no admin exists**, the first admin account from `ADMIN_EMAIL` / `ADMIN_PASSWORD`.

### Demo data

Development only (refused when `ENVIRONMENT=production`): 6 categories and 10 published apps with small generated files and screenshots, matching the mobile app's demo data.

```bash
.venv/bin/python -m app.scripts.seed           # only if the catalog is empty
.venv/bin/python -m app.scripts.seed --reset   # delete the catalog and recreate it
```

### Connecting the client app

In `kaskad-mobile/.env`, set `EXPO_PUBLIC_API_URL` to the API URL, then restart Metro (`yarn start:clear`):

| Client | URL to use |
|---|---|
| Web / Electron on the same computer | `http://localhost:8000` |
| iOS simulator | `http://localhost:8000` |
| Android emulator | `http://10.0.2.2:8000` |
| Physical phone | `http://<computer LAN IP>:8000` (same Wi-Fi network) |

Set `PUBLIC_BASE_URL` in the backend `.env` to the same host, because download and media links are built from it.

---

## Running with Docker

```bash
cp .env.example .env
docker compose up --build
```

| Service | Port | |
|---|---|---|
| `api` | 8000 | Kaskad API (S3 storage + ClamAV preconfigured) |
| `mongo` | — | MongoDB 7, authentication enabled (`MONGODB_USERNAME` / `MONGODB_PASSWORD`) |
| `minio` | 9000 / 9001 | S3 storage / MinIO web console (`MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD`) |
| `clamav` | — | Antivirus daemon (virus database downloaded on first start, a few minutes) |

Default credentials (`kaskad` / `kaskad-mongo-secret`, `kaskad` / `kaskad-minio-secret`) are for local use only: override them in `.env`.
The MongoDB account is created only when the `mongo-data` volume is empty.

---

## Configuration

All settings are environment variables, read from the process environment or from `.env`.

### General

| Variable | Default | Description |
|---|---|---|
| `ENVIRONMENT` | `development` | `development`, `production` or `test`. Production enforces a strong `JWT_SECRET` and forbids demo data |
| `PUBLIC_BASE_URL` | `http://localhost:8000` | Public URL of the API, used to build media and download links |
| `CORS_ORIGINS` | Expo web, Vite, `kaskad://app` | JSON list of allowed origins (admin console, Expo web, Electron) |

### MongoDB

| Variable | Default | Description |
|---|---|---|
| `MONGODB_URI` | `mongodb://localhost:27017` | Server address (a full Atlas URI also works) |
| `MONGODB_DB` | `kaskad` | Database name |
| `MONGODB_USERNAME` / `MONGODB_PASSWORD` | — | Credentials, written as-is (no URL encoding needed) |
| `MONGODB_AUTH_SOURCE` | `admin` | Database where the user is defined |

### Authentication

| Variable | Default | Description |
|---|---|---|
| `JWT_SECRET` | — | Random secret, **at least 32 characters in production** (`python -c "import secrets; print(secrets.token_urlsafe(48))"`) |
| `ADMIN_ACCESS_TTL_MINUTES` / `ADMIN_REFRESH_TTL_DAYS` | 30 / 7 | Admin session lifetimes |
| `USER_ACCESS_TTL_MINUTES` / `USER_REFRESH_TTL_DAYS` | 60 / 90 | End-user session lifetimes |
| `LOGIN_MAX_ATTEMPTS` / `LOGIN_WINDOW_MINUTES` | 10 / 15 | Login attempts allowed per account and IP address |
| `ADMIN_EMAIL` / `ADMIN_PASSWORD` | — | First admin account, created only if there is none |

### Storage

| Variable | Default | Description |
|---|---|---|
| `STORAGE_BACKEND` | `local` | `local` (development) or `s3` |
| `LOCAL_STORAGE_DIR` | `./storage` | Local storage folder |
| `S3_ENDPOINT_URL` | — | e.g. `http://minio:9000`, `https://s3.eu-central-003.backblazeb2.com` |
| `S3_PUBLIC_ENDPOINT_URL` | — | Endpoint used in signed URLs when clients reach S3 through another address |
| `S3_REGION` / `S3_BUCKET` | `us-east-1` / `kaskad` | Bucket (created automatically if missing) |
| `S3_ACCESS_KEY` / `S3_SECRET_KEY` | — | Credentials |
| `DOWNLOAD_URL_TTL_SECONDS` | 600 | Lifetime of signed download URLs |
| `MAX_UPLOAD_SIZE_MB` | 2048 | Maximum binary size |
| `ORPHAN_UPLOAD_MAX_AGE_HOURS` / `CLEANUP_INTERVAL_MINUTES` | 6 / 60 | Cleanup of incomplete uploads and orphan files |

### Security scanning

| Variable | Default | Description |
|---|---|---|
| `CLAMAV_HOST` / `CLAMAV_PORT` | — / 3310 | ClamAV daemon (clamd) |
| `VIRUSTOTAL_API_KEY` | — | VirusTotal API v3 key |
| `SCAN_REQUIRE_ANTIVIRUS` | `false` | Reject a file if no antivirus scan succeeded (**set to `true` in production**) |
| `REQUIRE_AUTHENTICODE` | `false` | Reject Windows executables without a verified Authenticode signature |

### Push notifications

| Variable | Default | Description |
|---|---|---|
| `FIREBASE_CREDENTIALS_FILE` | — | Firebase service account JSON (FCM HTTP v1) |
| `APNS_KEY_FILE`, `APNS_KEY_ID`, `APNS_TEAM_ID` | — | APNs auth key (`.p8`) and identifiers |
| `APNS_BUNDLE_ID` | `com.kaskad.store` | iOS bundle identifier |
| `APNS_USE_SANDBOX` | `true` | `false` for App Store / TestFlight builds |

---

## Authentication and roles

Two independent scopes, each with its own tokens: **admin** (console) and **user** (client app).

- **Access token:** short-lived JWT, sent as `Authorization: Bearer <token>`.
- **Refresh token:** single use. Each refresh returns a new pair and invalidates the old token, so a reused token is rejected. Refresh tokens are stored (with automatic expiry) so they can be revoked: logout, disabled admin, password change, account deletion.
- **Passwords:** hashed with Argon2id.
- **Rate limiting:** failed logins are limited per account and IP address (`429 too_many_attempts`, with `retry_after`). The limiter is in memory; use a shared store (e.g. Redis) if you run several API instances.

**Admin roles**

| Role | Permissions |
|---|---|
| `admin` | Everything, including admin account management |
| `editor` | Content: apps, versions, categories, publication, statistics, moderation |

The last active `admin` cannot be demoted or disabled.

**End-user accounts** are optional: the client app works without one.

- **Anonymous:** `POST /auth/anonymous` with a random device ID. The same device gets the same account back, without personal data.
- **Email:** `POST /auth/register` / `POST /auth/login`. When an anonymous user registers, their account (and library) is converted into an email account, and the device ID is detached.
- **Deletion:** `DELETE /me` deletes the account and all its data (required by the App Store when accounts can be created).

---

## Version lifecycle

```
upload ──► upload_status: uploading ──► stored ──► security scan ──► passed ──► publish ──► published ──► archive
                         │                           │                               ▲
                         └─► failed / abandoned      └─► failed (rescan possible) ───┘ (only "passed" can be published)
```

| Field | Values | Meaning |
|---|---|---|
| `upload_status` | `uploading`, `stored`, `failed`, `abandoned` | File transfer state (`abandoned`: interrupted upload removed by the cleanup) |
| `security_scan_status` | `pending`, `scanning`, `passed`, `failed` | Security scan state (`scan_report` holds the details) |
| `status` | `draft`, `published`, `archived` | Catalog visibility. A new version starts as `draft` |

A version is visible in the client app only if it is `published`, its scan `passed`, its file is `stored`, **and** its app is `published`.

Upload rules:

- the format must match the platform: Android → APK; Windows → EXE, MSI; macOS → DMG, PKG; Linux → AppImage, DEB, RPM;
- the file extension must match the format;
- the version name must be a semantic version (`1.4`, `1.4.2`, `2.0.0-beta.1`, `3.1.0+45`);
- a version code and format pair can only be uploaded once per app.

Publishing a version recomputes the app's available platforms and latest version. If the app is published and the version is the newest for its platform, followers are notified.

---

## Security scanning

Every stored version goes through a background queue (resumed automatically after a restart). **Files are only read, never executed.**

| Step | Check | Failure |
|---|---|---|
| ClamAV | File streamed to clamd (`INSTREAM`) | Threat found |
| VirusTotal | Hash lookup, then upload and analysis if unknown | Malicious or suspicious verdict |
| Antivirus requirement | With `SCAN_REQUIRE_ANTIVIRUS=true`, at least one engine must succeed | No successful antivirus scan |
| Format check | File header matches the declared format (ZIP, MZ, OLE, `koly`, `xar!`, ELF, `!<arch>`, RPM lead) | Content does not match the format |
| APK (androguard) | Package, versions, SDK levels, permissions, signing certificate fingerprints (v3, v2, v1) | Unsigned APK, package different from the app's package, **signing certificate different from the previous validated version** |
| EXE (signify) | Authenticode signature | File modified after signing (always). Untrusted, unverifiable or missing signature only with `REQUIRE_AUTHENTICODE=true` (otherwise a warning) |

The report (`scan_report`: engines, errors, warnings, APK details) is returned to the console. The first validated APK sets the app's `android_package`. Rescanning is possible (`POST /admin/versions/{id}/rescan`), for example after configuring an antivirus.

---

## File storage

| Object | Key |
|---|---|
| Binary | `binaries/{app_id}/{version_id}/{App_Name-1.2.0-platform.ext}` |
| Icon / screenshot | `media/apps/{app_id}/{icon\|screenshot}-{random}.{png\|jpg\|webp}` |

- **Downloads:** `GET /versions/{id}/download` counts the download, then redirects to a signed URL valid for `DOWNLOAD_URL_TTL_SECONDS`, with the download file name. With S3 the URL is presigned; with local storage it is an HMAC-signed API URL. Both support `Range` requests, so interrupted downloads can resume.
- **Media:** icons and screenshots are served by `GET /media/{key}` (redirect to S3 or direct file).
- **Cleanup** (every `CLEANUP_INTERVAL_MINUTES`):
  - uploads interrupted for more than `ORPHAN_UPLOAD_MAX_AGE_HOURS` are marked `abandoned` and their files deleted;
  - files that no version or app references are deleted;
  - unfinished S3 multipart uploads are aborted.

---

## Push notifications

Sent when a version is published, to users who follow the app **with notifications enabled** and have registered a push token (`POST /me/push-tokens`).

- **Android — Firebase Cloud Messaging (HTTP v1):** in the Firebase console, *Project settings › Service accounts › Generate new private key*. Store the JSON file next to the API (never commit it) and set `FIREBASE_CREDENTIALS_FILE`.
- **iOS — APNs:** the iOS app registers native APNs tokens, so the API talks to APNs directly. In the Apple Developer account, create an APNs auth key (`.p8`), then set `APNS_KEY_FILE`, `APNS_KEY_ID`, `APNS_TEAM_ID`, `APNS_BUNDLE_ID` (and `APNS_USE_SANDBOX=false` for App Store / TestFlight builds).

Messages use the language registered with the token (French or English) and open the app page (`data.url = /app/{id}`). Tokens rejected by FCM or APNs (app uninstalled) are removed.

---

## API reference

Base URL: `/api/v1`. Full schemas and a test console at **`/api/v1/docs`**.

### Public (client app, no authentication)

| Method | Path | Description |
|---|---|---|
| GET | `/health` | Health check |
| GET | `/categories` | Categories, in display order |
| GET | `/home?platform=` | `featured`, `new`, `popular` sections. `platform` puts compatible apps first |
| GET | `/apps` | List and search: `q`, `category_id`, `platform`, `sort` (`popular`, `recent`, `name`), `page`, `limit`, `ids` (comma-separated batch fetch) → `{items, total, page, limit}` |
| GET | `/search` | Same as `/apps` |
| GET | `/apps/{id}` | App details with categories, screenshots and published versions |
| GET | `/apps/{id}/versions?platform=` | Published versions |
| GET | `/versions/{id}/download?platform=` | Counts the download and redirects (302) to the signed file URL |
| POST | `/updates/check` | `{installed: [{app_id, version_id, version_code, platform}]}` → `[{app_id, latest_version}]` |
| GET | `/media/{key}` | Icons and screenshots |
| GET | `/files/{key}?exp=&sig=&name=` | Signed file download (local storage) |

### End users

| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `/auth/register` | optional | Email account (converts the anonymous account of the caller) |
| POST | `/auth/login` | — | Email login |
| POST | `/auth/anonymous` | — | `{device_id}` → anonymous account |
| POST | `/auth/refresh` | — | `{refresh_token}` → new token pair |
| POST | `/auth/logout` | — | `{refresh_token}` → revokes the session |
| GET | `/me` | user | `{id, email, anonymous}` |
| DELETE | `/me` | user | Deletes the account and its data |
| GET / PUT | `/me/library` | user | Favorites, followed apps (with notification preference), installed apps |
| GET | `/me/apps` | user | Followed and installed apps with installed and latest versions, `update_available` |
| GET | `/me/updates` | user | Installed apps with a newer version |
| POST | `/me/push-tokens` | user | `{token, provider: fcm\|apns, platform, language}` |
| DELETE | `/me/push-tokens/{token}` | user | Unregisters a device |

Auth responses: `{access_token, refresh_token, token_type, user: {id, email, anonymous}}`.

### Admin (console)

All routes require an admin access token. Routes marked **admin** require the `admin` role.

| Method | Path | Description |
|---|---|---|
| POST | `/admin/auth/login` · `/refresh` · `/logout` | Session (`{access_token, refresh_token, admin}`) |
| GET / PATCH | `/admin/auth/me` | Own profile (name, password change with current password) |
| GET / POST | `/admin/admins` | **admin** — list / create admin accounts |
| PATCH | `/admin/admins/{id}` | **admin** — name, role, active, password (sessions revoked when disabled or password changed) |
| GET / POST | `/admin/categories` | List (with `apps_count`) / create |
| PATCH / DELETE | `/admin/categories/{id}` | Update / delete (`?reassign_to=` required if the category has apps) |
| PUT | `/admin/categories/order` | `{ids: [...]}` → new display order |
| POST | `/admin/categories/{id}/reassign` | `{to_category_id, app_ids?}` → move apps |
| GET / POST | `/admin/apps` | List (`status`, `q`, `category_id`, pagination, `pending_versions`) / create |
| GET / PATCH | `/admin/apps/{id}` | Details / update |
| POST | `/admin/apps/{id}/status` | `{status: draft\|published\|archived}` |
| GET | `/admin/apps/{id}/preview` | App as displayed in the client app, whatever its status |
| POST | `/admin/apps/{id}/icon` | Multipart `file` (PNG, JPEG, WebP, 10 MB max) |
| POST / PUT | `/admin/apps/{id}/screenshots` | Add (multipart `files`, 12 max) / reorder or remove (`{urls}`) |
| GET / POST | `/admin/apps/{id}/versions` | List / upload (multipart: `file`, `version_name`, `version_code`, `platform`, `file_format`, `changelog`) |
| GET / PATCH | `/admin/versions/{id}` | Details with scan report / update changelog or version name |
| POST | `/admin/versions/{id}/publish` · `/archive` · `/rescan` | Lifecycle actions |
| GET | `/admin/versions/{id}/download-url` | Signed link for the console (not counted in statistics) |
| GET | `/admin/stats/overview` | Apps per status, total downloads, last 30 days, pending reviews, recent publications |
| GET | `/admin/stats/downloads` | Time series: `from`, `to`, `interval` (`day`, `week`, `month`), `app_id`, `version_id` |
| GET | `/admin/stats/breakdown` | Downloads by `platform`, `file_format`, `version_id` or `app_id` |
| GET | `/admin/stats/top-apps` | Most downloaded apps over a period |
| GET | `/admin/stats/export.csv` | CSV export (one line per download) |
| GET | `/admin/moderation/queue` | Versions not published yet (scanning, rejected or awaiting publication) |
| GET | `/admin/activity` | Activity log (`action` prefix, `actor_id`, pagination) |

---

## Errors

Errors return a stable `code` and a `detail` message localized from `Accept-Language` (`fr` or `en`):

```json
{ "detail": "Email ou mot de passe incorrect.", "code": "invalid_credentials" }
```

Validation errors return `422` with `code: "validation_error"` and an `errors` list.
Main codes: `not_authenticated`, `invalid_token`, `forbidden`, `invalid_credentials`, `too_many_attempts`, `email_taken`, `weak_password`, `app_not_found`, `version_not_found`, `version_unavailable`, `invalid_format`, `invalid_version_name`, `version_exists`, `file_too_large`, `empty_file`, `scan_not_passed`, `category_in_use`, `last_admin`, `invalid_image`.

---

## Data model

| Collection | Main fields |
|---|---|
| `apps` | `name`, `short_description`, `long_description`, `icon_key`, `screenshot_keys`, `category_ids`, `target_platforms`, `featured`, `status`, `android_package`, `available_platforms`, `latest_version_name`, `last_published_at`, `downloads_count`, `created_at`, `updated_at` |
| `versions` | `app_id`, `version_name`, `version_code`, `platform`, `file_format`, `storage_key`, `file_name`, `file_size`, `sha256_hash`, `changelog`, `status`, `upload_status`, `security_scan_status`, `scan_report`, `apk_info`, `downloads_count`, `published_at`, `created_by`, `created_at` |
| `categories` | `name`, `icon`, `order` |
| `users` | `email`, `password_hash`, `anonymous`, `device_id`, `favorites`, `followed_apps [{app_id, notify}]`, `installed_apps [{app_id, version_id}]`, `push_tokens [{token, provider, platform, language}]` |
| `admins` | `email`, `password_hash`, `name`, `role`, `active`, `last_login_at` |
| `download_stats` | `app_id`, `version_id`, `timestamp`, `platform`, `file_format` |
| `refresh_tokens` | `jti`, `subject_id`, `scope`, `expires_at` (TTL index) |
| `activity_log` | `actor_id`, `actor_email`, `action`, `target_type`, `target_id`, `details`, `created_at` |

`available_platforms`, `latest_version_name` and `last_published_at` are recomputed from published versions. File URLs are not stored: they are built from storage keys.

---

## Project structure

```
kaskad-backend/
├── app/
│   ├── main.py              # App factory, lifespan (indexes, first admin, storage, scan queue, cleanup), error handlers
│   ├── db.py                # MongoDB client and indexes
│   ├── deps.py              # FastAPI dependencies (db, storage, current admin / user, roles, rate limiter)
│   ├── core/                # config, security (JWT, Argon2), i18n error messages, rate limiting
│   ├── models/              # enums, formats, Pydantic schemas
│   ├── routers/             # public, users, admin_auth, admin_apps, admin_versions, admin_categories, admin_stats
│   ├── services/            # auth, catalog serializers, storage, scanning + scanners/, notifications, stats, cleanup, activity
│   └── scripts/seed.py      # demo data
├── tests/                   # pytest suite (real mongod, local storage, moto S3)
├── Dockerfile, docker-compose.yml
├── requirements.txt, requirements-dev.txt, pyproject.toml
└── .env.example
```

---

## Tests

```bash
.venv/bin/pytest          # full suite
.venv/bin/ruff check .    # lint
```

The suite starts a **real `mongod`** (downloaded once into `.mongo-bin/`, no Docker needed). Each test gets an isolated database and storage folder, and your `.env` is ignored. Coverage:

- full catalog flow: upload → scan → publish → public catalog → download (with `Range`) → statistics;
- authentication: refresh rotation, roles, rate limiting;
- security: fake clamd detecting EICAR, format checks, APK certificate continuity, required antivirus;
- S3 storage against a local S3 server (moto);
- push notifications with a fake sender;
- cleanup, categories, CSV export, localized errors.

---

## Production checklist

- `ENVIRONMENT=production` and a random `JWT_SECRET` (32+ characters)
- `STORAGE_BACKEND=s3` (MinIO or Backblaze B2), private bucket
- `SCAN_REQUIRE_ANTIVIRUS=true` with ClamAV and/or VirusTotal
- MongoDB with authentication, and backups of MongoDB and of the bucket
- HTTPS reverse proxy in front of the API (uvicorn runs with `--proxy-headers`), `PUBLIC_BASE_URL` set to the public HTTPS URL
- `CORS_ORIGINS` restricted to the real console and client origins
- Push credentials outside the repository (`FIREBASE_CREDENTIALS_FILE`, APNs key)
- A shared rate limit store if several API instances run

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `Authentication failed` from MongoDB | Check `MONGODB_USERNAME`, `MONGODB_PASSWORD` and `MONGODB_AUTH_SOURCE` (usually `admin`) |
| No admin account | The first admin is only created when the `admins` collection is empty and `ADMIN_EMAIL` / `ADMIN_PASSWORD` are set |
| Download links point to the wrong host | Set `PUBLIC_BASE_URL` (and `S3_PUBLIC_ENDPOINT_URL` with Docker / MinIO) to an address reachable by the clients |
| Versions stay in `pending` / `scanning` | The scan queue runs inside the API process: check the logs. After fixing an antivirus configuration, use `rescan` |
| Every file is rejected with "An antivirus scan is required" | `SCAN_REQUIRE_ANTIVIRUS=true` but ClamAV / VirusTotal are not reachable |
| Electron client gets CORS errors | Keep `kaskad://app` in `CORS_ORIGINS` |
| `JWT_SECRET must be set…` at startup | Production requires a random secret of at least 32 characters |
