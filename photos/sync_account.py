"""Private adaptation of DaochiClient login and device registration builders."""
import struct

from . import sync_transaction


def request(mode, public, private, seed, client, challenge=b'', now=0):
    if (type(public) is not bytes or len(public) != 1312 or type(private) is not bytes or len(private) != 2560
            or type(seed) is not bytes or len(seed) != 32 or type(client) is not str
            or type(challenge) is not bytes or type(now) is not int or mode not in {1, 2}
            or not 0 <= now <= 9007199254740691):
        raise ValueError('Invalid Daochi account request')
    try:
        client_bytes = client.encode('ascii')
    except UnicodeEncodeError:
        raise ValueError('Invalid Daochi client identity') from None
    if (not 0 < len(client_bytes) <= 128 or any(c < 33 or c >= 127 or c in (34, 92) for c in client_bytes)
            or (mode == 1 and (not 0 < len(challenge) <= 4096 or now != 0))
            or (mode == 2 and (challenge or now < 1))):
        raise ValueError('Invalid Daochi account request')
    frame = (struct.pack('!BBHIQ', 2, mode, len(client_bytes), len(challenge), now)
             + public + private + seed + client_bytes + challenge)
    response = sync_transaction.run(frame)
    if len(response) < 8:
        raise ValueError('Invalid Daochi account response')
    body_count, signature_count = struct.unpack('!II', response[:8])
    if (not 0 < body_count < 6144 or signature_count != (4840 if mode == 1 else 0)
            or len(response) != 8 + body_count + signature_count):
        raise ValueError('Invalid Daochi account response')
    body, signature = response[8:8 + body_count], response[8 + body_count:]
    if b'\0' in body:
        raise ValueError('Invalid Daochi account response')
    try:
        return body, signature.decode('ascii')
    except UnicodeDecodeError:
        raise ValueError('Invalid Daochi account response') from None


def login(public, private, client, challenge):
    return request(1, public, private, b'\0' * 32, client, challenge)


def registration(public, private, seed, client, now):
    return request(2, public, private, seed, client, now=now)[0]
