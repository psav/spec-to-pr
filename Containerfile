FROM registry.access.redhat.com/ubi9/python-311:latest

USER 0

# Install proxy CA cert if present in build context (for agent VM environment)
COPY proxy-ca.crt /etc/pki/ca-trust/source/anchors/proxy-ca.crt
RUN update-ca-trust

RUN dnf install -y git make tar && \
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

RUN uv pip install --python python3.11 --system-certs --system /opt/spec-to-pr/

# Configure git identity for commits
RUN git config --system user.name "spec-to-pr-bot" && \
    git config --system user.email "noreply@spec-to-pr.local"

ENTRYPOINT ["spec-to-pr"]
CMD ["--help"]
