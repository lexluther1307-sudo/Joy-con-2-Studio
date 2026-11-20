"""Encrypted link between the Windows app and the Steam Deck plugin.

Standard library only, so this same file runs on the PC and on the Deck.

Pairing uses a 6-digit code and SPAKE2 (a password-authenticated key exchange over the Ed25519 group).
The code itself is never sent, and nobody who captures the handshake can test guesses offline: every
guess needs a live exchange with the Deck, and the Deck ignores addresses that keep failing. After the
handshake both ends hold fresh keys, so a later leak of the code does not reveal earlier traffic.

Handshake messages carry public values (their confirmations prove the code):
  PC   -> Deck  "JC2H" 2 1  X                  X = x*B + w*M
  Deck -> PC    "JC2H" 2 2  Y + confirm_deck   Y = y*B + w*N
  PC   -> Deck  "JC2H" 2 3  confirm_pc
After that every packet is:
  header 23 bytes (readable, but authenticated): "JC2E", version 2, type, dir, session 8, counter 8
  ciphertext (HMAC-SHA256 keystream) + 16-byte tag (HMAC-SHA256, encrypt-then-MAC)
"""

import collections
import hashlib
import hmac
import secrets
import socket
import struct
import threading
import time
from typing import NamedTuple

MAGIC = b"JC2E"
HS_MAGIC = b"JC2H"
VERSION = 2
HS_X, HS_Y, HS_CONFIRM = 1, 2, 3
HEARTBEAT, STATE, RUMBLE, BYE = 3, 4, 5, 6
PC_TO_DECK, DECK_TO_PC = 1, 2
HEADER = struct.Struct(">4sBBB8sQ")  # magic, version, type, direction, session, counter
HS_HEADER = struct.Struct(">4sBB")  # magic, version, handshake message type
TAG_LEN = 16
REPORT_LEN = 64
# A DualShock 4 style input report with the sticks centred and nothing pressed
NEUTRAL_REPORT = bytes([0x01, 0x80, 0x80, 0x80, 0x80, 0x08]) + bytes(REPORT_LEN - 6)
BAD_LIMIT = 10  # failed packets from one address...
BAD_WINDOW = 60.0  # ...within this many seconds...
BLOCK_FOR = 60.0  # ...are answered with silence for this many seconds


# ---- the Ed25519 group (prime order L), in extended coordinates --------------------

