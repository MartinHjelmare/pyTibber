"""Tibber RT connection."""

import asyncio
import logging
from collections.abc import AsyncGenerator
from ssl import SSLContext
from typing import Any

import backoff
from gql import Client
from gql.transport.exceptions import TransportClosed, TransportError
from gql.transport.websockets import WebsocketsTransport
from graphql import DocumentNode
from websockets.exceptions import ConnectionClosed

from .exceptions import SubscriptionEndpointMissingError, WebsocketReconnectedError, WebsocketTransportError

DEFAULT_SUBSCRIPTION_ENDPOINT = "wss://default_subscription_endpoint"
KEEP_ALIVE_TIMEOUT = 90
LOCK_CONNECT = asyncio.Lock()
MAX_RECONNECT_INTERVAL = 60
PING_INTERVAL = 30

_LOGGER = logging.getLogger(__name__)


class TibberWebsocket:
    """Class to handle real time connection with the Tibber api."""

    def __init__(self, access_token: str, timeout: int, user_agent: str, ssl: SSLContext | bool) -> None:
        """Initialize the Tibber connection.

        :param access_token: The access token to access the Tibber API with.
        :param timeout: The timeout in seconds to use when communicating with the Tibber API.
        :param user_agent: User agent identifier for the platform running this. Required if websession is None.
        """
        self._access_token: str = access_token
        self._timeout: int = timeout
        self._user_agent: str = user_agent
        self._ssl_context = ssl

        self._client = Client(
            transport=TibberWebsocketsTransport(
                DEFAULT_SUBSCRIPTION_ENDPOINT,  # the url will be updated later
                self._access_token,
                self._user_agent,
                ssl=ssl,
            ),
        )
        self.connected = False
        self.reconnecting_task_in_progress = False

    async def disconnect(self) -> None:
        """Stop subscription manager.
        This method simply calls the stop method of the SubscriptionManager if it is defined.
        """
        _LOGGER.debug("Stopping subscription manager")
        await self._client.close_async()
        self.reconnecting_task_in_progress = False

    async def connect(self) -> None:
        """Start subscription manager."""
        if (
            isinstance(self._client.transport, TibberWebsocketsTransport)
            and self._client.transport.url == DEFAULT_SUBSCRIPTION_ENDPOINT
        ):
            raise SubscriptionEndpointMissingError("Subscription endpoint not initialized")

        async with LOCK_CONNECT:
            if self.connected or self.reconnecting_task_in_progress:
                return

            try:
                await asyncio.wait_for(
                    self._client.connect_async(
                        reconnecting=True,
                        retry_connect=backoff.on_exception(
                            backoff.expo,
                            Exception,
                            logger=_LOGGER,
                            max_value=MAX_RECONNECT_INTERVAL,
                        ),
                    ),
                    timeout=self._timeout,
                )
            except TimeoutError as err:
                _LOGGER.debug("Timeout connecting to websocket: %s", err)
                # The connection will be retried by the reconnecting task
            else:
                self.connected = True
            self.reconnecting_task_in_progress = True

    async def subscribe(self, document: DocumentNode) -> AsyncGenerator[dict[str, Any], None]:
        """Subscribe to a GraphQL query."""
        if not self.reconnecting_task_in_progress:
            raise RuntimeError("Connect must be called before subscribe")

        try:
            async for result in self._client.session.subscribe(document):
                yield result
        except (ConnectionClosed, TransportError) as err:
            _LOGGER.debug("%s: %s", err.__class__.__name__, err)
            self.connected = False
            if isinstance(transport := self._client.transport, TibberWebsocketsTransport):
                transport.tibber_connected.clear()
                if self.reconnecting_task_in_progress and isinstance(err, TransportClosed):
                    _LOGGER.debug("Waiting for reconnect")
                    await transport.tibber_connected.wait()
                    self.connected = True
                    _LOGGER.debug("Reconnected")
                    raise WebsocketReconnectedError from err
            raise WebsocketTransportError from err

    def set_subscription_endpoint(self, url: str) -> None:
        """Set subscription endpoint."""
        if isinstance(transport := self._client.transport, TibberWebsocketsTransport):
            _LOGGER.debug("Using websocket subscription url %s", url)
            transport.url = url


class TibberWebsocketsTransport(WebsocketsTransport):
    """Tibber websockets transport."""

    def __init__(self, url: str, access_token: str, user_agent: str, ssl: SSLContext | bool = True) -> None:
        """Initialize TibberWebsocketsTransport."""
        super().__init__(
            url=url,
            init_payload={"token": access_token},
            headers={"User-Agent": user_agent},
            ssl=ssl,
            keep_alive_timeout=KEEP_ALIVE_TIMEOUT,
            ping_interval=PING_INTERVAL,
        )
        self.tibber_connected = asyncio.Event()
        self._user_agent: str = user_agent

    async def connect(self) -> None:
        """Connect to the websocket."""
        await super().connect()
        self.tibber_connected.set()

    async def close(self) -> None:
        """Close the websocket connection.

        This method is only called by the client.
        """
        await self._fail(TransportClosed(f"Tibber websocket closed by {self._user_agent}"))
        await self.wait_closed()

    async def _close_hook(self) -> None:
        """Hook called by WebsocketsTransportBase on connection close.

        This method is called when the connection is closed
        for any reason (not only by the client).
        """
        self.tibber_connected.clear()
        await super()._close_hook()
