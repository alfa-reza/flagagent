FROM kalilinux/kali-rolling

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/usr/local/bin:$PATH

# Keep the image reproducible at the package-layer level: refresh once, install
# the stable Kali packages needed by the CTF agent, then clean APT metadata.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    wget \
    git \
    jq \
    file \
    xxd \
    unzip \
    zip \
    p7zip-full \
    p7zip-rar \
    tar \
    gzip \
    bzip2 \
    xz-utils \
    zstd \
    rsync \
    procps \
    iproute2 \
    iputils-ping \
    net-tools \
    dnsutils \
    whois \
    netcat-openbsd \
    socat \
    openssl \
    jq \
    ripgrep \
    fd-find \
    tree \
    vim-tiny \
    tmux \
    strace \
    ltrace \
    lsof \
    psmisc \
    build-essential \
    pkg-config \
    python3 \
    python3-dev \
    python3-pip \
    python3-venv \
    python3-setuptools \
    python3-wheel \
    python3-pwntools \
    gdb \
    gdbserver \
    binutils \
    patchelf \
    radare2 \
    nasm \
    yasm \
    clang \
    llvm \
    make \
    cmake \
    gcc \
    g++ \
    golang \
    rustc \
    cargo \
    nmap \
    masscan \
    nikto \
    sqlmap \
    gobuster \
    ffuf \
    feroxbuster \
    hydra \
    john \
    hashcat \
    hashid \
    hashcat-utils \
    impacket-scripts \
    smbclient \
    ldap-utils \
    rpcbind \
    snmp \
    snmp-mibs-downloader \
    tshark \
    wireshark-common \
    tcpdump \
    mitmproxy \
    exiftool \
    foremost \
    binwalk \
    sleuthkit \
    testdisk \
    yara \
    fplll-tools \
    pari-gp \
    libimage-exiftool-perl \
    && rm -rf /var/lib/apt/lists/*

# Kali/Debian's externally-managed Python environment should not be bypassed.
# Put optional Python CTF tooling in its own venv.
RUN python3 -m venv /opt/ctf-venv \
    && /opt/ctf-venv/bin/pip install --no-cache-dir --upgrade pip setuptools wheel \
    && /opt/ctf-venv/bin/pip install --no-cache-dir \
        volatility3 \
        requests \
        pwntools \
    && ln -sf /opt/ctf-venv/bin/vol /usr/local/bin/vol \
    && ln -sf /opt/ctf-venv/bin/pwntools /usr/local/bin/pwntools || true

# Go tools: set GOBIN directly to a directory on PATH.  Do NOT use
# /opt/go-out/bin; with GOBIN=/opt/go-out, `go install` writes /opt/go-out/<tool>.
RUN GOBIN=/usr/local/bin go install github.com/wader/fq@latest

# A few useful Go-based CTF utilities.  Fail the build if upstream changes
# make one of these unavailable rather than silently shipping a partial image.
RUN GOBIN=/usr/local/bin go install github.com/tomnomnom/assetfinder@latest \
    && GOBIN=/usr/local/bin go install github.com/tomnomnom/httprobe@latest \
    && GOBIN=/usr/local/bin go install github.com/tomnomnom/anew@latest

# Helper for GitHub release assets.  It resolves the actual asset name from the
# release API instead of hard-coding fragile filenames.
RUN cat >/usr/local/bin/install-github-release <<'EOF'
#!/bin/bash
set -euo pipefail
repo="$1"
pattern="$2"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

asset="$(
  curl -fsSL --retry 5 --retry-delay 2 \
    "https://api.github.com/repos/${repo}/releases/latest" |
  jq -r --arg re "$pattern" '
    .assets[]
    | select(.name | test($re; "i"))
    | .browser_download_url
  ' | head -n1
)"

if [[ -z "$asset" || "$asset" == "null" ]]; then
  echo "No release asset matched /$pattern/ for $repo" >&2
  exit 1
fi

name="${asset##*/}"
curl -fsSL --retry 5 --retry-delay 2 "$asset" -o "$tmp/$name"

case "$name" in
  *.tar.gz|*.tgz)
    tar -xzf "$tmp/$name" -C "$tmp"
    ;;
  *.tar.xz)
    tar -xJf "$tmp/$name" -C "$tmp"
    ;;
  *.zip)
    unzip -q "$tmp/$name" -d "$tmp/unpack"
    ;;
esac

# Find the first executable file that is not the downloaded archive.
bin="$(
  find "$tmp" -type f -perm -u+x \
    ! -name '*.tar.gz' ! -name '*.tgz' ! -name '*.tar.xz' ! -name '*.zip' |
  head -n1
)"

if [[ -z "$bin" ]]; then
  echo "No executable found in release asset $name" >&2
  exit 1
fi

install -m 0755 "$bin" "/usr/local/bin/${3:-${repo##*/}}"
EOF
RUN chmod +x /usr/local/bin/install-github-release

# Release binaries: use upstream Linux amd64 artifacts where available.
# These are deliberately isolated from Cargo builds, avoiding Rust registry/
# certificate failures and long native compilation during Docker builds.
RUN install-github-release Sh1Yo/x8 'linux.*(amd64|x86_64).*' x8 \
    && install-github-release slimm609/checksec 'linux.*(amd64|x86_64).*' checksec

# Common aliases expected by agents.
RUN ln -sf /usr/bin/fd-find /usr/local/bin/fd || true \
    && ln -sf /usr/bin/python3 /usr/local/bin/python

# Build-time smoke tests: catch missing binaries, broken PATH, and architecture
# mistakes before the image is published.
RUN set -eux; \
    command -v bash; \
    command -v curl; \
    command -v git; \
    command -v python3; \
    command -v go; \
    command -v cargo; \
    command -v nmap; \
    command -v ffuf; \
    command -v feroxbuster; \
    command -v gobuster; \
    command -v sqlmap; \
    command -v impacket-smbclient || true; \
    command -v tshark; \
    command -v tcpdump; \
    command -v gdb; \
    command -v radare2; \
    command -v x8; \
    command -v checksec; \
    command -v fq; \
    python3 -c 'import pwn, requests'; \
    python3 -c 'import volatility3'; \
    fq --version; \
    x8 --help >/dev/null; \
    checksec --help >/dev/null

WORKDIR /workspace
ENTRYPOINT ["/bin/bash"]
