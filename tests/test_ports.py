import pytest

import app.services.ports as ports


def test_port_allocators_return_in_range():
    tcp = ports.allocate_tcp_port(31000, 31100)
    udp = ports.allocate_udp_port(31200, 31300)
    assert 31000 <= tcp <= 31100
    assert 31200 <= udp <= 31300


def test_tcp_port_allocation_deduplicated_and_released(monkeypatch):
    """§S-6：进程内已分配端口不再重复分配（消除异步 docker run 串行 TOCTOU）。"""
    # 让 OS 探测恒为 free，聚焦进程内去重逻辑
    monkeypatch.setattr(ports, "is_port_free", lambda *a, **k: True)
    port = ports.allocate_tcp_port(32000, 32000)
    assert port == 32000

    # 进程内已分配 → 即使 OS 端口仍 free 也不再返回同一端口
    with pytest.raises(RuntimeError):
        ports.allocate_tcp_port(32000, 32000)

    # 释放后（容器销毁）可再次分配
    ports.release_tcp_port(32000)
    assert ports.allocate_tcp_port(32000, 32000) == 32000

    # 清理，避免污染其它用例
    ports.release_tcp_port(32000)
