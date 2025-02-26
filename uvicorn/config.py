import asyncio
import inspect
import json
import logging
import logging.config
import os
import socket
import ssl
import sys
from typing import Callable, Dict, List, Optional, Tuple, Type, Union

from uvicorn.logging import TRACE_LOG_LEVEL

if sys.version_info < (3, 8):
    from typing_extensions import Literal
else:
    from typing import Literal

import click
from asgiref.typing import ASGIApplication

try:
    import yaml
except ImportError:
    # If the code below that depends on yaml is exercised, it will raise a NameError.
    # Install the PyYAML package or the uvicorn[standard] optional dependencies to
    # enable this functionality.
    pass

from uvicorn.importer import ImportFromStringError, import_from_string
from uvicorn.middleware.asgi2 import ASGI2Middleware
from uvicorn.middleware.debug import DebugMiddleware
from uvicorn.middleware.message_logger import MessageLoggerMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
from uvicorn.middleware.wsgi import WSGIMiddleware

HTTPProtocolType = Literal["auto", "h11", "httptools"]
WSProtocolType = Literal["auto", "none", "websockets", "wsproto"]
LifespanType = Literal["auto", "on", "off"]
LoopSetupType = Literal["none", "auto", "asyncio", "uvloop"]
InterfaceType = Literal["auto", "asgi3", "asgi2", "wsgi"]

LOG_LEVELS: Dict[str, int] = {
    "critical": logging.CRITICAL,
    "error": logging.ERROR,
    "warning": logging.WARNING,
    "info": logging.INFO,
    "debug": logging.DEBUG,
    "trace": TRACE_LOG_LEVEL,
}
HTTP_PROTOCOLS: Dict[HTTPProtocolType, str] = {
    "auto": "uvicorn.protocols.http.auto:AutoHTTPProtocol",
    "h11": "uvicorn.protocols.http.h11_impl:H11Protocol",
    "httptools": "uvicorn.protocols.http.httptools_impl:HttpToolsProtocol",
}
WS_PROTOCOLS: Dict[WSProtocolType, Optional[str]] = {
    "auto": "uvicorn.protocols.websockets.auto:AutoWebSocketsProtocol",
    "none": None,
    "websockets": "uvicorn.protocols.websockets.websockets_impl:WebSocketProtocol",
    "wsproto": "uvicorn.protocols.websockets.wsproto_impl:WSProtocol",
}
LIFESPAN: Dict[LifespanType, str] = {
    "auto": "uvicorn.lifespan.on:LifespanOn",
    "on": "uvicorn.lifespan.on:LifespanOn",
    "off": "uvicorn.lifespan.off:LifespanOff",
}
LOOP_SETUPS: Dict[LoopSetupType, Optional[str]] = {
    "none": None,
    "auto": "uvicorn.loops.auto:auto_loop_setup",
    "asyncio": "uvicorn.loops.asyncio:asyncio_setup",
    "uvloop": "uvicorn.loops.uvloop:uvloop_setup",
}
INTERFACES: List[InterfaceType] = ["auto", "asgi3", "asgi2", "wsgi"]


# Fallback to 'ssl.PROTOCOL_SSLv23' in order to support Python < 3.5.3.
SSL_PROTOCOL_VERSION: int = getattr(ssl, "PROTOCOL_TLS", ssl.PROTOCOL_SSLv23)


LOGGING_CONFIG: dict = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {
            "()": "uvicorn.logging.DefaultFormatter",
            "fmt": "%(levelprefix)s %(message)s",
            "use_colors": None,
        },
        "access": {
            "()": "uvicorn.logging.AccessFormatter",
            "fmt": '%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s',  # noqa: E501
        },
    },
    "handlers": {
        "default": {
            "formatter": "default",
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stderr",
        },
        "access": {
            "formatter": "access",
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
        },
    },
    "loggers": {
        "uvicorn": {"handlers": ["default"], "level": "INFO"},
        "uvicorn.error": {"level": "INFO"},
        "uvicorn.access": {"handlers": ["access"], "level": "INFO", "propagate": False},
    },
}

