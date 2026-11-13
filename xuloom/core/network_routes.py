"""Remember a working model route and retry only before request delivery."""
import importlib
import ipaddress
import json
import logging
import re

from xuloom.core.storage import IOPath, atomic_json, data_directory

logger = logging.getLogger(__name__)
ROUTES = {"direct", "system"}
MODES = ROUTES | {"auto"}


class RoutesUnavailable(ConnectionError):
    def __init__(self, failures):
        self.failures = tuple(failures)
        super().__init__("; ".join(f"{route}: {reason}" for route, reason in self.failures))


def exhausted_routes(error):
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, RoutesUnavailable):
            return error
        error = error.__cause__
    return None


def route_file(identity):
    if identity is None:
        return None
    if not re.fullmatch(r"[a-f0-9]{64}", identity):
        raise ValueError("Invalid model route identity")
    return IOPath(data_directory() / "model-routes" / (identity + ".json"))


def preferred_route(path):
    if path is not None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            route = value.get("route") if isinstance(value, dict) else None
            if isinstance(route, str) and route in ROUTES:
                return route
        except (OSError, ValueError):
            pass
    return "direct"


def _loopback(host):
    if host.lower().rstrip(".") == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def automatic_transport(library, identity):
    path = route_file(identity)
    utils = importlib.import_module(library.__name__ + "._utils")

    class Transport(library.AsyncBaseTransport):
        def __init__(self):
            self.direct = library.AsyncHTTPTransport(trust_env=False)
            self.proxies = {}

        def proxy_url(self, url):
            if _loopback(url.host):
                return None
            # Reuse the SDK's system proxy discovery and NO_PROXY matching.
            patterns = ((utils.URLPattern(key), value) for key, value in utils.get_environment_proxies().items())
            return next((value for pattern, value in sorted(patterns) if pattern.matches(url)), None)

        def remember(self, route):
            if path is not None and (route != preferred_route(path) or not path.exists()):
                try:
                    atomic_json(path, {"route": route})
                except OSError:
                    logger.warning("Could not save the last successful model route")

        async def handle_async_request(self, request):
            previous = preferred_route(path)
            proxy = self.proxy_url(request.url)
            routes = [previous, "system" if previous == "direct" else "direct"] if proxy else ["direct"]
            failures = []
            for route in routes:
                transport = self.direct
                if route == "system":
                    try:
                        if proxy not in self.proxies:
                            self.proxies[proxy] = library.AsyncHTTPTransport(proxy=proxy, trust_env=True)
                        transport = self.proxies[proxy]
                    except (ValueError, ImportError):
                        failures.append((route, "ProxyConfigurationError"))
                        continue
                # Connect failures happen before the request body is sent. Read,
                # write and protocol errors may happen after the provider acts.
                timeout = request.extensions.get("timeout", {})
                connect = timeout.get("connect")
                request.extensions["timeout"] = {**timeout, "connect": min(10, connect) if connect is not None else 10}
                try:
                    response = await transport.handle_async_request(request)
                except (library.ConnectError, library.ConnectTimeout, library.ProxyError) as error:
                    failures.append((route, type(error).__name__))
                    continue
                finally:
                    request.extensions["timeout"] = timeout
                if route == "system" and response.status_code == 407:
                    await response.aclose()
                    failures.append((route, "HTTP 407"))
                    continue
                if response.is_success:
                    self.remember(route)
                return response
            raise RoutesUnavailable(failures)

        async def aclose(self):
            await self.direct.aclose()
            for transport in self.proxies.values():
                await transport.aclose()

    return Transport()
