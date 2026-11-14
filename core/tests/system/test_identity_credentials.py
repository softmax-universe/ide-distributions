"""A workspace derives its own cogent token; restricted conversations cannot borrow it."""

import json
import threading
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

import pytest
from tests.authority_service import authority_service

from research_core import layout
from research_core.authority import AuthorityDenied, identity
from research_core.platform import from_env
from research_core.platform.identity import player_token, user_token
from research_core.store import sessions
from research_core.store.actors import Actor
from research_core.store.db import Store


def test_workspace_token_and_cached_clients_cannot_escape_a_restricted_conversation(tmp_path):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            requests.append(
                (
                    self.path,
                    self.headers["Authorization"],
                    json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
                )
            )
            data = json.dumps(
                {"access_token": "scoped-game-token", "token_type": "Bearer", "expires_in": 300}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server, authority_service() as grants:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        workspace_id = uuid4()
        env = {
            "COGAMES_API_URL": f"http://127.0.0.1:{server.server_port}",
            "IDE_WORKSPACE_ID": str(workspace_id),
            "IDE_WORKSPACE": str(tmp_path),
            "SOFTMAX_PLAYER_CREDENTIAL": "workspace-bridge",
            "SOFTMAX_USER_CREDENTIAL": "owner-secret",
            "SOFTMAX_CONFIG_DIR": str(tmp_path / "credentials"),
        }
        try:
            with closing(from_env(env)) as platform:
                token = platform.identity_token("https://game.example/api", "play")
                assert token.access_token == "scoped-game-token"
                assert requests == [
                    (
                        f"/observatory/v2/workspaces/{workspace_id}/identity/token",
                        "Bearer workspace-bridge",
                        {"resource": "https://game.example/api", "scope": "play"},
                    )
                ]
                ws = layout.from_env(env)
                ws.ensure_dirs()
                with Store.open(ws.store_path, kind="workspace") as store:
                    sessions.register(
                        store,
                        Actor.harness(),
                        session_id="restricted-session",
                        profile="restricted",
                        kind="chat",
                        authority=identity(grants.issue()).model_dump(mode="json"),
                    )
                env["IDE_SESSION_ID"] = "restricted-session"
                for action in (
                    lambda: platform.identity_token("https://game.example/api", "play"),
                    lambda: platform.request("POST", "/owner-effect"),
                    lambda: player_token(env),
                    lambda: user_token(env),
                ):
                    with pytest.raises(AuthorityDenied):
                        action()
                assert len(requests) == 1
        finally:
            server.shutdown()
            thread.join(timeout=5)
