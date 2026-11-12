"""Lambda entrypoint: get_account."""

from typing import Any

from dr_demo.adapters.inbound.api_gateway import LambdaContext
from dr_demo.config.container import get_container


def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    return get_container().controller.get_account(event, context)
