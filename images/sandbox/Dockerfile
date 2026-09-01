# FlagAgent M2 sandbox image.
#
# Ubuntu 24.04 LTS with a non-root ``agent`` user and only the tools required
# by the frozen M2 smoke fixtures: Python for file decoding and netcat for the
# audited local TCP target.
#
# The runtime passes the invoking host process's numeric UID/GID to Docker so
# the ``agent`` image user does not determine bind-mounted workspace ownership.

FROM ubuntu:24.04@sha256:33ceb71981b602c1a7443a53469e4dba065f7503eab3078a2d7a57a2ab987517

LABEL flagagent.managed=true \
      flagagent.version=0.2.0

# ubuntu:24.04 ships a ``ubuntu`` user at UID/GID 1000; rename it to ``agent``
# as the image's named non-root default.  DockerExecutor overrides this user
# at runtime with the invoking process's numeric UID/GID.
RUN if id ubuntu >/dev/null 2>&1; then \
        usermod -l agent -d /home/agent -m ubuntu \
        && groupmod -n agent ubuntu; \
    else \
        groupadd --gid 1000 agent \
        && useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash agent; \
    fi

RUN apt-get update \
    && apt-get install --no-install-recommends --yes python3 netcat-openbsd \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

USER agent

CMD ["sleep", "infinity"]
