import json

import pytest

from core.session_manager import new_session
from tests.data_guard import file_manifest


@pytest.mark.asyncio
@pytest.mark.parametrize("projects", [[], ["one"], ["one", "two", "three"]])
async def test_project_stats_supports_zero_one_and_many(
    app_client,
    monkeypatch,
    projects,
):
    from routes import projects as projects_route

    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(projects_route, "list_projects", lambda: list(projects))

    def load_characters(project: str) -> list[dict]:
        calls.append((project, "characters"))
        return [{"id": project}]

    def load_worldbook(project: str) -> list[dict]:
        calls.append((project, "worldbook"))
        return [{"id": project}, {"id": f"{project}-2"}]

    def load_sessions(project: str) -> list[dict]:
        calls.append((project, "saves"))
        return [{"session_id": project, "status": "ready"}]

    monkeypatch.setattr(projects_route, "list_characters", load_characters)
    monkeypatch.setattr(projects_route, "load_worldbook", load_worldbook)
    monkeypatch.setattr(projects_route, "list_sessions", load_sessions)

    response = await app_client.get("/api/projects/stats")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "stats": [
            {
                "project": project,
                "characters": 1,
                "worldbook": 2,
                "saves": 1,
                "status": "ready",
                "errors": [],
            }
            for project in projects
        ]
    }
    assert sorted(calls) == sorted(
        (project, category)
        for project in projects
        for category in ("characters", "worldbook", "saves")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("loader_name", "count_field", "error_code"),
    [
        ("list_characters", "characters", "characters_unavailable"),
        ("load_worldbook", "worldbook", "worldbook_unavailable"),
        ("list_sessions", "saves", "sessions_unavailable"),
    ],
)
async def test_project_stats_uses_only_safe_error_codes(
    app_client,
    monkeypatch,
    loader_name,
    count_field,
    error_code,
):
    from routes import projects as projects_route

    project = "stats_safe_error"
    monkeypatch.setattr(projects_route, "list_projects", lambda: [project])
    monkeypatch.setattr(projects_route, "list_characters", lambda _project: [{}])
    monkeypatch.setattr(projects_route, "load_worldbook", lambda _project: [{}])
    monkeypatch.setattr(
        projects_route,
        "list_sessions",
        lambda _project: [{"status": "ready"}],
    )

    def fail_with_sensitive_detail(_project: str) -> list[dict]:
        raise RuntimeError(r"C:\secret\private.json TOP_SECRET_PAYLOAD")

    monkeypatch.setattr(projects_route, loader_name, fail_with_sensitive_detail)

    response = await app_client.get("/api/projects/stats")

    assert response.status_code == 200, response.text
    stat = response.json()["stats"][0]
    assert stat[count_field] == 0
    assert stat["status"] == "partial"
    assert stat["errors"] == [error_code]
    serialized = json.dumps(stat, ensure_ascii=False)
    assert "TOP_SECRET_PAYLOAD" not in serialized
    assert r"C:\secret" not in serialized


@pytest.mark.asyncio
async def test_project_stats_reads_yaml_and_yml_without_writing(
    app_client,
    isolated_paths,
    monkeypatch,
):
    from routes import projects as projects_route

    project = "stats_extensions"
    project_dir = isolated_paths["projects"] / project
    (project_dir / "characters").mkdir(parents=True)
    (project_dir / "worldbook").mkdir()
    (project_dir / "saves").mkdir()
    (project_dir / "characters" / "hero.yaml").write_text(
        "id: hero\nname: Hero\nactive: true\n",
        encoding="utf-8",
    )
    (project_dir / "worldbook" / "one.yaml").write_text(
        "id: one\ntitle: One\ncontent: YAML\n",
        encoding="utf-8",
    )
    (project_dir / "worldbook" / "two.yml").write_text(
        "id: two\ntitle: Two\ncontent: YML\n",
        encoding="utf-8",
    )
    (project_dir / "saves" / "save.json").write_text(
        json.dumps(new_session(project, "save"), ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(projects_route, "list_projects", lambda: [project])
    before = file_manifest(project_dir)

    response = await app_client.get("/api/projects/stats")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "stats": [{
            "project": project,
            "characters": 1,
            "worldbook": 2,
            "saves": 1,
            "status": "ready",
            "errors": [],
        }]
    }
    assert file_manifest(project_dir) == before


@pytest.mark.asyncio
async def test_project_stats_isolates_corruption_and_sanitizes_errors(
    app_client,
    isolated_paths,
    monkeypatch,
    request,
):
    from routes import projects as projects_route

    good = "stats_good"
    bad = "stats_bad"
    for project in (good, bad):
        project_dir = isolated_paths["projects"] / project
        (project_dir / "characters").mkdir(parents=True)
        (project_dir / "worldbook").mkdir()
        (project_dir / "saves").mkdir()
        (project_dir / "characters" / "hero.yaml").write_text(
            "id: hero\nname: Hero\nactive: true\n",
            encoding="utf-8",
        )

    good_dir = isolated_paths["projects"] / good
    (good_dir / "worldbook" / "lore.yaml").write_text(
        "id: lore\ncontent: safe\n",
        encoding="utf-8",
    )
    (good_dir / "saves" / "save.json").write_text(
        json.dumps(new_session(good, "save"), ensure_ascii=False),
        encoding="utf-8",
    )

    bad_dir = isolated_paths["projects"] / bad
    bad_worldbook = bad_dir / "worldbook" / "broken.yml"
    bad_save = bad_dir / "saves" / "broken.json"
    bad_worldbook.write_text(
        "id: broken\ncontent: [TOP_SECRET_PAYLOAD\n",
        encoding="utf-8",
    )
    bad_save.write_text(
        '{"TOP_SECRET_PAYLOAD":',
        encoding="utf-8",
    )

    def cleanup_corrupt_samples() -> None:
        bad_worldbook.unlink(missing_ok=True)
        bad_save.unlink(missing_ok=True)

    request.addfinalizer(cleanup_corrupt_samples)
    monkeypatch.setattr(projects_route, "list_projects", lambda: [good, bad])
    before = {
        project: file_manifest(isolated_paths["projects"] / project)
        for project in (good, bad)
    }

    response = await app_client.get("/api/projects/stats")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "stats": [
            {
                "project": good,
                "characters": 1,
                "worldbook": 1,
                "saves": 1,
                "status": "ready",
                "errors": [],
            },
            {
                "project": bad,
                "characters": 1,
                "worldbook": 0,
                "saves": 0,
                "status": "partial",
                "errors": ["worldbook_unavailable", "sessions_unavailable"],
            },
        ]
    }
    serialized = json.dumps(response.json(), ensure_ascii=False)
    assert "TOP_SECRET_PAYLOAD" not in serialized
    assert str(bad_dir) not in serialized
    assert {
        project: file_manifest(isolated_paths["projects"] / project)
        for project in (good, bad)
    } == before
