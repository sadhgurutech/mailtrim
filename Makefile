.PHONY: check lint format test security install-dev

# Run the full local CI equivalent — same checks as GitHub Actions
check: lint format security test

lint:
	ruff check mailtrim/

format:
	ruff format --check mailtrim/ tests/

security:
	bandit -r mailtrim/ -ll -q

test:
	python -m pytest tests/ -q --tb=short

# Fix lint and format issues in place (use before committing)
fix:
	ruff check mailtrim/ --fix
	ruff format mailtrim/ tests/

# Install all dev dependencies + pre-commit hooks
install-dev:
	pip install -e ".[dev]"
	pre-commit install
	@echo "Done. pre-commit will now run on every git commit."
