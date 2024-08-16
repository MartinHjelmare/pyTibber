"""Provide a class for handling the Tibber REST API."""

import logging
from http import HTTPStatus
from typing import Any

import aiohttp

from .const import API_ENDPOINT, API_ERR_CODE_UNAUTH, API_ERR_CODE_UNKNOWN, HTTP_CODES_FATAL, HTTP_CODES_RETRIABLE
from .exceptions import (
    FatalHttpExceptionError,
    InvalidLoginError,
    RetryableHttpExceptionError,
)

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


def extract_error_details(errors: list[Any], default_message: str) -> tuple[str, str]:
    """Tries to extract the error message and code from the provided 'errors' dictionary"""
    if not errors:
        return API_ERR_CODE_UNKNOWN, default_message
    return errors[0].get("extensions").get("code"), errors[0].get("message")


async def extract_response_data(response: aiohttp.ClientResponse) -> dict[Any, Any]:
    """Extracts the response as JSON or throws a HttpException"""
    _LOGGER.debug("Response status: %s", response.status)

    if response.content_type != "application/json":
        raise FatalHttpExceptionError(
            response.status,
            f"Unexpected content type: {response.content_type}",
            API_ERR_CODE_UNKNOWN,
        )

    result = await response.json()

    if response.status == HTTPStatus.OK:
        return result

    if response.status in HTTP_CODES_RETRIABLE:
        error_code, error_message = extract_error_details(result.get("errors", []), str(response.content))

        raise RetryableHttpExceptionError(response.status, message=error_message, extension_code=error_code)

    if response.status in HTTP_CODES_FATAL:
        error_code, error_message = extract_error_details(result.get("errors", []), "request failed")
        if error_code == API_ERR_CODE_UNAUTH:
            raise InvalidLoginError(response.status, error_message, error_code)

        raise FatalHttpExceptionError(response.status, error_message, error_code)

    error_code, error_message = extract_error_details(result.get("errors", []), "N/A")
    # if reached here the HTTP response code is not currently handled
    raise FatalHttpExceptionError(response.status, f"Unhandled error: {error_message}", error_code)
