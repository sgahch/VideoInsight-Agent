from __future__ import annotations

import base64
import json
import socket
import ssl
from pathlib import Path


def main() -> int:
    try:
        config = json.loads(Path("/etc/mihomo/config.json").read_text())
        auth = base64.b64encode(config["authentication"][0].encode()).decode()
        with socket.create_connection(("127.0.0.1", config["port"]), timeout=8) as sock:
            sock.sendall((
                "CONNECT api.groq.com:443 HTTP/1.1\r\n"
                "Host: api.groq.com:443\r\n"
                f"Proxy-Authorization: Basic {auth}\r\n\r\n"
            ).encode())
            headers = b""
            while b"\r\n\r\n" not in headers:
                chunk = sock.recv(1024)
                if not chunk or len(headers) + len(chunk) > 8192:
                    return 1
                headers += chunk
            if not headers.startswith(b"HTTP/1.1 200 "):
                return 1
            with ssl.create_default_context().wrap_socket(sock, server_hostname="api.groq.com") as tls:
                tls.sendall(
                    b"GET /openai/v1/models HTTP/1.1\r\n"
                    b"Host: api.groq.com\r\nConnection: close\r\n\r\n"
                )
                status = tls.makefile("rb").readline(1024).split()
                # A verified TLS connection and 401 prove egress without an API key.
                return 0 if len(status) >= 2 and status[1] == b"401" else 1
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
