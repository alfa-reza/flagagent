ARG UV_VERSION=0.12.15

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM kalilinux/kali-rolling

ARG DEBIAN_FRONTEND=noninteractive

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        7zip \
        7zip-rar \
        openssh-client \
        bsdextrautils \
        afflib-tools \
        androguard \
        binutils \
        binwalk3 \
        bulk-extractor \
        build-essential \
        capstone-tool \
        smbclient \
        chainsaw \
        ewf-tools \
        netexec \
        cmake \
        commix \
        curl \
        detect-it-easy \
        exiftool \
        ffmpeg \
        ffuf \
        foremost \
        fplll-tools \
        fq \
        feroxbuster \
        file \
        gdb \
        gdb-multiarch \
        git \
        grep \
        hashcat \
        hashid \
        hashcat-utils \
        imagemagick \
        impacket-scripts \
        jq \
        john \
        httpx-toolkit \
        wpscan \
        sqlmap \
        katana \
        ltrace \
        lz4 \
        make \
        multimon-ng \
        nasm \
        netcat-openbsd \
        nikto \
        nmap \
        oletools \
        openssl \
        patchelf \
        pdf-parser \
        pdfid \
        ncat \
        phpggc \
        pngcheck \
        poppler-utils \
        python3 \
        python3-dev \
        python3-cryptography \
        python3-fpylll \
        python3-gmpy2 \
        python3-keystone-engine \
        python3-pycryptodome \
        python3-pwntools \
        python3-sympy \
        python3-z3 \
        qemu-utils \
        qpdf \
        pcapfix \
        radare2 \
        readpe \
        ripgrep \
        ropper \
        sleuthkit \
        socat \
        sqlite3 \
        strace \
        steghide \
        stegseek \
        subfinder \
        testdisk \
        testssl.sh \
        tshark \
        tcpdump \
        tcpflow \
        tesseract-ocr \
        unzip \
        unblob \
        upx-ucl \
        vim-common \
        wget \
        whatweb \
        wireshark-common \
        xxd \
        yara \
        zip \
        zstd \
    && ln -s /usr/bin/binwalk3 /usr/local/bin/binwalk \
    && rm -rf /var/lib/apt/lists/*

# FlagAgent supplies its own long-lived container command; do not add an
# ENTRYPOINT that would conflict with docker exec / the executor lifecycle.
RUN printf '%s\n' \
    file grep rg xxd hexdump dd jq fq 7z zip unzip tar gzip xz zstd lz4 \
    curl wget git ssh openssl nc nmap ncat socat python3 gcc g++ make cmake \
    nasm gdb gdb-multiarch strace ltrace exiftool binwalk unblob \
    bulk_extractor foremost tshark tcpdump tcpflow sqlite3 qemu-img \
    ffmpeg ffprobe convert tesseract hashcat r2 ropper patchelf ffuf \
    feroxbuster nikto nmap whatweb subfinder katana phpggc commix chainsaw \
    > /tools.txt

# Verify that every executable expected by FlagAgent actually exists.
RUN missing=0; \
    while IFS= read -r tool; do \
        if ! command -v "$tool" >/dev/null 2>&1; then \
            echo "ERROR: required tool not found: $tool" >&2; \
            missing=1; \
        fi; \
    done < /tools.txt; \
    exit "$missing"
    
COPY --from=uv /uv /uvx /usr/local/bin/

ENV UV_TOOL_BIN_DIR=/opt/uv-bin \
    UV_TOOL_DIR=/opt/uv-tools \
    PATH=/opt/uv-bin:/usr/local/bin:/usr/bin:/bin

RUN mkdir -p /opt/uv-bin /opt/uv-tools \
    && uv --version
RUN uv tool install xortool
RUN uv tool install git-dumper
RUN uv tool install apkid --with "yara-python-dex @ git+https://github.com/MobSF/yara-python-dex.git"
RUN git clone --depth 1 https://github.com/ticarpi/jwt_tool.git /opt/jwt_tool \
    && uv venv --python 3 /opt/jwt_tool/.venv \
    && uv pip install \
        --python /opt/jwt_tool/.venv/bin/python \
        -r /opt/jwt_tool/requirements.txt \
    && chmod +x /opt/jwt_tool/jwt_tool.py \
    && ln -s /opt/jwt_tool/.venv/bin/python /usr/local/bin/jwt-tool-python \
    && printf '#!/bin/sh\nexec /opt/jwt_tool/.venv/bin/python /opt/jwt_tool/jwt_tool.py "$@"\n' > /usr/local/bin/jwt_tool \
    && chmod +x /usr/local/bin/jwt_tool

# Verify Python tools

RUN set -eux; \
    command -v uv; \
    command -v uvx; \
    command -v xortool; \
    command -v git-dumper; \
    command -v apkid; \
    command -v jwt_tool; \
    xortool --help >/dev/null; \
    git-dumper --help >/dev/null; \
    apkid --help >/dev/null; \
    jwt_tool --help >/dev/null

# Keep the installed Python tool metadata visible for diagnostics.
RUN printf '%s\n' \
    "uv" \
    "xortool" \
    "git-dumper" \
    "apkid" \
    "jwt_tool" \
    > /python-tools.txt

CMD ["sleep", "infinity"]
