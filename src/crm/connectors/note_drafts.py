"""Draft-only Gmail boundary. No send/update/delete operation is exposed.

Journal first, exact Message-ID recovery, raw MIME readback. Uncertain means stop,
not retry-create. This module is called only by the canonical server.
"""
import base64
import email.policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parseaddr, getaddresses
import hashlib
from html import escape
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import quote
from src.crm.connectors.gmail_cooldown import check as check_cooldown, observe as observe_quota
from src.crm.connectors.gmail_evidence import (
    ImmutableEvidenceCache, cache_scope, credential_generation, evidence_key,
)

BASE='https://gmail.googleapis.com/gmail/v1/users/me'
_MARKER_CACHE = ImmutableEvidenceCache(limit=2048, byte_limit=256*1024)


def _marker_headers(headers):
    """Unfiltered METADATA headers, or None requiring validated RAW fallback.

    Missing/partial/malformed payloads are not negative marker evidence. Gmail's
    METADATA contract includes headers; no metadataHeaders/fields filter is used.
    Require ordinary envelope headers too, so an empty/partial list cannot hide
    a marker. Duplicates/defects never become a cached exclusion.
    """
    if not isinstance(headers, list) or not headers:
        return None
    parsed = {}
    for item in headers:
        if not isinstance(item, dict):
            return None
        name, value = item.get('name'), item.get('value')
        if (type(name) is not str or not name or type(value) is not str
                or any(not 33 <= ord(c) <= 126 or c == ':' for c in name)
                or any(c in value for c in '\r\n')):
            return None
        name = name.lower()
        if name in parsed and name in {'from', 'to', 'cc', 'subject', 'message-id', 'x-crm-obligation'}:
            return None
        header = email.policy.default.header_factory(name, value)
        if header.defects:
            return None
        # RAW parsing decodes RFC2047 words. A noncanonical metadata spelling
        # must be resolved through RAW before it can become negative evidence.
        if name in {'message-id', 'x-crm-obligation'} and str(header) != value:
            return None
        parsed[name] = value
    if not all(parsed.get(k) for k in ('from', 'to', 'message-id')):
        return None
    return [parsed.get('x-crm-obligation', ''), parsed['message-id']]


def _raw_markers(encoded):
    raw = base64.b64decode(encoded+'='*(-len(encoded)%4), altchars=b'-_', validate=True)
    msg = BytesParser(policy=email.policy.default).parsebytes(raw)
    if msg.defects or any(len(msg.get_all(k, [])) > 1 for k in
                         ('From', 'To', 'Cc', 'Subject', 'Message-ID', 'X-CRM-Obligation')):
        raise ValueError('Ambiguous draft headers; no creation permitted')
    if any(value.defects for _, value in msg.items()):
        raise ValueError('Malformed draft headers; no creation permitted')
    return [str(msg.get('X-CRM-Obligation', '')), str(msg.get('Message-ID', ''))]


def _draft_identity(draft, draft_id, message_id, thread_id):
    message = draft.get('message') or {}
    if (draft.get('id') != draft_id or message.get('id') != message_id
            or message.get('threadId') != thread_id):
        raise ValueError('Draft identity changed; no creation permitted')
    labels = message.get('labelIds')
    if (not isinstance(labels, list) or any(type(label) is not str for label in labels)
            or 'DRAFT' not in labels or 'SENT' in labels):
        raise ValueError('Draft labels changed; no creation permitted')
    return message


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


class _PlainHTML(HTMLParser):
    """Visible MIME alternative with readable paragraphs and signature lines."""
    BLOCKS = frozenset({'p', 'div', 'br', 'li', 'ul', 'ol', 'tr'})

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style'}:
            self.hidden += 1
        if not self.hidden and tag in self.BLOCKS:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in {'script', 'style'}:
            self.hidden = max(0, self.hidden - 1)
        if not self.hidden and tag in self.BLOCKS:
            self.parts.append('\n')

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def html_to_plain(value):
    parser = _PlainHTML()
    parser.feed(value)
    parser.close()
    lines = [re.sub(r'[ \t\xa0]+', ' ', line).strip() for line in ''.join(parser.parts).splitlines()]
    return re.sub(r'\n{3,}', '\n\n', '\n'.join(lines)).strip()


