import json
import asyncio
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from cryptography.fernet import Fernet
from app import secure_store as store

from app import epic_native_auth as native

CODE = 'a' * 32
ACCOUNT_ID = 'b' * 32
SESSION = {'account_id': ACCOUNT_ID, 'access_token': 'test-access', 'refresh_token': 'test-refresh',
           'expires_at': '2030-01-01T00:00:00Z', 'refresh_expires_at': '2030-02-01T00:00:00Z'}

class NativeProtocolTest(unittest.IsolatedAsyncioTestCase):
    def test_login_url_sends_user_to_epic(self):
        url = urlparse(native.login_url())
        self.assertEqual(url.hostname, 'www.epicgames.com')
        callback = urlparse(parse_qs(url.query)['redirectUrl'][0])
        self.assertEqual(callback.path, '/id/api/redirect')
        self.assertEqual(parse_qs(callback.query)['responseType'], ['code'])

    def test_accepts_code_and_epic_json_but_rejects_otp_or_empty_code(self):
        self.assertEqual(native.parse_authorization_code(CODE), CODE)
        self.assertEqual(native.parse_authorization_code(json.dumps({'authorizationCode': CODE})), CODE)
        for value in ['123456', '', '{"authorizationCode":null}', '{"sid":"secret"}']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                native.parse_authorization_code(value)

    async def test_exchanges_code_and_uses_verified_account_email(self):
        def upstream(request):
            if request.url.path.endswith('/oauth/token'):
                self.assertEqual(parse_qs(request.content.decode())['code'], [CODE])
                return httpx.Response(200, json=SESSION)
            self.assertEqual(request.headers['authorization'], 'Bearer test-access')
            return httpx.Response(200, json={'id': ACCOUNT_ID, 'email': 'USER@example.com'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            email, session = await native.authorize(CODE, client)
        self.assertEqual(email, 'user@example.com')
        self.assertEqual(session['refresh_token'], 'test-refresh')

    async def test_rejects_account_mismatch(self):
        def upstream(request):
            return httpx.Response(200, json=SESSION if request.url.path.endswith('/oauth/token')
                                  else {'id': 'c' * 32, 'email': 'other@example.com'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            with self.assertRaises(native.NativeAuthError):
                await native.authorize(CODE, client)

    async def test_expired_code_does_not_expose_upstream_secrets(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(
                400, json={'errorCode': 'expired', 'errorMessage': 'secret-access-value'}))) as client:
            with self.assertRaises(native.NativeAuthError) as caught:
                await native.authorize(CODE, client)
        self.assertNotIn('secret-access-value', str(caught.exception))
        self.assertNotIn(CODE, str(caught.exception))

    async def test_refresh_requires_same_epic_account(self):
        def upstream(request):
            self.assertEqual(parse_qs(request.content.decode())['grant_type'], ['refresh_token'])
            return httpx.Response(200, json={**SESSION, 'refresh_token': 'rotated-refresh'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            updated = await native.refresh(SESSION, client)
        self.assertEqual(updated['refresh_token'], 'rotated-refresh')
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(
                200, json={**SESSION, 'account_id': 'c' * 32}))) as client:
            with self.assertRaises(native.NativeAuthError):
                await native.refresh(SESSION, client)

    async def test_web_exchange_uses_only_fixed_epic_destinations(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(
                200, json={'code': CODE}))) as client:
            url = urlparse(await native.web_exchange_url(SESSION, client))
        self.assertEqual(url.hostname, 'www.epicgames.com')
        self.assertEqual(url.path, '/id/exchange')
        self.assertEqual(parse_qs(url.query)['exchangeCode'], [CODE])
        self.assertEqual(parse_qs(url.query)['redirectUrl'], ['https://store.epicgames.com/en-US/free-games'])

class NativeStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name)/'test.db')
        self.cipher = store.CredentialCipher([Fernet.generate_key().decode()])
        store.ensure_schema(self.db)
    def tearDown(self):
        self.tmp.cleanup()

    def test_native_credential_survives_confirmation_and_rotation_without_password_login(self):
        run_id, token = store.create_task_run(self.db, self.cipher, 'user@example.com', 'verify',
                                             password=json.dumps(SESSION), auth_method='epic_oauth')
        context = store.load_task_context(self.db, self.cipher, run_id)
        self.assertEqual(context.auth_method, 'epic_oauth')
        rotated = json.dumps({**SESSION, 'refresh_token': 'rotated-refresh'})
        store.replace_native_credential(self.db, self.cipher, run_id, rotated)
        store.update_task(self.db, run_id, state='succeeded')
        store.confirm_pending_account(self.db, run_id, token)
        row = store.get_account(self.db, 'user@example.com')
        self.assertEqual(row['auth_method'], 'epic_oauth')
        self.assertIsNone(row['password'])
        self.assertNotIn('rotated-refresh', row['credential_ciphertext'])
        self.assertFalse(store.verify_account_password(self.db, self.cipher, 'user@example.com', rotated))
        claim_id, _ = store.create_task_run(self.db, self.cipher, 'user@example.com', 'claim')
        self.assertEqual(store.load_task_context(self.db, self.cipher, claim_id).password, rotated)
        store.replace_native_credential(self.db, self.cipher, claim_id, json.dumps(SESSION))
        self.assertEqual(store.load_task_context(self.db, self.cipher, claim_id).password, json.dumps(SESSION))

    def test_legacy_password_accounts_still_work(self):
        run_id, token = store.create_task_run(self.db, self.cipher, 'user@example.com', 'verify', password='test-password')
        store.update_task(self.db, run_id, state='succeeded')
        store.confirm_pending_account(self.db, run_id, token)
        self.assertTrue(store.verify_account_password(self.db, self.cipher, 'user@example.com', 'test-password'))
        self.assertEqual(store.load_task_context(self.db, self.cipher, run_id).auth_method, 'password')

