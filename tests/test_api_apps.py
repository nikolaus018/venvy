"""App configuration, file operations and log handling."""

from __future__ import annotations

import json

import pytest


class TestAppUpdate:
    def test_renames_an_app(self, admin_client, make_app):
        app_id = make_app("before")
        response = admin_client.put(f"/api/apps/{app_id}", json={"name": "after"})
        assert response.status_code == 200
        assert response.json()["name"] == "after"

    def test_rejects_a_blank_name(self, admin_client, make_app):
        app_id = make_app()
        assert admin_client.put(f"/api/apps/{app_id}", json={"name": "   "}).status_code == 422

    def test_rejects_an_overlong_name(self, admin_client, make_app):
        app_id = make_app()
        response = admin_client.put(f"/api/apps/{app_id}", json={"name": "x" * 51})
        assert response.status_code == 422

    @pytest.mark.parametrize("policy", ["never", "on-failure", "always"])
    def test_accepts_valid_restart_policies(self, admin_client, make_app, policy):
        app_id = make_app()
        response = admin_client.put(f"/api/apps/{app_id}", json={"restart_policy": policy})
        assert response.status_code == 200
        assert response.json()["restart_policy"] == policy

    def test_rejects_an_unknown_restart_policy(self, admin_client, make_app):
        app_id = make_app()
        response = admin_client.put(f"/api/apps/{app_id}", json={"restart_policy": "sometimes"})
        assert response.status_code == 422

    def test_rejects_an_unknown_runtime(self, admin_client, make_app):
        app_id = make_app()
        assert admin_client.put(f"/api/apps/{app_id}", json={"runtime": "cobol"}).status_code == 422

    @pytest.mark.parametrize("port", [0, 1, 8080, 65535])
    def test_accepts_valid_ports(self, admin_client, make_app, port):
        app_id = make_app()
        assert admin_client.put(f"/api/apps/{app_id}", json={"port": port}).status_code == 200

    @pytest.mark.parametrize("port", [-1, 65536, 99999])
    def test_rejects_out_of_range_ports(self, admin_client, make_app, port):
        app_id = make_app()
        assert admin_client.put(f"/api/apps/{app_id}", json={"port": port}).status_code == 422

    def test_port_zero_clears_the_port(self, admin_client, make_app):
        app_id = make_app(port=9999)
        assert admin_client.put(f"/api/apps/{app_id}", json={"port": 0}).json()["port"] is None

    def test_manual_ports_take_priority(self, admin_client, make_app):
        """A pinned port must win over whatever the supervisor detected."""
        app_id = make_app(detected_ports="[7000]")
        body = admin_client.put(f"/api/apps/{app_id}", json={"manual_ports": [8123]}).json()
        assert body["port"] == 8123
        assert json.loads(body["detected_ports"]) == [8123]

    def test_clearing_manual_ports_falls_back_to_detected(self, admin_client, make_app):
        app_id = make_app(manual_ports="[8123]", detected_ports="[8123]", port=8123)
        body = admin_client.put(f"/api/apps/{app_id}", json={"manual_ports": []}).json()
        assert json.loads(body["manual_ports"]) == []

    def test_rust_runtime_forces_the_entrypoint(self, admin_client, make_app):
        app_id = make_app()
        body = admin_client.put(f"/api/apps/{app_id}", json={"runtime": "rust"}).json()
        assert body["entrypoint"] == "main.rs"

    @pytest.mark.parametrize("entrypoint", ["../escape.py", "sub/dir.py", "/etc/passwd"])
    def test_rejects_path_like_entrypoints(self, admin_client, make_app, entrypoint):
        app_id = make_app()
        response = admin_client.put(f"/api/apps/{app_id}", json={"entrypoint": entrypoint})
        assert response.status_code == 422

    def test_environment_variables_round_trip(self, admin_client, make_app):
        app_id = make_app()
        admin_client.put(f"/api/apps/{app_id}", json={"env_vars": {"A": "1", "B": "two"}})
        apps = admin_client.get("/api/apps").json()
        entry = next(a for a in apps if a["id"] == app_id)
        assert json.loads(entry["env_json"]) == {"A": "1", "B": "two"}

    def test_environment_variables_are_encrypted_at_rest(self, admin_client, make_app, settings):
        """The value must not be readable by anything holding only the DB file."""
        import sqlite3

        app_id = make_app()
        admin_client.put(f"/api/apps/{app_id}", json={"env_vars": {"SECRET": "topsecret42"}})

        connection = sqlite3.connect(str(settings.db_file))
        try:
            stored = connection.execute(
                "SELECT env_json FROM app WHERE id = ?", (app_id,)
            ).fetchone()[0]
        finally:
            connection.close()

        assert "topsecret42" not in stored
        assert stored.startswith("enc:v1:")

    def test_memory_limit_zero_means_unlimited(self, admin_client, make_app):
        app_id = make_app()
        body = admin_client.put(f"/api/apps/{app_id}", json={"memory_limit_mb": 0}).json()
        assert body["memory_limit_mb"] is None

    def test_rejects_an_absurd_memory_limit(self, admin_client, make_app):
        app_id = make_app()
        response = admin_client.put(f"/api/apps/{app_id}", json={"memory_limit_mb": 5})
        assert response.status_code == 400

    def test_unknown_app_returns_404(self, admin_client):
        assert admin_client.put("/api/apps/99999", json={"name": "ghost"}).status_code == 404


