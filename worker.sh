#!/bin/bash
set -e

echo "=========================================================="
echo " 🚀 VAST.AI BLENDER RENDER RUNNER"
echo "=========================================================="

# Ensure git, curl, unzip, xz-utils, blender runtime libraries, python3, and gdown are present
apt-get update -qq && apt-get install -y -qq git curl unzip xz-utils libgl1 libxi6 libxrender1 libxfixes3 python3 python3-pip > /dev/null 2>&1 || true
pip install -q gdown > /dev/null 2>&1 || python3 -m pip install -q gdown > /dev/null 2>&1 || true

# Look for nvoptix.bin on system and link if present
if [ ! -f /usr/share/nvidia/nvoptix.bin ]; then
    mkdir -p /usr/share/nvidia
    NVOPTIX_FILE=$(find /usr -name "nvoptix.bin" 2>/dev/null | head -n 1)
    if [ -n "$NVOPTIX_FILE" ]; then
        ln -sf "$NVOPTIX_FILE" /usr/share/nvidia/nvoptix.bin
    fi
fi

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
