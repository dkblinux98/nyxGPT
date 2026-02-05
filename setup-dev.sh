#!/bin/bash
# Development environment setup script for nyxAgent

set -e  # Exit on error

echo "========================================="
echo "nyxAgent Development Environment Setup"
echo "========================================="
echo ""

# Check Python version
echo "Checking Python version..."
python_version=$(python3 --version 2>&1 | awk '{print $2}')
required_version="3.11"

if [ "$(printf '%s\n' "$required_version" "$python_version" | sort -V | head -n1)" != "$required_version" ]; then
    echo "❌ Error: Python 3.11+ is required (found $python_version)"
    exit 1
fi
echo "✓ Python $python_version detected"
echo ""

# Check if Docker is running
echo "Checking Docker..."
if ! docker info > /dev/null 2>&1; then
    echo "⚠ Warning: Docker is not running"
    echo "  Services (PostgreSQL, Redis, Cassandra) will not be available"
    echo "  Start Docker and run 'make docker-up' to start services"
else
    echo "✓ Docker is running"
fi
echo ""

# Create virtual environment if it doesn't exist
if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
    echo "✓ Virtual environment created"
else
    echo "✓ Virtual environment already exists"
fi
echo ""

# Activate virtual environment
echo "Activating virtual environment..."
source .venv/bin/activate
echo "✓ Virtual environment activated"
echo ""

# Upgrade pip
echo "Upgrading pip..."
pip install --upgrade pip setuptools wheel > /dev/null 2>&1
echo "✓ pip upgraded"
echo ""

# Install development dependencies
echo "Installing development dependencies..."
pip install -e ".[dev,ui,postgres]" > /dev/null 2>&1
echo "✓ Development dependencies installed"
echo ""

# Install pre-commit hooks
echo "Installing pre-commit hooks..."
pre-commit install > /dev/null 2>&1
echo "✓ Pre-commit hooks installed"
echo ""

# Run pre-commit on all files (optional, can be slow)
read -p "Run pre-commit checks on all files? (y/N): " run_precommit
if [ "$run_precommit" = "y" ] || [ "$run_precommit" = "Y" ]; then
    echo ""
    echo "Running pre-commit checks..."
    pre-commit run --all-files || true
    echo ""
fi

# Start Docker services if Docker is running
if docker info > /dev/null 2>&1; then
    read -p "Start Docker services (PostgreSQL, Redis, Cassandra)? (y/N): " start_docker
    if [ "$start_docker" = "y" ] || [ "$start_docker" = "Y" ]; then
        echo ""
        echo "Starting Docker services..."
        docker-compose up -d
        echo "✓ Services started"
        echo ""
        echo "Services running:"
        echo "  - PostgreSQL: localhost:5432"
        echo "  - Redis: localhost:6379"
        echo "  - Cassandra: localhost:9042"
        echo ""
    fi
fi

echo "========================================="
echo "✓ Setup Complete!"
echo "========================================="
echo ""
echo "Next steps:"
echo "  1. Activate venv: source .venv/bin/activate"
echo "  2. Run tests: make test"
echo "  3. Start coding!"
echo ""
echo "Available commands:"
echo "  make help         - Show all available commands"
echo "  make test         - Run all tests"
echo "  make pre-commit   - Run code quality checks"
echo "  make docker-up    - Start services"
echo "  make docker-down  - Stop services"
echo ""
echo "Documentation:"
echo "  - README.md - Project overview"
echo "  - CONTRIBUTING.md - Development guidelines"
echo "  - ARCHITECTURE.md - Architecture constraints"
echo ""
