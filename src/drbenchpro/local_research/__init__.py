"""DRBenchPro Local Research Framework."""

from .agents import LocalResearchWorkflow
from .config import LocalResearchConfig
from .engine import LocalResearchEngine, run_research_request
from .model_client import (
    ModelClientError,
    ModelConfig,
    OpenAICompatibleModelClient,
    RoutedModelClient,
    UnavailableModelClient,
)
from .schemas import (
    CitationValidation,
    ResearchResult,
    ResearchUnitResult,
    TokenUsage,
    ToolUsage,
)
from .wiki_client import (
    WIKI_TOOL_SCHEMAS,
    wikipedia_tool_schemas,
    WikiClientConfig,
    WikiToolClient,
    WikiToolContractError,
    WikiToolError,
)
from .version import FRAMEWORK_VERSION

__all__ = [
    "CitationValidation",
    "FRAMEWORK_VERSION",
    "LocalResearchConfig",
    "LocalResearchEngine",
    "LocalResearchWorkflow",
    "ModelClientError",
    "ModelConfig",
    "OpenAICompatibleModelClient",
    "ResearchResult",
    "ResearchUnitResult",
    "RoutedModelClient",
    "UnavailableModelClient",
    "TokenUsage",
    "ToolUsage",
    "WIKI_TOOL_SCHEMAS",
    "wikipedia_tool_schemas",
    "WikiClientConfig",
    "WikiToolClient",
    "WikiToolContractError",
    "WikiToolError",
    "run_research_request",
]
