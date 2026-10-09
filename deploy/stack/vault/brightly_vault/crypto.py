"""Files are encrypted while they stream in: AES-256-GCM in 1 MiB chunks, each with its own
nonce (8 random bytes + a counter) and the last chunk marked, so a truncated or reordered file
fails to decrypt. Every file has its own random key, stored wrapped by the master key."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"BVLT1\n"
CHUNK = 1024 * 1024
MORE, LAST = b"C", b"L"


class DecryptError(Exception):
    pass


def new_key() -> bytes:
    return AESGCM.generate_key(bit_length=256)


def seal(master: bytes, data: bytes, purpose: bytes) -> bytes:
    nonce = os.urandom(12)
    return nonce + AESGCM(master).encrypt(nonce, data, purpose)


def unseal(master: bytes, blob: bytes, purpose: bytes) -> bytes:
    return AESGCM(master).decrypt(blob[:12], blob[12:], purpose)


class EncryptingWriter:
    def __init__(self, path: Path, key: bytes):
        self.f = open(path, "wb")
        self.aes = AESGCM(key)
        self.prefix = os.urandom(8)
        self.counter = 0
        self.buf = bytearray()
        self.f.write(MAGIC + self.prefix)

    def _emit(self, chunk: bytes, last: bool) -> None:
        nonce = self.prefix + self.counter.to_bytes(4, "big")
        ct = self.aes.encrypt(nonce, chunk, LAST if last else MORE)
        self.f.write(len(ct).to_bytes(4, "big") + ct)
        self.counter += 1

    def write(self, data: bytes) -> None:
        self.buf += data
        while len(self.buf) > CHUNK:      # more data follows, so this chunk isn't the last
            self._emit(bytes(self.buf[:CHUNK]), False)
            del self.buf[:CHUNK]

    def close(self) -> None:
        self._emit(bytes(self.buf), True)
        self.buf.clear()
        self.f.close()

    def abort(self) -> None:
        self.f.close()


def decrypt_chunks(path: Path, key: bytes) -> Iterator[bytes]:
    aes = AESGCM(key)
    with open(path, "rb") as f:
        if f.read(len(MAGIC)) != MAGIC:
            raise DecryptError("not a vault file")
        prefix = f.read(8)
        counter = 0
        head = f.read(4)
        while head:
            if len(head) != 4:
                raise DecryptError("truncated")
            ct = f.read(int.from_bytes(head, "big"))
            head = f.read(4)
            nonce = prefix + counter.to_bytes(4, "big")
            try:
                yield aes.decrypt(nonce, ct, MORE if head else LAST)
            except Exception as exc:
                raise DecryptError("file is damaged or was changed") from exc
            counter += 1
        if counter == 0:
            raise DecryptError("empty")
