from __future__ import annotations

from fastapi import FastAPI, HTTPException, status

from research_agent.application import ResearchApplicationService, RunNotFoundError
from research_agent.domain import ResearchRequest, ResearchResult, RunSnapshot
from research_agent.workflow import ResearchWorkflow, build_default_workflow


def create_app(workflow: ResearchWorkflow | None = None) -> FastAPI:
    api = FastAPI(
        title="Research Agent Python Lab",
        version="0.1.0",
        description="Evidence-first and claim-verifiable intelligent research assistant",
    )
    service = ResearchApplicationService(workflow or build_default_workflow())

    @api.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @api.post("/v1/research/run", response_model=ResearchResult)
    async def run_research(request: ResearchRequest) -> ResearchResult:
        return await service.run(request)

    @api.post(
        "/v1/research/runs",
        response_model=RunSnapshot,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def submit_research(request: ResearchRequest) -> RunSnapshot:
        return await service.submit(request)

    @api.get("/v1/research/runs/{run_id}", response_model=RunSnapshot)
    async def get_research(run_id: str) -> RunSnapshot:
        try:
            return await service.get(run_id)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Research run not found") from exc

    return api


app = create_app()