class NativeBrowserTest(unittest.IsolatedAsyncioTestCase):
    async def test_rotated_credentials_are_saved_before_browser_failure_and_secret_url_is_hidden(self):
        from unittest.mock import AsyncMock
        with tempfile.TemporaryDirectory() as tmp:
            db=str(Path(tmp)/'test.db'); store.ensure_schema(db)
            cipher=store.CredentialCipher([Fernet.generate_key().decode()])
            run_id,_=store.create_task_run(db,cipher,'user@example.com','verify',
                password=json.dumps(SESSION),auth_method='epic_oauth')
            def upstream(request):
                if request.url.path.endswith('/oauth/token'):
                    return httpx.Response(200,json={**SESSION,'refresh_token':'rotated-refresh'})
                return httpx.Response(200,json={'code': CODE})
            page=type('Page',(),{})()
            page.goto=AsyncMock(side_effect=RuntimeError('failed URL with secret '+CODE))
            self.assertTrue(hasattr(native,'bootstrap_browser'),'Missing token-to-browser login')
            async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
                with self.assertRaises(native.NativeAuthError) as caught:
                    await native.bootstrap_browser(page,SESSION,client,db,run_id,cipher)
            self.assertNotIn(CODE,str(caught.exception))
            saved=json.loads(store.load_task_context(db,cipher,run_id).password)
            self.assertEqual(saved['refresh_token'],'rotated-refresh')

