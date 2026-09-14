import base64
from email import policy
from email.parser import BytesParser
from src.crm.connectors.note_drafts import create_verified_draft,GmailDraftProvider
from tests.test_hourly_drafts import Fake

class GmailRewritesID(Fake):
    def create(self,raw):
        msg=BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(raw))
        msg.replace_header('Message-ID','<provider@gmail.com>')
        return super().create(base64.urlsafe_b64encode(msg.as_bytes()).decode())

def test_gmail_rewritten_id_keeps_verified_content_and_replay(tmp_path):
    p=GmailRewritesID();args=dict(provider=p,journal=tmp_path/'journal',key='release-test',mailbox='sales@example.test',recipient='lead@example.test',subject='Test',body_html='<p>Olá</p>')
    r=create_verified_draft(**args)
    assert create_verified_draft(**args)==r
    assert p.creates==1
    assert r['sent'] is False

def test_lookup_recovers_marker_even_if_message_id_changed():
    p=GmailRewritesID();from email.message import EmailMessage
    msg=EmailMessage();msg['Message-ID']='<old@test>';msg['X-CRM-Obligation']='marker';msg.set_content('Test')
    p.create(base64.urlsafe_b64encode(msg.as_bytes()).decode())
    provider=object.__new__(GmailDraftProvider)
    def request(method,path,**kwargs):
        assert method=='GET'
        if path=='/drafts':return {'drafts':[]} if 'q' in kwargs.get('params',{}) else {'drafts':[{'id':'d1'}]}
        if path=='/drafts/d1':return p.get('d1')
        raise AssertionError(path)
    provider.request=request
    assert [d['id'] for d in provider.lookup('marker')]==['d1']
