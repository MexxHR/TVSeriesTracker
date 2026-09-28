"""Compare signer certificate in APK Signature Scheme v2 blocks."""
import hashlib
import struct
import sys
from pathlib import Path


def certificate_digest(path):
    data = Path(path).read_bytes()
    eocd = data.rfind(b"PK\x05\x06")
    if eocd < 0:
        raise ValueError("No APK ZIP directory")
    directory = struct.unpack_from("<I", data, eocd + 16)[0]
    size = struct.unpack_from("<Q", data, directory - 24)[0]
    if data[directory - 16:directory] != b"APK Sig Block 42":
        raise ValueError("No APK signing block")
    cursor, end = directory - size, directory - 24
    while cursor < end:
        length = struct.unpack_from("<Q", data, cursor)[0]
        identifier = struct.unpack_from("<I", data, cursor + 8)[0]
        value = data[cursor + 12:cursor + 8 + length]
        if identifier == 0x7109871A:
            def part(buffer, offset):
                count = struct.unpack_from("<I", buffer, offset)[0]
                return buffer[offset + 4:offset + 4 + count], offset + 4 + count
            signers, _ = part(value, 0)
            signer, _ = part(signers, 0)
            signed_data, _ = part(signer, 0)
            _, offset = part(signed_data, 0)  # digests
            certificates, _ = part(signed_data, offset)
            certificate, _ = part(certificates, 0)
            return hashlib.sha256(certificate).hexdigest()
        cursor += length + 8
    raise ValueError("No v2 signer")


if __name__ == "__main__":
    before, after = map(certificate_digest, sys.argv[1:3])
    if before != after:
        raise SystemExit("APK signing certificate changed")
    print("APK signer certificate unchanged (SHA-256: " + after + ")")
