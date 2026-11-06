"""A tiny real-socket IMAP server for tests, so Python's real imaplib client talks real protocol to it.
It records every command, and refuses anything that would change the mailbox."""
from __future__ import annotations

import datetime
import ipaddress
import socketserver
import ssl
import threading
from pathlib import Path


def make_cert(folder) -> tuple[str, str]:
    """A throwaway certificate for 127.0.0.1, so the real TLS and certificate check can run locally."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "fake imap")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), True).sign(key, hashes.SHA256()))
    cert_path, key_path = Path(folder) / "fake_imap.crt", Path(folder) / "fake_imap.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return str(cert_path), str(key_path)


class FakeImap:
    def __init__(self, messages: list[bytes], tls: tuple[str, str] | None = None, sent: list[bytes] | None = None):
        self.messages = messages
        self.sent = sent or []
        self.commands: list[str] = []
        fake = self

        class Handler(socketserver.StreamRequestHandler):
            def out(self, data: bytes) -> None:
                self.wfile.write(data)
                self.wfile.flush()

            def box(self) -> list[bytes]:
                return fake.sent if getattr(self, "in_sent", False) else fake.messages

            def handle(self):
                self.out(b"* OK fake ready\r\n")
                while True:
                    line = self.rfile.readline()
                    if not line:
                        return
                    tag, _, rest = line.decode().strip().partition(" ")
                    fake.commands.append(rest)
                    cmd = rest.upper()
                    if cmd.startswith("CAPABILITY"):
                        self.out(b"* CAPABILITY IMAP4rev1\r\n" + f"{tag} OK done\r\n".encode())
                    elif cmd.startswith("LOGIN"):
                        self.out(f"{tag} OK logged in\r\n".encode())
                    elif cmd.startswith("EXAMINE"):
                        self.in_sent = "SENT" in cmd
                        self.out(f"* {len(self.box())} EXISTS\r\n{tag} OK [READ-ONLY] done\r\n".encode())
                    elif cmd.startswith("UID SEARCH"):
                        uids = " ".join(str(i + 1) for i in range(len(self.box())))
                        self.out(f"* SEARCH {uids}\r\n{tag} OK done\r\n".encode())
                    elif cmd.startswith("UID FETCH"):
                        uid = int(rest.split()[2])
                        body = self.box()[uid - 1]
                        self.out(f"* {uid} FETCH (UID {uid} BODY[] {{{len(body)}}}\r\n".encode() + body + b")\r\n" + f"{tag} OK done\r\n".encode())
                    elif cmd.startswith("LOGOUT"):
                        self.out(b"* BYE\r\n" + f"{tag} OK bye\r\n".encode())
                        return
                    else:
                        self.out(f"{tag} NO refused\r\n".encode())

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True

            def get_request(self):
                sock, addr = super().get_request()
                if tls:
                    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                    context.load_cert_chain(*tls)
                    sock = context.wrap_socket(sock, server_side=True)
                return sock, addr

        self.server = Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
