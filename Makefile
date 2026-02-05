# Makefile for nyxAgent development
.PHONY: help install install-dev install-hooks test test-unit test-integration test-e2e coverage lint format type-check pre-commit clean docker-up docker-down

# Default target
help:
	@echo "nyxAgent Development Commands"
	@echo ""
	@echo "Setup:"
	@echo "  make install          Install package in development mode"
	@echo "  make install-dev      Install with all development dependencies"
	@echo "  make install-hooks    Install pre-commit hooks"
	@echo ""
	@echo "Code Quality:"
	@echo "  make lint             Run ruff linter"
	@echo "  make format           Run black formatter"
	@echo "  make type-check       Run mypy type checker"
	@echo "  make yaml-lint        Run yamllint on YAML files"
	@echo "  make pre-commit       Run all pre-commit hooks"
	@echo ""
	@echo "Testing:"
	@echo "  make test             Run all tests"
	@echo "  make test-unit        Run unit tests only"
	@echo "  make test-integration Run integration tests only"
	@echo "  make test-e2e         Run end-to-end tests only"
	@echo "  make coverage         Run tests with coverage report"
	@echo ""
	@echo "Docker:"
	@echo "  make docker-up        Start all services (PostgreSQL, Redis, Cassandra)"
	@echo "  make docker-down      Stop all services"
	@echo ""
	@echo "Maintenance:"
	@echo "  make clean            Remove build artifacts and caches"

# Installation
install:
	pip install -e .

install-dev:
	pip install -e ".[dev,ui,postgres]"

install-hooks:
	pre-commit install
	@echo "✓ Pre-commit hooks installed"

# Code Quality
lint:
	ruff check src/ tests/

format:
	black src/ tests/
	ruff check --fix src/ tests/

type-check:
	mypy src/

yaml-lint:
	yamllint .

pre-commit:
	pre-commit run --all-files

# Testing
test:
	pytest

test-unit:
	pytest -m unit

test-integration:
	pytest -m integration

test-e2e:
	pytest -m e2e

coverage:
	pytest --cov=nyxagent --cov-report=term-missing --cov-report=html
	@echo "Coverage report: htmlcov/index.html"

# Docker
docker-up:
	docker-compose up -d
	@echo "✓ Services started: PostgreSQL, Redis, Cassandra"

docker-down:
	docker-compose down
	@echo "✓ Services stopped"

# Maintenance
clean:
	rm -rf build/
	rm -rf dist/
	rm -rf *.egg-info
	rm -rf .pytest_cache/
	rm -rf .mypy_cache/
	rm -rf .ruff_cache/
	rm -rf htmlcov/
	rm -rf .coverage
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	find . -type f -name "*.pyo" -delete
	@echo "✓ Cleaned build artifacts and caches"
