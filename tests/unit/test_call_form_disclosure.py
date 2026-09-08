from pathlib import Path
from html.parser import HTMLParser


def test_call_dimensions_are_closed_by_default_without_removing_capture():
    source=(Path(__file__).parents[2]/'dashboard/app/templates/leads/index.html').read_text()
    class FormParser(HTMLParser):
        def __init__(self):
            super().__init__(); self.advanced=False; self.fields=set(); self.found=False
        def handle_starttag(self,tag,attrs):
            a=dict(attrs)
            if tag=='details' and 'data-call-advanced' in a:
                assert 'open' not in a
                self.advanced=True; self.found=True
            if self.advanced and tag in ('input','select') and a.get('name'):
                self.fields.add(a['name'])
        def handle_endtag(self,tag):
            if tag=='details': self.advanced=False
    p=FormParser(); p.feed(source)
    assert p.found
    assert {'occurred_at','answer_kind','first_conversation','contact_kind','useful','decision_maker','interlocutor_role','repeat_reason'}<=p.fields