class NativeRedirectRegressionTest(unittest.IsolatedAsyncioTestCase):
    async def run_browser(self, page):
        with tempfile.TemporaryDirectory() as tmp:
            db=str(Path(tmp)/'test.db'); store.ensure_schema(db)
            cipher=store.CredentialCipher([Fernet.generate_key().decode()])
            run_id,_=store.create_task_run(db,cipher,'user@example.com','verify',
                password=json.dumps(SESSION),auth_method='epic_oauth')
            def upstream(request):
                return httpx.Response(200,json=SESSION if request.url.path.endswith('/oauth/token') else {'code':CODE})
            async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
                await native.bootstrap_browser(page,SESSION,client,db,run_id,cipher)

    async def test_spa_exchange_is_not_interrupted_before_its_async_redirect(self):
        events=[]
        class Page:
            async def goto(self,url,**kwargs):
                events.append('document_loaded')
                if len(events)>1:
                    raise AssertionError('Navigation cancelled the SPA exchange before redirect')
                return type('Response',(),{'status':200})()
            async def wait_for_url(self,predicate,**kwargs):
                events.append('exchange_completed')
                self_test.assertFalse(predicate('https://www.epicgames.com/id/exchange?exchangeCode='+CODE))
                self_test.assertFalse(predicate('https://evil.example/free-games'))
                self_test.assertTrue(predicate(native.STORE_URL))
        self_test=self
        await self.run_browser(Page())
        self.assertEqual(events,['document_loaded','exchange_completed'])

    async def test_missing_web_redirect_is_not_reported_as_expired_authorization(self):
        from unittest.mock import AsyncMock
        page=type('Page',(),{})()
        page.goto=AsyncMock(return_value=type('Response',(),{'status':200})())
        page.wait_for_url=AsyncMock(side_effect=TimeoutError('URL contains secret '+CODE))
        with self.assertRaises(native.NativeAuthError) as caught:
            await self.run_browser(page)
        self.assertEqual(caught.exception.error_type,'native_web_session_failed')
        self.assertEqual(caught.exception.stage,'web_redirect')
        self.assertNotIn(CODE,str(caught.exception))

    async def test_refresh_transport_failure_is_not_expired_token(self):
        def upstream(request):
            raise httpx.ReadTimeout('secret '+CODE, request=request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            with self.assertRaises(native.NativeAuthError) as caught:
                await native.refresh(SESSION,client)
        self.assertEqual(caught.exception.error_type,'network_timeout')
        self.assertEqual(caught.exception.stage,'refresh_token')
        self.assertNotIn(CODE,str(caught.exception))

class NativeRecoveryTest(unittest.TestCase):
    def test_web_session_failure_retains_encrypted_credentials_for_retry_but_expiry_removes_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=str(Path(tmp)/'test.db'); store.ensure_schema(db)
            cipher=store.CredentialCipher([Fernet.generate_key().decode()])
            run_id,_=store.create_task_run(db,cipher,'user@example.com','verify',
                password=json.dumps(SESSION),auth_method='epic_oauth')
            store.update_task(db,run_id,state='failed',error_type='native_web_session_failed')
            self.assertEqual(json.loads(store.load_task_context(db,cipher,run_id).password),SESSION)
            store.update_task(db,run_id,state='failed',error_type='native_auth_expired')
            with self.assertRaises(store.CredentialError):
                store.load_task_context(db,cipher,run_id)

class NativeLibraryTest(unittest.IsolatedAsyncioTestCase):
    async def test_catalog_requests_overlap_with_a_bounded_limit_and_stable_order(self):
        active=0; peak=0
        async def upstream(request):
            nonlocal active, peak
            if '/library/' in request.url.path:
                return httpx.Response(200,json={'records':[{'appName':str(i),'namespace':str(i),'catalogItemId':str(i)} for i in range(20)]})
            active+=1; peak=max(peak,active)
            await asyncio.sleep(0.01)
            active-=1
            item=request.url.params['id']
            return httpx.Response(200,json={item:{'title':'Game '+item}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            games=await native.list_library(SESSION,client)
        self.assertGreater(peak,1)
        self.assertLessEqual(peak,8)
        self.assertEqual([g['game'] for g in games],['Game '+str(i) for i in range(20)])

    async def test_library_paginates_and_uses_catalog_titles_without_returning_tokens(self):
        calls=[]
        def upstream(request):
            calls.append(request)
            self.assertEqual(request.headers['authorization'],'Bearer test-access')
            if '/library/' in request.url.path:
                cursor=request.url.params.get('cursor')
                return httpx.Response(200,json={'records':[{'appName':'app2' if cursor else 'app1','namespace':'ns','catalogItemId':'item2' if cursor else 'item1'}],
                                               'responseMetadata':{} if cursor else {'nextCursor':'next'}})
            return httpx.Response(200,json={'item1':{'title':'First Game','keyImages':[]},'item2':{'title':'Second Game','keyImages':[]}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            games=await native.list_library(SESSION,client)
        self.assertEqual([x['game'] for x in games],['First Game','Second Game'])
        self.assertNotIn('test-access',json.dumps(games))
        self.assertEqual(len([c for c in calls if '/library/' in c.url.path]),2)

class NativeRefreshRecoveryTest(unittest.IsolatedAsyncioTestCase):
    async def test_library_refreshes_rejected_access_token_and_preserves_account_on_web_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=str(Path(tmp)/'test.db'); store.ensure_schema(db)
            cipher=store.CredentialCipher([Fernet.generate_key().decode()])
            store.save_native_account(db,cipher,'user@example.com',SESSION)
            refresh_calls=[]
            def upstream(request):
                if request.url.path.endswith('/oauth/token'):
                    refresh_calls.append(request)
                    return httpx.Response(200,json={**SESSION,'access_token':'new-access','refresh_token':'new-refresh'})
                if request.headers.get('authorization')=='Bearer test-access':
                    return httpx.Response(401,json={'errorCode':'errors.com.epicgames.account.auth_token.invalid_token'})
                return httpx.Response(200,json={'records':[],'responseMetadata':{}})
            async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
                self.assertEqual(await native.account_library(db,cipher,'user@example.com',client),[])
            self.assertEqual(len(refresh_calls),1)
            saved=json.loads(cipher.decrypt(store.get_account(db,'user@example.com')['credential_ciphertext']))
            self.assertEqual(saved['refresh_token'],'new-refresh')
