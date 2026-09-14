"""Draft-only Gmail boundary. No send/update/delete operation is exposed.

Journal first, exact Message-ID recovery, raw MIME readback. Uncertain means stop,
not retry-create. This module is called only by the canonical server.
"""
import base64
import email.policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parseaddr
import hashlib
from html import escape
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import quote

BASE='https://gmail.googleapis.com/gmail/v1/users/me'


def valid_address(value):
    if type(value) is not str or not value.isascii() or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,}",value):
        raise ValueError('Literal email is invalid or requires review')
    local,domain=value.split('@')
    if '..' in value or local.startswith('.') or local.endswith('.') or len(value)>254:
        raise ValueError('Literal email is invalid')
    return value


def persist(path,data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd,tmp=tempfile.mkstemp(dir=path.parent,prefix='.draft-')
    try:
        with os.fdopen(fd,'w') as stream:
            json.dump(data,stream,ensure_ascii=False);stream.flush();os.fsync(stream.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


class _TextHTML(HTMLParser):
    """Re-emit only formatting tags and escaped text; never raw input markup.

    No attributes/links/resources/styles, foreign namespaces, comments or
    declarations. Existing mailbox signatures are a separate provider-owned
    input; this policy applies to the untrusted model-authored body only.
    """
    TAGS = frozenset({'p', 'br', 'div', 'span', 'strong', 'b', 'em', 'i', 'u', 'ul', 'ol', 'li'})

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.stack = []

    def handle_starttag(self, tag, attrs):
        if tag not in self.TAGS or attrs:
            raise ValueError('Unsafe HTML body')
        self.parts.append('<' + tag + '>')
        if tag != 'br':
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1] != tag:
            raise ValueError('Unsafe HTML body')
        self.stack.pop()
        self.parts.append('</' + tag + '>')

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag != 'br':
            self.handle_endtag(tag)

    def handle_data(self, data):
        self.parts.append(escape(data, quote=False))

    def handle_comment(self, data):
        raise ValueError('Unsafe HTML body')

    def handle_decl(self, decl):
        raise ValueError('Unsafe HTML body')

    def unknown_decl(self, data):
        raise ValueError('Unsafe HTML body')

    def handle_pi(self, data):
        raise ValueError('Unsafe HTML body')


def safe_body_html(value):
    if type(value) is not str or not value.strip() or len(value) > 20000:
        raise ValueError('Invalid body')
    parser = _TextHTML()
    try:
        parser.feed(value)
        parser.close()
    except (AssertionError, ValueError):
        raise ValueError('Unsafe HTML body') from None
    if parser.stack:
        raise ValueError('Unsafe HTML body')
    return ''.join(parser.parts)


def create_verified_draft(*,provider,journal,key,mailbox,recipient,subject,body_html,since=None,reconcile_only=False):
    valid_address(mailbox);valid_address(recipient)
    if type(subject) is not str or not subject.strip() or len(subject)>240 or any(x in subject for x in '\r\n'):
        raise ValueError('Invalid subject')
    body_html = safe_body_html(body_html)
    payload={'mailbox':mailbox,'recipient':recipient,'subject':subject,'body_html':body_html}
    fingerprint=hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    marker=hashlib.sha256(key.encode()).hexdigest()+'@crm.zelusottomayor.com'
    journal=Path(journal)
    old=json.loads(journal.read_text()) if journal.exists() else None
    if reconcile_only and not old:raise ValueError('No recorded intent; audit cannot create')
    if old and old['fingerprint']!=fingerprint:raise ValueError('Draft obligation content changed; review required')
    if provider.profile()!=mailbox:raise ValueError('Mailbox identity mismatch')
    if old:
        raw=old['raw']
        matches=[provider.get(old['draft_id'])] if old.get('draft_id') else provider.lookup(marker)
        if not matches:raise ValueError('Previous draft outcome unresolved or deleted; no duplicate created')
    else:
        # Recover a provider object even when a local disk journal was lost.
        matches=provider.lookup(marker)
        if matches:raise ValueError('Existing provider marker without local intent; review required')
        message=EmailMessage(policy=email.policy.SMTP)
        message['From']=mailbox;message['To']=recipient;message['Subject']=subject;message['Message-ID']='<'+marker+'>'
        message['X-CRM-Obligation']=marker
        thread=provider.thread(recipient) if hasattr(provider,'thread') else None
        provider.preflight(recipient,since,thread)
        if thread:
            message['In-Reply-To']=thread['message_id'];message['References']=thread['message_id']
            message.replace_header('Subject',thread['subject'])
        signature=provider.signature()
        if not signature:raise ValueError('Existing mailbox signature unavailable')
        message.set_content('Mensagem HTML.');message.add_alternative('<div style="font-family:Verdana,sans-serif">'+body_html+signature+'</div>',subtype='html')
        raw=base64.urlsafe_b64encode(message.as_bytes()).decode()
        old={'fingerprint':fingerprint,'marker':marker,'state':'intent','raw':raw,'intent':payload,'thread_id':thread['id'] if thread else None}
        persist(journal,old)
        # Any uncertain provider outcome leaves intent intact; next call only
        # reconciles and NEVER blindly repeats the POST.
        if thread:created=provider.create(raw,thread_id=thread['id'])
        else:created=provider.create(raw)
        old.update(draft_id=created['id'],state='created');persist(journal,old)
        matches=[provider.get(created['id'])]
    if len(matches)!=1:raise ValueError('Ambiguous draft recovery')
    draft=matches[0];message=draft.get('message') or {}
    if 'DRAFT' not in message.get('labelIds',[]) or 'SENT' in message.get('labelIds',[]):raise ValueError('Provider object is not an unsent draft')
    # Google may reserialize raw MIME. Compare semantic headers and decoded
    # text/HTML bodies rather than assuming byte-identical transfer encoding.
    def semantic(encoded):
        msg=BytesParser(policy=email.policy.default).parsebytes(base64.urlsafe_b64decode(encoded+'='*(-len(encoded)%4)))
        return ([str(msg.get(k,'')) for k in ('From','To','Cc','Bcc','Subject','X-CRM-Obligation','In-Reply-To','References')],
                [(p.get_content_type(),p.get_content().replace('\r\n','\n')) for p in msg.walk() if p.get_content_type() in ('text/plain','text/html')])
    if semantic(message['raw'])!=semantic(raw):raise ValueError('Draft readback content mismatch')
    if old.get('thread_id') and message.get('threadId')!=old['thread_id']:raise ValueError('Draft thread mismatch')
    receipt={'draft_id':draft['id'],'message_id':message['id'],'status':'draft','fingerprint':fingerprint,
        'url':'https://mail.google.com/mail/u/'+quote(mailbox,safe='')+'/#drafts?compose='+quote(message['id'],safe=''),
        'verified_by':'Gmail drafts.get raw MIME','sent':False}
    if not reconcile_only:
        old.update(state='verified',draft_id=draft['id'],receipt=receipt);persist(journal,old)
    return receipt


class GmailDraftProvider:
    def __init__(self,credentials_file,mailbox):
        from src.crm.google_credentials import load_google_credentials
        from google.auth.transport.requests import AuthorizedSession
        self.mailbox=mailbox
        self.session=AuthorizedSession(load_google_credentials(credentials_file,scopes=['https://www.googleapis.com/auth/gmail.modify']))
    def request(self,method,path,**kwargs):
        response=self.session.request(method,BASE+path,timeout=20,allow_redirects=False,**kwargs)
        response.raise_for_status();return response.json()
    def profile(self):return self.request('GET','/profile')['emailAddress']
    def signature(self):
        values=self.request('GET','/settings/sendAs').get('sendAs',[])
        return next((x.get('signature') for x in values if x.get('sendAsEmail')==self.mailbox),None)
    def get(self,key):return self.request('GET','/drafts/'+quote(key,safe=''),params={'format':'raw'})
    def lookup(self,marker):
        # Gmail rewrites Message-ID on drafts.create. The private MIME header
        # survives; enumerate direct draft resources, never depend on indexing.
        matches=[];token=None
        for _ in range(5):
            params={'maxResults':100}
            if token:params['pageToken']=token
            page=self.request('GET','/drafts',params=params)
            for ref in page.get('drafts',[]):
                draft=self.get(ref['id']);raw=draft['message']['raw']
                msg=BytesParser(policy=email.policy.default).parsebytes(base64.urlsafe_b64decode(raw+'='*(-len(raw)%4)))
                if str(msg.get('X-CRM-Obligation',''))==marker or str(msg.get('Message-ID',''))=='<'+marker+'>':
                    matches.append(draft)
            token=page.get('nextPageToken')
            if not token:return matches
        raise ValueError('Incomplete draft inventory; no creation permitted')
    def create(self,raw,thread_id=None):
        message={'raw':raw}
        if thread_id:message['threadId']=thread_id
        return self.request('POST','/drafts',json={'message':message})
    def preflight(self,recipient,since,thread=None):
        from datetime import datetime
        valid_address(recipient)
        when=datetime.fromisoformat(since.replace('Z','+00:00'))
        if when.tzinfo is None:raise ValueError('Source time requires timezone')
        sent=self.request('GET','/messages',params={
            'q':'in:sent to:"'+recipient+'" after:'+str(int(when.timestamp())-1), 'maxResults':1})
        if sent.get('messages') or sent.get('nextPageToken'):
            raise ValueError('Outbound exists since source; reconcile instead of drafting')
        drafts=self.request('GET','/drafts',params={'q':'to:"'+recipient+'"','maxResults':1})
        if drafts.get('drafts') or drafts.get('nextPageToken'):
            raise ValueError('Existing manual draft requires reconciliation')
        if thread:
            current=self.request('GET','/threads/'+quote(thread['id'],safe=''),params={'format':'minimal'})
            for msg in current.get('messages',[]):
                if 'DRAFT' in msg.get('labelIds',[]):
                    raise ValueError('Existing thread draft requires reconciliation')
                if 'SENT' in msg.get('labelIds',[]) and int(msg['internalDate'])>=when.timestamp()*1000:
                    raise ValueError('Thread already sent since source')

    def thread(self,recipient):
        page=self.request('GET','/messages',params={'q':'(from:'+recipient+' OR to:'+recipient+') -in:trash -in:spam -in:drafts','maxResults':5})
        candidates=[]
        for ref in page.get('messages',[]):
            msg=self.request('GET','/messages/'+quote(ref['id'],safe=''),params={'format':'metadata','metadataHeaders':['From','To','Subject','Message-ID']})
            headers={x['name'].lower():x['value'] for x in msg.get('payload',{}).get('headers',[])}
            if recipient not in [parseaddr(headers.get(x,''))[1] for x in ('from','to')]:continue
            if not headers.get('message-id') or not headers.get('subject'):continue
            candidates.append((int(msg['internalDate']),{'id':msg['threadId'],'message_id':headers['message-id'],'subject':headers['subject']}))
        if page.get('nextPageToken') or len({x[1]['id'] for x in candidates})>1:
            raise ValueError('Ambiguous or incomplete thread evidence')
        return max(candidates,key=lambda x:x[0])[1] if candidates else None
