from app.llm_provider.capability.provider_capability import ProviderCapability
from app.service.depends import get_provider_service


class CapabilityService:
    def __init__(self):
        pass


    @staticmethod
    def get_thinking_channel(provider_id: int) -> str:
        provider = get_provider_service().get_provider(provider_id=provider_id)
        capability = ProviderCapability.get_capability(provider.name)
        return capability.thinking_channel