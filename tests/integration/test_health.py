import pytest
from httpx import ASGITransport, AsyncClient

from agent_data_oracle.web import create_app


@pytest.mark.asyncio
@pytest.mark.integration
async def test_ready_reports_ready_with_real_postgresql(postgres_url: str) -> None:
    app = create_app(database_url=postgres_url)

    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = await client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
