"""FlagAgent M1 target fixture: a tiny deterministic TCP service.

Returns the marker ``flagagent-target-ok`` on each connection, then closes.
This is a networking fixture only -- not a CTF target framework.  Bounded at
runtime by the Docker resource/capability limits applied by ``DockerExecutor``.
"""

import socket

HOST = "0.0.0.0"
PORT = 9999
MARKER = b"flagagent-target-ok\n"
BACKLOG = 16


def serve() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((HOST, PORT))
        listener.listen(BACKLOG)
        while True:
            conn, _ = listener.accept()
            with conn:
                conn.sendall(MARKER)


if __name__ == "__main__":
    serve()
