FROM kalilinux/kali-rolling

ARG DEBIAN_FRONTEND=noninteractive
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        7zip 7zip-rar afftools androguard binutils binwalk3 bulk-extractor \
        build-essential capstone-tool chainsaw cmake commix cryptography curl \
        detect-it-easy exiftool ffmpeg ffuf foremost fplll fpylll fq feroxbuster \
        file gdb gdb-multiarch git gmpy2 grep hashcat hashid hashcat-utils \
        imagemagick impacket-scripts jq keystone-engine katana ltrace lz4 make \
        mergecap multimon-ng nasm netcat-openbsd nikto nmap oletools openssl \
        patchelf pdf-parser pdfid phpggc pngcheck poppler-utils pwntools \
        python3 python3-cryptography python3-pycryptodome python3-sympy \
        python3-z3 qemu-utils qpdf radare2 readpe ripgrep ropper sleuthkit \
        socat sqlite3 strace steghide stegseek subfinder sympy testdisk \
        testssl.sh tshark tcpdump tcpflow tesseract-ocr unzip unblob upx-ucl \
        vim-common wget whatweb xxd yara zip zstd \
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

CMD ["sleep", "infinity"]
