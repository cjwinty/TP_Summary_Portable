import hashlib
import hmac
import datetime
from urllib.parse import urlparse, quote, unquote


def _sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def _hex_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac_sha256(key: bytes, msg: bytes) -> bytes:
    return hmac.new(key, msg, hashlib.sha256).digest()


def _sign(key: bytes, msg: bytes) -> bytes:
    return _hmac_sha256(key, msg)


def _get_signature_key(key: str, date_stamp: str, region: str, service: str) -> bytes:
    k_date = _sign(("AWS4" + key).encode("utf-8"), date_stamp.encode("utf-8"))
    k_region = _sign(k_date, region.encode("utf-8"))
    k_service = _sign(k_region, service.encode("utf-8"))
    k_signing = _sign(k_service, b"aws4_request")
    return k_signing


def _uri_encode(path: str, raw: bool = True) -> str:
    safe = "/~" if raw else "/"
    return quote(unquote(path), safe=safe)


def sign_aws_request(
    method: str,
    url: str,
    headers: dict,
    body: bytes,
    access_key_id: str,
    secret_key: str,
    region: str,
    service: str,
) -> dict:
    parsed = urlparse(url)
    amz_date = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    date_stamp = amz_date[:8]

    canonical_uri = _uri_encode(parsed.path) if parsed.path else "/"
    canonical_querystring = parsed.query

    payload_hash = _hex_sha256(body)

    required_headers = {"host": parsed.hostname, "x-amz-date": amz_date, "x-amz-content-sha256": payload_hash}
    all_headers = {k.lower(): v.strip() for k, v in {**required_headers, **headers}.items()}
    signed_headers = ";".join(sorted(all_headers.keys()))
    canonical_headers = "".join(f"{k}:{all_headers[k]}\n" for k in sorted(all_headers.keys()))

    canonical_request = (
        f"{method}\n"
        f"{canonical_uri}\n"
        f"{canonical_querystring}\n"
        f"{canonical_headers}\n"
        f"{signed_headers}\n"
        f"{payload_hash}"
    )

    algorithm = "AWS4-HMAC-SHA256"
    credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = (
        f"{algorithm}\n"
        f"{amz_date}\n"
        f"{credential_scope}\n"
        f"{_hex_sha256(canonical_request.encode('utf-8'))}"
    )

    signing_key = _get_signature_key(secret_key, date_stamp, region, service)
    signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    authorization_header = (
        f"{algorithm} "
        f"Credential={access_key_id}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, "
        f"Signature={signature}"
    )

    result = dict(headers)
    result["x-amz-date"] = amz_date
    result["Authorization"] = authorization_header
    return result
