"""HT patch P4 (HT-PATCHES.md, ``ht_patches.p4_mcp_private_ca``, default off): mTLS to an MCP
server under a private CA with ``ssl_verify: <ca path>`` plus a client certificate.

httpx 0.28 ``create_ssl_context`` returns early for ``verify=<str>``, so a CA path given as
``ssl_verify`` silently dropped ``cert=`` (httpx2 refuses the pair): the client never presented
its certificate and a server that requires one (an mTLS proxy in front of the MCP endpoint)
refused the handshake. With P4 on, the client builds one ``ssl.SSLContext`` from the CA bundle,
loads the client certificate into it and passes that as ``verify=``. With P4 off the behaviour is
upstream's, failure included.

The end-to-end tests start a real MCP server subprocess over HTTPS that requires a client
certificate, with certificates generated at test time (no key material is committed), and drive
``hermes mcp test`` through the shared connect path under every supported flag combination
(none, P1, P4, P1+P4).
"""

from __future__ import annotations

import argparse
import datetime
import ipaddress
import ssl
import subprocess
import sys
from pathlib import Path

import pytest

import hermes_yaml as yaml
from hermes_constants import get_hermes_home
from tests.waiting import wait_until

x509 = pytest.importorskip("cryptography.x509")
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

_SERVER = """
import asyncio, socket, ssl, sys
import uvicorn
from mcp.server import MCPServer
certfile, keyfile, client_ca, port_path = sys.argv[1:5]
server = MCPServer("mtls")

def ping() -> str:
    return "pong"

server.add_tool(ping, name="ping", description="answers pong")
sock = socket.socket()
sock.bind(("127.0.0.1", 0))
uv = uvicorn.Server(uvicorn.Config(server.streamable_http_app(), log_level="warning",
                                   ssl_certfile=certfile, ssl_keyfile=keyfile,
                                   ssl_ca_certs=client_ca, ssl_cert_reqs=ssl.CERT_REQUIRED))

async def main():
    task = asyncio.create_task(uv.serve(sockets=[sock]))
    while not uv.started:
        await asyncio.sleep(0.01)
    open(port_path, "w", encoding="utf-8").write(str(sock.getsockname()[1]))
    await task

asyncio.run(main())
"""


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _write_key(key, path: Path) -> None:
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))


