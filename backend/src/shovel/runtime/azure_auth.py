from __future__ import annotations

import os
from typing import Any

from azure.core.exceptions import ClientAuthenticationError
from azure.identity.aio import AzureCliCredential, get_bearer_token_provider
from openai import AsyncAzureOpenAI

from shovel.protection.egress import EgressChannel, require_ordinary
from shovel.runtime.errors import RuntimeFailure


class AzureCliOpenAIClient(AsyncAzureOpenAI):
    def __init__(self, *, egress_channel: EgressChannel = EgressChannel.MODEL, **kwargs: Any) -> None:
        if "AZURE_OPENAI_AD_TOKEN" in os.environ:
            raise RuntimeFailure(
                "Azure CLI authentication cannot be combined with AZURE_OPENAI_AD_TOKEN.",
                status=503,
                hint="Unset AZURE_OPENAI_AD_TOKEN so the provider can acquire and refresh tokens.",
            )
        self._egress_channel = egress_channel
        self._credential = AzureCliCredential(process_timeout=30)
        self._credential_closed = False
        self._token_provider = get_bearer_token_provider(
            self._credential, "https://cognitiveservices.azure.com/.default"
        )
        super().__init__(azure_ad_token_provider=self._access_token, **kwargs)

    async def _access_token(self) -> str:
        require_ordinary(self._egress_channel)
        try:
            return await self._token_provider()
        except ClientAuthenticationError:
            raise RuntimeFailure(
                "Azure CLI authentication failed.",
                status=503,
                hint="Run az login for the resource tenant and check Azure OpenAI access permissions.",
            ) from None

    async def close_credential(self) -> None:
        if not self._credential_closed:
            await self._credential.close()
            self._credential_closed = True

    async def close(self) -> None:
        try:
            await super().close()
        finally:
            await self.close_credential()
