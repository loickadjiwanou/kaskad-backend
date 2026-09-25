# Deploying Kaskad on a server

One `docker compose` command runs the whole platform on a Linux server (4 GB RAM minimum, ClamAV alone uses ~1.5 GB):

| Service | Role |
|---|---|
| `caddy` | HTTPS reverse proxy, certificates from Let's Encrypt (automatic) |
| `api` | Kaskad API (this repository), also serves the public app pages `/a/<id>` |
| `console` | Kaskad Console (built from `../../kaskad-console`, served by nginx) |
| `mongo` | MongoDB 7 with authentication |
| `minio` | S3-compatible file storage (installers, icons, screenshots) — Chainguard build, the official MinIO images are no longer published |
| `clamav` | Antivirus used by the security scan (signatures updated automatically) |

## 1. Prepare the server

- A Linux server with Docker and the Compose plugin (`docker compose version`).
- Three DNS records (A/AAAA) pointing to the server, e.g. `api.example.com`, `console.example.com`, `files.example.com`.
- Ports 80 and 443 open.

Clone both repositories side by side:

```bash
mkdir -p /srv/kaskad && cd /srv/kaskad
git clone <kaskad-backend repository> kaskad-backend
git clone <kaskad-console repository> kaskad-console
```

## 2. Configure

```bash
cd kaskad-backend/deploy
cp .env.production.example .env.production
openssl rand -hex 32   # run once per secret: JWT_SECRET, MONGO_ROOT_PASSWORD, MINIO_ROOT_PASSWORD
nano .env.production
```

Required: the three domains, `ACME_EMAIL`, `JWT_SECRET`, the MongoDB and MinIO passwords, `ADMIN_EMAIL` / `ADMIN_PASSWORD` (the platform admin, created on first start) and the Brevo settings for emails.

Optional:
- **Push notifications:** put `firebase.json` / the APNs `.p8` key in `deploy/secrets/` and set `FIREBASE_CREDENTIALS_FILE=/secrets/firebase.json`, `APNS_KEY_FILE=/secrets/apns.p8`…
- **Statistics by country:** behind Cloudflare set `GEOIP_HEADER=CF-IPCountry`; otherwise download a free country database ([DB-IP Lite](https://db-ip.com/db/download/ip-to-country-lite) or MaxMind GeoLite2) into `deploy/geoip/country.mmdb` and set `GEOIP_DATABASE=/geoip/country.mmdb`.
- `VIRUSTOTAL_API_KEY` as a second antivirus.

`.env.production`, `secrets/` and `geoip/` are ignored by git.

## 3. Start

```bash
docker compose -f docker-compose.prod.yml --env-file .env.production up -d --build
docker compose -f docker-compose.prod.yml --env-file .env.production ps      # everything "healthy"
docker compose -f docker-compose.prod.yml --env-file .env.production logs -f api
```

Open `https://console.example.com` and sign in with `ADMIN_EMAIL`: the console first asks to set up **two-step verification** (required for the platform admin).

The first start of ClamAV downloads its signatures (a few minutes); versions uploaded meanwhile wait in the scan queue.

## 4. Update

```bash
cd /srv/kaskad/kaskad-backend && git pull
cd ../kaskad-console && git pull
cd ../kaskad-backend/deploy
docker compose -f docker-compose.prod.yml --env-file .env.production up -d --build
```

Indexes and data migrations run automatically at API startup.

## 5. Back up

`backup.sh` writes a MongoDB dump and an archive of the files to `deploy/backups/<date>/` and keeps the last 14:

```bash
./backup.sh
# every night at 3:00
( crontab -l; echo "0 3 * * * cd /srv/kaskad/kaskad-backend/deploy && ./backup.sh >> backups/backup.log 2>&1" ) | crontab -
```

Copy `deploy/backups/` to another machine or storage (rsync, rclone…). To restore MongoDB:

```bash
docker compose -f docker-compose.prod.yml --env-file .env.production exec -T mongo \
  mongorestore --username "$MONGO_ROOT_USER" --password "$MONGO_ROOT_PASSWORD" --authenticationDatabase admin \
  --archive --gzip --drop < backups/<date>/mongo.archive.gz
```

## Notes

- **One API instance.** Scheduled releases and login rate limiting live in the API process: don't scale the `api` service.
- **Clients:** build the client app with `EXPO_PUBLIC_API_URL=https://api.example.com` (HTTPS is required by Android and iOS release builds).
- **Uploads** are limited to 2 GB by Caddy (`request_body` in the `Caddyfile`), matching `MAX_UPLOAD_SIZE_MB`.
- **Changing `JWT_SECRET`** signs everyone out and invalidates the authenticator apps set up for two-step verification (recovery codes keep working).