def create_verified_draft(*,provider,journal,key,mailbox,recipient,subject,body_html,since=None,reconcile_only=False,additional_to=None,cc=None,include_presentation=None,thread_id=None):
    valid_address(mailbox);valid_address(recipient)
    if type(subject) is not str or not subject.strip() or len(subject)>240 or any(x in subject for x in '\r\n'):
        raise ValueError('Invalid subject')
    body_html = safe_body_html(body_html)
    payload={'mailbox':mailbox,'recipient':recipient,'subject':subject,'body_html':body_html}
    additional_to=additional_to or [];cc=cc or []
    for address in additional_to+cc:valid_address(address)
    if len({x.casefold() for x in [recipient,*additional_to,*cc]})!=len([recipient,*additional_to,*cc]):raise ValueError('Duplicate recipients')
    if additional_to:payload['additional_to']=additional_to
    if cc:payload['cc']=cc
    if include_presentation is not None:payload['include_presentation']=include_presentation
    if thread_id is not None:
        if not isinstance(thread_id,str) or not re.fullmatch(r'[0-9a-fA-F]{1,128}',thread_id):raise ValueError('Invalid Gmail thread ID')
        payload['thread_id']=thread_id
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
        body_text = html_to_plain(body_html)
        if not body_text or re.search(r'mensagem\s+html\.?', body_text, re.I):
            raise ValueError('Placeholder or empty commercial body')
        # Recover a provider object even when a local disk journal was lost.
        matches=provider.lookup(marker)
        if matches:raise ValueError('Existing provider marker without local intent; review required')
        message=EmailMessage(policy=email.policy.SMTP)
        message['From']=mailbox;message['To']=', '.join([recipient,*additional_to]);message['Subject']=subject;message['Message-ID']='<'+marker+'>'
        if cc:message['Cc']=', '.join(cc)
        message['X-CRM-Obligation']=marker
        if thread_id:
            thread=provider.thread(recipient,thread_id=thread_id)
        else:
            thread=provider.thread(recipient) if hasattr(provider,'thread') and not additional_to and not cc else None
        for address in [recipient,*additional_to,*cc]:provider.preflight(address,since,thread if address==recipient else None)
        if thread:
            message['In-Reply-To']=thread['message_id'];message['References']=thread['message_id']
            message.replace_header('Subject',thread['subject'])
        signature=provider.signature()
        if not signature:raise ValueError('Existing mailbox signature unavailable')
        # Approved current commercial signature, independent of an older generic
        # personal signature configured in Gmail. Keep the user's identity exact.
        signature='<p>Obrigado,<br><strong>José Sottomayor</strong><br>+351 969 152 052<br><a href="https://ipiia.pt/">IPIIA.pt</a></p>' 
        message.set_content(html_to_plain(body_html + signature))
        message.add_alternative('<div style="font-family:Verdana,sans-serif">'+body_html+signature+'</div>',subtype='html')
        if include_presentation is not False:
            pdf=(Path(__file__).resolve().parents[1]/'assets/IPIIA.pdf').read_bytes()
            if hashlib.sha256(pdf).hexdigest()!='7e5772978b6c1871e2fa201c88d18a66f73d5b6c17cb3191d80356adc4ce48d2':
                raise ValueError('Approved presentation content changed')
            message.add_attachment(pdf,maintype='application',subtype='pdf',filename='IPIIA.pdf')
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
                [(p.get_content_type(),p.get_content().replace('\r\n','\n')) for p in msg.walk() if p.get_content_type() in ('text/plain','text/html')],
                [(p.get_filename(),p.get_content_type(),hashlib.sha256(p.get_payload(decode=True)).hexdigest()) for p in msg.iter_attachments()])
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
        self.credentials_file=credentials_file
        generation=credential_generation(credentials_file)
        scopes=('https://www.googleapis.com/auth/gmail.modify',)
        self.session=AuthorizedSession(load_google_credentials(credentials_file,scopes=list(scopes)))
        if generation is not None and credential_generation(credentials_file)==generation:
            self._gmail_cache_identity=(generation,scopes)
            self._gmail_cache_session=self.session
    def request(self,method,path,**kwargs):
        check_cooldown()
        response=self.session.request(method,BASE+path,timeout=20,allow_redirects=False,**kwargs)
        observe_quota(response)
        response.raise_for_status();return response.json()
    def profile(self):
        mailbox=self.request('GET','/profile')['emailAddress']
        if mailbox!=self.mailbox:raise ValueError('Mailbox identity mismatch')
        # One lookup may consume this fresh canonical preflight. No stored
        # profile/history is reused on a subsequent lookup.
        self._verified_lookup_scope=(mailbox,cache_scope(self,mailbox))
        return mailbox
    def signature(self):
        values=self.request('GET','/settings/sendAs').get('sendAs',[])
        return next((x.get('signature') for x in values if x.get('sendAsEmail')==self.mailbox),None)
    def get(self,key):return self.request('GET','/drafts/'+quote(key,safe=''),params={'format':'raw'})
    def _inventory(self):
        inventory={};message_ids=set();token=None;seen=set()
        for _ in range(5):
            params={'maxResults':100}
            if token:params['pageToken']=token
            page=self.request('GET','/drafts',params=params)
            rows=page.get('drafts',[])
            if not isinstance(rows,list):raise ValueError('Invalid draft inventory')
            for ref in rows:
                did=ref['id'];mid=ref['message']['id'];tid=ref['message']['threadId']
                if (any(type(v) is not str or not v for v in (did,mid,tid))
                        or did in inventory or mid in message_ids):
                    raise ValueError('Ambiguous draft inventory')
                inventory[did]=(mid,tid);message_ids.add(mid)
            token=page.get('nextPageToken')
            if token is None:return inventory
            if type(token) is not str or not token or token in seen:
                raise ValueError('Invalid draft inventory cursor')
            seen.add(token)
        raise ValueError('Incomplete draft inventory; no creation permitted')

    def lookup(self,marker):
        # Gmail rewrites Message-ID on create. Never use indexed private-header
        # search or cached membership. Only immutable marker evidence is reused.
        verified=getattr(self,'_verified_lookup_scope',None)
        self._verified_lookup_scope=None
        if verified is None:
            self.profile()
            verified=self._verified_lookup_scope
            self._verified_lookup_scope=None
        mailbox,scope=verified
        if cache_scope(self,mailbox)!=scope:
            raise ValueError('Draft authorization changed')
        inventory=self._inventory();matches=[]
        for did,(mid,tid) in sorted(inventory.items()):
            key=evidence_key(scope,mid)
            evidence=_MARKER_CACHE.get(key)
            draft=None
            if evidence is not None and evidence['thread_id']!=tid:
                raise ValueError('Draft identity changed; no creation permitted')
            if evidence is None:
                metadata=self.request('GET','/messages/'+quote(mid,safe=''),params={'format':'metadata'})
                _draft_identity({'id':did,'message':metadata},did,mid,tid)
                payload=metadata.get('payload')
                markers=_marker_headers(payload.get('headers') if isinstance(payload,dict) else None)
                if markers is None:
                    draft=self.get(did)
                    current=_draft_identity(draft,did,mid,tid)
                    markers=_raw_markers(current['raw'])
                evidence={'thread_id':tid,'markers':markers}
                _MARKER_CACHE.put(key,evidence)
            markers=evidence['markers']
            if markers[0]==marker or markers[1]=='<'+marker+'>':
                # A match always returns current RAW for semantic readback,
                # including uncertain POST recovery. Never synthesize a draft.
                draft=draft or self.get(did)
                current=_draft_identity(draft,did,mid,tid)
                actual=_raw_markers(current['raw'])
                if not (actual[0]==marker or actual[1]=='<'+marker+'>'):
                    raise ValueError('Draft marker changed; no creation permitted')
                matches.append(draft)
        if self._inventory()!=inventory:
            raise ValueError('Draft inventory changed; no creation permitted')
        if scope is not None and cache_scope(self,mailbox)!=scope:
            raise ValueError('Draft authorization changed')
        return matches
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

    def thread(self,recipient,thread_id=None):
        """Resolve a complete history, or a context-selected thread, read-only."""
        valid_address(recipient)
        if thread_id is None:
            # The former five-message cap rejected ordinary long conversations.
            # Read all reference pages; multiple topics require an explicit ID.
            ids=set();token=None;seen=set()
            for _ in range(10):
                params={'q':'(from:'+recipient+' OR to:'+recipient+') -in:trash -in:spam -in:drafts','maxResults':100}
                if token:params['pageToken']=token
                page=self.request('GET','/messages',params=params)
                for ref in page.get('messages',[]):
                    if not ref.get('id') or not ref.get('threadId'):raise ValueError('Incomplete thread evidence')
                    ids.add(ref['threadId'])
                if len(ids)>1:raise ValueError('Multiple conversations; select a verified thread_id from context')
                token=page.get('nextPageToken')
                if not token:break
                if token in seen:raise ValueError('Incomplete thread inventory')
                seen.add(token)
            else:raise ValueError('Incomplete thread inventory')
            if not ids:return None
            thread_id=next(iter(ids))
        if not isinstance(thread_id,str) or not re.fullmatch(r'[0-9a-fA-F]{1,128}',thread_id):raise ValueError('Invalid Gmail thread ID')
        current=self.request('GET','/threads/'+quote(thread_id,safe=''),params={
            'format':'metadata','metadataHeaders':['From','To','Cc','Subject','Message-ID']})
        if current.get('id')!=thread_id:raise ValueError('Thread identity mismatch')
        candidates=[]
        for msg in current.get('messages',[]):
            if msg.get('threadId')!=thread_id:raise ValueError('Message thread identity mismatch')
            if set(msg.get('labelIds',[])) & {'DRAFT','TRASH','SPAM'}:continue
            headers={x['name'].lower():x['value'] for x in msg.get('payload',{}).get('headers',[])}
            addresses={address.casefold() for _,address in getaddresses([headers.get(x,'') for x in ('from','to','cc')])}
            if recipient.casefold() not in addresses:continue
            if not headers.get('message-id') or not headers.get('subject'):raise ValueError('Incomplete reply headers')
            candidates.append((int(msg['internalDate']),{'id':thread_id,'message_id':headers['message-id'],'subject':headers['subject']}))
        if not candidates:raise ValueError('Selected thread does not contain the intended recipient')
        return max(candidates,key=lambda x:x[0])[1]
