#!/bin/sh
# Build and copy the plugin into Decky's plugins folder for local testing.
# One-time setup (needs sudo, the folder is root-owned):
#   sudo install -d -o "$USER" -g "$USER" ~/homebrew/plugins/decky-oled-tint
set -e
cd "$(dirname "$0")/.."
DEST="$HOME/homebrew/plugins/decky-oled-tint"
pnpm build
mkdir -p "$DEST/dist"
# Decky chowns the plugin folder and plugin.json to root on restart, so after
# the first deploy only the files it left writable can be updated. The
# backend and frontend must be: a skipped main.py would leave the old backend
# running behind a "Deployed".
for f in plugin.json package.json main.py LICENSE README.md; do
  if [ ! -e "$DEST/$f" ] || [ -w "$DEST/$f" ]; then
    cp "$f" "$DEST/"
  elif ! cmp -s "$f" "$DEST/$f"; then
    echo "warning: $DEST/$f isn't writable and differs (left as it is)"
  fi
done
cp dist/index.js "$DEST/dist/"
for f in main.py dist/index.js; do
  cmp -s "$f" "$DEST/$f" || { echo "NOT deployed: $DEST/$f is not the new $f (owned by root?)"; exit 1; }
done
echo "Deployed to $DEST"
