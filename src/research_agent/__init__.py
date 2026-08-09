"""Evidence-first research agent."""

from research_agent.domain import ResearchRequest, ResearchResult
from research_agent.workflow import ResearchWorkflow, build_default_workflow

__all__ = [
    "ResearchRequest",
    "ResearchResult",
    "ResearchWorkflow",
    "build_default_workflow",
]

__version__ = "0.1.0"
