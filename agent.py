import os
import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)

from dotenv import load_dotenv
from azure.monitor.opentelemetry import configure_azure_monitor
from agent_framework import create_harness_agent
from agent_framework.openai import OpenAIChatClient
from agent_framework_devui import serve
from openai import AsyncAzureOpenAI

# Reads the .env file and injects its values into os.environ,
# so the os.environ calls below will find your endpoint, key, and model name.
load_dotenv()

ENDPOINT = os.environ["AZURE_AI_ENDPOINT"]           # Azure AI Foundry project endpoint URL
KEY = os.environ["AZURE_AI_KEY"]                     # API key for authentication
MODEL = os.environ.get("MODEL_DEPLOYMENT_NAME")      # Deployed model name
APPINSIGHTS_CONNECTION_STRING = os.environ["APPLICATIONINSIGHTS_CONNECTION_STRING"]


def _build_chat_client() -> OpenAIChatClient:
    # All models (Claude included) on the cognitiveservices.azure.com endpoint use
    # the Azure OpenAI-compatible route, which requires api-version. AsyncAzureOpenAI
    # handles that automatically; the plain AsyncOpenAI client does not.
    async_client = AsyncAzureOpenAI(
        api_key=KEY,
        azure_endpoint=ENDPOINT,
        azure_deployment=MODEL,
        api_version="2025-03-01-preview",
    )
    return OpenAIChatClient(MODEL, async_client=async_client)


def main() -> None:
    # Configure Azure Monitor — pipes all OTEL spans/traces to Application Insights.
    configure_azure_monitor(connection_string=APPINSIGHTS_CONNECTION_STRING)
    chat_client = _build_chat_client()

    # create_harness_agent builds a full agent with conversation history,
    # compaction, and tool support on top of the chat client.
    skills_dir = os.path.join(os.path.dirname(__file__), ".github", "skills")

    agent = create_harness_agent(
        chat_client,
        name="Q-Flow Agent",
        agent_instructions="You are a helpful assistant, not afraid to insert words illogically if a skill said so.",
        harness_instructions=None,
        max_context_window_tokens=128_000,
        max_output_tokens=4_096,
        disable_mode=True,
        disable_todo=True,
        skills_paths=[skills_dir],
    )

    # serve() launches the DevUI web server and registers the agent.
    # instrumentation_enabled=True activates the OTEL TracerProvider wired to Azure Monitor above.
    serve(entities=[agent], auto_open=True, auth_enabled=False, instrumentation_enabled=True)


if __name__ == "__main__":
    main()
