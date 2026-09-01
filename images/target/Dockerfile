# FlagAgent M1 target fixture image.
#
# Tiny project-owned TCP service that returns a deterministic marker
# (``flagagent-target-ok``) on each connection.  Used as the ``local`` network
# target fixture for M1 containment proof.  This is NOT a CTF target framework.
#
# Non-root ``target`` user (UID 1001, distinct from the Agent's UID 1000 so the
# two containers never share an identity).  No host mounts, no Docker socket,
# no privileges; further bounds are enforced at runtime by DockerExecutor.

FROM python:3.12-slim@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a

LABEL flagagent.managed=true \
      flagagent.role=target \
      flagagent.version=0.2.0

RUN groupadd --gid 1001 target \
    && useradd --uid 1001 --gid 1001 --no-create-home target

WORKDIR /app
COPY server.py /app/server.py

USER target

EXPOSE 9999

CMD ["python3", "/app/server.py"]
