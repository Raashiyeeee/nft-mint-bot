#!/bin/sh
set -eu

if [ ! -s /run/secrets/fernet_key ]; then
    echo "ERROR: /run/secrets/fernet_key is missing or empty"
    exit 1
fi

export FERNET_KEY="$(cat /run/secrets/fernet_key)"

exec ./nft-mint-engine
