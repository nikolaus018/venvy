#!/bin/sh
# Rebuild web/static/css/tailwind.css after changing class names in web/.
# Needs Node.js (npx); the result is committed, so Venvy itself never does.
# CI runs this too and fails if the committed file is out of date.
set -e
cd "$(dirname "$0")/.."
npx --yes tailwindcss@3.4.17 --config web/tailwind.config.js \
    --input web/tailwind.src.css --output web/static/css/tailwind.css --minify
