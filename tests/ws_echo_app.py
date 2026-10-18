# A WebSocket echo server in the standard library only, run as a hosted app by the tests.
import base64, hashlib, os, socket, threading

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
srv = socket.socket()
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("0.0.0.0", int(os.environ["PORT"])))
srv.listen(5)
print("WS-START", flush=True)


def recvn(c, n):
    b = b""
    while len(b) < n:
        d = c.recv(n - len(b))
        if not d:
            raise EOFError
        b += d
    return b


def serve(c):
    try:
        req = b""
        while b"\r\n\r\n" not in req:
            req += c.recv(1024)
        key = [l.split(b":", 1)[1].strip() for l in req.split(b"\r\n") if l.lower().startswith(b"sec-websocket-key")][0]
        acc = base64.b64encode(hashlib.sha1(key + GUID.encode()).digest())
        c.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: " + acc + b"\r\n\r\n")
        while True:
            h = recvn(c, 2)
            op, ln = h[0] & 15, h[1] & 127
            if op == 8:
                c.sendall(b"\x88\x00")
                return
            mask = recvn(c, 4) if h[1] & 128 else b"\0\0\0\0"
            data = bytes(x ^ mask[i % 4] for i, x in enumerate(recvn(c, ln)))
            c.sendall(bytes([0x80 | op, len(data)]) + data)
    except Exception:
        pass
    finally:
        c.close()


while True:
    conn, _ = srv.accept()
    threading.Thread(target=serve, args=(conn,), daemon=True).start()
