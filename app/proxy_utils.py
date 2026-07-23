from urllib.parse import urlparse


def normalize_proxy_line(line: str) -> str:
    line = line.strip()
    if not line:
        return line
    if "://" in line:
        return line

    parts = line.split(":")
    if len(parts) == 4:
        host, port, user, password = parts
        return f"http://{user}:{password}@{host}:{port}"
    if len(parts) == 2:
        host, port = parts
        return f"http://{host}:{port}"

    raise ValueError(
        "Unsupported proxy line format. Expected URL, host:port, or host:port:user:password."
    )


def validate_proxy_url(
    proxy_url: str,
    *,
    error_prefix: str,
    allowed_schemes: set[str],
    unsupported_message: str | None = None,
) -> str:
    proxy_url = normalize_proxy_line(proxy_url).strip()
    if not proxy_url:
        raise ValueError(f"{error_prefix} proxy URL cannot be empty.")

    parsed = urlparse(proxy_url)
    if parsed.scheme not in allowed_schemes:
        allowed = ", ".join(sorted(allowed_schemes))
        message = unsupported_message or f"Unsupported {error_prefix} proxy scheme"
        raise ValueError(f"{message} {parsed.scheme!r}. Allowed: {allowed}")
    if not parsed.hostname or not parsed.port:
        raise ValueError(f"{error_prefix} proxy URL must include host and port: {proxy_url!r}")

    return proxy_url


def load_proxy_file(file_path: str) -> list[str]:
    proxies = []
    with open(file_path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            proxies.append(normalize_proxy_line(line))
    return proxies
