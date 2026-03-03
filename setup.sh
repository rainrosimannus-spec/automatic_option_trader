#!/bin/bash
# ============================================================
# Options Trader — First-time Setup Script
# Run this once: ./setup.sh
# ============================================================
set -e

echo "⚡ Options Trader — Setup"
echo "========================="
echo ""

# Check Python version
PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)

if [ "$MAJOR" -lt 3 ] || ([ "$MAJOR" -eq 3 ] && [ "$MINOR" -lt 11 ]); then
    echo "❌ Python 3.11+ required. Found: $PYTHON_VERSION"
    exit 1
fi
echo "✅ Python $PYTHON_VERSION"

# Create virtual environment
if [ ! -d ".venv" ]; then
    echo "📦 Creating virtual environment..."
    python3 -m venv .venv
    echo "✅ Virtual environment created"
else
    echo "✅ Virtual environment exists"
fi

# Activate
source .venv/bin/activate

# Install dependencies
echo "📦 Installing dependencies..."
pip install -e . --quiet
echo "✅ Dependencies installed"

# Create data directory
mkdir -p data
mkdir -p logs

# Create .env if not exists
if [ ! -f "config/.env" ]; then
    cp config/.env.example config/.env
    echo "✅ config/.env created from template"
else
    echo "✅ config/.env already exists"
fi

echo ""
echo "════════════════════════════════════════════"
echo "  Setup complete!"
echo ""
echo "  Before launching, make sure:"
echo "  1. TWS for Options Trader account (U23886415) is open"
echo "  2. API is enabled on port 4001"
echo ""
echo "  To start:  ./start.sh"
echo "  Dashboard: http://localhost:8000"
echo "════════════════════════════════════════════"
