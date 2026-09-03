import pytest
from httpx import ASGITransport, AsyncClient

from agent_data_oracle.web import create_app


@pytest.mark.asyncio
async def test_root_renders_the_product_boundary() -> None:
    app = create_app(
        database_url="postgresql+psycopg://unavailable:unavailable@127.0.0.1:1/unavailable"
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "CPSC evidence, not a safety verdict" in response.text
