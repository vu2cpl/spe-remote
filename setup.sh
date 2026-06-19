#!/bin/bash
# SPE Remote Control - Setup Script for Raspberry Pi
set -e

echo "=== SPE Remote Control Setup ==="

# Ensure python3-venv is available
if ! dpkg -s python3-venv &>/dev/null; then
    echo "Installing python3-venv..."
    sudo apt-get update && sudo apt-get install -y python3-full python3-venv
fi

# Create virtual environment
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

# Install dependencies
echo "Installing dependencies..."
venv/bin/pip install --upgrade pip
venv/bin/pip install -r requirements.txt

# First-time configuration: pick the amplifier's serial port and, if you
# have one, the Flex radio. Existing settings are retained on later runs,
# so this only prompts once (delete .configured to run it again, or just
# run ./configure.sh any time).
if [ ! -f ".configured" ]; then
    if [ -t 0 ]; then
        echo ""
        echo "=== First-time configuration ==="
        ./configure.sh && touch .configured
    else
        echo ""
        echo "Skipping interactive configuration (no terminal attached)."
        echo "Run ./configure.sh later to select the serial port and Flex radio."
    fi
else
    echo ""
    echo "Existing configuration retained. Run ./configure.sh to change it."
fi

echo ""
echo "=== Setup complete ==="
echo "To run:  ./run.sh"
echo "Or:      venv/bin/python server.py"
