from collections.abc import Iterator
from typing import Any

import pytest
from botocore.exceptions import NoCredentialsError

from dr_demo.adapters.outbound import iam_token
from dr_demo.domain.errors import DatabaseUnavailable


class FakeRdsClient:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def generate_db_auth_token(self, **kwargs: Any) -> str:
        if self.error:
            raise self.error
        self.calls.append(kwargs)
        return "signed-token"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeRdsClient]:
    fake = FakeRdsClient()
    created: list[tuple[str, str]] = []

    def factory(service: str, region_name: str) -> FakeRdsClient:
        created.append((service, region_name))
        return fake

    monkeypatch.setattr("dr_demo.adapters.outbound.iam_token.boto3.client", factory)
    yield fake
    assert created == [("rds", "us-east-2")]


def test_token_is_signed_for_the_cluster_user_and_region(client: FakeRdsClient) -> None:
    provider = iam_token.make_token_provider("db.example.internal", 5432, "app_user", "us-east-2")

    assert provider() == "signed-token"
    assert client.calls == [
        {
            "DBHostname": "db.example.internal",
            "Port": 5432,
            "DBUsername": "app_user",
            "Region": "us-east-2",
        }
    ]


def test_signing_failure_is_unavailable(client: FakeRdsClient) -> None:
    client.error = NoCredentialsError()
    provider = iam_token.make_token_provider("h", 5432, "u", "us-east-2")

    with pytest.raises(DatabaseUnavailable):
        provider()
