"""Current canonical source envelope over the reviewed unit policy.

One parent Activity, multiple exact spans. No fabricated activities or dates.
Raw provider evidence lives only in the hashed receipt, not the bounded audit.
"""
from copy import deepcopy
from src.crm.services import jose_note_policy_v2 as policy
from src.crm.services.jev_note_policy import digest
from src.crm.services.typed_decisions import MODEL, validate_answers

VERSION=policy.IMPORT_CONVENTION_VERSION
FLAGS={'personal_notes':'canary','rubric':VERSION}


def build_request(snapshot,*,flags=None):
    if flags is not None and flags!=FLAGS:raise ValueError('unsupported_personal_unit_policy')
    units=policy.source_units(snapshot)
    body={'model':MODEL,'state':{'notes':{}},'questions':{}}
    requests=[]
    for i,unit in enumerate(units):
        imported=unit['source'].get('provenance',{}).get('actor_type') in ('migration','import')
        request=policy.build_request(unit,import_convention=imported)
        requests.append(request)
        body['state']['notes'][f'n{i}']=request['body']['state']
        for key,question in request['body']['questions'].items():
            body['questions'][f'n{i}_{key}']={**question,'instructions':
                f'For this question read only state.notes.n{i}; ignore every other note. '+question['instructions']}
    signature=digest([r['snapshot_digest'] for r in requests])
    return {'decision_id':digest({'snapshot':signature,'version':VERSION,'body':body}),
        'snapshot_digest':signature,'catalog_digest':digest(body['questions']),
        'workspace_id':snapshot['workspace_id'],'lead_id':snapshot['lead_id'],
        'event_id':snapshot['source']['activity_id'],'source_digest':snapshot['source']['source_digest'],
        'correlation_id':snapshot['source']['source_digest'],'version':VERSION,'flags':dict(FLAGS),
        'model':MODEL,'body':body,'unit_requests':requests}


def map_response(request,response):
    validate_answers(response,request['body']['questions'])
    units=[]
    for i,part in enumerate(request['unit_requests']):
        raw={'model':response['model'],'answers':{key:response['answers'][f'n{i}_{key}']
            for key in part['body']['questions']}}
        units.append({'unit':deepcopy(part['unit']),'classification':policy.map_response(part,raw)})
    return {'units':units}
