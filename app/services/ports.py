"""端口分配工具。

单机可信模式（ADR 0003）：生产调用方为 operation worker，单进程内一次处理一个
operation（串行），因此不存在多线程并发分配窗口。但 `docker run` 是异步的——
容器真正 bind 端口晚于本函数返回，导致「上一容器尚未 bind → 下一次分配探测到
同一端口仍空闲」的串行 TOCTOU。为消除进程内重复分配，本模块维护进程级已分配
集合（分配时记录、`release_*` 时移除；调用方在销毁容器后释放端口）。

已知限制（TOCTOU）：跨进程/多主机共享同一 host 端口空间时（多实例控制面、
外部进程抢占），本进程内集合无法感知外部占用，仍存在端口被抢占的窗口。该场景
当前不在单机部署假设内；如需多实例，应将端口分配收敛到共享状态存储（如 DB/etcd）。
"""

import socket
import threading

_lock = threading.Lock()
_allocated_tcp: set[int] = set()
_allocated_udp: set[int] = set()


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
    with _lock:
        for port in range(start, end + 1):
            if port in reserved or port in _allocated_tcp:
                continue
            if is_port_free(port):
                _allocated_tcp.add(port)
                return port
    raise RuntimeError(f"No free TCP port in range {start}-{end}")


def release_tcp_port(port: int) -> None:
    """释放进程内已分配的 TCP 端口（调用方销毁容器/释放端口后调用）。"""
    with _lock:
        _allocated_tcp.discard(port)


def allocate_udp_port(start: int, end: int, reserved: set[int] | None = None) -> int:
    reserved = reserved or set()
    with _lock:
        for port in range(start, end + 1):
            if port in reserved or port in _allocated_udp:
                continue
            if is_port_free(port, sock_type=socket.SOCK_DGRAM):
                _allocated_udp.add(port)
                return port
    raise RuntimeError(f"No free UDP port in range {start}-{end}")


def release_udp_port(port: int) -> None:
    """释放进程内已分配的 UDP 端口。"""
    with _lock:
        _allocated_udp.discard(port)
