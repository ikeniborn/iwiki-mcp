from __future__ import annotations

import json
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import threading

import pytest
import urllib3

from iwiki_mcp.telegram_bot.access import AccessPolicy
from iwiki_mcp.telegram_bot.models import BotReply
from iwiki_mcp.telegram_bot.proxy import TelegramProxyClient
from iwiki_mcp.telegram_bot.transport import TelegramError, TelegramTransport


TOKEN = "TOKEN"


def _openssl(*args):
    subprocess.run(
        ["openssl", *args],
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )


def _certificate_authority(directory: Path):
    if shutil.which("openssl") is None:
        pytest.skip("OpenSSL executable unavailable for TLS integration")
    ca_key = directory / "ca.key"
    ca_cert = directory / "ca.pem"
    _openssl(
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-keyout",
        str(ca_key),
        "-out",
        str(ca_cert),
        "-days",
        "1",
        "-subj",
        "/CN=iwiki acceptance CA",
        "-addext",
        "basicConstraints=critical,CA:TRUE",
    )

    def issue(name, subject, san):
        key = directory / f"{name}.key"
        request = directory / f"{name}.csr"
        certificate = directory / f"{name}.pem"
        extensions = directory / f"{name}.ext"
        extensions.write_text(
            f"subjectAltName={san}\nextendedKeyUsage=serverAuth\n",
            encoding="ascii",
        )
        _openssl(
            "req",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(request),
            "-subj",
            f"/CN={subject}",
        )
        _openssl(
            "x509",
            "-req",
            "-in",
            str(request),
            "-CA",
            str(ca_cert),
            "-CAkey",
            str(ca_key),
            "-CAcreateserial",
            "-out",
            str(certificate),
            "-days",
            "1",
            "-extfile",
            str(extensions),
        )
        return certificate, key

    proxy = issue("proxy", "127.0.0.1", "IP:127.0.0.1")
    telegram = issue("telegram", "api.telegram.org", "DNS:api.telegram.org")
    return ca_cert, proxy, telegram


class _TlsOverTls:
    def __init__(self, transport, context):
        self.transport = transport
        self.incoming = ssl.MemoryBIO()
        self.outgoing = ssl.MemoryBIO()
        self.tls = context.wrap_bio(
            self.incoming, self.outgoing, server_side=True
        )

    def _flush(self):
        while self.outgoing.pending:
            self.transport.sendall(self.outgoing.read())

    def _receive_ciphertext(self):
        data = self.transport.recv(65536)
        if not data:
            raise EOFError
        self.incoming.write(data)

    def handshake(self):
        while True:
            try:
                self.tls.do_handshake()
                self._flush()
                return
            except ssl.SSLWantReadError:
                self._flush()
                self._receive_ciphertext()

    def recv(self, size):
        while True:
            try:
                data = self.tls.read(size)
                self._flush()
                return data
            except ssl.SSLWantReadError:
                self._flush()
                self._receive_ciphertext()

    def sendall(self, data):
        view = memoryview(data)
        while view:
            try:
                count = self.tls.write(view)
                view = view[count:]
                self._flush()
            except ssl.SSLWantReadError:
                self._flush()
                self._receive_ciphertext()


