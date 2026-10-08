#!/bin/bash
set -e

echo "=========================================================="
echo " 🚀 VAST.AI BLENDER RENDER RUNNER"
echo "=========================================================="

# Ensure git and curl are present
apt-get update -qq && apt-get install -y -qq git curl python3 > /dev/null 2>&1 || true

WORK_DIR="/workspace/VastAIReciever"
if [ -d "$WORK_DIR/.git" ]; then
    echo "[*] Updating VastAIReciever repository..."
    cd "$WORK_DIR"
    git pull origin main || git pull origin master || true
else
    echo "[*] Cloning VastAIReciever repository..."
    mkdir -p /workspace
    git clone https://github.com/hamkaaaa/VastAIReciever.git "$WORK_DIR"
    cd "$WORK_DIR"
fi

python3 worker.py "$@"
