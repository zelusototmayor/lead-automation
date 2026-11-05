import hashlib,importlib.util,os,unittest
from pathlib import Path
from types import SimpleNamespace
ROOT=Path(__file__).parent
PATH=Path(os.environ.get('GRAMMAR_PATH',ROOT.parent/'src/crm/services/call_note_stage.py'))
spec=importlib.util.spec_from_file_location('tested_grammar',PATH);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
EXACT='susana nao gosta de AI... por isso noa quer - lead perdida'
class StageGrammar(unittest.TestCase):
 def check(self,text,expected,reopening=False):
  value=m.explicit_stage_intent(text)
  self.assertEqual((value or {}).get('target_stage'),expected,text)
  if expected:
   self.assertIn(value['quote'],text)
   self.assertEqual(value['reviewed_correction'],reopening)
 def test_exact_CGC_suffix(self):self.check(EXACT,'lost')
 def test_existing_commands(self):
  for text,stage in [('lead perdida','lost'),('estado: perdida','lost'),('marcar a lead como perdida','lost'),('lead está contactada','contacted'),('reunião marcada','meeting_booked')]:
   with self.subTest(text=text):self.check(text,stage)
 def test_negation_and_conditions(self):
  for text in ['não marcar a lead como perdida','se não responder, marcar a lead como perdida','marcar a lead como perdida amanhã','marcar a lead como perdida?','não - lead perdida','se não responder - lead perdida','falámos. lead perdida amanhã','falámos - lead perdida?','nunca - lead perdida']:
   with self.subTest(text=text):self.check(text,None)
 def test_last_explicit_decision(self):
  self.check('marcar a lead como perdida. reabrir a lead como nova','new',True)
  self.check('lead perdida; reativar a lead como contactada','contacted',True)
  self.check('marcar a lead como perdida. não reabrir a lead como nova','lost')
  self.check('marcar a lead como perdida. se voltar a ligar, reabrir a lead como nova','lost')
 def test_suffix_requires_annotation_noun_and_boundary(self):
  for text in ['a oportunidade foi perdida no passado','o cliente disse perdida','falámos - perdida','falámos lead perdida']:
   with self.subTest(text=text):self.check(text,None)
  for text in ['falámos - lead perdida','falámos; estado: perdida','falámos. lead perdida','falámos\nlead perdida']:
   with self.subTest(text=text):self.check(text,'lost')
 def test_origin_gate_and_structured_outcome_retained(self):
  fields={'actor_type':'human','source_system':'manual','activity_type':'call','title':'Call logged','summary':EXACT,'outcome_code':'no_answer'}
  s=SimpleNamespace(**fields);before=vars(s).copy()
  self.assertEqual(m.stage_intent(s)['target_stage'],'lost');self.assertEqual(vars(s),before)
  for field,value in [('actor_type','agent'),('source_system','provider'),('activity_type','email_received')]:
   with self.subTest(field=field):self.assertIsNone(m.stage_intent(SimpleNamespace(**{**fields,field:value})))
  self.assertEqual(m.stage_intent(SimpleNamespace(**{**fields,'summary':'','outcome_code':'not_interested'}))['target_stage'],'lost')
if __name__=='__main__':unittest.main(verbosity=2)
