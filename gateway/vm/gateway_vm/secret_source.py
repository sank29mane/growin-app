"""Read the two Breeze secrets from Secret Manager once, at service start (GATE-03, D-14).

The VM service account's token comes from the metadata server, the secret
values from the Secret Manager REST API, both through the injected Transport
(so they obey the host allowlist and the byte caps). Nothing is cached beyond
one start-up: the access token is dropped by discard() and the values live
only in a Credentials object whose repr hides them.

SecretsUnavailable carries a fixed code and nothing else: never a name, a
project id, a token, a status line or a response body.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from typing import Mapping, Protocol

from .config import SECRET_NAME_RE
from .session import Credentials
from .transport import HttpResponse, Timeouts, Transport, TransportError

METADATA_BASE = "http://metadata.google.internal/computeMetadata/v1/"
PROJECT_ID_PATH = "project/project-id"
TOKEN_PATH = "instance/service-accounts/default/token"
SECRET_MANAGER_BASE = "https://secretmanager.googleapis.com/v1/projects/"

_PROJECT_ID_RE = re.compile(r"^[a-z][a-z0-9-]{3,61}[a-z0-9]$")
_ACCESS_TOKEN_RE = re.compile(r"^[\x21-\x7e]{1,4096}$")
# A secret value is one printable-ASCII token: no whitespace, no control character.
_SECRET_VALUE_RE = re.compile(r"^[\x21-\x7e]{1,512}$")
_METADATA_MAX_BYTES = 8 * 1024
_SECRET_MAX_BYTES = 64 * 1024
CODES = ("metadata_unreachable", "token_invalid", "access_denied", "not_found", "malformed")


class SecretsUnavailable(Exception):
    """code: metadata_unreachable | token_invalid | access_denied | not_found | malformed."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class SecretSource(Protocol):
    def read(self, name: str) -> str: ...


class SecretManagerSource:
    """Secret Manager over the metadata-server token. One project id and one
    token fetch serve every read until discard(); each name costs one access call.
    """

    def __init__(
        self, transport: Transport, *, timeouts: Timeouts = Timeouts(connect_s=2, read_s=5, total_s=8)
    ) -> None:
        self._transport = transport
        self._timeouts = timeouts
        self._project: str | None = None
        self._token: str | None = None

    def _metadata(self, path: str) -> HttpResponse:
        try:
            resp = self._transport.request(
                "GET",
                METADATA_BASE + path,
                headers={"Metadata-Flavor": "Google"},
                body=None,
                timeouts=self._timeouts,
                max_bytes=_METADATA_MAX_BYTES,
            )
        except TransportError:
            raise SecretsUnavailable("metadata_unreachable") from None
        if resp.status != 200:
            raise SecretsUnavailable("metadata_unreachable")
        return resp

    def _project_id(self) -> str:
        if self._project is None:
            text = self._metadata(PROJECT_ID_PATH).body.decode("ascii", errors="replace").strip()
            if not _PROJECT_ID_RE.fullmatch(text):
                raise SecretsUnavailable("malformed")
            self._project = text
        return self._project

    def _access_token(self) -> str:
        if self._token is None:
            try:
                payload = json.loads(self._metadata(TOKEN_PATH).body)
            except (ValueError, RecursionError):
                raise SecretsUnavailable("token_invalid") from None
            token = payload.get("access_token") if isinstance(payload, dict) else None
            if not isinstance(token, str) or not _ACCESS_TOKEN_RE.fullmatch(token):
                raise SecretsUnavailable("token_invalid")
            self._token = token
        return self._token

    def discard(self) -> None:
        """Forget the access token and the project id."""
        self._token = None
        self._project = None

    def read(self, name: str) -> str:
        if not isinstance(name, str) or not SECRET_NAME_RE.fullmatch(name):
            raise SecretsUnavailable("malformed")
        project = self._project_id()
        token = self._access_token()
        url = f"{SECRET_MANAGER_BASE}{project}/secrets/{name}/versions/latest:access"
        try:
            resp = self._transport.request(
                "GET",
                url,
                headers={"Authorization": f"Bearer {token}"},
                body=None,
                timeouts=self._timeouts,
                max_bytes=_SECRET_MAX_BYTES,
            )
        except TransportError:
            raise SecretsUnavailable("malformed") from None
        if resp.status == 401:
            raise SecretsUnavailable("token_invalid")
        if resp.status == 403:
            raise SecretsUnavailable("access_denied")
        if resp.status == 404:
            raise SecretsUnavailable("not_found")
        if resp.status != 200:
            raise SecretsUnavailable("malformed")
        try:
            data = json.loads(resp.body)["payload"]["data"]
            value = base64.b64decode(data, validate=True).decode("ascii")
        except (ValueError, KeyError, TypeError, binascii.Error, RecursionError):
            raise SecretsUnavailable("malformed") from None
        if not _SECRET_VALUE_RE.fullmatch(value):
            raise SecretsUnavailable("malformed")
        return value


def load_credentials(source: SecretSource, names: Mapping[str, str]) -> Credentials:
    """Read both secrets and drop the source's token, whether or not a read failed."""
    try:
        try:
            return Credentials(app_key=source.read(names["app_key"]), secret_key=source.read(names["secret_key"]))
        except KeyError:
            raise SecretsUnavailable("malformed") from None
    finally:
        discard = getattr(source, "discard", None)
        if callable(discard):
            discard()
