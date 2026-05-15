# Stub ephemeral targets for local demo/testing.
# In production these are provided by the root Makefile.

.PHONY: ephemeral-dev resync ephemeral-e2e build test clean

ephemeral-dev:
	@echo "[stub] Ephemeral environment provisioned"

resync:
	@echo "[stub] Resynced to branch"

ephemeral-e2e:
	@echo "[stub] All e2e tests passed"

# Container build with proxy CA cert
build:
	@echo "Copying proxy CA cert into build context..."
	@if [ -f /etc/pki/ca-trust/source/anchors/proxy-ca.crt ]; then \
		cp /etc/pki/ca-trust/source/anchors/proxy-ca.crt .; \
	else \
		echo "Warning: proxy CA cert not found, creating dummy cert"; \
		touch proxy-ca.crt; \
	fi
	@echo "Building container..."
	podman build -t spec-to-pr:latest -f Containerfile .
	@rm -f proxy-ca.crt

# Run tests
test:
	uv run pytest tests/ -v

# Clean build artifacts
clean:
	rm -rf .venv __pycache__ .pytest_cache .spec-to-pr
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
