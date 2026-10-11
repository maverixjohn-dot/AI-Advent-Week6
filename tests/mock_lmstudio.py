"""Mock LM Studio для тестов без реальной модели.

Имитирует сервер с включённым Require API Key: все запросы требуют
заголовок `Authorization: Bearer test-key`, иначе 401.

Запуск: python tests/mock_lmstudio.py  (слушает 127.0.0.1:1234)
Затем:  python tests/test_app.py
"""
import json
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

API_KEY = "test-key"


class H(BaseHTTPRequestHandler):
    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth_ok(self):
        return self.headers.get("Authorization") == f"Bearer {API_KEY}"

    def do_GET(self):
        if not self._auth_ok():
            self._json({"error": "unauthorized"}, 401)
            return
        if self.path == "/v1/models":
            self._json({"object": "list", "data": [{"id": "qwen3.8-27b", "object": "model"}]})
        elif self.path == "/api/v1/models":
            self._json({"models": [{
                "key": "qwen3.8-27b", "type": "llm", "format": "GGUF",
                "quantization": {"name": "IQ3_S"},
                "size_bytes": 12970000000,
                "max_context_length": 131072, "loaded_context_length": 16384,
                "state": "loaded",
            }]})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._auth_ok():
            self._json({"error": "unauthorized"}, 401)
            return
        if self.path == "/v1/chat/completions":
            length = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(length) or b"{}")
            temp = req.get("temperature", "?")
            last_user = ""
            for m in reversed(req.get("messages", [])):
                if m.get("role") == "user":
                    last_user = m.get("content", "")
                    break
            time.sleep(0.3)  # имитация времени генерации
            if "THINK_TEST" in last_user:
                # рассуждение вшито в content тегами <think>
                self._json({
                    "id": "chatcmpl-mock", "object": "chat.completion", "model": "qwen3.8-27b",
                    "choices": [{"index": 0, "message": {
                        "role": "assistant",
                        "content": "<think>внутреннее рассуждение модели</think>Видимый ответ"},
                        "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 50, "completion_tokens": 100, "total_tokens": 150,
                              "completion_tokens_details": {"reasoning_tokens": 80}},
                })
                return
            if "EMPTY_TEST" in last_user:
                # рассуждение съело весь лимит: content пуст, reasoning в отдельном поле
                self._json({
                    "id": "chatcmpl-mock", "object": "chat.completion", "model": "qwen3.8-27b",
                    "choices": [{"index": 0, "message": {
                        "role": "assistant", "content": "",
                        "reasoning_content": "длинное рассуждение без ответа"},
                        "finish_reason": "length"}],
                    "usage": {"prompt_tokens": 50, "completion_tokens": 512, "total_tokens": 562,
                              "completion_tokens_details": {"reasoning_tokens": 512}},
                })
                return
            answer = (f"Суть: mock-ответ модели qwen3.8-27b при temperature={temp}.\n\n"
                      f"- пункт первый\n- пункт второй\n- max_tokens={req.get('max_tokens')}")
            self._json({
                "id": "chatcmpl-mock", "object": "chat.completion", "model": "qwen3.8-27b",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": answer,
                                                     "reasoning_content": "сначала подумал о вопросе"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 120, "completion_tokens": 64, "total_tokens": 184},
            })
        else:
            self._json({"error": "not found"}, 404)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print("Mock LM Studio на http://127.0.0.1:1234, API-ключ: test-key")
    HTTPServer(("127.0.0.1", 1234), H).serve_forever()
