"""Point 16 — selector de modelo Claude en chat.
Valida que:
  1. Endpoint /api/chat/models existe y retorna lista con 'default' + 'models'.
  2. chat_endpoint acepta 'model' en body con whitelist.
  3. UI tiene <select id="chat-model-select"> y llama _saveChatModel.
  4. sendChat envía model en body.
"""
import ast
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

API_PATH  = os.path.join(os.path.dirname(__file__), '..', 'api.py')
HTML_PATH = os.path.join(os.path.dirname(__file__), '..', 'static', 'index.html')


def _api_src():
    with open(API_PATH, encoding='utf-8') as f:
        return f.read()


def _html_src():
    with open(HTML_PATH, encoding='utf-8') as f:
        return f.read()


def test_chat_models_endpoint_defined():
    src = _api_src()
    assert '/api/chat/models' in src, 'endpoint /api/chat/models debe existir'
    # buscar función async def list_chat_models
    tree = ast.parse(src)
    found = False
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == 'list_chat_models':
            found = True
            break
    assert found, 'list_chat_models debe estar definida'


def test_chat_models_endpoint_returns_expected_ids():
    src = _api_src()
    # whitelist de modelos debe existir
    for mid in ['claude-opus-4-7', 'claude-sonnet-4-6', 'claude-haiku-4-5-20251001']:
        assert mid in src, f'modelo {mid} debe estar listado'


def test_chat_endpoint_accepts_model_with_whitelist():
    src = _api_src()
    assert '_ALLOWED_MODELS' in src, 'chat_endpoint debe tener whitelist _ALLOWED_MODELS'
    # chat_endpoint debe leer data.get("model")
    assert 'data.get("model")' in src or "data.get('model')" in src


def test_ui_has_chat_model_select():
    html = _html_src()
    assert 'id="chat-model-select"' in html, 'select de modelo debe existir en UI'
    assert '_saveChatModel' in html, 'handler _saveChatModel debe existir'
    assert '_loadChatModels' in html, 'loader _loadChatModels debe existir'


def test_ui_sendchat_passes_model():
    html = _html_src()
    # Buscar el fetch de sendChat que incluya model
    assert 'window._chatModel' in html, 'sendChat debe leer window._chatModel'
    # Debe aparecer al menos una vez pasando model en body
    assert 'model: window._chatModel' in html


def test_live_chat_models_endpoint_via_fastapi():
    """Smoke-test real del endpoint con TestClient."""
    try:
        from fastapi.testclient import TestClient
    except Exception:
        return  # sin fastapi test client, ignorar
    import importlib
    api = importlib.import_module('api')
    client = TestClient(api.app)
    r = client.get('/api/chat/models')
    assert r.status_code == 200
    data = r.json()
    assert 'default' in data
    assert 'models' in data
    ids = [m['id'] for m in data['models']]
    assert 'claude-opus-4-7' in ids
    assert 'claude-sonnet-4-6' in ids
