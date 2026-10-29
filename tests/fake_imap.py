"""A tiny IMAP4rev1 server for tests: enough of LOGIN / SELECT / EXAMINE / UID SEARCH /
UID FETCH / LOGOUT for imaplib, plain TCP (tests swap IMAP4_SSL for IMAP4)."""

import re
import socketserver
import threading
from datetime import date


class FakeImap:
    def __init__(self, user: str = "inv@x.test", password: str = "app-pass") -> None:
        self.user, self.password = user, password
        self.uidvalidity = 1000
        self.messages: dict[int, tuple[date, bytes]] = {}
        self.flag_changes = 0  # anything that would alter the mailbox
        outer = self

        class Handler(socketserver.StreamRequestHandler):
            def send(self, line: bytes) -> None:
                self.wfile.write(line + b"\r\n")

            def handle(self) -> None:
                self.send(b"* OK fake IMAP ready")
                while True:
                    raw = self.rfile.readline()
                    if not raw:
                        return
                    tag, _, rest = raw.strip().partition(b" ")
                    cmd, _, args = rest.partition(b" ")
                    cmd = cmd.upper()
                    if cmd == b"CAPABILITY":
                        self.send(b"* CAPABILITY IMAP4rev1")
                        self.send(tag + b" OK done")
                    elif cmd == b"LOGIN":
                        u, p = (x.strip(b'"') for x in args.split(b" ", 1))
                        ok = (u.decode(), p.decode()) == (outer.user, outer.password)
                        self.send(
                            tag + (b" OK logged in" if ok else b" NO [AUTHENTICATIONFAILED] bad")
                        )
                    elif cmd in (b"SELECT", b"EXAMINE"):
                        if cmd == b"SELECT":
                            outer.flag_changes += 1
                        self.send(b"* %d EXISTS" % len(outer.messages))
                        self.send(b"* OK [UIDVALIDITY %d] ok" % outer.uidvalidity)
                        self.send(tag + b" OK [READ-ONLY] done")
                    elif cmd == b"UID":
                        self.uid(tag, args)
                    elif cmd == b"LOGOUT":
                        self.send(b"* BYE")
                        self.send(tag + b" OK bye")
                        return
                    else:
                        if cmd == b"STORE":
                            outer.flag_changes += 1
                        self.send(tag + b" BAD unsupported")

            def uid(self, tag: bytes, args: bytes) -> None:
                sub, _, rest = args.partition(b" ")
                if sub.upper() == b"SEARCH":
                    text = rest.decode()
                    uids = sorted(outer.messages)
                    if m := re.search(r"UID (\d+):\*", text):
                        hits = [u for u in uids if u >= int(m.group(1))] or uids[-1:]
                    elif m := re.search(r"SINCE (\S+)", text):
                        from datetime import datetime

                        since = datetime.strptime(m.group(1), "%d-%b-%Y").date()
                        hits = [u for u in uids if outer.messages[u][0] >= since]
                    else:
                        hits = uids
                    self.send(b"* SEARCH " + b" ".join(str(u).encode() for u in hits))
                    self.send(tag + b" OK search done")
                elif sub.upper() == b"FETCH":
                    uid_s, _, what = rest.partition(b" ")
                    uid = int(uid_s)
                    body = outer.messages[uid][1]
                    if b"RFC822.SIZE" in what:
                        self.send(b"* 1 FETCH (UID %d RFC822.SIZE %d)" % (uid, len(body)))
                    else:
                        if b"PEEK" not in what:
                            outer.flag_changes += 1
                        self.wfile.write(b"* 1 FETCH (UID %d BODY[] {%d}\r\n" % (uid, len(body)))
                        self.wfile.write(body + b")\r\n")
                    self.send(tag + b" OK fetch done")
                else:
                    self.send(tag + b" BAD")

        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
