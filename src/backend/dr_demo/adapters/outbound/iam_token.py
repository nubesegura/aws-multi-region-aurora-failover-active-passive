"""IAM database authentication tokens (the database password is a short-lived signed token)."""

from collections.abc import Callable

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from dr_demo.domain.errors import DatabaseUnavailable

TokenProvider = Callable[[], str]


def make_token_provider(host: str, port: int, user: str, region: str) -> TokenProvider:
    """Return a callable that signs a fresh token for ``host:port`` and ``user`` in ``region``.

    Signing is local (no network call) and uses the function's execution role credentials.
    """
    client = boto3.client("rds", region_name=region)

    def provide() -> str:
        try:
            token: str = client.generate_db_auth_token(
                DBHostname=host, Port=port, DBUsername=user, Region=region
            )
        except (BotoCoreError, ClientError) as error:
            raise DatabaseUnavailable from error
        return token

    return provide
