import base64
import gzip
import json
from urllib.parse import parse_qs, quote

import httpx

from app.providers.aliyundrive.qr_login import AliyunDriveQrLogin


async def test_qr_login_generates_local_svg_and_extracts_private_token() -> None:
    query_count = 0
    login_result = base64.b64encode(
        json.dumps({"pds_login_result": {"refreshToken": "private-refresh-token"}}).encode()
    ).decode()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal query_count
        assert request.url.params["appName"] == "aliyun_drive"
        if request.url.path.endswith("/generate.do"):
            assert request.method == "GET"
            return httpx.Response(
                200,
                json={
                    "content": {
                        "data": {
                            "t": 123456,
                            "ck": "login-cookie",
                            "codeContent": "https://example.test/scan/123",
                            "resultCode": 100,
                        }
                    }
                },
            )
        query_count += 1
        assert request.method == "POST"
        form = parse_qs(request.content.decode())
        assert form["t"] == ["123456"]
        assert form["ck"] == ["login-cookie"]
        assert form["fromSite"] == ["52"]
        assert form["appEntrance"] == ["web"]
        if query_count == 1:
            return httpx.Response(
                200,
                json={"content": {"data": {"qrCodeStatus": "SCANED"}}},
            )
        return httpx.Response(
            200,
            json={
                "content": {
                    "data": {
                        "qrCodeStatus": "CONFIRMED",
                        "bizExt": login_result,
                    }
                }
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    qr_login = AliyunDriveQrLogin(
        base_url="https://passport.alipan.test",
        http_client=client,
    )
    try:
        started = await qr_login.start(account_name="家庭影音盘")
        scanned, _ = await qr_login.poll(started.session_id)
        confirmed, session = await qr_login.poll(started.session_id)
    finally:
        await client.aclose()

    assert started.qr_code_data_url.startswith("data:image/svg+xml;base64,")
    assert scanned == "scanned"
    assert confirmed == "confirmed"
    assert session.account_name == "家庭影音盘"
    assert session.refresh_token == "private-refresh-token"


def test_qr_login_decodes_url_encoded_nested_login_result() -> None:
    nested = {"pds_login_result": json.dumps({"refreshToken": "nested-private-token"})}
    encoded = base64.urlsafe_b64encode(json.dumps(nested).encode()).decode()

    token = AliyunDriveQrLogin._decode_token({"bizExt": quote(encoded)})

    assert token == "nested-private-token"


def test_qr_login_decodes_double_encoded_and_compressed_login_result() -> None:
    payload = json.dumps(
        {"pds_login_result": {"refresh_token": "compressed-private-token"}}
    ).encode()
    compressed = base64.b64encode(gzip.compress(payload)).decode()
    double_encoded = base64.urlsafe_b64encode(json.dumps(compressed).encode()).decode()

    token = AliyunDriveQrLogin._decode_token({"bizExt": double_encoded})

    assert token == "compressed-private-token"
