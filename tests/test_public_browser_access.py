import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID
from starlette.requests import Request
from fastapi import HTTPException
from pydantic import SecretStr
from dashboard.app.security import require_crm_principal, require_crm_command_access

S = SimpleNamespace(workspace_id=UUID(int=1),actor_id=UUID(int=2),username='test',password=SecretStr('test'),permissions=frozenset({'crm:read','crm:lead-stage:write'}),is_admin=True)
def request(headers=None):
    return Request({'type':'http','method':'GET','path':'/','headers':[(k.lower().encode(),v.encode()) for k,v in (headers or {}).items()]})
class PublicBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_still_requires_auth(self):
        with patch.dict(os.environ,{'CRM_PUBLIC_BROWSER_ACCESS':'false'}),patch('dashboard.app.security.get_principal_settings',return_value=S):
            with self.assertRaises(HTTPException) as error: await require_crm_principal(request())
            self.assertEqual(error.exception.status_code,401)
    async def test_explicit_public_browser_no_admin(self):
        with patch.dict(os.environ,{'CRM_PUBLIC_BROWSER_ACCESS':'true'}),patch('dashboard.app.security.get_principal_settings',return_value=S):
            principal=await require_crm_principal(request())
            self.assertEqual(principal.subject,'public-browser')
            self.assertEqual(principal.workspace_id,S.workspace_id)
            self.assertEqual(principal.permissions,S.permissions)
            self.assertFalse(principal.is_admin)
    async def test_explicit_basic_identity_preserved(self):
        with patch.dict(os.environ,{'CRM_PUBLIC_BROWSER_ACCESS':'true'}),patch('dashboard.app.security.get_principal_settings',return_value=S):
            principal=await require_crm_principal(request({'Authorization':'Basic dGVzdDp0ZXN0'}))
            self.assertEqual(principal.subject,'test')
            self.assertTrue(principal.is_admin)
    async def test_public_write_still_requires_csrf_origin(self):
        with patch.dict(os.environ,{'CRM_PUBLIC_BROWSER_ACCESS':'true'}),patch('dashboard.app.security.get_principal_settings',return_value=S),patch('dashboard.app.security.get_settings',return_value=SimpleNamespace(csrf_token='test-csrf',allowed_write_origins=('https://example.invalid',))):
            principal=await require_crm_principal(request())
            with self.assertRaises(HTTPException) as error: await require_crm_command_access(request(),principal)
            self.assertEqual(error.exception.status_code,403)
if __name__=='__main__':unittest.main()
