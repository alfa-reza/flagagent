# FlagAgent HEAD sandbox — curated Kali CTF workstation (candidate).
# Minimal kali-rolling base, selective install only (no kali-linux-headless).
# Complements images/sandbox/Dockerfile, which stays the minimal harness-smoke
# image until solve-rate evidence justifies a switch of the default.
#
# Base digest (multi-arch image index, fetched 2026-09-09). Re-pin before
# rebuilding: docker buildx imagetools inspect kalilinux/kali-rolling@sha256:<digest>
#   kalilinux/kali-rolling@sha256:c6d78f57ebfdd9fec428a8b04f74876e66fddd08dfd328c076224a552e7332a3
#
# Build (tag :dev — the harness has no --image flag and always uses it):
#   docker build -f ~/artefak/Dockerfile -t flagagent-sandbox:head ~/artefak
#   docker tag flagagent-sandbox:head flagagent-sandbox:dev
#
# The harness runs this as a numeric host UID:GID with --cap-drop ALL,
# no-new-privileges, 2g/2cpu/pids 256, network none/internal, -w /workspace.
# Test under those exact constraints, never with a plain `docker run`.
# 1) Rust tools. Same Kali base as runtime keeps glibc compatible.
# Versions pinned; cargo verifies registry checksums. yara-x-cli (not the
# `yara-x` crate) provides the `yr` binary.
FROM kalilinux/kali-rolling@sha256:c6d78f57ebfdd9fec428a8b04f74876e66fddd08dfd328c076224a552e7332a3 AS rust-builder
ARG DEBIAN_FRONTEND=noninteractive
RUN set -eux; \
    apt-get update; \
    apt-get install --no-install-recommends --yes cargo pkg-config libssl-dev ca-certificates; \
    update-ca-certificates; \
    test -s /etc/ssl/certs/ca-certificates.crt; \
    rm -rf /var/lib/apt/lists/*
RUN set -eux; \
    cargo install --root /opt/rust-out --git https://github.com/VirusTotal/yara-x.git --tag v1.20.0 --locked yara-x-cli; \
    cargo install --root /opt/rust-out --version 3.3.3 pwninit; \
    cargo install --root /opt/rust-out --version 0.0.9 checksec; \
    cargo install --root /opt/rust-out --version 0.2.26 ropr; \
    cargo install --root /opt/rust-out --version 4.3.2 x8; \
    cargo install --root /opt/rust-out --version 0.12.0 ciphey; \
    ls /opt/rust-out/bin
# 2) Go tools. The SDK is purged afterwards so it never reaches runtime.
FROM kalilinux/kali-rolling@sha256:c6d78f57ebfdd9fec428a8b04f74876e66fddd08dfd328c076224a552e7332a3 AS go-builder
ARG DEBIAN_FRONTEND=noninteractive
RUN set -eux; \
    apt-get update; \
    apt-get install --no-install-recommends --yes golang-go ca-certificates; \
    rm -rf /var/lib/apt/lists/*; \
    GOBIN=/opt/go-out go install github.com/wader/fq@v0.18.0; \
    ls /opt/go-out/bin
# 3) Native tools. Every source is pinned to a full commit SHA; the rev-parse
# guard fails the build on drift instead of silently tracking HEAD.
FROM kalilinux/kali-rolling@sha256:c6d78f57ebfdd9fec428a8b04f74876e66fddd08dfd328c076224a552e7332a3 AS native-builder
ARG DEBIAN_FRONTEND=noninteractive
RUN set -eux; \
    apt-get update; \
    apt-get install --no-install-recommends --yes \
        build-essential cmake git ca-certificates \
        libgmp-dev libmpfr-dev libecm-dev \
        fplll-tools libfplll-dev libeigen3-dev libopenblas-dev; \
    rm -rf /var/lib/apt/lists/*; \
    mkdir -p /opt/stage/bin /opt/stage/lib /opt/stage/sha
RUN set -eux; \
    git init -q /tmp/flatter; \
    git -C /tmp/flatter fetch --depth=1 --no-tags https://github.com/keeganryan/flatter.git d2b8026f29b4a69e987b15d4b240f8a5053275d3; \
    git -C /tmp/flatter checkout --detach -q FETCH_HEAD; \
    test "$(git -C /tmp/flatter rev-parse HEAD)" = "d2b8026f29b4a69e987b15d4b240f8a5053275d3"; \
    echo d2b8026f29b4a69e987b15d4b240f8a5053275d3 > /opt/stage/sha/flatter.BUILD_SHA; \
    cmake -S /tmp/flatter -B /tmp/flatter/build -DCMAKE_BUILD_TYPE=Release; \
    cmake --build /tmp/flatter/build -j"$(nproc)"; \
    cmake --install /tmp/flatter/build --prefix /opt/stage/usr; \
    ls /opt/stage/usr/bin/flatter; \
    latticegen q 4 2 10 b | /opt/stage/usr/bin/flatter -q; \
    rm -rf /tmp/flatter
# Canonical upstream kimci86 (not the owenjamesbecker fork).
RUN set -eux; \
    git init -q /tmp/bkcrack; \
    git -C /tmp/bkcrack fetch --depth=1 --no-tags https://github.com/kimci86/bkcrack.git f7e61bab2dd5976e252d8451527c66a6342b68d2; \
    git -C /tmp/bkcrack checkout --detach -q FETCH_HEAD; \
    test "$(git -C /tmp/bkcrack rev-parse HEAD)" = "f7e61bab2dd5976e252d8451527c66a6342b68d2"; \
    echo f7e61bab2dd5976e252d8451527c66a6342b68d2 > /opt/stage/sha/bkcrack.BUILD_SHA; \
    cmake -S /tmp/bkcrack -B /tmp/bkcrack/build -DCMAKE_BUILD_TYPE=Release; \
    cmake --build /tmp/bkcrack/build -j"$(nproc)"; \
    install -m0755 /tmp/bkcrack/build/src/bkcrack /opt/stage/bin/bkcrack; \
    rm -rf /tmp/bkcrack
RUN set -eux; \
    git init -q /tmp/hash_extender; \
    git -C /tmp/hash_extender fetch --depth=1 --no-tags https://github.com/iagox86/hash_extender.git f00b1a02eca02b0907e26726f7efe437bc396aa4; \
    git -C /tmp/hash_extender checkout --detach -q FETCH_HEAD; \
    test "$(git -C /tmp/hash_extender rev-parse HEAD)" = "f00b1a02eca02b0907e26726f7efe437bc396aa4"; \
    echo f00b1a02eca02b0907e26726f7efe437bc396aa4 > /opt/stage/sha/hash_extender.BUILD_SHA; \
    make -C /tmp/hash_extender -j"$(nproc)"; \
    install -m0755 /tmp/hash_extender/hash_extender /opt/stage/bin/hash_extender; \
    rm -rf /tmp/hash_extender
# Official YAFU build: cp config.mk.example config.mk and make yafu.
RUN set -eux; \
    git init -q /tmp/yafu; \
    git -C /tmp/yafu fetch --depth=1 --no-tags https://github.com/bbuhrow/yafu.git 825c5db3f9f6d141bde88f67571479a96c9cd35b; \
    git -C /tmp/yafu checkout --detach -q FETCH_HEAD; \
    test "$(git -C /tmp/yafu rev-parse HEAD)" = "825c5db3f9f6d141bde88f67571479a96c9cd35b"; \
    echo 825c5db3f9f6d141bde88f67571479a96c9cd35b > /opt/stage/sha/yafu.BUILD_SHA; \
    cp /tmp/yafu/config.mk.example /tmp/yafu/config.mk; \
    make -C /tmp/yafu yafu -j"$(nproc)"; \
    install -m0755 /tmp/yafu/yafu /opt/stage/bin/yafu; \
    rm -rf /tmp/yafu
# 4) Runtime. Per-category layers: one bad package name fails one layer,
# never the whole install.
FROM kalilinux/kali-rolling@sha256:c6d78f57ebfdd9fec428a8b04f74876e66fddd08dfd328c076224a552e7332a3
LABEL flagagent.managed=true \
      flagagent.version=0.2.0 \
      flagagent.profile=head
ARG DEBIAN_FRONTEND=noninteractive
# Non-root user matching the old image; the harness overrides it at runtime
# with the invoking host UID:GID, so this only sets workspace ownership.
RUN groupadd --gid 1000 agent \
    && useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash agent
# ---- APT: one category per layer ----
RUN set -eux; \
    apt-get update; \
    apt-get install --no-install-recommends --yes \
        file binutils grep ripgrep fd-find xxd jq tree \
        zip git openssh-client openssl netcat-openbsd socat \
        curl wget iputils-ping iproute2 ca-certificates perl; \
    rm -rf /var/lib/apt/lists/*
# Network tools (nmap ships ncat; tshark is the console-only Wireshark).
RUN set -eux; \
    apt-get update; \
    apt-get install --no-install-recommends --yes nmap tshark tcpdump tcpflow ngrep pcapfix; \
    rm -rf /var/lib/apt/lists/*
# Compilers stay in runtime: building exploits is a CTF capability, not just
# an image-build dependency. Only the Rust/Go SDKs are builder-confined.
RUN set -eux; \
    apt-get update; \
    apt-get install --no-install-recommends --yes \
        python3 python3-venv \
        build-essential gcc g++ make cmake nasm \
        gdb gdb-multiarch gdbserver strace ltrace \
        ruby php-cli; \
    rm -rf /var/lib/apt/lists/*
# Firmware/file carving (7zip provides the `7z` extractor unblob expects).
RUN set -eux; \
    apt-get update; \
    apt-get install --no-install-recommends --yes \
        7zip binwalk3 unblob libimage-exiftool-perl bulk-extractor foremost \
        sasquatch lziprecover android-sdk-libsparse-utils \
        unrar unar lz4 zstd lzip lzop e2fsprogs cpio; \
    rm -rf /var/lib/apt/lists/*
# Disk, memory, document, and media forensics.
RUN set -eux; \
    apt-get update; \
    apt-get install --no-install-recommends --yes \
        sleuthkit ewf-tools afflib-tools sqlite3 testdisk qemu-utils \
        mupdf-tools qpdf poppler-utils oletools pdfid pdf-parser \
        ffmpeg sox multimon-ng imagemagick chainsaw; \
    rm -rf /var/lib/apt/lists/*
# Crypto libraries and crackers (pocl gives hashcat a CPU backend).
RUN set -eux; \
    apt-get update; \
    apt-get install --no-install-recommends --yes \
        python3-cryptography python3-sympy python3-gmpy2 z3 python3-z3 \
        python3-pycryptodome python3-fpylll pocl-opencl-icd \
        hashid hashcat john fcrackzip; \
    rm -rf /var/lib/apt/lists/*
# Pwn/reverse: exactly one RE framework (radare2 — the best-evidenced choice
# across donor harnesses; rizin waits for usage telemetry in an eval image).
RUN set -eux; \
    apt-get update; \
    apt-get install --no-install-recommends --yes \
        python3-pwntools radare2 patchelf \
        qemu-user gcc-multilib g++-multilib \
        capstone-tool python3-capstone python3-unicorn detect-it-easy upx-ucl; \
    rm -rf /var/lib/apt/lists/*
# Stego and visual triage.
RUN set -eux; \
    apt-get update; \
    apt-get install --no-install-recommends --yes \
        stegseek steghide pngcheck tesseract-ocr zbar-tools python3-pil; \
    rm -rf /var/lib/apt/lists/*
# Web stack (impacket-scripts ships the CLI examples; the library follows).
RUN set -eux; \
    apt-get update; \
    apt-get install --no-install-recommends --yes \
        ffuf feroxbuster httpx-toolkit katana sqlmap whatweb testssl.sh \
        nuclei nikto dalfox commix phpggc wpscan websocat mitmproxy grpcurl \
        subfinder bind9-dnsutils whois hydra impacket-scripts; \
    rm -rf /var/lib/apt/lists/*
# Lightweight mobile triage only (jadx/apktool need a JVM — profile material).
RUN set -eux; \
    apt-get update; \
    apt-get install --no-install-recommends --yes apkid androguard redis-tools; \
    rm -rf /var/lib/apt/lists/*
# 5) Builder artifacts into place (bin->bin, lib->lib, then ldconfig).
COPY --from=rust-builder /opt/rust-out/bin/ /usr/local/bin/
COPY --from=go-builder /opt/go-out/bin/ /usr/local/bin/
COPY --from=native-builder /opt/stage/bin/ /usr/local/bin/
COPY --from=native-builder /opt/stage/usr/bin/ /usr/local/bin/
COPY --from=native-builder /opt/stage/usr/lib/ /usr/local/lib/
COPY --from=native-builder /opt/stage/sha/ /opt/build-sha/
RUN set -eux; ldconfig; chmod 0755 /usr/local/bin/*; ls /usr/local/bin
# 6) Project-owned venv. --system-site-packages keeps the venv python aware
# of APT libraries (pwntools, z3, impacket) while venv entry points
# (RsaCtfTool, xortool, floss) win on PATH. Without it the venv is blind.
RUN set -eux; \
    python3 -m venv --system-site-packages /opt/flagagent-py; \
    /opt/flagagent-py/bin/pip install --no-cache-dir \
        xortool randcrack keystone-engine flare-floss volatility3 \
        jefferson ubi-reader \
        cprint pycryptodomex ratelimit \
        requests termcolor beautifulsoup4 PySocks dulwich requests-pkcs12; \
    chmod -R a+rX /opt/flagagent-py
ENV PATH="/opt/flagagent-py/bin:/usr/local/bin:/usr/bin:/bin"
# 7) Ruby gems (pure-Ruby tools, no native build risk).
RUN set -eux; gem install --no-document zsteg seccomp-tools one_gadget
# 8) Pinned manual sources (git fetch by SHA + rev-parse guard, BUILD_SHA receipt).
# RsaCtfTool has no release: install the pinned commit as a package so the
# rsacrack/RsaCtfTool entry points exist (a bare symlink no longer works).
RUN set -eux; \
    git init -q /opt/RsaCtfTool; \
    git -C /opt/RsaCtfTool fetch --depth=1 --no-tags https://github.com/RsaCtfTool/RsaCtfTool.git af87bb487666b1bf3070e1bb058d97b78a342808; \
    git -C /opt/RsaCtfTool checkout --detach -q FETCH_HEAD; \
    test "$(git -C /opt/RsaCtfTool rev-parse HEAD)" = "af87bb487666b1bf3070e1bb058d97b78a342808"; \
    echo af87bb487666b1bf3070e1bb058d97b78a342808 > /opt/build-sha/RsaCtfTool.BUILD_SHA; \
    /opt/flagagent-py/bin/pip install --no-cache-dir /opt/RsaCtfTool; \
    chmod -R a+rX /opt/RsaCtfTool
# pwndbg at the official 2026.07.29 release SHA. sudo is build-time only and
# is purged in the same layer (setuid binaries must not reach runtime).
RUN set -eux; \
    apt-get update; \
    apt-get install --no-install-recommends --yes git sudo; \
    rm -rf /var/lib/apt/lists/*; \
    git init -q /opt/pwndbg; \
    git -C /opt/pwndbg fetch --depth=1 --no-tags https://github.com/pwndbg/pwndbg.git f4de22244b7e9f0aeda26a92208c42edaca2f8d8; \
    git -C /opt/pwndbg checkout --detach -q FETCH_HEAD; \
    test "$(git -C /opt/pwndbg rev-parse HEAD)" = "f4de22244b7e9f0aeda26a92208c42edaca2f8d8"; \
    echo "f4de22244b7e9f0aeda26a92208c42edaca2f8d8 (tag 2026.07.29)" > /opt/build-sha/pwndbg.BUILD_SHA; \
    /opt/pwndbg/setup.sh; \
    apt-get purge --yes sudo; apt-get autoremove --yes; \
    rm -rf /var/lib/apt/lists/*; \
    chmod -R a+rX /opt/pwndbg
# Single-file tools. Their upstream deps live in the project venv; the
# wrappers force that interpreter because `env python3` cannot see the venv.
RUN set -eux; \
    git init -q /opt/DidierStevensSuite; \
    git -C /opt/DidierStevensSuite fetch --depth=1 --no-tags https://github.com/DidierStevens/DidierStevensSuite.git dea6816048fb2fd3a3597f2e131449fc87f60138; \
    git -C /opt/DidierStevensSuite checkout --detach -q FETCH_HEAD; \
    test "$(git -C /opt/DidierStevensSuite rev-parse HEAD)" = "dea6816048fb2fd3a3597f2e131449fc87f60138"; \
    echo dea6816048fb2fd3a3597f2e131449fc87f60138 > /opt/build-sha/DidierStevensSuite.BUILD_SHA; \
    ln -sf /opt/DidierStevensSuite/oledump.py /usr/local/bin/oledump; \
    git init -q /opt/jwt_tool; \
    git -C /opt/jwt_tool fetch --depth=1 --no-tags https://github.com/ticarpi/jwt_tool.git 3bc7407cf2222d6a821dcc19c776e5a1b1cb9a9b; \
    git -C /opt/jwt_tool checkout --detach -q FETCH_HEAD; \
    test "$(git -C /opt/jwt_tool rev-parse HEAD)" = "3bc7407cf2222d6a821dcc19c776e5a1b1cb9a9b"; \
    echo 3bc7407cf2222d6a821dcc19c776e5a1b1cb9a9b > /opt/build-sha/jwt_tool.BUILD_SHA; \
    printf '#!/bin/sh\nexec /opt/flagagent-py/bin/python /opt/jwt_tool/jwt_tool.py "$@"\n' > /usr/local/bin/jwt_tool; \
    chmod 0755 /usr/local/bin/jwt_tool; \
    git init -q /opt/git-dumper; \
    git -C /opt/git-dumper fetch --depth=1 --no-tags https://github.com/arthaud/git-dumper.git 2b47da1973a9f0640b47e0a1ac2a6567751fe253; \
    git -C /opt/git-dumper checkout --detach -q FETCH_HEAD; \
    test "$(git -C /opt/git-dumper rev-parse HEAD)" = "2b47da1973a9f0640b47e0a1ac2a6567751fe253"; \
    echo 2b47da1973a9f0640b47e0a1ac2a6567751fe253 > /opt/build-sha/git-dumper.BUILD_SHA; \
    printf '#!/bin/sh\nexec /opt/flagagent-py/bin/python /opt/git-dumper/git_dumper.py "$@"\n' > /usr/local/bin/git-dumper; \
    chmod 0755 /usr/local/bin/git-dumper; \
    chmod -R a+rX /opt/DidierStevensSuite /opt/jwt_tool /opt/git-dumper
# Gopherus is intentionally excluded: upstream is Python 2 and Python 2 will
# not be installed for a single tool (same rule excluded tplmap).
# 9) nuclei-templates pinned to a release SHA (never -update-templates:
# the rule corpus must not drift between rebuilds).
RUN set -eux; \
    git init -q /opt/nuclei-templates; \
    git -C /opt/nuclei-templates fetch --depth=1 --no-tags https://github.com/projectdiscovery/nuclei-templates.git e5f19e6144135e107962bb943231413796fd7fe7; \
    git -C /opt/nuclei-templates checkout --detach -q FETCH_HEAD; \
    test "$(git -C /opt/nuclei-templates rev-parse HEAD)" = "e5f19e6144135e107962bb943231413796fd7fe7"; \
    echo "e5f19e6144135e107962bb943231413796fd7fe7 (tag v10.4.8)" > /opt/build-sha/nuclei-templates.BUILD_SHA; \
    test "$(find /opt/nuclei-templates -name '*.yaml' | wc -l)" -gt 1000; \
    chmod -R a+rX /opt/nuclei-templates
# 10) Agent manifest: concrete invocations, one per line. This is the single
# source of truth shared with the health-check below.
RUN set -eux; \
    printf '%s\n' \
    'BINWALK3 carving triage: binwalk3 <file>  (Rust v3; not binwalk)' \
    'YARA-X scan: yr scan <rule.yar> <target>  (binary is yr)' \
    'CHECKSEC mitigations ELF/PE/Mach-O: checksec --help  (Rust; JSON-capable)' \
    'DIEC packer/compiler: diec --json <file>' \
    'PCAP offline: tshark -r <file.pcap>  (live capture blocked by cap-drop)' \
    'UNBLOB extraction: unblob <blob>  (check: unblob --show-external-dependencies)' \
    'PWN: pwninit (setup) | ropr (fast x86 gadgets) | pwntools ROP (general/ARM fallback)' \
    'GDB: gdb --batch ... | pwndbg: source /opt/pwndbg/gdbinit.py inside gdb' \
    'WEB: ffuf (fuzz) + feroxbuster (recursive) | -w /usr/share/wordlists/flagagent-common.txt' \
    'WEB trio: httpx-toolkit (probe) | katana (crawl) | nuclei (scan)' \
    'NUCLEI: nuclei -t /opt/nuclei-templates -disable-update-check ...  (-ud is NOT for scanning)' \
    'RSA: RsaCtfTool | YAFU: yafu "factor(<int>)" | lattice: flatter' \
    'JWT: jwt_tool.py | PDF: mutool/qpdf/pdftotext | OFFICE: oletools/oledump.py' \
    'STEGO: steghide+stegseek, zsteg, pngcheck | OCR: tesseract | QR: zbarimg' \
    'AD/SMB: impacket-secretsdump ...  (impacket-scripts package)' \
    'TLS: testssl --banner <host:port>  (binary is testssl, not testssl.sh)' \
    'PING needs cap_net_raw — fails in sandbox by design; use curl/nc for liveness' \
    'RE: r2 (radare2; rizin deferred to eval image) | QEMU: qemu-aarch64 <bin>' \
    'Dynamic ARM needs target loader+libc (via challenge libs with patchelf/pwninit, or static binaries)' \
    'QEMU explicit invocation only (no magic binfmt registration)' \
    > /tools.txt
# Runtime shared libraries for native binaries (builder libs do not carry
# over). libfplll-dev is a version-proof carrier for the fplll runtime lib.
RUN set -eux; \
    apt-get update; \
    apt-get install --no-install-recommends --yes \
        libgmp10 libmpfr6 libecm1 libopenblas0-pthread libfplll-dev; \
    rm -rf /var/lib/apt/lists/*; \
    ldconfig
RUN set -eux; \
    mkdir -p /usr/share/wordlists; \
    printf '%s\n' \
    admin backup config data db debug dev git old prod secret server test tmp \
    api v1 v2 login dashboard uploads files images assets static backup.zip \
    .git flag.txt flag robots.txt sitemap.xml config.php info.php \
    > /usr/share/wordlists/flagagent-common.txt
# 11) Fail-closed verification: presence sweep plus functional smoke tests.
# A missing binary or a failing tool breaks the build — never a silent gap.
RUN set -eux; \
    missing=0; \
    for b in file strings rg fdfind xxd jq tree fq 7z git openssl nc nmap socat \
        python3 gcc nasm gdb gdbserver strace ltrace curl wget redis-cli \
        binwalk3 unblob exiftool bulk_extractor foremost yr tshark tcpdump \
        tcpflow ngrep pcapfix mmls ewfinfo sqlite3 testdisk photorec qemu-img vol mutool qpdf \
        pdftotext pdfid pdf-parser oledump ffmpeg ffprobe sox multimon-ng \
        convert chainsaw hashid hashcat john fcrackzip RsaCtfTool yafu xortool floss \
        bkcrack hash_extender ciphey pwninit checksec ropr patchelf r2 \
        qemu-aarch64 diec upx zsteg seccomp-tools one_gadget stegseek steghide pngcheck tesseract \
        zbarimg ffuf feroxbuster httpx-toolkit katana x8 sqlmap whatweb testssl \
        nuclei nikto dalfox commix jwt_tool git-dumper phpggc wpscan websocat mitmdump \
        grpcurl subfinder dig whois hydra apkid androguard impacket-secretsdump \
        flatter gdb sasquatch simg2img lziprecover; do \
        command -v "$b" >/dev/null || { echo "MISSING: $b" >&2; missing=1; }; \
    done; \
    test "$missing" -eq 0
RUN set -eux; \
    python3 -c "import pwn, Crypto, sympy, gmpy2, z3, capstone, unicorn, PIL, impacket; print('py-libs OK')"; \
    /opt/flagagent-py/bin/python -c "import keystone, Crypto; print('venv OK')"; \
    printf 'rule t { strings: $a = "abc" condition: $a }' > /tmp/t.yar; \
    printf 'xxabcxx' > /tmp/s.bin; \
    yr scan /tmp/t.yar /tmp/s.bin | grep -q t; \
    fq . /bin/true > /dev/null; \
    checksec --help > /dev/null; \
    diec --json /bin/true | python3 -c "import sys,json; json.load(sys.stdin)"; \
    printf '0000 00 11 22 33 44 55 00 11 22 33 44 55 08 00\n' | text2pcap - /tmp/t.pcap; \
    tshark -r /tmp/t.pcap -T fields -e frame.number | grep -q 1; \
    unblob --show-external-dependencies > /tmp/unblob.deps; cat /tmp/unblob.deps; \
    ! grep -qE '✗|missing|NOT FOUND' /tmp/unblob.deps; \
    python3 -c "from pwn import ELF; e=ELF('/bin/true'); assert e.arch"; \
    /opt/flagagent-py/bin/pip check; \
    RsaCtfTool --help > /dev/null; \
    jwt_tool -h > /dev/null; \
    impacket-secretsdump --help > /dev/null; \
    httpx-toolkit -version > /dev/null; \
    testssl --banner > /dev/null 2>&1; \
    ciphey --help > /dev/null; ropr --help > /dev/null; \
    pwninit --help > /dev/null; x8 --help > /dev/null; \
    gdb --batch -nx -ex 'source /opt/pwndbg/gdbinit.py' -ex quit /bin/true; \
    yafu 'factor(91)' | grep -qE '\\b(7|13)\\b'; \
    hashcat -I > /dev/null; \
    john --list=formats | grep -q raw-md5; \
    nuclei --version; \
    rm -f /tmp/t.yar /tmp/s.bin /tmp/t.pcap /tmp/unblob.deps; \
    echo SMOKE-OK
RUN set -eux; \
    for b in flatter bkcrack hash_extender yafu RsaCtfTool yr fq checksec ropr pwninit; do \
        ldd "$(command -v "$b")" 2>/dev/null | grep 'not found' && exit 1 || true; \
    done; \
    echo LDD-OK
# HOME for the numeric runtime UID (absent from /etc/passwd, so the default
# is empty and stateful tools cannot write). /tmp is world-writable,
# container-local, and vanishes on cleanup — the workspace stays clean.
ENV HOME=/tmp
WORKDIR /workspace

USER agent

CMD ["sleep", "infinity"]