class _CA:
    """A throwaway certificate authority that issues leaf certificates into *directory*."""

    def __init__(self, directory: Path, label: str):
        self.directory, self.label = directory, label
        self.key = ec.generate_private_key(ec.SECP256R1())
        now = datetime.datetime.now(datetime.UTC)
        self.cert = (x509.CertificateBuilder()
                     .subject_name(_name(f"hermes test CA {label}")).issuer_name(_name(f"hermes test CA {label}"))
                     .public_key(self.key.public_key()).serial_number(x509.random_serial_number())
                     .not_valid_before(now - datetime.timedelta(minutes=5))
                     .not_valid_after(now + datetime.timedelta(hours=1))
                     .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                     .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                                  content_commitment=False, key_encipherment=False,
                                                  data_encipherment=False, key_agreement=False,
                                                  encipher_only=False, decipher_only=False), critical=True)
                     .add_extension(x509.SubjectKeyIdentifier.from_public_key(self.key.public_key()),
                                    critical=False)
                     .sign(self.key, hashes.SHA256()))
        self.cert_path = directory / f"ca-{label}.crt"
        self.cert_path.write_bytes(self.cert.public_bytes(serialization.Encoding.PEM))

    def issue(self, common_name: str, *, server: bool) -> tuple[Path, Path]:
        """Issue a server (SAN 127.0.0.1) or client leaf; returns ``(cert path, key path)``."""
        key = ec.generate_private_key(ec.SECP256R1())
        now = datetime.datetime.now(datetime.UTC)
        usage = ExtendedKeyUsageOID.SERVER_AUTH if server else ExtendedKeyUsageOID.CLIENT_AUTH
        builder = (x509.CertificateBuilder()
                   .subject_name(_name(common_name)).issuer_name(self.cert.subject)
                   .public_key(key.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(now - datetime.timedelta(minutes=5))
                   .not_valid_after(now + datetime.timedelta(hours=1))
                   .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                   .add_extension(x509.ExtendedKeyUsage([usage]), critical=False)
                   .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(self.key.public_key()),
                                  critical=False))
        if server:
            builder = builder.add_extension(
                x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
        cert = builder.sign(self.key, hashes.SHA256())
        stem = f"{self.label}-{common_name}"
        cert_path, key_path = self.directory / f"{stem}.crt", self.directory / f"{stem}.key"
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        _write_key(key, key_path)
        return cert_path, key_path


@pytest.fixture
def pki(tmp_path):
    """Two unrelated CAs: ``trusted`` (the one the client is configured with) and ``other``."""
    directory = tmp_path / "pki"
    directory.mkdir()
    return {"trusted": _CA(directory, "trusted"), "other": _CA(directory, "other")}


@pytest.fixture
def mtls_server(tmp_path):
    """Start an MCP server over HTTPS that requires a client certificate; yields its URL."""
    procs = []

    def start(server_cert: Path, server_key: Path, client_ca: Path) -> str:
        script, port_file = tmp_path / "mtls_server.py", tmp_path / f"port-{len(procs)}"
        script.write_text(_SERVER, encoding="utf-8")
        procs.append(subprocess.Popen([sys.executable, str(script), str(server_cert), str(server_key),
                                       str(client_ca), str(port_file)]))
        wait_until(lambda: port_file.exists() and port_file.read_text(encoding="utf-8"), timeout=30,
                   what="mTLS MCP test server to bind")
        return f"https://127.0.0.1:{port_file.read_text(encoding='utf-8')}/mcp"

    yield start
    from tools.mcp_tool_lifecycle import shutdown_mcp_servers
    shutdown_mcp_servers()
    for proc in procs:
        proc.terminate()
        proc.wait(10)


# ht_patches sections for every supported combination, split by whether P4 is on.
_P4_OFF = [pytest.param(None, id="none"), pytest.param({"p4_mcp_private_ca": False}, id="P4-false"),
           pytest.param({"p1_run_hook_kwargs": True}, id="P1")]
_P4_ON = [pytest.param({"p4_mcp_private_ca": True}, id="P4"),
          pytest.param({"p1_run_hook_kwargs": True, "p4_mcp_private_ca": True}, id="P1+P4")]


def _write_config(patches: dict | None, mcp_servers: dict | None = None) -> None:
    config: dict = {}
    if patches is not None:
        config["ht_patches"] = patches
    if mcp_servers is not None:
        config["mcp_servers"] = mcp_servers
    (get_hermes_home() / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")


def _mcp_test(capsys, patches: dict | None, entry: dict) -> tuple[int, str]:
    """``hermes mcp test mtls`` against *entry* under *patches*; returns (exit code, printed output)."""
    from hermes_cli.ht_patches import check_patch_combination
    from hermes_cli.mcp_config import cmd_mcp_test

    _write_config(patches, {"mtls": entry})
    check_patch_combination()  # every combination used here is a supported one
    capsys.readouterr()
    code = cmd_mcp_test(argparse.Namespace(name="mtls"))
    return code, capsys.readouterr().out


def _entry(url: str, ca: Path, client_cert: Path, client_key: Path) -> dict:
    return {"url": url, "ssl_verify": str(ca), "client_cert": str(client_cert), "client_key": str(client_key),
            "connect_timeout": 15}


@pytest.mark.parametrize("patches", _P4_ON)
def test_p4_on_private_ca_and_client_cert_complete_mtls(pki, mtls_server, capsys, patches):
    trusted = pki["trusted"]
    url = mtls_server(*trusted.issue("server", server=True), trusted.cert_path)
    client_cert, client_key = trusted.issue("hermes", server=False)

    code, out = _mcp_test(capsys, patches, _entry(url, trusted.cert_path, client_cert, client_key))

    assert code == 0, out
    assert "Tools discovered: 1" in out


@pytest.mark.parametrize("patches", _P4_ON)
def test_p4_on_rejects_server_under_another_ca(pki, mtls_server, capsys, patches):
    trusted, other = pki["trusted"], pki["other"]
    # The server chains to ``other`` (and would accept the client), but the client trusts only
    # ``trusted``: the handshake must fail on the server certificate.
    url = mtls_server(*other.issue("server", server=True), trusted.cert_path)
    client_cert, client_key = trusted.issue("hermes", server=False)

    code, out = _mcp_test(capsys, patches, _entry(url, trusted.cert_path, client_cert, client_key))

    assert code != 0, out
    assert "Tools discovered" not in out


@pytest.mark.parametrize("patches", _P4_OFF)
def test_p4_off_keeps_upstream_behaviour(pki, mtls_server, capsys, patches):
    """Upstream behaviour, failure included: the CA path and the client certificate reach httpx
    separately, so even the trusted server's mTLS handshake does not complete."""
    trusted = pki["trusted"]
    url = mtls_server(*trusted.issue("server", server=True), trusted.cert_path)
    client_cert, client_key = trusted.issue("hermes", server=False)

    code, out = _mcp_test(capsys, patches, _entry(url, trusted.cert_path, client_cert, client_key))

    assert code != 0, out
    assert "Tools discovered" not in out


class TestTlsVerify:
    """``_tls_verify`` resolves ``(ssl_verify, client_cert)`` for the HTTP/SSE transports."""

    @pytest.mark.parametrize("patches", _P4_ON)
    def test_p4_on_ca_path_with_client_cert_builds_one_context(self, pki, patches):
        from tools.mcp_tool_transport import _tls_verify

        _write_config(patches)
        trusted = pki["trusted"]
        client_cert, client_key = trusted.issue("hermes", server=False)
        verify, cert = _tls_verify(str(trusted.cert_path), (str(client_cert), str(client_key)))

        assert isinstance(verify, ssl.SSLContext)
        assert cert is None  # the chain lives in the context; httpx must not see cert= at all
        assert verify.verify_mode == ssl.CERT_REQUIRED and verify.check_hostname
        assert any(ca["subject"] == ((("commonName", "hermes test CA trusted"),),)
                   for ca in verify.get_ca_certs())

    def test_p4_on_ca_directory_with_combined_pem(self, pki, tmp_path):
        from tools.mcp_tool_transport import _tls_verify

        _write_config({"p4_mcp_private_ca": True})
        trusted = pki["trusted"]
        client_cert, client_key = trusted.issue("hermes", server=False)
        combined = tmp_path / "combined.pem"
        combined.write_bytes(client_cert.read_bytes() + client_key.read_bytes())
        verify, cert = _tls_verify(str(trusted.cert_path.parent), str(combined))

        assert isinstance(verify, ssl.SSLContext) and cert is None

    @pytest.mark.parametrize("patches", _P4_OFF)
    def test_p4_off_returns_the_config_unchanged(self, pki, patches):
        from tools.mcp_tool_transport import _tls_verify

        _write_config(patches)
        pair = (str(pki["trusted"].cert_path), ("c.pem", "k.pem"))
        assert _tls_verify(*pair) == pair

    @pytest.mark.parametrize("patches", _P4_ON)
    @pytest.mark.parametrize("ssl_verify", [True, False])
    def test_p4_on_bool_verify_keeps_httpx_cert_handling(self, patches, ssl_verify):
        from tools.mcp_tool_transport import _tls_verify

        _write_config(patches)
        assert _tls_verify(ssl_verify, ("c.pem", "k.pem")) == (ssl_verify, ("c.pem", "k.pem"))

    @pytest.mark.parametrize("patches", _P4_ON)
    def test_p4_on_ca_path_without_client_cert_is_unchanged(self, patches):
        from tools.mcp_tool_transport import _tls_verify

        _write_config(patches)
        assert _tls_verify("/etc/ca.pem", None) == ("/etc/ca.pem", None)
