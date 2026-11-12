"""A small, real IMAP4rev1 server over TLS for tests (no mocks between the adapter and the socket).

Implements the commands the Mailbox Trigger uses: CAPABILITY, LOGIN, SELECT/EXAMINE, UID SEARCH ALL,
UID FETCH (BODY.PEEK[]), UID STORE +FLAGS, NOOP, LOGOUT. The certificate is self-signed for
``localhost``; tests trust it through ``app.integrations.imap._ssl_context``.
"""
from __future__ import annotations

import datetime
import re
import socket
import ssl
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path


def _certificate(directory: Path) -> tuple[Path, Path]:
    import ipaddress

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    cert_path, key_path = directory / "cert.pem", directory / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return cert_path, key_path


@dataclass
class Folder:
    uidvalidity: int = 1000
    next_uid: int = 1
    messages: list[dict] = field(default_factory=list)  # {"uid", "raw", "flags"}


class ImapServer:
    def __init__(self, username: str = "desk@example.com", password: str = "app-password-1") -> None:
        self.username, self.password = username, password
        self.folders: dict[str, Folder] = {"INBOX": Folder()}
        self.logins = 0
        self._dir = Path(tempfile.mkdtemp(prefix="wae-imap-"))
        self.cert_path, key_path = _certificate(self._dir)
        self._ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        self._ctx.load_cert_chain(self.cert_path, key_path)
        self._sock = socket.create_server(("127.0.0.1", 0))
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    # -- test helpers
    def deliver(self, raw: bytes, folder: str = "INBOX") -> int:
        f = self.folders.setdefault(folder, Folder())
        uid, f.next_uid = f.next_uid, f.next_uid + 1
        f.messages.append({"uid": uid, "raw": raw, "flags": set()})
        return uid

    def reset_uidvalidity(self, folder: str = "INBOX") -> None:
        """What a server does when it rebuilds a folder: every UID becomes meaningless."""
        f = self.folders[folder]
        f.uidvalidity += 1
        for i, m in enumerate(f.messages, start=1):
            m["uid"] = i
        f.next_uid = len(f.messages) + 1

    def flags(self, uid: int, folder: str = "INBOX") -> set[str]:
        return next(m["flags"] for m in self.folders[folder].messages if m["uid"] == uid)

    def client_context(self) -> ssl.SSLContext:
        return ssl.create_default_context(cafile=str(self.cert_path))

    def close(self) -> None:
        self._stop.set()
        self._sock.close()

    # -- protocol
    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                raw, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._session, args=(raw,), daemon=True).start()

    def _session(self, raw: socket.socket) -> None:
        try:
            conn = self._ctx.wrap_socket(raw, server_side=True)
        except (ssl.SSLError, OSError):
            raw.close()
            return
        f = conn.makefile("rb")
        send = lambda line: conn.sendall(line if isinstance(line, bytes) else line.encode() + b"\r\n")  # noqa: E731
        send("* OK [CAPABILITY IMAP4rev1] WAE test server ready")
        authed, selected, writable = False, None, False
        try:
            while line := f.readline():
                text = line.decode().rstrip("\r\n")
                tag, _, rest = text.partition(" ")
                parts = re.findall(r'"((?:[^"\\]|\\.)*)"|(\S+)', rest)
                args = [q.replace('\\"', '"').replace("\\\\", "\\") if q else a for q, a in parts]
                cmd = args[0].upper() if args else ""
                if cmd == "CAPABILITY":
                    send("* CAPABILITY IMAP4rev1")
                    send(f"{tag} OK done")
                elif cmd == "NOOP":
                    send(f"{tag} OK done")
                elif cmd == "LOGIN":
                    if args[1:3] == [self.username, self.password]:
                        authed = True
                        self.logins += 1
                        send(f"{tag} OK logged in")
                    else:
                        send(f"{tag} NO [AUTHENTICATIONFAILED] invalid credentials")
                elif cmd in ("SELECT", "EXAMINE") and authed:
                    folder = self.folders.get(args[1])
                    if folder is None:
                        send(f"{tag} NO no such folder")
                        continue
                    selected, writable = folder, cmd == "SELECT"
                    send(f"* {len(folder.messages)} EXISTS")
                    send(f"* OK [UIDVALIDITY {folder.uidvalidity}] valid")
                    send(f"* OK [UIDNEXT {folder.next_uid}] next")
                    send(f"{tag} OK [{'READ-WRITE' if writable else 'READ-ONLY'}] selected")
                elif cmd == "UID" and selected is not None:
                    sub = args[1].upper()
                    if sub == "SEARCH":
                        send("* SEARCH " + " ".join(str(m["uid"]) for m in selected.messages))
                        send(f"{tag} OK search done")
                    elif sub == "FETCH":
                        for seq, m in enumerate(selected.messages, start=1):
                            if m["uid"] == int(args[2]):
                                send(f"* {seq} FETCH (UID {m['uid']} BODY[] {{{len(m['raw'])}}}".encode() + b"\r\n" + m["raw"] + b")\r\n")
                        send(f"{tag} OK fetch done")
                    elif sub == "STORE":
                        if not writable:
                            send(f"{tag} NO read-only")
                            continue
                        for seq, m in enumerate(selected.messages, start=1):
                            if m["uid"] == int(args[2]):
                                m["flags"].add("\\Seen")
                                send(f"* {seq} FETCH (UID {m['uid']} FLAGS (\\Seen))")
                        send(f"{tag} OK store done")
                    else:
                        send(f"{tag} BAD unsupported")
                elif cmd == "LOGOUT":
                    send("* BYE bye")
                    send(f"{tag} OK done")
                    return
                else:
                    send(f"{tag} BAD unsupported or not allowed now")
        except (OSError, ssl.SSLError, IndexError, ValueError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass
