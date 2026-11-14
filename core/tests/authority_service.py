"""A local product authorization endpoint for runtime boundary scenarios."""

import json
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import UUID, uuid4

from research_core.authority import RestrictedAuthority


class AuthorityService:
    def __init__(self) -> None:
        self.env: dict[str, str] = {}
        self.proofs: dict[str, dict] = {}
        self.redirect_to: str | None = None
        self.unavailable = False
        self.checks: list[dict] = []
        self.check_barriers: dict[str, threading.Barrier] = {}

    def issue(
        self,
        *,
        client_id: str = "game-a",
        subject: str = "cogent-a",
        subject_type: str = "cogent",
        player_id: str | None = None,
        grant_id: UUID | None = None,
        expires_at: int | None = None,
    ) -> RestrictedAuthority:
        proof = RestrictedAuthority(
            issuer="https://identity.example",
            subject=subject,
            subject_type=subject_type,
            client_id=client_id,
            player_id=player_id or f"ply_{uuid4()}",
            grant_id=grant_id or uuid4(),
            expires_at=int(time.time()) + 300 if expires_at is None else expires_at,
            authorization_id=uuid4(),
        )
        self.proofs[str(proof.authorization_id)] = proof.model_dump(mode="json")
        return proof

    def revoke(self, proof: RestrictedAuthority) -> None:
        del self.proofs[str(proof.authorization_id)]


@contextmanager
def authority_service():
    service = AuthorityService()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            service.checks.append(request)
            barrier = service.check_barriers.pop(request["authorization_id"], None)
            if barrier is not None:
                barrier.wait(timeout=5)
            if service.unavailable:
                self.send_response(503)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if service.redirect_to is not None:
                self.send_response(302)
                self.send_header("Location", service.redirect_to)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            active = (
                self.headers["Authorization"] == "Bearer workspace-bridge"
                and service.proofs.get(request["authorization_id"]) == request
            )
            response = json.dumps({"active": active}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def do_GET(self):  # noqa: N802
            service.checks.append({"method": "GET"})
            response = b'{"active":false}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, *_):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        service.env = {
            "IDE_AUTHORITY_CHECK_URL": f"http://127.0.0.1:{server.server_port}/authority/check",
            "SOFTMAX_PLAYER_CREDENTIAL": "workspace-bridge",
        }
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield service
        finally:
            server.shutdown()
            thread.join(timeout=5)
