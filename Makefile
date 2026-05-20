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
	@echo "Building container..."
	@if [ -f /etc/pki/ca-trust/source/anchors/proxy-ca.crt ]; then \
		echo "Found proxy CA cert, passing as build arg..."; \
		podman build -t spec-to-pr:latest -f Containerfile \
			--build-arg PROXY_CA_CERT="$$(cat /etc/pki/ca-trust/source/anchors/proxy-ca.crt)" .; \
	else \
		echo "No proxy CA cert found, building without it..."; \
		podman build -t spec-to-pr:latest -f Containerfile .; \
	fi

# Run tests
test:
	uv run pytest tests/ -v

# Clean build artifacts
clean:
	rm -rf .venv __pycache__ .pytest_cache .spec-to-pr
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
