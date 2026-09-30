from fastapi.testclient import TestClient

from app.api import create_app
from app.engine import ResearchEngine


def test_request_status_report_and_evidence(settings, store):
    with TestClient(create_app(settings, store)) as client:
        assert client.get("/health").status_code == 200
        response = client.post("/research-runs", json={"ticker": "test"})
        assert response.status_code == 202
        run_id = response.json()["run_id"]
        assert client.get(f"/research-runs/{run_id}").json()["status"] == "PENDING"
        ResearchEngine(settings, store).run(run_id)
        assert client.get(f"/research-runs/{run_id}/report").json()["fixture"]
        assert client.get(f"/research-runs/{run_id}/evidence").json()
        assert "# 10)" in client.get(f"/research-runs/{run_id}/markdown").text
        assert client.get("/research-runs/missing").status_code == 404
