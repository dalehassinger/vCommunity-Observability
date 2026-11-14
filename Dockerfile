# Two image targets from one Dockerfile:
#   docker build --target core      -t vco:core .        # Python only, smallest
#   docker build --target core-pwsh -t vco:core-pwsh .   # adds PowerShell 7 + PowerCLI
#
# Stages: base (Python image) -> pwsh (base + PowerShell + PowerCLI, no app code) ->
# core / core-pwsh (Python deps, then app code on top). PowerShell and PowerCLI come from
# the build cache until PWSH_VERSION, POWERCLI_VERSION or vendor/pwsh/ change, so a code
# change or a patched Python package rebuilds only the later layers.

ARG PYTHON_IMAGE=python:3.13-slim

FROM ${PYTHON_IMAGE} AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

# PowerShell 7 + PowerCLI, built once and cached. No Python deps or app code here on purpose.
FROM base AS pwsh
USER root
# 7.6 is the current LTS (.NET 10). 7.5.x (.NET 9) crashes with "Illegal instruction"
# on some Apple Silicon Macs; override with --build-arg PWSH_VERSION=... if needed.
ARG PWSH_VERSION=7.6.9
# Leave empty for the newest PowerCLI, or pin it (recommended) e.g. 13.4.0.24798382
ARG POWERCLI_VERSION=
# Pick the PowerShell build that matches the image CPU (arm64 on Apple Silicon, x64 elsewhere).
# A local copy in vendor/pwsh/ is used when it has this exact version and CPU
# (scripts/Save-PwshVendor.ps1 fills it); otherwise it is downloaded from GitHub.
RUN --mount=type=bind,source=vendor/pwsh,target=/vendor \
    case "$(uname -m)" in aarch64|arm64) PWSH_ARCH=arm64 ;; *) PWSH_ARCH=x64 ;; esac \
 && PWSH_TGZ="powershell-${PWSH_VERSION}-linux-${PWSH_ARCH}.tar.gz" \
 && apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl libicu76 \
 && if [ -f "/vendor/$PWSH_TGZ" ]; then \
      echo "PowerShell: using local vendor/pwsh/$PWSH_TGZ"; cp "/vendor/$PWSH_TGZ" /tmp/pwsh.tgz; \
    else \
      echo "PowerShell: vendor/pwsh/$PWSH_TGZ not found, downloading"; \
      curl -fsSL -o /tmp/pwsh.tgz "https://github.com/PowerShell/PowerShell/releases/download/v${PWSH_VERSION}/$PWSH_TGZ"; \
    fi \
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
# PowerCLI: a local copy in vendor/pwsh/Modules (saved by scripts/Save-PwshVendor.ps1) is
# copied in when it has VMware.PowerCLI (and POWERCLI_VERSION, if that is set). Otherwise it
# comes from the PowerShell Gallery. PSResourceGet (built into PowerShell 7.4+) is used
# when present, Install-Module otherwise. The gallery sometimes fails a download of one of
# PowerCLI's ~20 sub-modules ("Source Location ... is not valid"), so it tries 4 times and
# stops the build with the real error if every try fails.
RUN --mount=type=bind,source=vendor/pwsh,target=/vendor \
    pwsh -NoProfile -Command " \
      \$ErrorActionPreference = 'Stop'; \
      \$v = '${POWERCLI_VERSION}'; \
      \$local = '/vendor/Modules/VMware.PowerCLI'; \
      \$dest = '/usr/local/share/powershell/Modules'; \
      if ((Test-Path \$local) -and (-not \$v -or (Test-Path (Join-Path \$local \$v)))) { \
        Write-Host 'PowerCLI: using local vendor/pwsh/Modules'; \
        New-Item -ItemType Directory -Force \$dest | Out-Null; \
        Copy-Item /vendor/Modules/* \$dest -Recurse -Force \
      } else { \
      Write-Host 'PowerCLI: no matching local copy in vendor/pwsh/Modules, using the PowerShell Gallery'; \
      for (\$i = 1; \$i -le 4; \$i++) { \
        try { \
          if (Get-Command Install-PSResource -ErrorAction SilentlyContinue) { \
            \$p = @{ Name = 'VMware.PowerCLI'; Scope = 'AllUsers'; TrustRepository = \$true; Quiet = \$true }; \
            if (\$v) { \$p.Version = \$v }; \
            Install-PSResource @p \
          } else { \
            Set-PSRepository PSGallery -InstallationPolicy Trusted; \
            if (\$v) { Install-Module VMware.PowerCLI -RequiredVersion \$v -Scope AllUsers -Force } \
            else     { Install-Module VMware.PowerCLI -Scope AllUsers -Force } \
          }; \
          break \
        } catch { \
          if (\$i -eq 4) { throw }; \
          Write-Warning ('PowerCLI download failed (try ' + \$i + ' of 4): ' + \$_ + ' Retrying in ' + (\$i * 20) + ' s.'); \
          Start-Sleep -Seconds (\$i * 20) \
        } \
      } \
      }; \
      Import-Module VMware.VimAutomation.Core" \
 && pwsh -NoProfile -Command "Set-PowerCLIConfiguration -Scope AllUsers -ParticipateInCEIP \$false -Confirm:\$false | Out-Null"

# Python-only image.
FROM base AS core
COPY requirements.txt .
# --require-hashes: every package must match the hash in the lock file.
RUN pip install --no-cache-dir --require-hashes -r requirements.txt
COPY core ./core
COPY modules ./modules
COPY config/config.example.yaml ./config/config.yaml
# The About page shows the README and its screenshots.
COPY README.md ./README.md
COPY docs/screenshots ./docs/screenshots
# Make app files readable whatever permissions they had on the build machine.
RUN chmod -R a+rX /app \
 && useradd --system --uid 10001 vco && mkdir -p /app/data /app/exports /modules && chown -R vco /app/data /app/exports /modules
USER vco
EXPOSE 8080 5514/udp 5514/tcp
VOLUME ["/app/data", "/app/exports", "/modules"]
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/core/health')"
CMD ["uvicorn", "core.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]

# Same deps and app layers on top of the cached PowerShell stage (keep in step with "core" above).
FROM pwsh AS core-pwsh
COPY requirements.txt .
# --require-hashes: every package must match the hash in the lock file.
RUN pip install --no-cache-dir --require-hashes -r requirements.txt
COPY core ./core
COPY modules ./modules
COPY config/config.example.yaml ./config/config.yaml
# The About page shows the README and its screenshots.
COPY README.md ./README.md
COPY docs/screenshots ./docs/screenshots
# Make app files readable whatever permissions they had on the build machine.
RUN chmod -R a+rX /app \
 && useradd --system --uid 10001 vco && mkdir -p /app/data /app/exports /modules && chown -R vco /app/data /app/exports /modules
USER vco
EXPOSE 8080 5514/udp 5514/tcp
VOLUME ["/app/data", "/app/exports", "/modules"]
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/core/health')"
CMD ["uvicorn", "core.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]
