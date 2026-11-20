from __future__ import annotations

import os
from typing import Any

from azure.core.exceptions import ClientAuthenticationError
from azure.identity.aio import AzureCliCredential, get_bearer_token_provider
from openai import AsyncAzureOpenAI, AsyncOpenAI

from shovel.protection.egress import EgressChannel, require_ordinary
from shovel.runtime.errors import RuntimeFailure


class AzureCliTokenProvider:
    def __init__(self, *, scope: str, egress_channel: EgressChannel, resource: str) -> None:
        if "AZURE_OPENAI_AD_TOKEN" in os.environ:
            raise RuntimeFailure(
                "Azure CLI authentication cannot be combined with AZURE_OPENAI_AD_TOKEN.",
                status=503,
                hint="Unset AZURE_OPENAI_AD_TOKEN so the provider can acquire and refresh tokens.",
            )
        self._egress_channel = egress_channel
        self._resource = resource
        self._credential = AzureCliCredential(process_timeout=30)
        self._credential_closed = False
        self._token_provider = get_bearer_token_provider(self._credential, scope)

    async def __call__(self) -> str:
        require_ordinary(self._egress_channel)
        try:
            return await self._token_provider()
        except ClientAuthenticationError:
            raise RuntimeFailure(
                "Azure CLI authentication failed.",
                status=503,
                hint=f"Run az login for the resource tenant and check {self._resource} access permissions.",
            ) from None

    async def close(self) -> None:
        if not self._credential_closed:
            await self._credential.close()
            self._credential_closed = True


class AzureCliOpenAIClient(AsyncAzureOpenAI):
    def __init__(self, *, egress_channel: EgressChannel = EgressChannel.MODEL, **kwargs: Any) -> None:
        self._tokens = AzureCliTokenProvider(
            scope="https://cognitiveservices.azure.com/.default",
            egress_channel=egress_channel,
            resource="Azure OpenAI",
        )
        super().__init__(azure_ad_token_provider=self._tokens, **kwargs)

    async def close_credential(self) -> None:
        await self._tokens.close()

    async def close(self) -> None:
        try:
            await super().close()
        finally:
            await self.close_credential()


class FoundryCliOpenAIClient(AsyncOpenAI):
    def __init__(self, *, egress_channel: EgressChannel = EgressChannel.MODEL, **kwargs: Any) -> None:
        self._tokens = AzureCliTokenProvider(
            scope="https://ai.azure.com/.default",
            egress_channel=egress_channel,
            resource="Microsoft Foundry",
        )
        kwargs["api_key"] = self._tokens
        super().__init__(**kwargs)

    async def close(self) -> None:
        try:
            await super().close()
        finally:
            await self._tokens.close()
