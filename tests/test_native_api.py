import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
from fastapi.testclient import TestClient
from cryptography.fernet import Fernet
import main
from app.secure_store import CredentialCipher, ensure_schema, load_task_context

class NativeApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name)/'kiosk.db')
        ensure_schema(self.db)
        self.cipher = CredentialCipher([Fernet.generate_key().decode()])
        self.values = {}; self.queues = {}
        redis = Mock()
        redis.incr.return_value = 1; redis.ttl.return_value = -2
        redis.exists.side_effect = lambda key: key in self.values
        redis.setex.side_effect = lambda key, seconds, value: self.values.__setitem__(key,value)
        def set_value(key,value,**kwargs):
            if kwargs.get('nx') and key in self.values: return False
            self.values[key]=value; return True
        redis.set.side_effect = set_value
        redis.getdel.side_effect = lambda key: self.values.pop(key,None)
        redis.rpush.side_effect = lambda key,value: self.queues.setdefault(key,[]).append(value)
        self.patches=[patch.object(main,'DB_PATH',self.db), patch.object(main,'r',redis),
                      patch.object(main,'get_cipher',return_value=self.cipher)]
        for p in self.patches: p.start()
        self.client=TestClient(main.app)
    def tearDown(self):
        self.client.close()
        for p in self.patches: p.stop()
        self.tmp.cleanup()

    def start(self):
        response=self.client.post('/api/epic/login/start')
        self.assertEqual(response.status_code,200,response.text)
        return response.json()['login_state']

    def test_login_requires_one_time_local_state(self):
        state=self.start()
        response=self.client.post('/api/epic/login',json={'authorization_code':'a'*32,'login_state':'wrong'})
        self.assertEqual(response.status_code,401)
        session={'account_id':'b'*32,'access_token':'test-access','refresh_token':'test-refresh'}
        with patch.object(main,'authorize_epic',AsyncMock(return_value=('user@example.com',session))):
            response=self.client.post('/api/epic/login',json={'authorization_code':'a'*32,'login_state':state})
            self.assertEqual(response.status_code,200,response.text)
            result=response.json()
            self.assertEqual(result['status'],'queued')
            self.assertNotIn('test-refresh',response.text)
            context=load_task_context(self.db,self.cipher,result['task_id'])
            self.assertEqual(context.auth_method,'epic_oauth')
            self.assertEqual(json.loads(context.password)['refresh_token'],'test-refresh')
            queued=json.loads(self.queues['task_queue'][0])
            self.assertEqual(queued,{'run_id':result['task_id']})
            repeated=self.client.post('/api/epic/login',json={'authorization_code':'a'*32,'login_state':state})
            self.assertEqual(repeated.status_code,401)

    def test_cross_origin_cannot_start_or_submit_login(self):
        for path,body in [('/api/epic/login/start',{}),('/api/epic/login',{'authorization_code':'a'*32,'login_state':'fake'})]:
            response=self.client.post(path,json=body,headers={'Origin':'https://evil.example'})
            self.assertEqual(response.status_code,403)

    def test_validation_never_echoes_submitted_credential(self):
        value='sensitive-test-value'
        response=self.client.post('/api/epic/login',json={'authorization_code':value,'login_state':self.start()})
        self.assertEqual(response.status_code,422)
        self.assertNotIn(value,response.text)

class UnifiedAccountTest(NativeApiTest):
    def connect(self,email='user@example.com',account_id='b'*32):
        session={'account_id':account_id,'access_token':'test-access','refresh_token':'test-refresh',
                 'expires_at':'2030-01-01T00:00:00Z'}
        with patch.object(main,'authorize_epic',AsyncMock(return_value=(email,session))):
            result=self.client.post('/api/epic/login',json={'authorization_code':'a'*32,'login_state':self.start()})
        self.assertEqual(result.status_code,200,result.text)
        return result.json()

    def test_official_authorization_saves_account_before_browser_verification(self):
        from app.secure_store import account_for_token
        result=self.connect()
        self.assertEqual(result['oauth_status'],'authorized')
        self.assertEqual(result['web_session_status'],'pending')
        account=account_for_token(self.db,result['account_access_token'])
        self.assertEqual(account['email'],'user@example.com')
        self.assertEqual(account['auth_method'],'epic_oauth')

    def test_claim_reuses_current_authorized_account_without_password(self):
        result=self.connect()
        self.values.clear()
        response=self.client.post('/api/claim',headers={'Authorization':'Bearer '+result['account_access_token']})
        self.assertEqual(response.status_code,200,response.text)
        context=load_task_context(self.db,self.cipher,response.json()['task_id'])
        self.assertEqual(context.email,'user@example.com')
        self.assertEqual(context.auth_method,'epic_oauth')
        self.assertEqual(context.mode,'claim')

    def test_library_is_account_scoped_and_requires_account_capability(self):
        self.assertEqual(self.client.get('/api/library').status_code,401)
        a=self.connect(); b=self.connect('other@example.com','c'*32)
        async def library(db,cipher,email,client):
            return [{'game':email,'id':'game-id','image':''}]
        with patch.object(main,'account_library',library,create=True):
            for account,email in [(a,'user@example.com'),(b,'other@example.com')]:
                response=self.client.get('/api/library',headers={'Authorization':'Bearer '+account['account_access_token']})
                self.assertEqual(response.status_code,200,response.text)
                self.assertEqual(response.json()['data'][0]['game'],email)

    def test_browser_failure_does_not_remove_authorized_account(self):
        from app.secure_store import account_for_token,update_task
        result=self.connect()
        update_task(self.db,result['task_id'],state='failed',error_type='native_web_session_failed')
        self.assertIsNotNone(account_for_token(self.db,result['account_access_token']))
