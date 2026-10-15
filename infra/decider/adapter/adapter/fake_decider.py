"""A stand-in for `strands-decider serve`: no GPU, no model. For tests and the local Docker smoke run.

Like the real server it handles ONE request at a time (single-threaded) and offers `/health` and
`POST /v1/systemone`. Every question id it receives gets a fixed answer.

Environment: DECIDER_UPSTREAM_PORT (default 8099), FAKE_DELAY_S (per request), FAKE_STARTUP_S (health
answers 503 until this many seconds after start).
"""

from __future__ import annotations

import json
import os
import time
from http.server import BaseHTTPRequestHandler, HTTPServer


def answer_for(question: dict) -> dict:
    kind = question.get("type")
    if kind == "noul":
        return {"p_true": 0.5}
    if kind == "choice":
        options = list(question.get("criteria", {}))
        return {"label": options[0] if options else None, "confidence": 0.9}
    return {"value": 0.5}


class Handler(BaseHTTPRequestHandler):
    started = time.monotonic()
    startup_s = float(os.environ.get("FAKE_STARTUP_S", "0"))
    delay_s = float(os.environ.get("FAKE_DELAY_S", "0"))

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        if self.path != "/health":
            return self._send(404, {"error": "not_found"})
        if time.monotonic() - self.started < self.startup_s:
            return self._send(503, {"status": "loading"})
        self._send(200, {"status": "ok"})

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        if self.path != "/v1/systemone":
            return self._send(404, {"error": "not_found"})
        raw = self.rfile.read(int(self.headers.get("content-length", 0)))
        try:
            request = json.loads(raw)
            questions = request["questions"]
            if not isinstance(request["state"], str) or not isinstance(questions, dict):
                raise TypeError
        except (ValueError, KeyError, TypeError):
            return self._send(422, {"detail": "expected {state: str, questions: object}"})
        started = time.monotonic()
        time.sleep(self.delay_s)
        self._send(
            200,
            {
                "answers": {qid: answer_for(q) for qid, q in questions.items()},
                "usage": {"input_tokens": len(request["state"].split())},
                "latency_ms": round((time.monotonic() - started) * 1000, 1),
            },
        )

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        pass


def main() -> None:
    port = int(os.environ.get("DECIDER_UPSTREAM_PORT", "8099"))
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