class _HttpsConnectProxy:
    def __init__(self, proxy_certificate, telegram_certificate):
        self.connect_targets = []
        self.observed = []
        self.errors = []
        self._stopping = threading.Event()
        self._active = None
        self._listener = socket.socket()
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self.port = self._listener.getsockname()[1]

        self._proxy_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._proxy_context.load_cert_chain(*map(str, proxy_certificate))
        self._telegram_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._telegram_context.load_cert_chain(*map(str, telegram_certificate))
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    @staticmethod
    def _read_headers(stream, initial=b""):
        buffer = initial
        while b"\r\n\r\n" not in buffer:
            data = stream.recv(65536)
            if not data:
                raise EOFError
            buffer += data
        head, buffer = buffer.split(b"\r\n\r\n", 1)
        lines = head.decode("ascii").split("\r\n")
        headers = {}
        for line in lines[1:]:
            key, value = line.split(":", 1)
            headers[key.lower()] = value.strip()
        length = int(headers.get("content-length", "0"))
        while len(buffer) < length:
            buffer += stream.recv(65536)
        return lines[0], buffer[:length], buffer[length:]

    @staticmethod
    def _response(payload, *, content_type="application/json"):
        return (
            b"HTTP/1.1 200 OK\r\n"
            + f"Content-Length: {len(payload)}\r\n".encode("ascii")
            + f"Content-Type: {content_type}\r\n".encode("ascii")
            + b"Connection: keep-alive\r\n\r\n"
            + payload
        )

    def _payload(self, method, path):
        if path == f"/bot{TOKEN}/getUpdates":
            return json.dumps(
                {
                    "ok": True,
                    "result": [
                        {
                            "update_id": 10,
                            "message": {
                                "from": {"id": 1001},
                                "chat": {"id": 9},
                                "text": "question",
                            },
                        },
                        {
                            "update_id": 11,
                            "message": {
                                "from": {"id": 1001},
                                "chat": {"id": 9},
                                "voice": {"file_id": "voice-1"},
                            },
                        },
                    ],
                },
                separators=(",", ":"),
            ).encode()
        if path == f"/bot{TOKEN}/getFile":
            return b'{"ok":true,"result":{"file_path":"voice/file_1.ogg"}}'
        if path == f"/file/bot{TOKEN}/voice/file_1.ogg":
            return b"deterministic-audio"
        if path == f"/bot{TOKEN}/sendMessage":
            return b'{"ok":true,"result":{}}'
        raise AssertionError(f"unexpected tunneled request {method} {path}")

    def _serve(self):
        try:
            raw, _address = self._listener.accept()
            self._active = raw
            with self._proxy_context.wrap_socket(raw, server_side=True) as outer:
                self._active = outer
                first_line, _body, remainder = self._read_headers(outer)
                method, target, _version = first_line.split(" ")
                if method != "CONNECT" or target != "api.telegram.org:443":
                    outer.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                    return
                self.connect_targets.append(target)
                outer.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                if remainder:
                    raise AssertionError("unexpected bytes after CONNECT headers")

                inner = _TlsOverTls(outer, self._telegram_context)
                inner.handshake()
                buffered = b""
                while not self._stopping.is_set():
                    first_line, _body, buffered = self._read_headers(inner, buffered)
                    method, path, _version = first_line.split(" ")
                    self.observed.append((method, path))
                    payload = self._payload(method, path)
                    content_type = (
                        "application/octet-stream"
                        if method == "GET"
                        else "application/json"
                    )
                    inner.sendall(self._response(payload, content_type=content_type))
        except (EOFError, OSError, ssl.SSLError):
            if not self._stopping.is_set():
                self.errors.append("proxy_transport_failed")
        except Exception as exc:
            self.errors.append(f"{type(exc).__name__}: {exc}")

    def stop(self):
        self._stopping.set()
        try:
            self._listener.close()
        except OSError:
            pass
        if self._active is not None:
            try:
                self._active.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self._active.close()
            except OSError:
                pass
        self._thread.join(timeout=5)
        assert not self._thread.is_alive()


class _Conversation:
    def __init__(self):
        self.calls = []

    def expire_state(self):
        self.calls.append(("expire_state",))

    async def answer_question(self, telegram_id, text):
        self.calls.append(("answer_question", telegram_id, text))
        return BotReply("text reply")

    async def answer_voice(self, telegram_id, filename, audio):
        self.calls.append(("answer_voice", telegram_id, filename, audio))
        return BotReply("voice reply")


@pytest.mark.asyncio
async def test_poll_text_and_voice_use_one_https_connect_tunnel_without_fallback(
    tmp_path, monkeypatch
):
    ca_cert, proxy_certificate, telegram_certificate = _certificate_authority(
        tmp_path
    )
    proxy = _HttpsConnectProxy(proxy_certificate, telegram_certificate)
    direct_attempts = []
    original_getaddrinfo = socket.getaddrinfo
    original_connect = socket.socket.connect

    def guarded_getaddrinfo(host, *args, **kwargs):
        if host == "api.telegram.org":
            direct_attempts.append(("resolve", host))
            raise AssertionError("direct Telegram resolution attempted")
        return original_getaddrinfo(host, *args, **kwargs)

    def guarded_connect(sock, address):
        if address[0] == "api.telegram.org":
            direct_attempts.append(("connect", address[0]))
            raise AssertionError("direct Telegram connection attempted")
        return original_connect(sock, address)

    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)

    proxy_context = ssl.create_default_context(cafile=str(ca_cert))
    telegram_context = ssl.create_default_context(cafile=str(ca_cert))
    manager = urllib3.ProxyManager(
        f"https://127.0.0.1:{proxy.port}",
        proxy_ssl_context=proxy_context,
        ssl_context=telegram_context,
        retries=False,
    )
    client = TelegramProxyClient(manager)
    conversation = _Conversation()
    transport = TelegramTransport(
        TOKEN,
        AccessPolicy(frozenset({1001})),
        conversation,
        client,
    )

    try:
        assert await transport.poll_once(None) == 12
        assert proxy.errors == []
        assert proxy.observed == [
            ("POST", f"/bot{TOKEN}/getUpdates"),
            ("POST", f"/bot{TOKEN}/sendMessage"),
            ("POST", f"/bot{TOKEN}/getFile"),
            ("GET", f"/file/bot{TOKEN}/voice/file_1.ogg"),
            ("POST", f"/bot{TOKEN}/sendMessage"),
        ]
        assert proxy.connect_targets == ["api.telegram.org:443"]
        assert direct_attempts == []
        assert ("answer_question", 1001, "question") in conversation.calls
        assert (
            "answer_voice",
            1001,
            "file_1.ogg",
            b"deterministic-audio",
        ) in conversation.calls

        proxy.stop()
        with pytest.raises(TelegramError, match="^telegram_request_failed$"):
            await transport.poll_once(12)
        assert direct_attempts == []
    finally:
        if proxy._thread.is_alive():
            proxy.stop()
        await client.close()