logger = logging.getLogger("uvicorn.error")


def create_ssl_context(
    certfile: Union[str, os.PathLike],
    keyfile: Optional[Union[str, os.PathLike]],
    password: Optional[str],
    ssl_version: int,
    cert_reqs: int,
    ca_certs: Optional[Union[str, os.PathLike]],
    ciphers: Optional[str],
) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl_version)
    get_password = (lambda: password) if password else None
    ctx.load_cert_chain(certfile, keyfile, get_password)
    ctx.verify_mode = cert_reqs
    if ca_certs:
        ctx.load_verify_locations(ca_certs)
    if ciphers:
        ctx.set_ciphers(ciphers)
    return ctx


class Config:
    def __init__(
        self,
        app: Union[ASGIApplication, Callable, str],
        host: str = "127.0.0.1",
        port: int = 8000,
        uds: Optional[str] = None,
        fd: Optional[int] = None,
        loop: LoopSetupType = "auto",
        http: Union[Type[asyncio.Protocol], HTTPProtocolType] = "auto",
        ws: Union[Type[asyncio.Protocol], WSProtocolType] = "auto",
        ws_max_size: int = 16 * 1024 * 1024,
        ws_ping_interval: int = 20,
        ws_ping_timeout: int = 20,
        lifespan: LifespanType = "auto",
        env_file: Optional[Union[str, os.PathLike]] = None,
        log_config: Optional[Union[dict, str]] = LOGGING_CONFIG,
        log_level: Optional[Union[str, int]] = None,
        access_log: bool = True,
        access_log_format: Optional[str] = None,
        use_colors: Optional[bool] = None,
        interface: InterfaceType = "auto",
        debug: bool = False,
        reload: bool = False,
        reload_dirs: Optional[Union[List[str], str]] = None,
        reload_delay: Optional[float] = None,
        workers: Optional[int] = None,
        proxy_headers: bool = True,
        server_header: bool = True,
        date_header: bool = True,
        forwarded_allow_ips: Optional[str] = None,
        root_path: str = "",
        limit_concurrency: Optional[int] = None,
        limit_max_requests: Optional[int] = None,
        backlog: int = 2048,
        timeout_keep_alive: int = 5,
        timeout_notify: int = 30,
        callback_notify: Callable[..., None] = None,
        ssl_keyfile: Optional[str] = None,
        ssl_certfile: Optional[Union[str, os.PathLike]] = None,
        ssl_keyfile_password: Optional[str] = None,
        ssl_version: int = SSL_PROTOCOL_VERSION,
        ssl_cert_reqs: int = ssl.CERT_NONE,
        ssl_ca_certs: Optional[str] = None,
        ssl_ciphers: str = "TLSv1",
        headers: Optional[List[List[str]]] = None,
        factory: bool = False,
    ):
        self.app = app
        self.host = host
        self.port = port
        self.uds = uds
        self.fd = fd
        self.loop = loop
        self.http = http
        self.ws = ws
        self.ws_max_size = ws_max_size
        self.ws_ping_interval = ws_ping_interval
        self.ws_ping_timeout = ws_ping_timeout
        self.lifespan = lifespan
        self.log_config = log_config
        self.log_level = log_level
        self.access_log = access_log
        self.use_colors = use_colors
        self.interface = interface
        self.debug = debug
        self.reload = reload
        self.reload_delay = reload_delay or 0.25
        self.workers = workers or 1
        self.proxy_headers = proxy_headers
        self.server_header = server_header
        self.date_header = date_header
        self.root_path = root_path
        self.limit_concurrency = limit_concurrency
        self.limit_max_requests = limit_max_requests
        self.backlog = backlog
        self.timeout_keep_alive = timeout_keep_alive
        self.timeout_notify = timeout_notify
        self.callback_notify = callback_notify
        self.ssl_keyfile = ssl_keyfile
        self.ssl_certfile = ssl_certfile
        self.ssl_keyfile_password = ssl_keyfile_password
        self.ssl_version = ssl_version
        self.ssl_cert_reqs = ssl_cert_reqs
        self.ssl_ca_certs = ssl_ca_certs
        self.ssl_ciphers = ssl_ciphers
        self.headers: List[List[str]] = headers or []
        self.encoded_headers: Optional[List[Tuple[bytes, bytes]]] = None
        self.factory = factory

        self.access_log_format = access_log_format

        self.loaded = False
        self.configure_logging()

        if reload_dirs is None:
            self.reload_dirs = [os.getcwd()]
        else:
            if isinstance(reload_dirs, str):
                self.reload_dirs = [reload_dirs]
            else:
                self.reload_dirs = reload_dirs

        if env_file is not None:
            from dotenv import load_dotenv

            logger.info("Loading environment from '%s'", env_file)
            load_dotenv(dotenv_path=env_file)

        if workers is None and "WEB_CONCURRENCY" in os.environ:
            self.workers = int(os.environ["WEB_CONCURRENCY"])

        if forwarded_allow_ips is None:
            self.forwarded_allow_ips = os.environ.get(
                "FORWARDED_ALLOW_IPS", "127.0.0.1"
            )
        else:
            self.forwarded_allow_ips = forwarded_allow_ips

    @property
    def asgi_version(self) -> Literal["2.0", "3.0"]:
        mapping: Dict[str, Literal["2.0", "3.0"]] = {
            "asgi2": "2.0",
            "asgi3": "3.0",
            "wsgi": "3.0",
        }
        return mapping[self.interface]

    @property
    def is_ssl(self) -> bool:
        return bool(self.ssl_keyfile or self.ssl_certfile)

    def configure_logging(self) -> None:
        logging.addLevelName(TRACE_LOG_LEVEL, "TRACE")

        if self.log_config is not None:
            if isinstance(self.log_config, dict):
                if self.use_colors in (True, False):
                    self.log_config["formatters"]["default"][
                        "use_colors"
                    ] = self.use_colors
                    self.log_config["formatters"]["access"][
                        "use_colors"
                    ] = self.use_colors
                if self.access_log_format:
                    self.log_config["formatters"]["access"][
                        "fmt"
                    ] = "%(levelprefix)s %(message)s"
                logging.config.dictConfig(self.log_config)
            elif self.log_config.endswith(".json"):
                with open(self.log_config) as file:
                    loaded_config = json.load(file)
                    logging.config.dictConfig(loaded_config)
            elif self.log_config.endswith((".yaml", ".yml")):
                with open(self.log_config) as file:
                    loaded_config = yaml.safe_load(file)
                    logging.config.dictConfig(loaded_config)
            else:
                # See the note about fileConfig() here:
                # https://docs.python.org/3/library/logging.config.html#configuration-file-format
                logging.config.fileConfig(
                    self.log_config, disable_existing_loggers=False
                )

        if self.log_level is not None:
            if isinstance(self.log_level, str):
                log_level = LOG_LEVELS[self.log_level]
            else:
                log_level = self.log_level
            logging.getLogger("uvicorn.error").setLevel(log_level)
            logging.getLogger("uvicorn.access").setLevel(log_level)
            logging.getLogger("uvicorn.asgi").setLevel(log_level)
        if self.access_log is False:
            logging.getLogger("uvicorn.access").handlers = []
            logging.getLogger("uvicorn.access").propagate = False

    def load(self) -> None:
        assert not self.loaded

        if self.is_ssl:
            assert self.ssl_certfile
            self.ssl: Optional[ssl.SSLContext] = create_ssl_context(
                keyfile=self.ssl_keyfile,
                certfile=self.ssl_certfile,
                password=self.ssl_keyfile_password,
                ssl_version=self.ssl_version,
                cert_reqs=self.ssl_cert_reqs,
                ca_certs=self.ssl_ca_certs,
                ciphers=self.ssl_ciphers,
            )
        else:
            self.ssl = None

        encoded_headers = [
            (key.lower().encode("latin1"), value.encode("latin1"))
            for key, value in self.headers
        ]
        self.encoded_headers = (
            [(b"server", b"uvicorn")] + encoded_headers
            if b"server" not in dict(encoded_headers) and self.server_header
            else encoded_headers
        )

        if isinstance(self.http, str):
            http_protocol_class = import_from_string(HTTP_PROTOCOLS[self.http])
            self.http_protocol_class: Type[asyncio.Protocol] = http_protocol_class
        else:
            self.http_protocol_class = self.http

        if isinstance(self.ws, str):
            ws_protocol_class = import_from_string(WS_PROTOCOLS[self.ws])
            self.ws_protocol_class: Optional[Type[asyncio.Protocol]] = ws_protocol_class
        else:
            self.ws_protocol_class = self.ws

        self.lifespan_class = import_from_string(LIFESPAN[self.lifespan])

        try:
            self.loaded_app = import_from_string(self.app)
        except ImportFromStringError as exc:
            logger.error("Error loading ASGI app. %s" % exc)
            sys.exit(1)

        try:
            self.loaded_app = self.loaded_app()
        except TypeError as exc:
            if self.factory:
                logger.error("Error loading ASGI app factory: %s", exc)
                sys.exit(1)
        else:
            if not self.factory:
                logger.warning(
                    "ASGI app factory detected. Using it, "
                    "but please consider setting the --factory flag explicitly."
                )

        if self.interface == "auto":
            if inspect.isclass(self.loaded_app):
                use_asgi_3 = hasattr(self.loaded_app, "__await__")
            elif inspect.isfunction(self.loaded_app):
                use_asgi_3 = asyncio.iscoroutinefunction(self.loaded_app)
            else:
                call = getattr(self.loaded_app, "__call__", None)
                use_asgi_3 = asyncio.iscoroutinefunction(call)
            self.interface = "asgi3" if use_asgi_3 else "asgi2"

        if self.interface == "wsgi":
            self.loaded_app = WSGIMiddleware(self.loaded_app)
            self.ws_protocol_class = None
        elif self.interface == "asgi2":
            self.loaded_app = ASGI2Middleware(self.loaded_app)

        if self.debug:
            self.loaded_app = DebugMiddleware(self.loaded_app)
        if logger.level <= TRACE_LOG_LEVEL:
            self.loaded_app = MessageLoggerMiddleware(self.loaded_app)
        if self.proxy_headers:
            self.loaded_app = ProxyHeadersMiddleware(
                self.loaded_app, trusted_hosts=self.forwarded_allow_ips
            )

        self.loaded = True

    def setup_event_loop(self) -> None:
        loop_setup: Optional[Callable] = import_from_string(LOOP_SETUPS[self.loop])
        if loop_setup is not None:
            loop_setup()

    def bind_socket(self) -> socket.socket:
        family = socket.AF_INET
        addr_format = "%s://%s:%d"

        if self.host and ":" in self.host:
            # It's an IPv6 address.
            family = socket.AF_INET6
            addr_format = "%s://[%s]:%d"

        sock = socket.socket(family=family)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((self.host, self.port))
        except OSError as exc:
            logger.error(exc)
            sys.exit(1)
        sock.set_inheritable(True)

        message = f"Uvicorn running on {addr_format} (Press CTRL+C to quit)"
        color_message = (
            "Uvicorn running on "
            + click.style(addr_format, bold=True)
            + " (Press CTRL+C to quit)"
        )
        protocol_name = "https" if self.is_ssl else "http"
        logger.info(
            message,
            protocol_name,
            self.host,
            self.port,
            extra={"color_message": color_message},
        )
        return sock

    @property
    def should_reload(self) -> bool:
        return isinstance(self.app, str) and (self.debug or self.reload)
