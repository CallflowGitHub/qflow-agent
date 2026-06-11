import os
import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)

from dotenv import load_dotenv
from azure.monitor.opentelemetry import configure_azure_monitor
from agent_framework import create_harness_agent
from agent_framework.openai import OpenAIChatClient
from agent_framework_devui import serve
from openai import AsyncAzureOpenAI
from tools.rag_search import search_knowledge_base

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
    agent = create_harness_agent(
        chat_client,
        name="Q-Flow Agent",
        agent_instructions=(
            "You are the Q-Flow product assistant, built by Q-nomy.\n\n"
            "## What Q-Flow is\n"
            "Q-Flow is Q-nomy's enterprise customer journey orchestration platform. "
            "It manages the full customer journey — from appointment scheduling and preparation, "
            "through intake, routing, and queue management, to service delivery and reporting. "
            "It is used by enterprise clients across financial services, government and public "
            "sector, healthcare, and retail.\n\n"
            "## Who you are serving\n"
            "You assist internal Q-nomy team members, partners, and administrators "
            "who want to understand, operate, or troubleshoot Q-Flow.\n\n"
            "## How to answer\n"
            "- Always call `search_knowledge_base` before answering any question about "
            "Q-Flow features, queue management, appointments, routing rules, reporting, "
            "troubleshooting, or best practices. "
            "Never rely on general knowledge alone for Q-Flow-specific topics.\n"
            "- When looking something up, you may briefly say you are checking the knowledge base, "
            "but never mention technical details such as hybrid search, semantic search, "
            "vector search, re-ranking, or any other internal retrieval mechanism.\n"
            "- Synthesise the retrieved knowledge base results into a clear, accurate answer. "
            "Cite the source article title and file where relevant so the user can find more detail.\n"
            "- If the knowledge base returns no useful results, say so honestly. "
            "Suggest the user contact Q-nomy support at support@qnomy.com or consult "
            "the official documentation.\n"
            "- Do not speculate or invent product behaviour that is not grounded in the "
            "retrieved knowledge base content."
            "\n- ANSWER STYLE: Keep answers short and to the point. Always include the KB source(s) "
            "used for the answer (cite article title and file path) at the end of the reply."
        ),
        harness_instructions=None,
        max_context_window_tokens=128_000,
        max_output_tokens=4_096,
        disable_mode=True,
        disable_todo=True,
        tools=[search_knowledge_base],
    )

    # serve() launches the DevUI web server and registers the agent.
    # instrumentation_enabled=True activates the OTEL TracerProvider wired to Azure Monitor above.
    serve(entities=[agent], auto_open=True, auth_enabled=False, instrumentation_enabled=True)


if __name__ == "__main__":
    main()
