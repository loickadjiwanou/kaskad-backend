#!/usr/bin/env sh
# Sauvegarde de MongoDB (mongodump) et des fichiers (volume MinIO) dans ./backups/<date>.
# À lancer depuis kaskad-backend/deploy, par exemple chaque nuit via cron :
#   0 3 * * * cd /srv/kaskad/kaskad-backend/deploy && ./backup.sh >> backups/backup.log 2>&1
set -eu
. ./.env.production
STAMP=$(date +%Y-%m-%d_%H%M)
DEST="backups/$STAMP"
mkdir -p "$DEST"
COMPOSE="docker compose -f docker-compose.prod.yml --env-file .env.production"

$COMPOSE exec -T mongo mongodump --username "$MONGO_ROOT_USER" --password "$MONGO_ROOT_PASSWORD" \
    --authenticationDatabase admin --db kaskad --archive --gzip > "$DEST/mongo.archive.gz"
$COMPOSE exec -T minio tar -C /data -czf - . > "$DEST/files.tar.gz"

# Conserve les 14 dernières sauvegardes
ls -1d backups/20* | head -n -14 | xargs -r rm -rf
echo "Backup written to $DEST"
