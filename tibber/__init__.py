"""Library to handle connection with Tibber API."""

import asyncio
import datetime as dt
import logging
import zoneinfo
from ssl import SSLContext

import aiohttp

from .const import DEFAULT_TIMEOUT, DEMO_TOKEN, __version__
from .exceptions import (
    UserAgentMissingError,
)
from .gql_queries import INFO, PUSH_NOTIFICATION
from .home import TibberHome
from .rest import TibberREST
from .websocket import TibberWebsocket

_LOGGER = logging.getLogger(__name__)


class Tibber:
    """Class to communicate with the Tibber api."""

    def __init__(
        self,
        access_token: str = DEMO_TOKEN,
        timeout: int = DEFAULT_TIMEOUT,
        websession: aiohttp.ClientSession | None = None,
        time_zone: dt.tzinfo | None = None,
        user_agent: str | None = None,
        ssl: SSLContext | bool = True,
    ) -> None:
        """Initialize the Tibber connection.

        :param access_token: The access token to access the Tibber API with.
        :param timeout: The timeout in seconds to use when communicating with the Tibber API.
        :param websession: The websession to use when communicating with the Tibber API.
        :param time_zone: The time zone to display times in and to use.
        :param user_agent: User agent identifier for the platform running this. Required if websession is None.
        :param ssl: SSLContext to use.
        """
        if websession is None:
            websession = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl))
        elif user_agent is None:
            user_agent = websession.headers.get(aiohttp.hdrs.USER_AGENT)
        if user_agent is None:
            raise UserAgentMissingError("Please provide value for HTTP user agent")
        self._user_agent: str = f"{user_agent} pyTibber/{__version__}"
        self.rest = TibberREST(access_token, timeout, self._user_agent, websession)
        self.ws = TibberWebsocket(
            access_token,
            timeout,
            self._user_agent,
            ssl=ssl,
        )

        self.time_zone: dt.tzinfo = time_zone or zoneinfo.ZoneInfo("UTC")
        self._name: str = ""
        self._user_id: str | None = None
        self._active_home_ids: list[str] = []
        self._all_home_ids: list[str] = []
        self._homes: dict[str, TibberHome] = {}

    async def update_info(self) -> None:
        """Updates home info asynchronously."""
        if (data := await self.rest.execute(INFO)) is None:
            return

        if not (viewer := data.get("viewer")):
            return

        if sub_endpoint := viewer.get("websocketSubscriptionUrl"):
            self.ws.set_subscription_endpoint(sub_endpoint)

        self._name = viewer.get("name")
        self._user_id = viewer.get("userId")

        self._active_home_ids = []
        for _home in viewer.get("homes", []):
            if not (home_id := _home.get("id")):
                continue
            self._all_home_ids += [home_id]
            if not (subs := _home.get("subscriptions")):
                continue
            if subs[0].get("status") is not None and subs[0]["status"].lower() == "running":
                self._active_home_ids += [home_id]

    def get_home_ids(self, only_active: bool = True) -> list[str]:
        """Return list of home ids."""
        if only_active:
            return self._active_home_ids
        return self._all_home_ids

    def get_homes(self, only_active: bool = True) -> list[TibberHome]:
        """Return list of Tibber homes."""
        return [home for home_id in self.get_home_ids(only_active) if (home := self.get_home(home_id))]

    def get_home(self, home_id: str) -> TibberHome | None:
        """Return an instance of TibberHome for given home id."""
        if home_id not in self._all_home_ids:
            return None
        if home_id not in self._homes:
            self._homes[home_id] = TibberHome(home_id, self)
        return self._homes[home_id]

    async def send_notification(self, title: str, message: str) -> bool:
        """Sends a push notification to the Tibber app on registered devices.

        :param title: The title of the push notification.
        :param message: The message of the push notification.
        """
        if not (
            res := await self.rest.execute(
                PUSH_NOTIFICATION.format(
                    title,
                    message,
                ),
            )
        ):
            return False
        notification = res.get("sendPushNotification", {})
        successful = notification.get("successful", False)
        pushed_to_number_of_devices = notification.get("pushedToNumberOfDevices", 0)
        _LOGGER.debug(
            "send_notification: status %s, send to %s devices",
            successful,
            pushed_to_number_of_devices,
        )
        return successful

    async def fetch_consumption_data_active_homes(self) -> None:
        """Fetch consumption data for active homes."""
        await asyncio.gather(
            *[tibber_home.fetch_consumption_data() for tibber_home in self.get_homes(only_active=True)],
        )

    async def fetch_production_data_active_homes(self) -> None:
        """Fetch production data for active homes."""
        await asyncio.gather(
            *[
                tibber_home.fetch_production_data()
                for tibber_home in self.get_homes(only_active=True)
                if tibber_home.has_production
            ],
        )

    async def rt_disconnect(self) -> None:
        """Stop subscription manager.
        This method simply calls the stop method of the SubscriptionManager if it is defined.
        """
        await self.ws.disconnect()

    @property
    def user_id(self) -> str | None:
        """Return user id of user."""
        return self._user_id

    @property
    def name(self) -> str:
        """Return name of user."""
        return self._name

    @property
    def home_ids(self) -> list[str]:
        """Return list of home ids."""
        return self.get_home_ids(only_active=True)
