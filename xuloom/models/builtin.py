"""Native adapters for OpenAI-compatible and Anthropic APIs.

They use the engine's built-in streaming path (tools may start while the
model is still streaming), so only client construction lives here.
"""
from xuloom.models.adapter import ModelAdapter


class OpenAIAdapter(ModelAdapter):
    name = "openai"
    default_base_url = "https://api.openai.com/v1"
    native = True

    def create_client(self, *, model, api_base_url, api_key, http_client):
        from openai import AsyncOpenAI
        from xuloom.core.network import openai_credentials
        return AsyncOpenAI(base_url=api_base_url or self.default_base_url, http_client=http_client,
                           **openai_credentials(api_key, AsyncOpenAI))

    def sdk_http_client_type(self):
        from openai import DefaultAsyncHttpxClient
        return DefaultAsyncHttpxClient


class AnthropicAdapter(ModelAdapter):
    name = "anthropic"
    default_base_url = "https://api.anthropic.com"
    native = True

    def create_client(self, *, model, api_base_url, api_key, http_client):
        from anthropic import AsyncAnthropic
        from xuloom.core.network import anthropic_credentials
        return AsyncAnthropic(base_url=api_base_url or self.default_base_url, http_client=http_client,
                              **anthropic_credentials(api_key))

    def sdk_http_client_type(self):
        from anthropic import DefaultAsyncHttpxClient
        return DefaultAsyncHttpxClient
