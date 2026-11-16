from __future__ import annotations

import datetime
import socket
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from app.url_safety import PublicOnlyTransport, UnsafeURL, resolve_public_url, validate_public_url


PUBLIC_IP = "93.184.216.34"


def dns_records(*addresses):
    return [
        (socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))
        for address in addresses
    ]


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "ftp://example.com/file", "//example.com/path",
    "https://user:password@example.com", "https://@example.com",
    "https://example.com#fragment", "https://example.com#",
    "https://example.com?query=1", "https://example.com?",
    "https://example.com\\@127.0.0.1", " https://example.com",
    "https://example.com/\r\nheader", "https://example.com/\x00",
    "https://example.com:0", "https://example.com:65536",
    "http://[fe80::1%25eth0]", "http://127.0.0.1", "http://10.0.0.1",
    "http://172.16.0.1", "http://192.168.0.1", "http://169.254.169.254",
    "http://100.64.0.1", "http://0.0.0.0", "http://224.0.0.1",
    "http://192.0.2.1", "http://[::1]", "http://[fc00::1]",
    "http://[fe80::1]", "http://[fec0::1]", "http://[ff02::1]", "http://[2001:db8::1]",
    "http://[::ffff:127.0.0.1]",
])
def test_untrusted_destinations_are_rejected(url, monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: dns_records(PUBLIC_IP))
    with pytest.raises(UnsafeURL):
        validate_public_url(url)


@pytest.mark.parametrize("addresses", [
    ("127.0.0.1",), (PUBLIC_IP, "10.1.2.3"), (PUBLIC_IP, "::1"), (),
])
def test_all_dns_answers_must_be_public(monkeypatch, addresses):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: dns_records(*addresses))
    with pytest.raises(UnsafeURL):
        resolve_public_url("https://gateway.example/v1")


@pytest.mark.parametrize("hostname", ["127.1", "2130706433", "0x7f000001"])
def test_legacy_numeric_hostnames_cannot_bypass_dns_validation(monkeypatch, hostname):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: dns_records("127.0.0.1"))
    with pytest.raises(UnsafeURL):
        validate_public_url(f"http://{hostname}/")


def test_public_url_preserves_path_and_optional_query(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: dns_records(
        "2606:4700:4700::1111", PUBLIC_IP, PUBLIC_IP,
    ))
    destination = resolve_public_url("https://gateway.example:8443/v1")
    assert destination.addresses == (PUBLIC_IP, "2606:4700:4700::1111")
    assert str(destination.url) == "https://gateway.example:8443/v1"
    assert validate_public_url("https://gateway.example/video?id=1", allow_query=True).endswith("?id=1")


def test_transport_pins_ip_preserves_host_and_checks_each_redirect(monkeypatch):
    queries, calls = [], []

    def dns(host, port, **kwargs):
        queries.append(host)
        if host == "private.example":
            return dns_records("127.0.0.1")
        return dns_records(PUBLIC_IP)

    def handler(request):
        calls.append(request)
        assert request.url.host == PUBLIC_IP
        assert request.headers["host"] == "gateway.example:8443"
        assert request.extensions["sni_hostname"] == "gateway.example"
        return httpx.Response(302, headers={"location": "http://private.example/private"})

    monkeypatch.setattr(socket, "getaddrinfo", dns)
    transport = PublicOnlyTransport(transport_factory=lambda: httpx.MockTransport(handler))
    with httpx.Client(transport=transport, trust_env=False, follow_redirects=True) as client:
        with pytest.raises(UnsafeURL):
            client.get("https://gateway.example:8443/v1")
    assert queries == ["gateway.example", "private.example"]
    assert len(calls) == 1


def test_transport_does_not_share_tls_pools_between_names_on_the_same_ip(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: dns_records(PUBLIC_IP))
    pools = []

    def factory():
        pool = httpx.MockTransport(lambda request: httpx.Response(200))
        pools.append(pool)
        return pool

    with httpx.Client(transport=PublicOnlyTransport(transport_factory=factory), trust_env=False) as client:
        client.get("https://first.example/a")
        client.get("https://second.example/b")
        client.get("https://first.example/c")
    assert len(pools) == 2


def test_transport_uses_real_tls_sni_and_verifies_original_hostname(monkeypatch, tmp_path):
    pytest.importorskip("cryptography")
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "gateway.example")])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("gateway.example")]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "test-cert.pem", tmp_path / "test-key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
    ))
    requests, server_names, dns_queries, connections = [], [], [], []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            requests.append((self.path, self.headers["Host"]))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(str(cert_path), str(key_path))
    server_context.set_servername_callback(lambda sock, hostname, context: server_names.append(hostname))
    server.socket = server_context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    original_dns, original_connect = socket.getaddrinfo, socket.create_connection

    def dns(host, port, *args, **kwargs):
        if host in ("gateway.example", "wrong.example"):
            dns_queries.append(host)
            # A subsequent name lookup would rebind to loopback.
            address = PUBLIC_IP if dns_queries.count(host) == 1 else "127.0.0.1"
            return dns_records(address)
        return original_dns(host, port, *args, **kwargs)

    def connect(address, *args, **kwargs):
        connections.append(address)
        assert address == (PUBLIC_IP, server.server_port)
        # Only this synthetic public IP is routed to our own temporary TLS server.
        return original_connect(("127.0.0.1", server.server_port), *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", dns)
    monkeypatch.setattr(socket, "create_connection", connect)
    context = ssl.create_default_context(cafile=str(cert_path))
    assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
    transport = PublicOnlyTransport(
        transport_factory=lambda: httpx.HTTPTransport(verify=context, trust_env=False),
    )
    try:
        with httpx.Client(transport=transport, trust_env=False) as client:
            assert client.get(f"https://gateway.example:{server.server_port}/safe").text == "ok"
            with pytest.raises(httpx.ConnectError, match="CERTIFICATE_VERIFY_FAILED"):
                client.get(f"https://wrong.example:{server.server_port}/blocked")
        assert dns_queries == ["gateway.example", "wrong.example"]
        assert len(connections) == 2
        assert server_names == ["gateway.example", "wrong.example"]
        assert requests == [("/safe", f"gateway.example:{server.server_port}")]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