class TestAppDeletion:
    def test_deletes_the_app_and_its_directory(self, admin_client, make_app, settings):
        app_id = make_app("doomed")
        app_dir = settings.apps_dir / "1" / "doomed"
        assert app_dir.exists()

        assert admin_client.delete(f"/api/apps/{app_id}").status_code == 200
        assert not app_dir.exists()
        assert admin_client.get("/api/apps").json() == []

    def test_unknown_app_returns_404(self, admin_client):
        assert admin_client.delete("/api/apps/99999").status_code == 404


class TestFileOperations:
    def test_creates_and_lists_a_file(self, admin_client, make_app):
        app_id = make_app()
        assert (
            admin_client.post(
                f"/api/apps/{app_id}/files/op",
                json={"operation": "create_file", "path": "main.py"},
            ).status_code
            == 200
        )

        names = [e["name"] for e in admin_client.get(f"/api/apps/{app_id}/files/tree").json()]
        assert "main.py" in names

    def test_saves_and_downloads_content(self, admin_client, make_app):
        app_id = make_app()
        admin_client.post(
            f"/api/apps/{app_id}/files/op",
            json={"operation": "save", "path": "hello.py", "content": "print('hi')\n"},
        )
        response = admin_client.get(f"/api/apps/{app_id}/files/download?path=hello.py")
        assert response.status_code == 200
        assert response.text == "print('hi')\n"

    def test_creates_a_folder(self, admin_client, make_app):
        app_id = make_app()
        admin_client.post(
            f"/api/apps/{app_id}/files/op",
            json={"operation": "create_folder", "path": "pkg"},
        )
        entries = admin_client.get(f"/api/apps/{app_id}/files/tree").json()
        assert any(e["name"] == "pkg" and e["is_dir"] for e in entries)

    def test_renames_a_file(self, admin_client, make_app):
        app_id = make_app()
        admin_client.post(
            f"/api/apps/{app_id}/files/op",
            json={"operation": "save", "path": "old.py", "content": "x = 1"},
        )
        admin_client.post(
            f"/api/apps/{app_id}/files/op",
            json={"operation": "rename", "path": "old.py", "new_path": "new.py"},
        )
        names = [e["name"] for e in admin_client.get(f"/api/apps/{app_id}/files/tree").json()]
        assert "new.py" in names and "old.py" not in names

    def test_deletes_a_file(self, admin_client, make_app):
        app_id = make_app()
        admin_client.post(
            f"/api/apps/{app_id}/files/op",
            json={"operation": "save", "path": "gone.py", "content": "x"},
        )
        admin_client.post(
            f"/api/apps/{app_id}/files/op", json={"operation": "delete", "path": "gone.py"}
        )
        names = [e["name"] for e in admin_client.get(f"/api/apps/{app_id}/files/tree").json()]
        assert "gone.py" not in names

    def test_save_without_content_is_rejected(self, admin_client, make_app):
        app_id = make_app()
        response = admin_client.post(
            f"/api/apps/{app_id}/files/op", json={"operation": "save", "path": "x.py"}
        )
        assert response.status_code == 400

    def test_unknown_operation_is_rejected(self, admin_client, make_app):
        app_id = make_app()
        response = admin_client.post(
            f"/api/apps/{app_id}/files/op", json={"operation": "chmod", "path": "x.py"}
        )
        assert response.status_code == 422

    @pytest.mark.parametrize(
        "path", ["../../../etc/passwd", "../outside.txt", "sub/../../escape.py"]
    )
    def test_rejects_path_traversal(self, admin_client, make_app, path):
        app_id = make_app()
        response = admin_client.post(
            f"/api/apps/{app_id}/files/op",
            json={"operation": "save", "path": path, "content": "pwned"},
        )
        assert response.status_code == 403

    def test_rejects_traversal_on_the_rename_destination(self, admin_client, make_app):
        """Both ends of a rename have to be contained, not just the source."""
        app_id = make_app()
        admin_client.post(
            f"/api/apps/{app_id}/files/op",
            json={"operation": "save", "path": "ok.py", "content": "x"},
        )
        response = admin_client.post(
            f"/api/apps/{app_id}/files/op",
            json={"operation": "rename", "path": "ok.py", "new_path": "../escaped.py"},
        )
        assert response.status_code == 403

    def test_rejects_traversal_on_download(self, admin_client, make_app):
        app_id = make_app()
        response = admin_client.get(f"/api/apps/{app_id}/files/download?path=../../../etc/passwd")
        assert response.status_code in (403, 404)

    def test_rejects_writing_jail_support_files(self, admin_client, make_app):
        """These enforce the terminal's directory restriction."""
        app_id = make_app()
        response = admin_client.post(
            f"/api/apps/{app_id}/files/op",
            json={"operation": "save", "path": ".bashrc-venvy", "content": "cd /"},
        )
        assert response.status_code == 403

    def test_tree_hides_build_directories(self, admin_client, make_app, settings):
        app_id = make_app()
        root = settings.apps_dir / "1" / "demo"
        (root / "venv").mkdir(exist_ok=True)
        (root / "__pycache__").mkdir(exist_ok=True)
        (root / "real.py").write_text("x = 1")

        names = [e["name"] for e in admin_client.get(f"/api/apps/{app_id}/files/tree").json()]
        assert names == ["real.py"]

    def test_tree_sorts_directories_first(self, admin_client, make_app, settings):
        app_id = make_app()
        root = settings.apps_dir / "1" / "demo"
        (root / "zzz_dir").mkdir(exist_ok=True)
        (root / "aaa.py").write_text("x")

        entries = admin_client.get(f"/api/apps/{app_id}/files/tree").json()
        assert [e["name"] for e in entries] == ["zzz_dir", "aaa.py"]

    def test_unknown_app_returns_404(self, admin_client):
        assert admin_client.get("/api/apps/99999/files/tree").status_code == 404


