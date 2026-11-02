# Two image targets from one Dockerfile:
#   docker build --target core      -t vco:core .        # Python only, smallest
#   docker build --target core-pwsh -t vco:core-pwsh .   # adds PowerShell 7 + PowerCLI
#
# Layers are ordered base OS -> Python deps -> PowerShell -> app code, so patching
# one dependency (e.g. a FastAPI CVE) rebuilds only from that layer down.

ARG PYTHON_IMAGE=python:3.13-slim

FROM ${PYTHON_IMAGE} AS core
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
# --require-hashes: every package must match the hash in the lock file.
RUN pip install --no-cache-dir --require-hashes -r requirements.txt
COPY core ./core
COPY modules ./modules
COPY config/config.example.yaml ./config/config.yaml
# Make app files readable whatever permissions they had on the build machine.
RUN chmod -R a+rX /app \
 && useradd --system --uid 10001 vco && mkdir -p /app/data /app/exports /modules && chown -R vco /app/data /app/exports /modules
USER vco
EXPOSE 8080
VOLUME ["/app/data", "/app/exports", "/modules"]
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/core/health')"
CMD ["uvicorn", "core.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]

FROM core AS core-pwsh
USER root
# 7.6 is the current LTS (.NET 10). 7.5.x (.NET 9) crashes with "Illegal instruction"
# on some Apple Silicon Macs; override with --build-arg PWSH_VERSION=... if needed.
ARG PWSH_VERSION=7.6.9
# Leave empty for the newest PowerCLI, or pin it (recommended) e.g. 13.4.0.24798382
ARG POWERCLI_VERSION=
# Pick the PowerShell build that matches the image CPU (arm64 on Apple Silicon, x64 elsewhere).
RUN case "$(uname -m)" in aarch64|arm64) PWSH_ARCH=arm64 ;; *) PWSH_ARCH=x64 ;; esac \
 && apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl libicu76 \
 && curl -fsSL -o /tmp/pwsh.tgz \
      "https://github.com/PowerShell/PowerShell/releases/download/v${PWSH_VERSION}/powershell-${PWSH_VERSION}-linux-${PWSH_ARCH}.tar.gz" \
 && mkdir -p /opt/microsoft/powershell \
 && tar xzf /tmp/pwsh.tgz -C /opt/microsoft/powershell \
 && chmod +x /opt/microsoft/powershell/pwsh \
 && ln -s /opt/microsoft/powershell/pwsh /usr/local/bin/pwsh \
 && rm /tmp/pwsh.tgz \
 && apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*
# Some Mac VMs (Rancher Desktop, newer M-series chips) don't pass every ARM CPU
# feature to Linux, and .NET (PowerShell) then dies with "Illegal instruction".
# Build with --build-arg DOTNET_EnableHWIntrinsic=0 (or set it in docker-compose.yml)
# to turn those CPU fast paths off. 1 is the normal .NET default.
ARG DOTNET_EnableHWIntrinsic=1
ENV POWERSHELL_TELEMETRY_OPTOUT=1 DOTNET_EnableHWIntrinsic=${DOTNET_EnableHWIntrinsic}
RUN pwsh -NoProfile -Command " \
      Set-PSRepository PSGallery -InstallationPolicy Trusted; \
      \$v = '${POWERCLI_VERSION}'; \
      if (\$v) { Install-Module VMware.PowerCLI -RequiredVersion \$v -Scope AllUsers -Force } \
      else     { Install-Module VMware.PowerCLI -Scope AllUsers -Force }" \
 && pwsh -NoProfile -Command "Set-PowerCLIConfiguration -Scope AllUsers -ParticipateInCEIP \$false -Confirm:\$false | Out-Null"
USER vco