P = 2 ** 255 - 19
L = 2 ** 252 + 27742317777372353535851937790883648493
D = (-121665 * pow(121666, P - 2, P)) % P
SQRT_M1 = pow(2, (P - 1) // 4, P)
IDENTITY = (0, 1, 1, 0)


def _add(p1, p2):
    x1, y1, z1, t1 = p1
    x2, y2, z2, t2 = p2
    a = (y1 - x1) * (y2 - x2) % P
    b = (y1 + x1) * (y2 + x2) % P
    c = t1 * 2 * D * t2 % P
    d = z1 * 2 * z2 % P
    e, f, g, h = (b - a) % P, (d - c) % P, (d + c) % P, (b + a) % P
    return (e * f % P, g * h % P, f * g % P, e * h % P)


def _mul(k, point):
    """k * point, without reducing k (the subgroup check needs k = L). Not constant-time."""
    result = IDENTITY
    addend = point
    while k > 0:
        if k & 1:
            result = _add(result, addend)
        addend = _add(addend, addend)
        k >>= 1
    return result


def _neg(point):
    x, y, z, t = point
    return ((-x) % P, y, z, (-t) % P)


def _equal(p1, p2):
    return (p1[0] * p2[2] - p2[0] * p1[2]) % P == 0 and (p1[1] * p2[2] - p2[1] * p1[2]) % P == 0


def _affine(point):
    x, y, z, _ = point
    inv = pow(z, P - 2, P)
    return x * inv % P, y * inv % P


def _from_affine(x, y):
    return (x % P, y % P, 1, x * y % P)


def _on_curve(x, y):
    return (y * y - x * x - 1 - D * x * x * y * y) % P == 0


def _recover_x(u, v):
    """An x with v * x^2 == u (mod P), or None (RFC 8032 method)."""
    x = u * pow(v, 3, P) * pow(u * pow(v, 7, P) % P, (P - 5) // 8, P) % P
    if v * x * x % P == u % P:
        return x
    if v * x * x % P == (-u) % P:
        return x * SQRT_M1 % P
    return None


_BY = 4 * pow(5, P - 2, P) % P
_BX = _recover_x((_BY * _BY - 1) % P, (D * _BY * _BY + 1) % P)
if _BX % 2:
    _BX = P - _BX  # the standard base point has an even x
B = _from_affine(_BX, _BY)


def _hash_to_point(label: bytes):
    """A group element nobody knows the discrete log of (needed for M and N)."""
    counter = 0
    while True:
        digest = hashlib.sha512(label + counter.to_bytes(4, "big")).digest()
        y = int.from_bytes(digest[:32], "little") % P
        u = (y * y - 1) % P
        v = (D * y * y + 1) % P
        x = _recover_x(u, v) if v else None
        if x is not None:
            point = _mul(8, _from_affine(x, y))  # clear the cofactor: the result has order L
            if not _equal(point, IDENTITY):
                return point
        counter += 1


M = _hash_to_point(b"joycon2studio spake2 M")
N = _hash_to_point(b"joycon2studio spake2 N")


def _self_check():
    assert _on_curve(*_affine(B)), "base point is not on the curve"
    assert _equal(_mul(L, B), IDENTITY), "base point does not have order L"
    assert _equal(_mul(L, M), IDENTITY) and _equal(_mul(L, N), IDENTITY), "M or N is not in the group"


_self_check()


def encode_point(point) -> bytes:
    x, y = _affine(point)
    return x.to_bytes(32, "big") + y.to_bytes(32, "big")


def decode_point(data: bytes):
    """The point for 64 bytes from the other end, or None if it is not a valid group element."""
    if len(data) != 64:
        return None
    x = int.from_bytes(data[:32], "big")
    y = int.from_bytes(data[32:], "big")
    if x >= P or y >= P or (x == 0 and y == 1) or not _on_curve(x, y):
        return None
    point = _from_affine(x, y)
    if not _equal(_mul(L, point), IDENTITY):
        return None
    return point


def random_scalar() -> int:
    return secrets.randbelow(L - 1) + 1


# ---- the 6-digit pairing code ----------------------------------------------------------

def new_pin() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def normalize_pin(text: str) -> str:
    """The six digits of a pairing code (spaces, dashes and line ends are ignored), or ValueError."""
    digits = "".join(text.split()).replace("-", "")
    if len(digits) != 6 or not digits.isdigit() or not digits.isascii():
        raise ValueError("The pairing code is six digits, for example 042917.")
    return digits


def pin_scalar(pin: str) -> int:
    return int.from_bytes(hashlib.sha512(b"joycon2studio pin" + pin.encode("ascii")).digest(), "little") % L


# ---- packets after the handshake ---------------------------------------------------------

class Packet(NamedTuple):
    ptype: int
    direction: int
    session: bytes
    counter: int
    payload: bytes


class Keys:
    """Encryption and authentication keys made from a session secret."""

    def __init__(self, secret: bytes):
        if len(secret) < 16:
            raise ValueError("the session secret is too short")
        self.enc = hmac.new(secret, b"joycon2studio encryption", hashlib.sha256).digest()
        self.mac = hmac.new(secret, b"joycon2studio authentication", hashlib.sha256).digest()


def _keystream(key: bytes, direction: int, session: bytes, counter: int, length: int) -> bytes:
    prefix = struct.pack(">B8sQ", direction, session, counter)
    out = bytearray()
    block = 0
    while len(out) < length:
        out += hmac.new(key, prefix + struct.pack(">I", block), hashlib.sha256).digest()
        block += 1
    return bytes(out[:length])


def _xor(data: bytes, stream: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(data, stream))


def _tag(key: bytes, header: bytes, body: bytes) -> bytes:
    return hmac.new(key, header + body, hashlib.sha256).digest()[:TAG_LEN]


def seal(keys: Keys, ptype: int, direction: int, session: bytes, counter: int, payload: bytes = b"") -> bytes:
    header = HEADER.pack(MAGIC, VERSION, ptype, direction, session, counter)
    body = _xor(payload, _keystream(keys.enc, direction, session, counter, len(payload)))
    return header + body + _tag(keys.mac, header, body)


def open_packet(keys: Keys, data: bytes):
    """A Packet, or None when the bytes are not a valid packet for these keys."""
    if len(data) < HEADER.size + TAG_LEN:
        return None
    header, body, tag = data[:HEADER.size], data[HEADER.size:-TAG_LEN], data[-TAG_LEN:]
    magic, version, ptype, direction, session, counter = HEADER.unpack(header)
    if magic != MAGIC or version != VERSION or counter == 0:
        return None
    if not hmac.compare_digest(_tag(keys.mac, header, body), tag):
        return None
    payload = _xor(body, _keystream(keys.enc, direction, session, counter, len(body)))
    return Packet(ptype, direction, session, counter, payload)


# ---- SPAKE2 pieces ----------------------------------------------------------------------

def _transcript(x_bytes: bytes, y_bytes: bytes, z_bytes: bytes, w: int) -> bytes:
    return hashlib.sha256(b"joycon2studio spake2 v1" + x_bytes + y_bytes + z_bytes
                          + w.to_bytes(32, "little")).digest()


def _confirm(k: bytes, role: bytes, x_bytes: bytes, y_bytes: bytes) -> bytes:
    key = hmac.new(k, b"joycon2studio confirm", hashlib.sha256).digest()
    return hmac.new(key, role + x_bytes + y_bytes, hashlib.sha256).digest()


def _session_secret(k: bytes) -> bytes:
    return hmac.new(k, b"joycon2studio session", hashlib.sha256).digest()


def _session_id(k: bytes) -> bytes:
    return hmac.new(k, b"joycon2studio session id", hashlib.sha256).digest()[:8]


# ---- the two ends -------------------------------------------------------------------------

class PcLink:
    """The PC end. Pairs with the Deck, sends the controller, and takes the Deck's rumble.

    get_report() returns the 64-byte controller report. Callbacks run on the link's thread:
    on_state("off" | "searching" | "confirming" | "connected"), on_rumble(small, large), on_message(text).
    """

    HS_EVERY = 0.5
    STATE_EVERY = 0.01
    TIMEOUT = 1.5

    def __init__(self, port, get_report, on_state, on_rumble, on_message):
        self.port = port
        self.get_report = get_report
        self.on_state = on_state
        self.on_rumble = on_rumble
        self.on_message = on_message
        self.deck_ip = ""
        self.pin = None
        self.w = 0
        self.want = False
        self.state = "off"
        self.x = 0
        self.x_bytes = b""
        self.hs_confirm = b""
        self.keys = None
        self.sid = b""
        self.counter = 0
        self.deck_counter = 0
        self.last_deck = 0.0
        self.last_hs = 0.0
        self.last_state = 0.0
        self._sock = None
        self._thread = None
        self._lock = threading.Lock()
        self._mismatch_told = False

    def configure(self, deck_ip: str, pin_text: str):
        self.pin = normalize_pin(pin_text)
        self.w = pin_scalar(self.pin)
        self.deck_ip = deck_ip.strip()

    def start(self):
        if self.pin is None or not self.deck_ip:
            raise ValueError("enter the Deck's address and its pairing code")
        if self.state == "off":
            self._new_attempt()
            self._set_state("searching", f"Searching for the Deck at {self.deck_ip}.")
        self.want = True
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def disconnect(self):
        if self.keys is not None:
            self._send(BYE, bytes([1]))  # 1 = the PC disconnected
        self.want = False
        self.keys = None
        self._set_state("off", "Disconnected from the Deck. This PC uses the Joy-Cons again.")

    def _new_attempt(self):
        """A fresh X for a new attempt. Each attempt has its own keys, so nothing from an old one is reused."""
        self._mismatch_told = False
        self.x = random_scalar()
        self.x_bytes = encode_point(_add(_mul(self.x, B), _mul(self.w, M)))
        self.keys = None
        self.sid = b""
        self.counter = 0
        self.deck_counter = 0
        self.last_hs = 0.0

    def _run(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("", 0))
        sock.settimeout(0.005)
        self._sock = sock
        try:
            while self.want or self.state != "off":
                data = None
                addr = None
                try:
                    data, addr = sock.recvfrom(512)
                except socket.timeout:
                    pass
                except ConnectionResetError:
                    pass  # Windows reports an unreachable Deck this way; keep trying
                now = time.monotonic()
                try:
                    if data is not None:
                        self._handle(data, addr, now)
                    self._tick(time.monotonic())
                except Exception as exc:  # a broken callback must not stop the link
                    self.on_message(f"link error: {exc}")
        except OSError as exc:
            self.want = False
            self._set_state("off", f"The network link stopped: {exc}")
        finally:
            self._sock = None
            sock.close()

    def _handle(self, data, addr, now):
        if addr[0] != self.deck_ip or self.pin is None:
            return
        if data[:4] == HS_MAGIC:
            self._handle_handshake(data, now)
            return
        if self.keys is None:
            return
        pkt = open_packet(self.keys, data)
        if pkt is None or pkt.direction != DECK_TO_PC or pkt.session != self.sid or pkt.counter <= self.deck_counter:
            return
        self.deck_counter = pkt.counter
        self.last_deck = now
        if self.state == "confirming":
            self._set_state("connected", "Connected to the Deck. This PC stops using the Joy-Cons.")
        if pkt.ptype == RUMBLE and len(pkt.payload) >= 2:
            self.on_rumble(pkt.payload[0], pkt.payload[1])
        elif pkt.ptype == BYE:
            self.want = False
            self.keys = None
            self._set_state("off", "The Deck disconnected. This PC uses the Joy-Cons again.")

    def _handle_handshake(self, data, now):
        if self.state != "searching" or len(data) != HS_HEADER.size + 96:
            return
        magic, version, mtype = HS_HEADER.unpack(data[:HS_HEADER.size])
        if version != VERSION or mtype != HS_Y:
            return
        y_bytes = data[HS_HEADER.size:HS_HEADER.size + 64]
        confirm_deck = data[HS_HEADER.size + 64:]
        y_point = decode_point(y_bytes)
        if y_point is None:
            return
        z = _mul(self.x, _add(y_point, _neg(_mul(self.w, N))))
        k = _transcript(self.x_bytes, y_bytes, encode_point(z), self.w)
        if not hmac.compare_digest(_confirm(k, b"deck", self.x_bytes, y_bytes), confirm_deck):
            if not self._mismatch_told:  # once per attempt, not on every retry
                self._mismatch_told = True
                self.on_message("The pairing code does not match the Deck's. Check the six digits.")
            return
        self.sid = _session_id(k)
        self.keys = Keys(_session_secret(k))
        self.counter = 0
        self.deck_counter = 0
        self.last_deck = now
        self.hs_confirm = HS_HEADER.pack(HS_MAGIC, VERSION, HS_CONFIRM) + _confirm(k, b"pc", self.x_bytes, y_bytes)
        self.last_hs = now
        self._send_raw(self.hs_confirm)
        self._set_state("confirming", "The Deck accepted the code. Finishing the connection.")

    def _tick(self, now):
        if self.pin is None or not self.want:
            return
        if self.state in ("confirming", "connected") and now - self.last_deck > self.TIMEOUT:
            self._new_attempt()
            self._set_state("searching", "Lost the Deck. Searching again.")
        if self.state == "searching":
            if now - self.last_hs >= self.HS_EVERY:
                self.last_hs = now
                self._send_raw(HS_HEADER.pack(HS_MAGIC, VERSION, HS_X) + self.x_bytes)
        elif self.state == "confirming":
            if now - self.last_hs >= self.HS_EVERY:
                self.last_hs = now
                self._send_raw(self.hs_confirm)
        elif self.state == "connected" and now - self.last_state >= self.STATE_EVERY:
            self.last_state = now
            self._send(STATE, self.get_report())

    def _send(self, ptype, payload=b""):
        if self.keys is None:
            return
        with self._lock:
            self.counter += 1
            packet = seal(self.keys, ptype, PC_TO_DECK, self.sid, self.counter, payload)
        self._send_raw(packet)

    def _send_raw(self, packet: bytes):
        sock = self._sock
        if sock is None:
            return
        try:
            sock.sendto(packet, (self.deck_ip, self.port))
        except OSError:
            pass  # the Deck is not reachable right now; the link keeps trying

    def _set_state(self, state, message=""):
        changed = state != self.state
        self.state = state
        if changed:
            self.on_state(state)
        if message:
            self.on_message(message)


class DeckLink:
    """The Deck end. Accepts a PC that gives the right code, shows its controller, and sends rumble back.

    Callbacks run on the link's thread: on_state("idle" | "connected" | "refused"), on_message(text).
    """

    TIMEOUT = 1.5
    HEARTBEAT_EVERY = 0.5
    REPORT_FRESH = 0.5

    def __init__(self, port, pin_text, on_state, on_message, on_new_pin=None):
        self.port = port
        self.on_state = on_state
        self.on_message = on_message
        self.on_new_pin = on_new_pin  # called with each new code, so the Deck screen and the saved file show it
        self.w = pin_scalar(normalize_pin(pin_text))
        self.state = "idle"
        self.refused = False
        self.hs = None  # the current handshake: X, Y, the shared secret k, its reply, whether it is confirmed
        self.pc_addr = None
        self.keys = None
        self.sid = b""
        self.peer_counter = 0
        self.out_counter = 0
        self.report = None
        self.last_pc = 0.0
        self.last_report = 0.0
        self.last_heartbeat = 0.0
        self.senders = {}  # PC address -> packets accepted from it, for the network scan
        self.blocked = {}  # address -> time until which its packets get no answer
        self.bad = {}  # address -> times of recent failed packets
        self._sock = None
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    def _use_pin(self, pin: str):
        self.w = pin_scalar(pin)
        self.hs = None
        self.keys = None
        self.sid = b""
        self.peer_counter = 0
        self.out_counter = 0

    def set_pin(self, pin_text: str):
        """A code the Deck user chose (New pairing code). The PC has to be given the new code."""
        self._use_pin(normalize_pin(pin_text))
        if self.state != "refused":
            self._set_state("idle", "The pairing code changed. Waiting for the PC.", new_code=False)

    def _new_code_after_disconnect(self):
        pin = new_pin()
        self._use_pin(pin)
        if self.on_new_pin is not None:
            try:
                self.on_new_pin(pin)
            except Exception as exc:  # saving the code must not stop the link
                self.on_message(f"Could not save the new pairing code: {exc}")

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(1.0)
            if self._thread.is_alive():
                return
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("", self.port))
        sock.settimeout(0.05)
        self._sock = sock
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        sock = self._sock
        try:
            while not self._stop.is_set():
                data = None
                addr = None
                try:
                    data, addr = sock.recvfrom(512)
                except socket.timeout:
                    pass
                except ConnectionResetError:
                    pass  # Windows reports an unreachable PC this way; keep listening
                now = time.monotonic()
                try:
                    if data is not None:
                        self._handle(data, addr, now)
                    self._tick(now)
                except Exception as exc:  # a broken callback must not stop the link
                    self.on_message(f"link error: {exc}")
        except OSError as exc:
            self.on_message(f"network error: {exc}")
        finally:
            self._sock = None
            sock.close()

    def _note_bad(self, ip, now):
        times = self.bad.setdefault(ip, collections.deque(maxlen=BAD_LIMIT))
        times.append(now)
        if len(times) == BAD_LIMIT and now - times[0] <= BAD_WINDOW:
            self.blocked[ip] = now + BLOCK_FOR
            times.clear()
            self.on_message(f"Ignoring {ip} for {BLOCK_FOR:.0f} seconds after repeated failed pairing attempts.")

    def _handle(self, data, addr, now):
        ip = addr[0]
        if self.blocked.get(ip, 0.0) > now or self.refused:
            return
        if data[:4] == HS_MAGIC:
            self._handle_handshake(data, addr, now)
            return
        if len(data) < HEADER.size or data[:4] != MAGIC:
            self._note_bad(ip, now)  # not a packet of this app at all
            return
        if self.keys is None or HEADER.unpack(data[:HEADER.size])[4] != self.sid:
            return  # from an earlier session, e.g. a PC that has not heard the new code yet: not a guess
        pkt = open_packet(self.keys, data)
        if pkt is None:
            self._note_bad(ip, now)  # this session's packet that does not verify
            return
        if pkt.direction != PC_TO_DECK or pkt.counter <= self.peer_counter:
            return  # a replayed packet
        self.peer_counter = pkt.counter
        self.pc_addr = addr
        self.last_pc = now
        self.senders[ip] = self.senders.get(ip, 0) + 1
        if self.state == "idle":
            self._set_state("connected", f"The PC at {ip} is connected.")
        if pkt.ptype == STATE and self.state == "connected" and len(pkt.payload) == REPORT_LEN:
            self.report = pkt.payload
            self.last_report = now
        elif pkt.ptype == BYE:
            self.keys = None
            self.sid = b""
            self._set_state("idle", "The PC disconnected. Waiting for the PC.")

    def _handle_handshake(self, data, addr, now):
        ip = addr[0]
        if len(data) < HS_HEADER.size:
            self._note_bad(ip, now)
            return
        magic, version, mtype = HS_HEADER.unpack(data[:HS_HEADER.size])
        body = data[HS_HEADER.size:]
        if version != VERSION:
            return
        if mtype == HS_X:
            if len(body) != 64:
                self._note_bad(ip, now)
                return
            if self.hs is not None and self.hs["x"] == body:
                self._send_raw(self.hs["reply"], addr)  # the same attempt again: give the same answer
                return
            x_point = decode_point(body)
            if x_point is None:
                self._note_bad(ip, now)
                return
            y = random_scalar()
            y_point = _add(_mul(y, B), _mul(self.w, N))
            z = _mul(y, _add(x_point, _neg(_mul(self.w, M))))
            y_bytes = encode_point(y_point)
            k = _transcript(body, y_bytes, encode_point(z), self.w)
            reply = HS_HEADER.pack(HS_MAGIC, VERSION, HS_Y) + y_bytes + _confirm(k, b"deck", body, y_bytes)
            self.hs = {"x": body, "y": y_bytes, "k": k, "reply": reply, "confirmed": False}
            self._send_raw(reply, addr)
            return
        if mtype == HS_CONFIRM:
            if self.hs is None or self.hs["confirmed"] or len(body) != 32:
                return
            expected = _confirm(self.hs["k"], b"pc", self.hs["x"], self.hs["y"])
            if not hmac.compare_digest(expected, body):
                self._note_bad(ip, now)  # a guess at the code that failed
                return
            self.hs["confirmed"] = True
            k = self.hs["k"]
            self.sid = _session_id(k)
            self.keys = Keys(_session_secret(k))
            self.peer_counter = 0
            self.out_counter = 0
            self.pc_addr = addr
            self.last_pc = now
            self.last_heartbeat = now
            self.senders[ip] = self.senders.get(ip, 0) + 1
            self._set_state("connected", f"The PC at {ip} is connected.")
            self._send(HEARTBEAT)  # the first encrypted packet, so the PC knows the link is up

    def _tick(self, now):
        if self.state != "connected":
            return
        if now - self.last_pc > self.TIMEOUT:
            self._set_state("idle", "The PC stopped responding. Waiting for the PC.")
        elif now - self.last_heartbeat >= self.HEARTBEAT_EVERY:
            self.last_heartbeat = now
            self._send(HEARTBEAT)

    def _send(self, ptype, payload=b""):
        if self.keys is None or self.pc_addr is None:
            return
        with self._lock:
            self.out_counter += 1
            packet = seal(self.keys, ptype, DECK_TO_PC, self.sid, self.out_counter, payload)
        self._send_raw(packet, self.pc_addr)

    def _send_raw(self, packet: bytes, addr=None):
        sock = self._sock
        target = addr if addr is not None else self.pc_addr
        if sock is None or target is None:
            return
        try:
            sock.sendto(packet, target)
        except OSError:
            pass

    def send_rumble(self, small: int, large: int):
        if self.state == "connected":
            self._send(RUMBLE, bytes([small & 0xFF, large & 0xFF]))

    def current_report(self, now: float) -> bytes:
        """The PC's controller while it is connected and sending; otherwise a neutral controller."""
        if self.state == "connected" and self.report is not None and now - self.last_report < self.REPORT_FRESH:
            return self.report
        return NEUTRAL_REPORT

    def disconnect(self):
        """The Deck user disconnects: the PC is told, and its connection is refused until connect()."""
        self._send(BYE, bytes([2]))  # 2 = the Deck disconnected
        self.refused = True
        self.keys = None
        self.sid = b""
        self._set_state("refused", "Disconnected from the PC. Press Connect to accept the PC again.")

    def connect(self):
        self.refused = False
        if self.state == "refused":
            self._set_state("idle", "Waiting for the PC.")

    def _set_state(self, state, message="", new_code=True):
        if new_code and self.state == "connected" and state != "connected":
            # Every disconnect (the Deck's button, the PC's, or a dropped link) makes a new code
            self._new_code_after_disconnect()
            message = (message + " " if message else "") + "A new pairing code is shown. Enter it on the PC."
        changed = state != self.state
        self.state = state
        if changed:
            self.on_state(state)
        if message:
            self.on_message(message)
