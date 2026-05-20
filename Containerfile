FROM registry.access.redhat.com/ubi9/python-311:latest

USER 0

# Optionally install a proxy CA cert at build time (e.g. egress proxy environments).
# Pass the PEM content via: --build-arg PROXY_CA_CERT="$(cat /path/to/ca.crt)"
ARG PROXY_CA_CERT=""
RUN if [ -n "${PROXY_CA_CERT}" ]; then \
        echo "${PROXY_CA_CERT}" > /etc/pki/ca-trust/source/anchors/proxy-ca.crt && \
        update-ca-trust; \
    fi

RUN dnf install -y git make tar jq podman && \
    curl -fsSL https://cli.github.com/packages/rpm/gh-cli.repo \
        -o /etc/yum.repos.d/github-cli.repo && \
    dnf install -y gh && \
    dnf clean all

# Install uv from astral.sh (in proxy allowlist)
RUN curl -LsSf https://astral.sh/uv/install.sh | sh && \
    mv /opt/app-root/src/.local/bin/uv /usr/local/bin/uv && \
    mv /opt/app-root/src/.local/bin/uvx /usr/local/bin/uvx

WORKDIR /workspace

COPY . /opt/spec-to-pr/

ARG PROXY_CA_CERT
RUN if [ -n "${PROXY_CA_CERT}" ]; then \
        uv pip install --python python3.11 --native-tls --system /opt/spec-to-pr/; \
    else \
        uv pip install --python python3.11 --system-certs --system /opt/spec-to-pr/; \
    fi

# Configure git identity for commits
RUN git config --system user.name "spec-to-pr-bot" && \
    git config --system user.email "noreply@spec-to-pr.local"

ENTRYPOINT ["spec-to-pr"]
CMD ["--help"]
