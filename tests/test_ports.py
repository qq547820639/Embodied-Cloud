from app.services.ports import allocate_tcp_port, allocate_udp_port


def test_port_allocators_return_in_range():
    tcp = allocate_tcp_port(31000, 31100)
    udp = allocate_udp_port(31200, 31300)
    assert 31000 <= tcp <= 31100
    assert 31200 <= udp <= 31300
