"""Untrusted model draft bodies must not contain active/resource HTML."""
import base64
from email import policy
from email.parser import BytesParser

import pytest

from src.crm.connectors.note_drafts import create_verified_draft
from tests.test_hourly_drafts import Fake


@pytest.mark.parametrize('html', [
    '<svg><image href="https://tracker.example/pixel"></image></svg>',
    '<img src="https://tracker.example/pixel">',
    '<img srcset="https://tracker.example/a 1x">',
    '<link rel="stylesheet" href="https://tracker.example/style">',
    '<video poster="https://tracker.example/pixel"><source src="https://tracker.example/v"></video>',
    '<audio src="https://tracker.example/a"></audio>',
    '<p src="https://tracker.example/pixel">Hi</p>',
    '<p srcset="file:///etc/passwd">Hi</p>',
    '<p style="background:url(https://tracker.example/pixel)">Hi</p>',
    '<p onclick="alert(1)">Hi</p>',
    '<a href="data:text/html,test">Link</a>',
    '<a href="javascript:alert(1)">Link</a>',
    '<a href="java&#x73;cript:alert(1)">Link</a>',
    '<a href="https://tracker.example">Link</a>',
    '<object data="file:///etc/passwd"></object>',
    '<iframe srcdoc="<script>alert(1)</script>"></iframe>',
    '<math><mtext>hi</mtext></math>',
    '<form action="https://tracker.example"><input autofocus></form>',
    '<p data-url="https://tracker.example">Hi</p>',
    '<!--[if mso]><img src="https://tracker.example/pixel"><![endif]-->',
    '<!DOCTYPE html>',
    '<?xml-stylesheet href="https://tracker.example/style"?>',
    '<p><strong>bad nesting</p></strong>',
    '<p>unclosed',
    '</img>',
])
def test_unsafe_model_html_is_rejected_before_provider_or_journal(tmp_path, html):
    provider = Fake()
    journal = tmp_path / 'journal.json'
    with pytest.raises(ValueError, match='Unsafe HTML'):
        create_verified_draft(provider=provider, journal=journal, key='html-review',
            mailbox='sales@example.test', recipient='lead@example.test', subject='Intro', body_html=html)
    assert provider.creates == 0
    assert not journal.exists()


def test_safe_formatted_text_preserves_words_without_resources(tmp_path):
    provider = Fake()
    html = '<p>Olá &amp; obrigado<br/>Texto <strong>forte</strong> e <em>ênfase</em>.</p><ul><li>Um</li></ul>'
    result = create_verified_draft(provider=provider, journal=tmp_path/'journal.json', key='safe',
        mailbox='sales@example.test', recipient='lead@example.test', subject='Intro', body_html=html)
    assert result['sent'] is False
    raw = provider.drafts['d1']['message']['raw']
    message = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(raw))
    html_part = message.get_body(preferencelist=('html',))
    assert html_part is not None
    body = html_part.get_content()
    assert 'Olá &amp; obrigado' in body
    assert '<strong>forte</strong>' in body
    assert '<p>Signature</p>' in body
    assert provider.creates == 1
