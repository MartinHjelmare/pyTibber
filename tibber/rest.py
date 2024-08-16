"""Provide a class for handling the Tibber REST API."""

import logging
from http import HTTPStatus
from typing import Any

import aiohttp

from .const import API_ENDPOINT
from .exceptions import (
    FatalHttpExceptionError,
    InvalidLoginError,
    RetryableHttpExceptionError,
)
from .response_handler import extract_response_data

_LOGGER = logging.getLogger(__name__)


class TibberREST:
    """Represent the Tibber REST API."""

    def __init__(self, access_token: str, timeout: int, user_agent: str, websession: aiohttp.ClientSession) -> None:
        """Initialize the Tibber connection.

        :param access_token: The access token to access the Tibber API with.
        :param timeout: The timeout in seconds to use when communicating with the Tibber API.
        :param user_agent: User agent identifier for the platform running this. Required if websession is None.
        """
        self._access_token: str = access_token
        self._timeout: int = timeout
        self._user_agent: str = user_agent
        self._websession = websession

    async def execute(
        self,
        document: str,
        variable_values: dict[Any, Any] | None = None,
        timeout: int | None = None,  # noqa: ASYNC109
        retry: int = 3,
    ) -> dict[Any, Any] | None:
        """Execute a GraphQL query and return the data.

        :param document: The GraphQL query to request.
        :param variable_values: The GraphQL variables to parse with the request.
        :param timeout: The timeout to use for the request.
        :param retry: The number of times to retry the request.
        """
        timeout = timeout or self._timeout

        payload = {"query": document, "variables": variable_values or {}}

        try:
            resp = await self._websession.post(
                API_ENDPOINT,
                headers={
                    "Authorization": f"Bearer {self._access_token}",
                    aiohttp.hdrs.USER_AGENT: self._user_agent,
                },
                data=payload,
                timeout=aiohttp.ClientTimeout(total=timeout),
            )
            return (await extract_response_data(resp)).get("data")
        except (TimeoutError, aiohttp.ClientError) as err:
            if retry > 0:
                return await self.execute(
                    document,
                    variable_values,
                    timeout,
                    retry - 1,
                )
            if isinstance(err, TimeoutError):
                _LOGGER.error("Timed out when connecting to Tibber")
            else:
                _LOGGER.exception("Error connecting to Tibber")
            raise
        except (InvalidLoginError, FatalHttpExceptionError) as err:
            _LOGGER.error(
                "Fatal error interacting with Tibber API, HTTP status: %s. API error: %s / %s",
                err.status,
                err.extension_code,
                err.message,
            )
            raise
        except RetryableHttpExceptionError as err:
            _LOGGER.warning(
                "Temporary failure interacting with Tibber API, HTTP status: %s. API error: %s / %s",
                err.status,
                err.extension_code,
                err.message,
            )
            raise

    async def close_connection(self) -> None:
        """Close the Tibber connection.

        This method simply closes the websession used by the object.
        """
        await self._websession.close()
