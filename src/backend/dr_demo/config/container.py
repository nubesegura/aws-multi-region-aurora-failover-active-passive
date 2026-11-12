"""Composition root: the only place that wires adapters to use cases."""

import os
from dataclasses import dataclass
from functools import cache

from dr_demo.adapters.inbound.api_gateway import ApiGatewayController
from dr_demo.adapters.outbound.iam_token import make_token_provider
from dr_demo.adapters.outbound.postgres_repository import (
    PgConnectionFactory,
    PostgresTransactionRepository,
)
from dr_demo.adapters.outbound.system import StaticRegionProvider, SystemClock, UuidGenerator
from dr_demo.application.use_cases.create_transaction import CreateTransaction
from dr_demo.application.use_cases.get_account import GetAccount
from dr_demo.application.use_cases.get_consistency import GetConsistency
from dr_demo.application.use_cases.get_health import GetHealth
from dr_demo.application.use_cases.get_stats import GetStats
from dr_demo.application.use_cases.list_transactions import ListTransactions
from dr_demo.config.logging import configure_logging
from dr_demo.config.settings import Settings


@dataclass(frozen=True)
class Container:
    settings: Settings
    controller: ApiGatewayController


def build_container(settings: Settings) -> Container:
    """Wire the object graph; no network access happens here."""
    logger = configure_logging(settings.log_level)
    token_provider = make_token_provider(
        settings.db_host, settings.db_port, settings.db_user, settings.region
    )
    repository = PostgresTransactionRepository(
        PgConnectionFactory(
            host=settings.db_host,
            port=settings.db_port,
            database=settings.db_name,
            user=settings.db_user,
            ca_bundle_path=settings.ca_bundle_path,
            token_provider=token_provider,
        )
    )
    clock = SystemClock()
    region = StaticRegionProvider(settings.region)
    controller = ApiGatewayController(
        create_transaction=CreateTransaction(repository, clock, UuidGenerator(), region),
        list_transactions=ListTransactions(repository),
        get_stats=GetStats(repository, clock, region),
        get_health=GetHealth(repository, region),
        get_account=GetAccount(repository, region),
        get_consistency=GetConsistency(repository, region),
        region=settings.region,
        allowed_origins=settings.allowed_origins,
        logger=logger,
        allowed_client_ids=settings.allowed_client_ids,
    )
    return Container(settings=settings, controller=controller)


@cache
def get_container() -> Container:
    """Build the container once per execution environment (settings validated at cold start)."""
    return build_container(Settings.from_env(os.environ))