class TestLogs:
    def test_empty_when_no_log_file_exists(self, admin_client, make_app):
        app_id = make_app()
        assert admin_client.get(f"/api/apps/{app_id}/logs").json()["content"] == ""

    def test_returns_log_content(self, admin_client, make_app, settings):
        app_id = make_app()
        log = settings.apps_dir / "1" / "demo" / "logs" / "app.log"
        log.write_text("line one\nline two\n")

        body = admin_client.get(f"/api/apps/{app_id}/logs").json()
        assert "line one" in body["content"]
        assert body["total_lines"] == 2

    def test_truncates_to_the_requested_line_count(self, admin_client, make_app, settings):
        app_id = make_app()
        log = settings.apps_dir / "1" / "demo" / "logs" / "app.log"
        log.write_text("\n".join(f"line {i}" for i in range(500)))

        body = admin_client.get(f"/api/apps/{app_id}/logs?lines=10").json()
        assert body["truncated"] is True
        assert len(body["content"].splitlines()) == 10
        assert "line 499" in body["content"]

    @pytest.mark.parametrize("lines", [0, -1, 10001])
    def test_rejects_out_of_range_line_counts(self, admin_client, make_app, lines):
        app_id = make_app()
        assert admin_client.get(f"/api/apps/{app_id}/logs?lines={lines}").status_code == 422

    def test_clearing_truncates_rather_than_deletes(self, admin_client, make_app, settings):
        """A running process keeps writing to the same descriptor."""
        app_id = make_app()
        log = settings.apps_dir / "1" / "demo" / "logs" / "app.log"
        log.write_text("noise\n")

        assert admin_client.delete(f"/api/apps/{app_id}/logs").status_code == 200
        assert log.exists()
        assert log.read_text() == ""


class TestSerialization:
    def test_local_url_reflects_the_port(self, admin_client, make_app):
        app_id = make_app(port=8080)
        entry = next(a for a in admin_client.get("/api/apps").json() if a["id"] == app_id)
        assert entry["local_url"].endswith(":8080")
        assert entry["url_source"] == "local"

    def test_no_url_without_a_port(self, admin_client, make_app):
        app_id = make_app()
        entry = next(a for a in admin_client.get("/api/apps").json() if a["id"] == app_id)
        assert entry["access_url"] is None

    def test_public_preference_uses_the_external_url(self, admin_client, make_app):
        app_id = make_app(
            port=8080, external_url="https://app.example.com", url_source_pref="public"
        )
        entry = next(a for a in admin_client.get("/api/apps").json() if a["id"] == app_id)
        assert entry["access_url"] == "https://app.example.com"
        assert entry["url_source"] == "cloudflare"

    def test_public_preference_falls_back_when_unpublished(self, admin_client, make_app):
        app_id = make_app(port=8080, url_source_pref="public")
        entry = next(a for a in admin_client.get("/api/apps").json() if a["id"] == app_id)
        assert entry["url_source"] == "local"

    def test_timestamps_are_iso_strings(self, admin_client, make_app):
        app_id = make_app()
        entry = next(a for a in admin_client.get("/api/apps").json() if a["id"] == app_id)
        assert isinstance(entry["created_at"], str)
        assert "T" in entry["created_at"]
