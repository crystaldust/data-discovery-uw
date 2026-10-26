import json
import threading

import httpx
import pytest

from ddm.eval.label_server import MAX_BODY_BYTES, make_server
from ddm.eval.labeling import load_pairs, save_pairs, with_label
from tests.eval.test_labeling import rows, write_csv

TOKEN = "t" * 32
# The machine may have proxy settings; the page is local, so never go through a proxy.
HTTP = httpx.Client(trust_env=False)


@pytest.fixture
def server(tmp_path):
    path = write_csv(tmp_path / "labels.csv", rows())
    srv = make_server(path, port=0, token=TOKEN)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv, path
    srv.shutdown()
    srv.server_close()


def base(srv):
    return f"http://127.0.0.1:{srv.server_address[1]}"


def post(srv, body, *, token=TOKEN, content_type="application/json", host=None):
    headers = {"X-Label-Token": token, "Content-Type": content_type}
    if host:
        headers["Host"] = host
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    return HTTP.post(f"{base(srv)}/api/label", content=data, headers=headers)


def test_the_page_needs_the_token_in_its_address(server):
    srv, _ = server

    assert HTTP.get(f"{base(srv)}/").status_code == 403
    assert HTTP.get(f"{base(srv)}/?token=wrong").status_code == 403
    page = HTTP.get(f"{base(srv)}/?token={TOKEN}")
    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]


def test_the_page_carries_a_strict_content_security_policy(server):
    srv, _ = server

    page = HTTP.get(f"{base(srv)}/?token={TOKEN}")

    policy = page.headers["content-security-policy"]
    assert "default-src 'none'" in policy
    assert "frame-ancestors 'none'" in policy
    assert page.headers["x-content-type-options"] == "nosniff"


def test_pairs_are_served_only_with_the_token_header(server):
    srv, _ = server

    assert HTTP.get(f"{base(srv)}/api/pairs").status_code == 403
    answer = HTTP.get(f"{base(srv)}/api/pairs", headers={"X-Label-Token": TOKEN})
    assert answer.status_code == 200
    assert [r["pair_id"] for r in answer.json()["rows"]] == ["p001", "p002"]
    assert answer.headers["cache-control"] == "no-store"


def test_a_label_is_written_back_to_the_file(server):
    srv, path = server

    answer = post(srv, {"pair_id": "p001", "label": "different", "note": "other host"})

    assert answer.status_code == 200
    assert answer.json()["row"]["label"] == "different"
    saved = load_pairs(path).rows[0]
    assert (saved["label"], saved["note"]) == ("different", "other host")


def test_labels_from_another_tool_on_other_rows_survive_a_save(server):
    srv, path = server
    save_pairs(path, with_label(load_pairs(path), "p002", "different", ""))  # another tool

    post(srv, {"pair_id": "p001", "label": "same", "note": ""})

    assert [r["label"] for r in load_pairs(path).rows] == ["same", "different"]


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"pair_id": "p001", "label": "yes", "note": ""}, 400),
        ({"pair_id": "p999", "label": "same", "note": ""}, 400),
        ({"pair_id": "p001"}, 400),
        (b"not json", 400),
        (b"x" * (MAX_BODY_BYTES + 1), 413),
    ],
)
def test_bad_label_requests_are_refused_and_change_nothing(server, body, status):
    srv, path = server
    before = path.read_bytes()

    assert post(srv, body).status_code == status
    assert path.read_bytes() == before


def test_writes_need_the_token_json_and_a_local_host_name(server):
    srv, path = server
    before = path.read_bytes()
    body = {"pair_id": "p001", "label": "same", "note": ""}

    assert post(srv, body, token="wrong").status_code == 403
    assert post(srv, body, content_type="text/plain").status_code == 415
    assert post(srv, body, host="evil.example").status_code == 403
    assert path.read_bytes() == before


def test_unknown_paths_are_not_found(server):
    srv, _ = server

    answer = HTTP.get(f"{base(srv)}/etc/passwd", headers={"X-Label-Token": TOKEN})

    assert answer.status_code == 404


def test_the_server_only_listens_on_the_loopback_address(server):
    srv, _ = server

    assert srv.server_address[0] == "127.0.0.1"


def raw_post(srv, headers: str, body: bytes = b"") -> bytes:
    import socket

    with socket.create_connection(srv.server_address, timeout=5) as sock:
        host = f"127.0.0.1:{srv.server_address[1]}"
        head = (
            f"POST /api/label HTTP/1.1\r\nHost: {host}\r\nX-Label-Token: {TOKEN}\r\n"
            f"Content-Type: application/json\r\n{headers}\r\n"
        )
        sock.sendall(head.encode() + body)
        return sock.recv(200)


@pytest.mark.parametrize("length", ["-1", "abc", ""])
def test_a_bad_content_length_is_refused(server, length):
    srv, path = server
    before = path.read_bytes()

    answer = raw_post(srv, f"Content-Length: {length}\r\n", b'{"pair_id":"p001"}')

    assert answer.startswith(b"HTTP/1.0 400") or answer.startswith(b"HTTP/1.1 400")
    assert path.read_bytes() == before


def test_deeply_nested_json_is_a_bad_request(server):
    srv, _ = server

    assert post(srv, b"[" * 3000 + b"]" * 3000).status_code in (400, 413)


def test_handlers_do_not_wait_forever_for_a_body():
    from ddm.eval.label_server import LabelHandler

    assert 0 < LabelHandler.timeout <= 30
