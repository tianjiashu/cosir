"""真实 OAuth HTTP 边界与本地凭据持久化，所有 token 都是测试值。"""

import asyncio
import base64
import hashlib
import json
import stat
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.core.llm_provider.claude.credentials import ClaudeCredentialsManager
from app.core.llm_provider.claude.errors import ClaudeAPIError
from app.models.claude_credentials import ClaudeCredentials
from app.storage.claude_credentials import ClaudeCredentialStore


def manager(tmp_path, handler):
    store = ClaudeCredentialStore(tmp_path / 'credentials' / 'claude.json')
    return ClaudeCredentialsManager(
        store,
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def token_reply(access='access-one', refresh='refresh-one'):
    return {'access_token': access, 'refresh_token': refresh, 'expires_in': 3600,
            'scope': 'user:profile user:inference', 'token_type': 'Bearer',
            'account': {'email_address': 'local@example.test'}}


@pytest.mark.asyncio
async def test_pkce_exchange_persists_credentials_and_survives_new_manager(tmp_path):
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=token_reply())
    auth = manager(tmp_path, handler)
    login = await auth.begin_login()
    query = parse_qs(urlsplit(login.authorization_url).query)
    assert query['code_challenge_method'] == ['S256']
    assert query['scope'] == ['user:profile user:inference']
    assert 'code_verifier' not in query
    result = await auth.complete_login(login.login_id, 'one-time-code#' + query['state'][0])
    assert result.status == 'connected' and result.can_refresh
    assert 'access-one' not in result.model_dump_json()
    body = requests[0]
    assert body['grant_type'] == 'authorization_code' and body['code'] == 'one-time-code'
    challenge = base64.urlsafe_b64encode(hashlib.sha256(body['code_verifier'].encode()).digest())
    assert challenge.decode().rstrip('=') == query['code_challenge'][0]
    assert body['state'] == query['state'][0]
    assert stat.S_IMODE(auth.store.path.stat().st_mode) == 0o600
    fresh = manager(tmp_path, handler)
    assert await fresh.access_token() == 'access-one'
    assert len(requests) == 1
    assert 'access-one' not in repr(fresh.store.load())
    await fresh.logout()
    assert (await fresh.status()).status == 'logged_out'
    assert not fresh.store.path.exists()


@pytest.mark.asyncio
async def test_wrong_state_expired_and_consumed_login_never_exchange(tmp_path):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=token_reply())
    auth = manager(tmp_path, handler)
    login = await auth.begin_login()
    with pytest.raises(ClaudeAPIError):
        await auth.complete_login(login.login_id, 'code#wrong-state')
    assert not requests and auth.store.load() is None
    await auth.cancel_login(login.login_id)
    with pytest.raises(ClaudeAPIError):
        await auth.complete_login(login.login_id, 'code#wrong-state')
    assert not requests


@pytest.mark.asyncio
async def test_refresh_is_single_flight_and_rotated_token_is_persisted(tmp_path):
    requests = []
    async def handler(request):
        requests.append(json.loads(request.content))
        await asyncio.sleep(0)
        return httpx.Response(200, json=token_reply('access-new', 'refresh-new'))
    auth = manager(tmp_path, handler)
    auth.store.save(ClaudeCredentials(access_token='expired', refresh_token='refresh-old',
        expires_at=time.time()-1, scopes=['user:inference']))
    assert await asyncio.gather(*(auth.access_token() for _ in range(5))) == ['access-new'] * 5
    assert len(requests) == 1
    assert requests[0]['refresh_token'] == 'refresh-old'
    assert auth.store.load().refresh_token.get_secret_value() == 'refresh-new'
    assert (await auth.status()).status == 'connected'


@pytest.mark.asyncio
async def test_logout_waits_for_refresh_and_cannot_resurrect_credentials(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    async def handler(_):
        entered.set()
        await release.wait()
        return httpx.Response(200, json=token_reply())
    auth = manager(tmp_path, handler)
    auth.store.save(ClaudeCredentials(access_token='expired', refresh_token='refresh-old',
        expires_at=time.time()-1, scopes=['user:inference']))
    refresh = asyncio.create_task(auth.access_token())
    await entered.wait()
    logout = asyncio.create_task(auth.logout())
    release.set()
    await asyncio.gather(refresh, logout)
    assert auth.store.load() is None


@pytest.mark.asyncio
async def test_failed_refresh_preserves_credentials_and_returns_safe_error(tmp_path):
    auth = manager(tmp_path, lambda _: httpx.Response(400, json={
        'error': 'invalid_grant', 'error_description': 'refresh-old private-value'}))
    auth.store.save(ClaudeCredentials(access_token='expired', refresh_token='refresh-old',
        expires_at=time.time()-1, scopes=['user:inference']))
    with pytest.raises(ClaudeAPIError) as captured:
        await auth.access_token()
    assert captured.value.status_code == 401
    assert 'refresh-old' not in str(captured.value)
    assert auth.store.load().refresh_token.get_secret_value() == 'refresh-old'
    assert (await auth.status()).status == 'expired'


def test_corrupt_store_never_echoes_credentials(tmp_path):
    store = ClaudeCredentialStore(tmp_path/'bad.json')
    store.path.write_text('{"access_token":"secret"}')
    with pytest.raises(ValueError) as captured:
        store.load()
    assert 'secret' not in str(captured.value)
