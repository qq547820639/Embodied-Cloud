import socket


def is_port_free(port: int, host: str = "0.0.0.0", sock_type: int = socket.SOCK_STREAM) -> bool:  # noqa: S104
    # S104: 绑定 0.0.0.0 探测是端口可用性检查的有意行为（需覆盖所有接口）。
    sock = socket.socket(socket.AF_INET, sock_type)
    try:
        sock.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def allocate_tcp_port(start: int, end: int, reserved: set[int] | None = None) -> int:
    reserved = reserved or set()
    for port in range(start, end + 1):
        if port not in reserved and is_port_free(port):
            return port
    raise RuntimeError(f"No free TCP port in range {start}-{end}")


def allocate_udp_port(start: int, end: int, reserved: set[int] | None = None) -> int:
    reserved = reserved or set()
    for port in range(start, end + 1):
        if port not in reserved and is_port_free(port, sock_type=socket.SOCK_DGRAM):
            return port
    raise RuntimeError(f"No free UDP port in range {start}-{end}")
