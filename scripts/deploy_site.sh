#!/bin/sh
# Publish Portland in voxels on Cloudflare: the app (web/, ~2 MB) as a Worker's static assets on SITE, the map's data
# (web/public/data: ~1.1 GB, ~83,000 files, almost all tiles) in an R2 bucket served on DATA. Too many files for a
# Worker's assets (20,000 free / 100,000 paid) or GitHub Pages (1 GB); R2 has no egress fees, and with a custom domain
# Cloudflare's cache serves repeat requests without touching the bucket.
#
# usage: sh scripts/deploy_site.sh data   # sync the data to R2
#        sh scripts/deploy_site.sh app    # build + deploy the app
#        sh scripts/deploy_site.sh        # both, data first
# (SITE and DATA default to portland.ooo-yay.com and tiles.ooo-yay.com)
#
# The data sync uses rclone (brew install rclone) over R2's S3 API, with an R2 API token (Object Read & Write on the
# bucket) from the environment, never from a file in the repo:
#   R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY   (dashboard: R2 > Manage API tokens)
#   CLOUDFLARE_ACCOUNT_ID                    (npx wrangler whoami)
# `rclone sync` uploads only what changed and deletes what the build no longer makes (an old transit feed's files).
# One-time setup (bucket, its domain, CORS) is printed by `sh scripts/deploy_site.sh setup`.
set -e
cd "$(dirname "$0")/.."
SITE=${SITE:-portland.ooo-yay.com}  # the site (also the Worker's custom domain in web/wrangler.jsonc)
DATA=${DATA:-tiles.ooo-yay.com}     # the data: the R2 bucket's custom domain
BUCKET=${BUCKET:-portland-voxel-data}
what=${1:-all}

if [ "$what" = setup ]; then
  cat <<EOF
one-time setup (wrangler, logged in):
  npx wrangler r2 bucket create $BUCKET --location wnam
  npx wrangler r2 bucket domain add $BUCKET --domain $DATA --zone-id <the zone's id>
  npx wrangler r2 bucket cors set $BUCKET --file scripts/r2-cors.json   (after: SITE=$SITE sh scripts/deploy_site.sh cors)
EOF
  exit 0
fi

if [ "$what" = cors ]; then  # who may read the data from a browser: the site, and the dev server for testing
  cat > scripts/r2-cors.json <<EOF
{
  "rules": [
    { "allowed": { "origins": ["https://$SITE", "http://localhost:5179"], "methods": ["GET", "HEAD"] } }
  ]
}
EOF
  npx wrangler r2 bucket cors set "$BUCKET" --file scripts/r2-cors.json -y
  exit 0
fi

if [ "$what" = data ] || [ "$what" = all ]; then
  : "${R2_ACCESS_KEY_ID:?set R2_ACCESS_KEY_ID (an R2 API token with Object Read & Write on $BUCKET)}"
  : "${R2_SECRET_ACCESS_KEY:?set R2_SECRET_ACCESS_KEY}"
  : "${CLOUDFLARE_ACCOUNT_ID:?set CLOUDFLARE_ACCOUNT_ID (npx wrangler whoami shows it)}"
  export RCLONE_CONFIG_R2_TYPE=s3 RCLONE_CONFIG_R2_PROVIDER=Cloudflare RCLONE_CONFIG_R2_ACL=private \
    RCLONE_CONFIG_R2_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID" RCLONE_CONFIG_R2_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY" \
    RCLONE_CONFIG_R2_ENDPOINT="https://$CLOUDFLARE_ACCOUNT_ID.r2.cloudflarestorage.com" RCLONE_CONFIG_R2_NO_CHECK_BUCKET=true
  # the token page shows three values: the S3 access key ID (32 characters) and secret (64), not the token value.
  # With wrong keys rclone 1.75 reports the destination as "a file not a directory": check them first instead.
  if [ ${#R2_ACCESS_KEY_ID} -ne 32 ] || [ ${#R2_SECRET_ACCESS_KEY} -ne 64 ]; then
    echo "R2_ACCESS_KEY_ID should be 32 characters (got ${#R2_ACCESS_KEY_ID}) and R2_SECRET_ACCESS_KEY 64 (got ${#R2_SECRET_ACCESS_KEY}):"
    echo "use the 'Access Key ID' and 'Secret Access Key' from R2 > Manage API tokens, not the token value"
    exit 1
  fi
  rclone lsf "r2:$BUCKET" --max-depth 1 >/dev/null 2>&1 || {
    echo "R2 refused these keys for bucket $BUCKET: check the token has Object Read & Write on it"; exit 1; }
  # tiles change only with a rebuild: a day in browsers; the rest (meta, places, streets, transit) an hour
  rclone sync web/public/data/tiles "r2:$BUCKET/tiles" --transfers 64 --checkers 64 --fast-list --progress \
    --header-upload "Cache-Control: public, max-age=86400" --header-upload "Content-Type: application/octet-stream"
  rclone sync web/public/data "r2:$BUCKET" --exclude "tiles/**" --transfers 16 --fast-list --progress \
    --header-upload "Cache-Control: public, max-age=3600"
fi

if [ "$what" = app ] || [ "$what" = all ]; then
  (cd web && VITE_DATA_URL="https://$DATA/" VITE_SITE_URL="https://$SITE/" pnpm build:site)
  (cd web && npx wrangler deploy)
fi
