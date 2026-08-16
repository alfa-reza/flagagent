# FlagAgent M1 sandbox image.
#
# Minimal Ubuntu 24.04 LTS with a non-root ``agent`` user.  Tagged as
# ``flagagent-sandbox:dev`` and used as the Agent container root filesystem.
# CTF tooling is intentionally deferred to later milestones.
#
# The ``agent`` user reuses UID/GID 1000 so it can read/write the host
# workspace bind-mount owned by the reference host user (UID 1000).

FROM ubuntu:24.04

LABEL flagagent.managed=true \
      flagagent.version=0.1.0

# ubuntu:24.04 ships a ``ubuntu`` user at UID/GID 1000; rename it to ``agent``
# so the container user matches the host workspace owner without colliding.
RUN if id ubuntu >/dev/null 2>&1; then \
        usermod -l agent -d /home/agent -m ubuntu \
        && groupmod -n agent ubuntu; \
    else \
        groupadd --gid 1000 agent \
        && useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash agent; \
    fi

WORKDIR /workspace

USER agent

CMD ["sleep", "infinity"]
