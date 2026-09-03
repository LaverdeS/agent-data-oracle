import pytest
from httpx import ASGITransport, AsyncClient

from agent_data_oracle.web import create_app


@pytest.mark.asyncio
async def test_root_renders_the_product_boundary(
    unavailable_database_url: str,
) -> None:
    app = create_app(database_url=unavailable_database_url)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "CPSC evidence, not a safety verdict" in response.text
